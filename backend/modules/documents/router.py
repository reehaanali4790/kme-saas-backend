"""
LME Monitoring System - LC Upload API
Version: 2.1.0
"""

from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import Optional
import tempfile
import os
import pandas as pd
from datetime import timedelta, datetime
from decimal import Decimal
import sys
import logging
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.tenant import get_tenant_db, SessionLocal
from config.settings import settings
from models.database_models import User, LCMaster, LCProduct
from modules.auth.dependencies import get_current_user
from modules.auth.services import AuthService
from infrastructure.formula_engine.lme_calculator import LMECalculator
from infrastructure.normalization.normalization_service import looks_like_date_value

router = APIRouter(prefix="/api/upload", tags=["File Upload"])

# Get logger
logger = logging.getLogger("uvicorn")


VALID_PRODUCTS = [
    'HRP', 'HRS', 'CRS', 'CRP', 'GPS', 'GPP', 'GLP',
    'PPGIS', 'PPGIP', 'WRLC', 'WRHC', 'CRNGO',
    'PUPHRS', 'PUPCRS', 'PMC'
]

# Core required columns
REQUIRED_COLUMNS = [
    'L/c Number', 'L/c. Date', 'LC Expiry Date',
    'Sub Category Name', 'Product Short Code', 'Product Name', 'Origin Name'
]

MAX_FILE_SIZE = 5 * 1024 * 1024


def setup_logger(operation_id):
    """Setup file logger"""
    log_dir = Path(__file__).parent.parent / 'logs'
    log_dir.mkdir(exist_ok=True)
    
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_file = log_dir / f'lc_import_{timestamp}_{operation_id}.log'
    
    file_logger = logging.getLogger(f'lc_import_{operation_id}')
    file_logger.setLevel(logging.INFO)
    file_logger.handlers = []
    
    fh = logging.FileHandler(log_file)
    fh.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    fh.setFormatter(formatter)
    file_logger.addHandler(fh)
    
    return file_logger, str(log_file)


def is_valid_excel_file(filename):
    """Check if file is valid Excel format"""
    return filename.endswith('.xlsx') or filename.endswith('.xls')


def normalize_origin(origin_name):
    """Normalize origin"""
    if not origin_name:
        return "UNKNOWN"
    if looks_like_date_value(origin_name):
        return "UNKNOWN"

    origin = str(origin_name).upper().strip()
    
    mapping = {
        'CHINA': ['CHINA', 'CHINESE'],
        'TAIWAN': ['TAIWAN'],
        'EUROPE': ['EUROPE', 'GERMAN', 'ITALY', 'SPAIN'],
        'UAE': ['UAE', 'DUBAI', 'EMIRATES'],
        'IRAN': ['IRAN', 'IRANIAN'],
        'SOUTH AFRICA': ['AFRICA', 'SOUTH AFRICA'],
        'CIS': ['CIS', 'RUSSIA'],
        'TURKEY': ['TURKEY', 'TURKISH'],
        'USA': ['USA', 'AMERICA', 'US']
    }
    
    for standard, keywords in mapping.items():
        if any(kw in origin for kw in keywords):
            return standard
    
    return origin[:50]


def determine_quality(sub_category, product_name=None):
    """Determine quality from Sub Category Name"""
    text = f"{sub_category or ''} {product_name or ''}".upper().strip()
    if not text:
        return "SECONDARY"

    # Secondary-first rules (includes common OCR typo "SECONDAY")
    if any(k in text for k in ("NPRM", "SECONDARY", "SECONDAY", "NON PRIME", "NON-PRIME", "2ND", " SEC ")):
        return 'SECONDARY'

    if 'PRM' in text or 'PRIME' in text:
        return 'PRIME'

    return 'SECONDARY'


def parse_decimal(value):
    """Safely parse decimal value"""
    if pd.isna(value):
        return None
    try:
        return Decimal(str(value))
    except:
        return None


def parse_date(value):
    """Safely parse date value"""
    if pd.isna(value):
        return None
    try:
        return pd.to_datetime(value).date()
    except:
        return None


def find_lme_columns(df_columns):
    """
    Find LME columns with flexible matching
    Returns dict of found columns
    """
    columns_list = list(df_columns)
    
    # Possible variations for each LME column
    lme_variations = {
        'lme': ['LME', 'Lme', 'lme', 'L.M.E', 'LME Rate', 'LME Price'],
        'diff': ['LC / LME Difference', 'LC/LME Difference', 'LC-LME Difference', 'Difference', 'LC vs LME'],
        'date_from': ['LME Date From', 'LME From Date', 'From Date', 'Date From', 'LME Start Date'],
        'date_to': ['LME Date To', 'LME To Date', 'To Date', 'Date To', 'LME End Date']
    }
    
    found = {}
    
    for field, variations in lme_variations.items():
        for col in columns_list:
            col_clean = str(col).strip()
            for variation in variations:
                if col_clean.lower() == variation.lower():
                    found[field] = col
                    break
            if field in found:
                break
    
    return found


@router.post("/analyze-lc-file")
async def analyze_lc_file(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_tenant_db)
):
    """Analyze LC Excel file before import — returns row counts and column info."""
    
    if not AuthService.check_permission(current_user, "import_lc"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions"
        )
    
    if not is_valid_excel_file(file.filename):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only .xlsx and .xls files are supported"
        )
    
    contents = await file.read()
    file_size = len(contents)

    if file_size > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"File size ({file_size / 1024 / 1024:.1f}MB) exceeds limit (5MB)"
        )
    
    suffix = '.xlsx' if file.filename.endswith('.xlsx') else '.xls'
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(contents)
        tmp_path = tmp.name
    
    try:
        df = pd.read_excel(tmp_path)
        all_columns = list(df.columns)

        missing_columns = [col for col in REQUIRED_COLUMNS if col not in df.columns]
        if missing_columns:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Missing required columns: {', '.join(missing_columns)}"
            )
        
        lme_columns_found = find_lme_columns(df.columns)

        stats = {
            'total_rows': len(df),
            'valid_rows': 0,
            'invalid_product': 0,
            'invalid_date': 0,
            'duplicate': 0,
            'new': 0,
            'with_lme_data': 0,
            'without_lme_data': 0,
            'invalid_products_found': [],
            'all_columns_in_file': all_columns,
            'lme_columns_detected': lme_columns_found
        }
        
        for idx, row in df.iterrows():
            try:
                lc_number = str(row['L/c Number']).strip()
                product_code = str(row['Product Short Code']).strip().upper()
                
                if product_code not in VALID_PRODUCTS:
                    stats['invalid_product'] += 1
                    if product_code not in stats['invalid_products_found']:
                        stats['invalid_products_found'].append(product_code)
                    continue
                
                try:
                    pd.to_datetime(row['L/c. Date'])
                except:
                    stats['invalid_date'] += 1
                    continue
                
                existing = db.query(LCMaster).filter(
                    LCMaster.lc_number == lc_number
                ).first()
                
                if existing:
                    stats['duplicate'] += 1
                else:
                    stats['new'] += 1
                
                if 'lme' in lme_columns_found and pd.notna(row.get(lme_columns_found['lme'])):
                    stats['with_lme_data'] += 1
                else:
                    stats['without_lme_data'] += 1
                
                stats['valid_rows'] += 1
                
            except Exception as e:
                logger.error(f"Error processing row {idx}: {str(e)}")
                continue

        return {
            "success": True,
            "filename": file.filename,
            "file_size": file_size,
            "analysis": stats
        }

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to analyze file: {str(e)}"
        )
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


@router.post("/import-lc-file")
async def import_lc_file(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_tenant_db)
):
    """Import LCs from Excel using LCImporter (reads all fields correctly)."""
    from lc_importer import LCImporter
    
    if not AuthService.check_permission(current_user, "import_lc"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)

    if not is_valid_excel_file(file.filename):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only .xlsx and .xls files are supported"
        )

    contents = await file.read()
    if len(contents) > MAX_FILE_SIZE:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="File too large (max 5 MB)")

    suffix = '.xlsx' if file.filename.endswith('.xlsx') else '.xls'
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(contents)
            tmp_path = tmp.name

        importer = LCImporter(tmp_path, user_id=current_user.user_id)
        importer.load_excel()
        stats = importer.import_data()

        s = stats
        parts = []
        if s['lcs_new']:
            parts.append(f"{s['lcs_new']} new LCs created")
        if s['lcs_updated']:
            parts.append(f"{s['lcs_updated']} LCs updated")
        if s['products_added']:
            parts.append(f"{s['products_added']} products imported")
        if s['skipped_invalid_product']:
            parts.append(f"{s['skipped_invalid_product']} rows skipped (unknown product)")
        if s['errors']:
            parts.append(f"{s['errors']} errors")

        return {
            "success": True,
            "message": ". ".join(parts) + "." if parts else "No data imported.",
            "stats": {
                "unique_lcs": s['lcs_new'] + s['lcs_updated'],
                "lcs_new": s['lcs_new'],
                "lcs_updated": s['lcs_updated'],
                "products_added": s['products_added'],
                "skipped": s['skipped_invalid_product'] + s['skipped_invalid_date'],
                "errors": s['errors'],
                "error_details": s['error_details'],
                "invalid_products_found": s['invalid_products_found'],
            }
        }

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error(f"Import failed: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail=f"Import failed: {str(e)}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)
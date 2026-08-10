"""
LME Monitoring System - LC Importer
Imports full LC data from old software Excel export.
"""

import pandas as pd
from datetime import timedelta, datetime
from decimal import Decimal, InvalidOperation
import sys
import os
import logging
from pathlib import Path
from collections import defaultdict, Counter

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.database import SessionLocal
from config.settings import settings
from models.database_models import LCMaster, LCProduct, BillOfLading
from infrastructure.normalization.normalization_service import looks_like_date_value

VALID_PRODUCTS = [
    'HRP', 'HRS', 'CRP', 'CRS', 'GPS', 'GPP', 'GLP',
    'PPGIS', 'PPGIP', 'WRLC', 'WRHC', 'CRNGO',
    'PUPHRS', 'PUPCRS', 'PMC'
]

REQUIRED_COLUMNS = ['L/c Number', 'L/c. Date', 'Product Short Code', 'Origin Name']


def _safe_decimal(val, default=Decimal('0')):
    if val is None:
        return default
    try:
        if pd.isna(val):
            return default
    except Exception:
        pass
    try:
        return Decimal(str(val))
    except InvalidOperation:
        return default


def _safe_date(val):
    if val is None:
        return None
    try:
        ts = pd.to_datetime(val)
        if pd.isna(ts):
            return None
        return ts.date()
    except Exception:
        return None


def _safe_str(val):
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    s = str(val).strip()
    return s if s and s.lower() not in ('nan', 'none', '0') else None


def _normalize_origin(origin_name):
    # A cell that pandas/openpyxl reads back as a real datetime/date/Timestamp (or a string
    # that is obviously date-shaped, e.g. "2024-03-15" / "15-03-2024 00:00:00") is never a
    # valid country/origin value — this happens when the source Excel's "Origin Name" column
    # has inconsistent cell formatting left over from the old software's export. Silently
    # str()-ing such a value used to produce garbage like "2024-03-15 00:00:00" stored
    # straight into lc_products.origin, which is what showed up in the UI as "Country
    # displayed as a Date". Reject it here instead of passing it through.
    if not origin_name:
        return 'UNKNOWN'
    if looks_like_date_value(origin_name):
        return 'UNKNOWN'
    origin = str(origin_name).upper().strip()
    mapping = {
        'CHINA': ['CHINA', 'CHINESE'],
        'TAIWAN': ['TAIWAN'],
        'EUROPE': ['EUROPE', 'GERMAN', 'ITALY', 'SPAIN', 'NETHERLANDS', 'DUTCH'],
        'UAE': ['UAE', 'DUBAI', 'EMIRATES'],
        'IRAN': ['IRAN', 'IRANIAN'],
        'SOUTH AFRICA': ['AFRICA', 'SOUTH AFRICA'],
        'CIS': ['CIS', 'RUSSIA'],
        'TURKEY': ['TURKEY', 'TURKISH'],
        'USA': ['USA', 'AMERICA'],
    }
    for standard, keywords in mapping.items():
        if any(kw in origin for kw in keywords):
            return standard
    return origin[:50]


def _determine_quality(sub_category):
    if not sub_category:
        return 'SECONDARY'
    sub = str(sub_category).upper()
    return 'PRIME' if ('PRIME' in sub or 'PRM' in sub) else 'SECONDARY'


def _map_status(contract_status):
    mapping = {
        'open': 'OPEN', 'closed': 'CLOSED', 'shipped': 'SHIPPED',
        'expired': 'EXPIRED', 'cancelled': 'CANCELLED', 'canceled': 'CANCELLED',
    }
    if not contract_status:
        return 'OPEN'
    return mapping.get(str(contract_status).lower().strip(), 'OPEN')


class LCImporter:
    def __init__(self, file_path, user_id=5, dry_run=False):
        self.file_path = file_path
        self.user_id = user_id
        self.dry_run = dry_run
        self.df = None
        self.stats = {
            'total_rows': 0,
            'lcs_new': 0,
            'lcs_updated': 0,
            'products_added': 0,
            'bls_created': 0,
            'skipped_invalid_product': 0,
            'skipped_invalid_date': 0,
            'errors': 0,
            'error_details': [],
            'invalid_products_found': [],
        }
        self._setup_logging()

    def _setup_logging(self):
        log_dir = Path(__file__).parent.parent / 'logs'
        log_dir.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        log_file = log_dir / f'lc_import_{timestamp}.log'
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s %(levelname)s %(message)s',
            handlers=[logging.FileHandler(log_file, encoding='utf-8'), logging.StreamHandler(sys.stdout)],
        )
        self.logger = logging.getLogger(__name__)
        self.logger.info(f'Log: {log_file}')

    def load_excel(self):
        self.logger.info(f'Loading: {self.file_path}')
        self.df = pd.read_excel(self.file_path)
        self.stats['total_rows'] = len(self.df)
        self.logger.info(f'Loaded {len(self.df)} rows, {len(self.df.columns)} columns')

        missing = [c for c in REQUIRED_COLUMNS if c not in self.df.columns]
        if missing:
            raise ValueError(f'Missing required columns: {", ".join(missing)}')
        return True

    def _group_by_lc(self):
        """Group rows by LC number. Returns dict: lc_number -> list of rows."""
        groups = defaultdict(list)
        for idx, row in self.df.iterrows():
            lc_number = _safe_str(row.get('L/c Number'))
            product_code = _safe_str(row.get('Product Short Code'))
            if not lc_number:
                continue
            if product_code:
                product_code = product_code.upper()
            if not product_code or product_code not in VALID_PRODUCTS:
                self.stats['skipped_invalid_product'] += 1
                if product_code and product_code not in self.stats['invalid_products_found']:
                    self.stats['invalid_products_found'].append(product_code)
                continue
            if _safe_date(row.get('L/c. Date')) is None:
                self.stats['skipped_invalid_date'] += 1
                continue
            groups[lc_number].append(row)
        return groups

    def import_data(self):
        if self.dry_run:
            self.logger.info('DRY RUN — no data written')
            return self.stats

        groups = self._group_by_lc()
        self.logger.info(f'Unique LCs to process: {len(groups)}')

        db = SessionLocal()
        try:
            for lc_number, rows in groups.items():
                try:
                    self._import_lc(db, lc_number, rows)
                except Exception as e:
                    self.stats['errors'] += 1
                    msg = f'LC {lc_number}: {e}'
                    self.stats['error_details'].append(msg)
                    self.logger.error(msg)
                    db.rollback()

            db.commit()
            self.logger.info('All changes committed.')
        except Exception as e:
            db.rollback()
            self.logger.error(f'Fatal: {e}')
            raise
        finally:
            db.close()

        return self.stats

    def _import_lc(self, db, lc_number, rows):
        # Use first row for LC-level fields
        r = rows[0]

        lc_date = _safe_date(r.get('L/c. Date'))
        monitoring_expiry = lc_date + timedelta(days=settings.LC_MONITORING_DAYS) if lc_date else None

        existing = db.query(LCMaster).filter(LCMaster.lc_number == lc_number).first()
        if existing:
            lc = existing
            self.stats['lcs_updated'] += 1
            action = 'Updated'
        else:
            lc = LCMaster()
            db.add(lc)
            self.stats['lcs_new'] += 1
            action = 'Created'

        # LC-level fields
        lc.lc_number = lc_number
        lc.contract_number = _safe_str(r.get('Contract Number'))
        lc.contract_date = _safe_date(r.get('Contract Date'))
        lc.lc_date = lc_date
        lc.expiry_date = _safe_date(r.get('LC Expiry Date'))
        lc.last_ship_date = _safe_date(r.get('Last Ship Date'))
        lc.currency = _safe_str(r.get('Currency')) or 'USD'
        lc.exchange_rate = _safe_decimal(r.get('Exchange Rate'), None)
        lc.supplier_name = _safe_str(r.get('Supplier'))
        lc.importer_name = _safe_str(r.get('Importer'))
        lc.hoa = _safe_str(r.get('HOA'))
        lc.payment_terms = _safe_str(r.get('Payment Terms'))
        lc.delivery_terms = _safe_str(r.get('Delivery Terms'))
        lc.bank_name = _safe_str(r.get('Bank Name'))
        lc.booked_by = _safe_str(r.get('Booked By'))
        lc.indentor = _safe_str(r.get('Indentor'))
        lc.insurance = _safe_str(r.get('Insurance'))
        lc.monitoring_expiry = monitoring_expiry
        lc.status = _map_status(r.get('Contract Status'))
        if action == 'Created':
            lc.created_by = self.user_id
            lc.assigned_to = self.user_id

        # Shipment fields (may be empty for new LCs)
        lc.vessel_name = _safe_str(r.get('Vessel Name'))
        lc.bl_number = _safe_str(r.get('B/L Number'))
        lc.ship_on_board = _safe_date(r.get('Ship On Board'))
        lc.eta = _safe_date(r.get('ETA'))
        lc.arrival_port = _safe_str(r.get('Arrival Port Name'))
        lc.invoice_number = _safe_str(r.get('Invoice Number'))
        lc.invoice_date = _safe_date(r.get('Invoice Date'))
        lc.doc_payment_rate = _safe_decimal(r.get('Doc Payment Rate'), None)
        lc.doc_payment_date = _safe_date(r.get('Doc Payment Date'))
        lc.bank_int_date = _safe_date(r.get('Bank Int. Date'))
        lc.delivery_date = _safe_date(r.get('Delivery Date'))
        lc.shipped_amount = _safe_decimal(r.get('Shipped Amount'), None)

        db.flush()

        # Auto-create BillOfLading row if BL number is present in imported data
        self._maybe_create_bl(db, lc, r)

        # Count how many times each (product_code, size, grade, qty) appears in source
        source_counts = Counter()
        for row in rows:
            key = (
                str(row.get('Product Short Code', '')).strip().upper(),
                _safe_str(row.get('Size')),
                _safe_str(row.get('Grade')),
                _safe_decimal(row.get('Weight'), Decimal('0')),
            )
            source_counts[key] += 1

        # Count how many of each already exist in the DB (committed state before this run)
        from sqlalchemy import func
        db_counts = {}
        for (pcode, size, grade, qty) in source_counts:
            q = db.query(func.count(LCProduct.line_id)).filter(
                LCProduct.lc_id == lc.lc_id,
                LCProduct.product_code == pcode,
                LCProduct.quantity == qty,
            )
            q = q.filter(LCProduct.size == size) if size is not None else q.filter(LCProduct.size.is_(None))
            q = q.filter(LCProduct.grade == grade) if grade is not None else q.filter(LCProduct.grade.is_(None))
            db_counts[(pcode, size, grade, qty)] = q.scalar() or 0

        # Track insertions made in this run per key
        inserted_counts = Counter()

        # Products — one per row
        for row in rows:
            product_code = str(row.get('Product Short Code', '')).strip().upper()
            quality = _determine_quality(row.get('Sub Category Name'))
            raw_origin = row.get('Origin Name')
            if raw_origin and looks_like_date_value(raw_origin):
                self.logger.warning(
                    f'  {lc_number}: Origin Name looked like a date ({raw_origin!r}) — '
                    f'stored as UNKNOWN instead of the raw value. Check the source file.'
                )
            origin = _normalize_origin(raw_origin)

            qty = _safe_decimal(row.get('Weight'), Decimal('0'))
            size = _safe_str(row.get('Size'))
            grade = _safe_str(row.get('Grade'))
            shipped_qty = _safe_decimal(row.get('Shipped Quantity'), Decimal('0'))
            pkgs = int(_safe_decimal(row.get('Pkgs / Coils'), Decimal('0')))
            containers = int(_safe_decimal(row.get('# Of Cont'), Decimal('0')))

            key = (product_code, size, grade, qty)
            already_in_db = db_counts.get(key, 0)
            inserted_this_run = inserted_counts[key]
            if already_in_db + inserted_this_run >= source_counts[key]:
                continue

            product = LCProduct(
                lc_id=lc.lc_id,
                product_code=product_code,
                product_name=_safe_str(row.get('Product Name')) or product_code,
                origin=origin,
                quality=quality,
                cargo_nature=_safe_str(row.get('Category Name')),
                quantity=qty,
                unit=_safe_str(row.get('Unit')) or 'MT',
                lc_unit_price=_safe_decimal(row.get('L/c. Price'), Decimal('0')),
                lc_amount=_safe_decimal(row.get('L/c Amount'), None),
                hs_code=_safe_str(row.get('HS Code')),
                grade=grade,
                size=size,
                shipped_quantity=shipped_qty,
                pkgs_coils=pkgs,
                num_containers=containers,
            )
            db.add(product)
            inserted_counts[key] += 1
            self.stats['products_added'] += 1

        self.logger.info(f'{action}: {lc_number} ({len(rows)} product(s))')

    def _maybe_create_bl(self, db, lc: LCMaster, row):
        """
        If the imported LC row has a BL number, create a BillOfLading record
        (source=IMPORTED) so it appears in the BL management screen.
        Skips if a BL with the same bl_number already exists for this LC.
        """
        bl_number = _safe_str(row.get('B/L Number'))
        if not bl_number:
            return

        existing_bl = db.query(BillOfLading).filter(
            BillOfLading.lc_id == lc.lc_id,
            BillOfLading.bl_number == bl_number,
        ).first()

        if existing_bl:
            return

        bl = BillOfLading(
            lc_id=lc.lc_id,
            source='IMPORTED',
            status='IMPORTED',
            bl_number=bl_number,
            bl_date=lc.ship_on_board,
            vessel_name=lc.vessel_name,
            voyage_number=_safe_str(row.get('Voyage Number')),
            port_of_discharge=lc.arrival_port,
            shipper_name=lc.supplier_name,
            created_by=self.user_id,
        )
        db.add(bl)
        self.stats['bls_created'] += 1
        self.logger.info(f'  BL auto-created: {bl_number} (IMPORTED)')

    def print_summary(self):
        self.logger.info('=' * 60)
        self.logger.info('IMPORT SUMMARY')
        self.logger.info('=' * 60)
        self.logger.info(f'Total rows in file : {self.stats["total_rows"]}')
        self.logger.info(f'LCs created        : {self.stats["lcs_new"]}')
        self.logger.info(f'LCs updated        : {self.stats["lcs_updated"]}')
        self.logger.info(f'Products imported  : {self.stats["products_added"]}')
        self.logger.info(f'BLs auto-created   : {self.stats["bls_created"]}')
        self.logger.info(f'Skipped (product)  : {self.stats["skipped_invalid_product"]}')
        if self.stats['invalid_products_found']:
            self.logger.info(f'  Unknown codes: {", ".join(self.stats["invalid_products_found"])}')
        self.logger.info(f'Skipped (date)     : {self.stats["skipped_invalid_date"]}')
        self.logger.info(f'Errors             : {self.stats["errors"]}')
        for e in self.stats['error_details']:
            self.logger.error(f'  {e}')
        self.logger.info('=' * 60)


def main():
    import argparse
    parser = argparse.ArgumentParser(description='LC Importer — import from old software Excel export')
    parser.add_argument('file', help='Excel file path (.xls or .xlsx)')
    parser.add_argument('--dry-run', action='store_true', help='Preview only, no DB writes')
    parser.add_argument('--user-id', type=int, default=5, help='User ID for created_by (default: 5)')
    args = parser.parse_args()

    if not os.path.exists(args.file):
        print(f'ERROR: File not found: {args.file}')
        sys.exit(1)

    importer = LCImporter(args.file, user_id=args.user_id, dry_run=args.dry_run)
    importer.load_excel()
    importer.import_data()
    importer.print_summary()
    sys.exit(1 if importer.stats['errors'] > 0 else 0)


if __name__ == '__main__':
    main()

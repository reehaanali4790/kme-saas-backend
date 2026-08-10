"""
PDF Price Extractor - FIXED VERSION
Extracts steel prices from FastMarkets bulletins
"""

import pdfplumber
import re
from typing import Dict, List, Optional
from datetime import datetime
import logging

logger = logging.getLogger("uvicorn")


class PDFPriceExtractor:
    """Extract steel prices from FastMarkets PDF bulletins"""
    
    # All FastMarkets symbols we're looking for
    TARGET_SYMBOLS = [
        'MB-STE-0009', 'MB-STE-0014', 'MB-STE-0017', 'MB-STE-0026',
        'MB-STE-0027', 'MB-STE-0028', 'MB-STE-0030', 'MB-STE-0031',
        'MB-STE-0053', 'MB-STE-0054', 'MB-STE-0120', 'MB-STE-0123',
        'MB-STE-0124', 'MB-STE-0125', 'MB-STE-0144', 'MB-STE-0145',
        'MB-STE-0148', 'MB-STE-0181', 'MB-STE-0182', 'MB-STE-0892',
        'MB-STE-0893'
    ]
    
    @staticmethod
    def extract_bulletin_date(text: str) -> Optional[str]:
        """Extract bulletin date from PDF text"""
        # Look for date patterns like "27 January 2026" or "27Jan2026"
        date_patterns = [
            r'(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})',
            r'(\d{1,2})\s?(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s?(\d{4})',
        ]
        
        for pattern in date_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                try:
                    day = match.group(1)
                    month = match.group(2)
                    year = match.group(3)
                    
                    # Parse to date
                    date_str = f"{day} {month} {year}"
                    date_obj = datetime.strptime(date_str, "%d %B %Y")
                    return date_obj.strftime("%Y-%m-%d")
                except:
                    try:
                        date_str = f"{day} {month} {year}"
                        date_obj = datetime.strptime(date_str, "%d %b %Y")
                        return date_obj.strftime("%Y-%m-%d")
                    except:
                        pass
        
        return None
    
    @staticmethod
    def extract_prices_from_line(line: str, symbol: str) -> Optional[Dict]:
        """
        Extract prices from a line containing the symbol.
        
        Pattern:
        MB-STE-XXXX ... DDMmmYYYY LOW- HIGH CHANGE% ...
        MB-STE-XXXX ... DDMmmYYYY SINGLE CHANGE% ...
        
        Examples:
        "27Jan2026 527- 538 0 (0.00%)"     → LOW=527, HIGH=538
        "27Jan 2026 562- 580 -7.5(-1.30%)" → LOW=562, HIGH=580
        "21 Jan 2026 690- 700 5 (0.72%)"   → LOW=690, HIGH=700
        "26 Dec 2025 55.3 0 (0.00%)"       → LOW=55.3, HIGH=55.3
        """
        
        # First, find the date pattern
        # Formats: "27Jan2026", "27 Jan2026", "21 Jan 2026", "27Jan 2026"
        date_pattern = r'(\d{1,2})\s?([A-Za-z]{3})\s?(\d{4})'
        date_match = re.search(date_pattern, line)
        
        if not date_match:
            return None
        
        # Get the text AFTER the date
        date_end_pos = date_match.end()
        after_date = line[date_end_pos:].strip()
        
        # Pattern 1: Range format "527- 538" or "690- 700"
        # Look for: NUMBER(decimal optional) DASH SPACE NUMBER
        range_pattern = r'^(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)'
        range_match = re.match(range_pattern, after_date)
        
        if range_match:
            low = float(range_match.group(1))
            high = float(range_match.group(2))
            
            # CRITICAL CHECK: If high < low, this is actually a single value
            # Example: "466.14-0.17" where -0.17 is the change, not high price
            if high < low:
                # Treat as single value
                return {
                    'symbol': symbol,
                    'low': low,
                    'high': low,
                    'avg': low
                }
            
            # Valid range
            avg = (low + high) / 2
            
            return {
                'symbol': symbol,
                'low': low,
                'high': high,
                'avg': avg
            }
        
        # Pattern 2: Single value "55.3" or "53"
        # Look for: NUMBER(decimal optional) followed by space and change indicator
        single_pattern = r'^(\d+(?:\.\d+)?)\s+[-+]?\d+(?:\.\d+)?\s*\('
        single_match = re.match(single_pattern, after_date)
        
        if single_match:
            value = float(single_match.group(1))
            
            return {
                'symbol': symbol,
                'low': value,
                'high': value,
                'avg': value
            }
        
        return None
    
    @classmethod
    def extract_from_pdf(cls, pdf_path: str) -> Dict:
        """Extract all prices from PDF"""
        try:
            extracted_data = {
                'bulletin_date': None,
                'prices': [],
                'symbols_found': [],
                'extraction_date': datetime.now().isoformat()
            }
            
            # Open PDF
            with pdfplumber.open(pdf_path) as pdf:
                full_text = ""
                
                # Extract text from all pages
                for page in pdf.pages:
                    full_text += page.extract_text() + "\n"
                
                # Extract bulletin date
                extracted_data['bulletin_date'] = cls.extract_bulletin_date(full_text)
                
                if not extracted_data['bulletin_date']:
                    logger.warning("Could not extract bulletin date from PDF")
                
                # Split into lines for processing
                lines = full_text.split('\n')
                
                # Process each target symbol
                for symbol in cls.TARGET_SYMBOLS:
                    # Find lines containing this symbol
                    for line in lines:
                        if symbol in line:
                            # Extract prices from this line
                            price_data = cls.extract_prices_from_line(line, symbol)
                            
                            if price_data:
                                extracted_data['prices'].append(price_data)
                                extracted_data['symbols_found'].append(symbol)
                                logger.info(f"Extracted {symbol}: L={price_data['low']}, H={price_data['high']}")
                                break  # Move to next symbol after finding prices
                
                logger.info(f"Extraction complete: {len(extracted_data['prices'])} symbols extracted")
                
                return extracted_data
                
        except Exception as e:
            logger.error(f"PDF extraction error: {str(e)}")
            raise Exception(f"Failed to extract data from PDF: {str(e)}")
    
    @classmethod
    def get_region_from_symbol(cls, symbol: str) -> str:
        """Map symbol to region"""
        region_map = {
            'MB-STE-0009': 'CHINA',
            'MB-STE-0014': 'CIS',
            'MB-STE-0017': 'CIS',
            'MB-STE-0026': 'EUROPE_NORTH',
            'MB-STE-0027': 'EUROPE_SOUTH',
            'MB-STE-0028': 'EUROPE_NORTH',
            'MB-STE-0030': 'EUROPE_NORTH',
            'MB-STE-0031': 'EUROPE_SOUTH',
            'MB-STE-0053': 'EUROPE_NORTH',
            'MB-STE-0054': 'EUROPE_SOUTH',
            'MB-STE-0120': 'TURKEY',
            'MB-STE-0123': 'UAE',
            'MB-STE-0124': 'UAE',
            'MB-STE-0125': 'UAE',
            'MB-STE-0144': 'CHINA',
            'MB-STE-0145': 'CHINA',
            'MB-STE-0148': 'CHINA',
            'MB-STE-0181': 'USA',
            'MB-STE-0182': 'USA',
            'MB-STE-0892': 'EUROPE_ITALY',
            'MB-STE-0893': 'EUROPE_SPAIN',
        }
        return region_map.get(symbol, 'UNKNOWN')


# Compatibility function for existing code
def extract_prices_from_pdf(pdf_path: str) -> Dict:
    """
    Wrapper function for compatibility with existing import statements.
    
    Usage:
        from modules.documents.extractors.pdf_price_extractor import extract_prices_from_pdf
        result = extract_prices_from_pdf("/path/to/bulletin.pdf")
    """
    return PDFPriceExtractor.extract_from_pdf(pdf_path)
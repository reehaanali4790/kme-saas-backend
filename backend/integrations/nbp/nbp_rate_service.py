"""
NBP (National Bank of Pakistan) daily FX rate fetcher.
The ratesheet index lists daily PDF files; we find the latest, download it, and
extract USD / EUR / AED TT-Selling rates via the Claude Vision pipeline.

https://www.nbp.com.pk/ratesheet/index.aspx
"""

import os
import re
import json
import logging
import tempfile
from datetime import date, datetime

import httpx

from config.settings import settings
from infrastructure.document_ai.document_ai import extract_with_tiers

logger = logging.getLogger("uvicorn")

INDEX_URL = "https://www.nbp.com.pk/ratesheet/index.aspx"
BASE = "https://www.nbp.com.pk"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

# href like ../RateSheetFiles/NBP-RateSheet-04-06-2026.pdf
LINK_RE = re.compile(r'(?:\.\./)?RateSheetFiles/(NBP-RateSheet-(\d{2})-(\d{2})-(\d{4})\.pdf)', re.IGNORECASE)

NBP_PROMPT = """This is a National Bank of Pakistan daily foreign-exchange rate sheet.
Find the TT & OD SELLING rate for these three currencies:
- US DOLLAR (USD)
- EURO (EUR)
- UAE DIRHAM (AED)

Return ONLY this JSON (numbers only for rates, null if a currency is missing):
{"sheet_date": "YYYY-MM-DD", "usd": null, "eur": null, "aed": null}

Use the TT (telegraphic transfer) SELLING column. Return ONLY the JSON object."""


def _find_latest_pdf() -> tuple[str, date]:
    """Return (absolute_pdf_url, sheet_date) for the most recent ratesheet."""
    r = httpx.get(INDEX_URL, headers=UA, timeout=20, verify=True)
    r.raise_for_status()
    matches = LINK_RE.findall(r.text)
    if not matches:
        raise ValueError("No rate-sheet links found on NBP index page (layout may have changed).")
    # matches: list of (filename, dd, mm, yyyy)
    best = max(matches, key=lambda m: date(int(m[3]), int(m[2]), int(m[1])))
    filename, dd, mm, yyyy = best
    sheet_date = date(int(yyyy), int(mm), int(dd))
    url = f"{BASE}/RateSheetFiles/{filename}"
    return url, sheet_date


def fetch_nbp_rates() -> dict:
    """
    Download the latest NBP ratesheet PDF and extract USD/EUR/AED TT-selling rates.
    Returns: {sheet_date(date), usd(float), eur(float), aed(float), source_url(str)}
    Raises on failure (caller handles).
    """
    if not settings.ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY is not configured.")

    pdf_url, sheet_date = _find_latest_pdf()
    logger.info(f"NBP: latest ratesheet {sheet_date} -> {pdf_url}")

    resp = httpx.get(pdf_url, headers=UA, timeout=30, verify=True)
    resp.raise_for_status()

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    try:
        tmp.write(resp.content)
        tmp.close()
        data = extract_with_tiers(tmp.name, NBP_PROMPT, settings.ANTHROPIC_API_KEY, doc_type="nbp_ratesheet")
    finally:
        try:
            os.remove(tmp.name)
        except OSError:
            pass

    # prefer the date parsed from the filename; fall back to model's sheet_date
    parsed_date = sheet_date
    if data.get("sheet_date"):
        try:
            parsed_date = date.fromisoformat(data["sheet_date"])
        except (ValueError, TypeError):
            pass

    def _num(v):
        try:
            return float(v) if v is not None else None
        except (ValueError, TypeError):
            return None

    result = {
        "sheet_date": parsed_date,
        "usd": _num(data.get("usd")),
        "eur": _num(data.get("eur")),
        "aed": _num(data.get("aed")),
        "source_url": pdf_url,
    }
    if not result["usd"]:
        raise ValueError(f"Could not extract USD rate from NBP sheet {parsed_date}.")
    logger.info(f"NBP rates {parsed_date}: USD={result['usd']} EUR={result['eur']} AED={result['aed']}")
    return result

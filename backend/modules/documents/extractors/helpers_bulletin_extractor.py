"""
Extracts steel LME reference prices from thehelpers.pk's "LMB" duty-calculation PDF.

This is a DIFFERENT document from the FastMarkets bulletins pdf_price_extractor.py
parses for the manual-upload flow: thehelpers.pk publishes its own Pakistan-import
duty sheet, organized by HS code x quality (Prime/Secondary) x alloy grade, with a
"Consumer LME" column holding an already-computed USD/ton figure per row - there is
no FastMarkets symbol/region data in it at all. Verified against a real bulletin
downloaded from the live site (2026-07-15 edition) before writing this parser.

Only HR / CR / GP are extracted in this version - "Wire Rod" (and everything after
it: scrap, tin plate, country-of-origin SRO tables) either renders as a graphic/legend
page with no parseable table, or as HS codes whose section identity isn't confidently
determined yet from a single sample bulletin. Extraction stops the moment any of those
downstream section markers is seen, rather than guessing.
"""

import logging
import re
from datetime import date, datetime
from typing import Optional

import pdfplumber

logger = logging.getLogger("uvicorn")

# Order matters: a page's group is "whatever known title most recently appeared",
# since a group's table can span multiple pages (e.g. HOT ROLLED starts on page 1,
# continues on page 2 before COLD ROLLED begins).
SECTION_MAP = {
    "HOT ROLLED": "HR",
    "COLD ROLLED": "CR",
    "GP / EG / PPGI": "GP",
}

# Seeing any of these means we've walked past the sections this version supports -
# stop extracting rather than risk mis-attributing rows to the wrong group.
STOP_MARKERS = ["WIRE ROD", "SCRAP", "Electrolytic", "Tin Plate"]

QUALITY_RE = re.compile(r"\b(PRIME|SECONDARY)\b")
HS_CODE_RE = re.compile(r"\b(\d{4}\.\d{4})\b")
PRICE_RE = re.compile(r"Industrial\s*\$\s*([\d,\s]+(?:\.\d+)?)")
BULLETIN_DATE_RE = re.compile(r"LMB\s*DATE\s*(\d{1,2}-[A-Za-z]{3}-\d{4})")

# Sanity bound for a per-ton USD steel reference price - anything outside this is
# almost certainly a mis-parsed row (e.g. two table columns fused together) rather
# than a real price, and is dropped rather than stored.
MIN_SANE_PRICE = 50.0
MAX_SANE_PRICE = 5000.0

# A bulletin is only accepted if at least this many (group, quality) combinations
# were found with a sane price - protects against a layout change silently yielding
# a handful of garbage rows that happen to pass the price bound.
MIN_REQUIRED_ROWS = 4
REQUIRED_GROUPS = {"HR", "CR", "GP"}


def _clean_price(raw: str) -> Optional[float]:
    try:
        return float(raw.replace(",", "").replace(" ", ""))
    except ValueError:
        return None


def extract_bulletin_date(text: str) -> Optional[date]:
    match = BULLETIN_DATE_RE.search(text)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%d-%b-%Y").date()
    except ValueError:
        return None


def extract_from_pdf(pdf_path: str, expected_date_hint: Optional[date] = None) -> dict:
    """
    Returns:
        {
            "bulletin_date": date | None,
            "rows": [{"group": "HR", "quality": "PRIME", "alloy": "ALLOY" | "NON-ALLOY" | None,
                       "hs_code": "7225.3000", "price": 498.65}, ...],
            "groups_found": {"HR", "CR", "GP"},
        }
    Raises ValueError if validation fails (bad/missing date, too few sane rows,
    a required group entirely missing). Never returns partial/garbage data silently.
    """
    current_group = None
    bulletin_date = None
    rows: list[dict] = []

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""

            if bulletin_date is None:
                bulletin_date = extract_bulletin_date(text)

            if any(marker in text for marker in STOP_MARKERS):
                break

            for title, code in SECTION_MAP.items():
                if title in text:
                    current_group = code

            if current_group is None:
                continue

            matches = list(QUALITY_RE.finditer(text))
            for i, m in enumerate(matches):
                start = m.end()
                end = matches[i + 1].start() if i + 1 < len(matches) else min(len(text), start + 800)
                chunk = text[start:end]

                hs_match = HS_CODE_RE.search(chunk)
                price_match = PRICE_RE.search(chunk)
                if not hs_match or not price_match:
                    continue

                price = _clean_price(price_match.group(1))
                if price is None or not (MIN_SANE_PRICE <= price <= MAX_SANE_PRICE):
                    logger.debug(
                        f"helpers_bulletin_extractor: dropping out-of-range row "
                        f"group={current_group} hs={hs_match.group(1)} raw_price={price_match.group(1)!r}"
                    )
                    continue

                alloy = "NON-ALLOY" if "NON-ALLOY" in chunk else ("ALLOY" if "ALLOY" in chunk else None)
                rows.append({
                    "group": current_group,
                    "quality": m.group(1),
                    "alloy": alloy,
                    "hs_code": hs_match.group(1),
                    "price": price,
                })

    if not bulletin_date:
        raise ValueError("Could not extract bulletin date (LMB DATE header) from PDF.")

    today = date.today()
    if bulletin_date > today:
        raise ValueError(f"Bulletin date {bulletin_date} is in the future - refusing to store.")

    if expected_date_hint and expected_date_hint != bulletin_date:
        logger.warning(
            f"helpers_bulletin_extractor: PDF's internal date {bulletin_date} does not match "
            f"the site's listed date {expected_date_hint} - trusting the PDF's own header."
        )

    groups_found = {r["group"] for r in rows}
    missing = REQUIRED_GROUPS - groups_found
    if missing:
        raise ValueError(f"Required product group(s) not found in bulletin: {sorted(missing)}")

    if len(rows) < MIN_REQUIRED_ROWS:
        raise ValueError(f"Only {len(rows)} sane price row(s) extracted - expected at least {MIN_REQUIRED_ROWS}.")

    logger.info(
        f"helpers_bulletin_extractor: bulletin_date={bulletin_date} "
        f"rows={len(rows)} groups={sorted(groups_found)}"
    )

    return {
        "bulletin_date": bulletin_date,
        "rows": rows,
        "groups_found": groups_found,
    }

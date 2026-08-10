"""Crawl Karachi Port Trust Expected Arrivals and extract vessel ETAs.

Uses crawl4ai (Playwright) because the KPT site is JS-rendered and has a
self-signed / incomplete TLS chain on some networks.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Dict, List, Optional

from bs4 import BeautifulSoup

logger = logging.getLogger("uvicorn")

KPT_EXPECTED_ARRIVALS_URL = "https://kpt.gov.pk/pages/49/expected-arrivals"
_ETA_RE = re.compile(
    r"(\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}:\d{2}\s*(?:am|pm))",
    re.IGNORECASE,
)
_VOY_RE = re.compile(r"\s+(?:VOYAGE|VOY)(?![A-Z]).*$|\s+V(?:[\s\.-]*\d+.*)$", re.IGNORECASE)
_PREFIX_RE = re.compile(r"^(M\.?V\.?|M\.?T\.?|S\.?S\.?|MS|MT)\s+", re.IGNORECASE)


@dataclass
class KPTVesselArrival:
    name: str
    agent: str
    operation_type: str
    badge: str
    eta_text: str
    eta_date: date
    status: str


def normalize_vessel_name(name: Optional[str]) -> Optional[str]:
    """Canonical vessel key — mirrors api.report_endpoints._norm_vessel."""
    if not name:
        return None
    s = str(name).upper().split(",")[0]
    s = _PREFIX_RE.sub("", s)
    s = s.replace(".", " ")
    s = re.sub(r"\s+", " ", s).strip()
    s = _VOY_RE.sub("", s).strip()
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def parse_kpt_eta_text(raw: str) -> Optional[date]:
    if not raw:
        return None
    m = _ETA_RE.search(str(raw))
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1).strip().lower(), "%d/%m/%Y %I:%M:%S %p").date()
    except ValueError:
        return None


def parse_expected_arrivals_html(html: str) -> List[KPTVesselArrival]:
    soup = BeautifulSoup(html or "", "html.parser")
    out: List[KPTVesselArrival] = []
    for tile in soup.select("div.av-tile[data-ship]"):
        name = (tile.get("data-name") or "").strip()
        if not name:
            h = tile.select_one(".av-tile-name")
            name = (h.get_text(strip=True) if h else "").strip()
        if not name:
            continue
        agent = (tile.get("data-agent") or "").strip()
        op_type = (tile.get("data-type") or "").strip().lower() or "na"
        badge_el = tile.select_one(".av-badge")
        badge = badge_el.get_text(" ", strip=True) if badge_el else ""
        time_el = tile.select_one(".av-tile-time")
        eta_text = time_el.get_text(" ", strip=True) if time_el else ""
        eta_date = parse_kpt_eta_text(eta_text)
        if not eta_date:
            continue
        status = badge or (op_type.title() if op_type != "na" else "Expected Arrival")
        out.append(
            KPTVesselArrival(
                name=name,
                agent=agent,
                operation_type=op_type,
                badge=badge,
                eta_text=eta_text,
                eta_date=eta_date,
                status=status,
            )
        )
    return out


def index_arrivals(arrivals: List[KPTVesselArrival]) -> Dict[str, KPTVesselArrival]:
    """Map normalised vessel name -> arrival (first wins on duplicates)."""
    idx: Dict[str, KPTVesselArrival] = {}
    for row in arrivals:
        key = normalize_vessel_name(row.name)
        if key and key not in idx:
            idx[key] = row
    return idx


def find_arrival_for_vessel(
    vessel_name: str,
    arrivals: List[KPTVesselArrival],
    *,
    index: Optional[Dict[str, KPTVesselArrival]] = None,
) -> Optional[KPTVesselArrival]:
    target = normalize_vessel_name(vessel_name)
    if not target:
        return None
    idx = index or index_arrivals(arrivals)
    if target in idx:
        return idx[target]
    # Fuzzy: allow partial containment when KPT drops prefixes/suffixes.
    for key, row in idx.items():
        if target in key or key in target:
            return row
    return None


async def fetch_expected_arrivals_html(url: str = KPT_EXPECTED_ARRIVALS_URL) -> str:
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig

    browser = BrowserConfig(headless=True, ignore_https_errors=True)
    run_cfg = CrawlerRunConfig(wait_until="networkidle", page_timeout=90000)
    async with AsyncWebCrawler(config=browser) as crawler:
        result = await crawler.arun(url, config=run_cfg)
    if not result.success or not result.html:
        raise RuntimeError(
            f"KPT crawl failed (success={result.success}, status={getattr(result, 'status_code', None)})"
        )
    return result.html


async def crawl_expected_arrivals(url: str = KPT_EXPECTED_ARRIVALS_URL) -> List[KPTVesselArrival]:
    html = await fetch_expected_arrivals_html(url)
    arrivals = parse_expected_arrivals_html(html)
    logger.info(f"[KPT crawler] parsed {len(arrivals)} vessel ETA row(s) from {url}")
    return arrivals


def crawl_expected_arrivals_sync(url: str = KPT_EXPECTED_ARRIVALS_URL) -> List[KPTVesselArrival]:
    return asyncio.run(crawl_expected_arrivals(url))

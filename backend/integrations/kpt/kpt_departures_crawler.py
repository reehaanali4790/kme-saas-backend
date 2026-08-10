"""Crawl Karachi Port Trust Ship Departures and extract departing vessels."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

from bs4 import BeautifulSoup

from integrations.kpt.kpt_eta_crawler import normalize_vessel_name

logger = logging.getLogger("uvicorn")

KPT_SHIP_DEPARTURES_URL = "https://kpt.gov.pk/pages/53/ship-departures"
_DEPARTURE_RE = re.compile(
    r"(\d{1,2}\s+[A-Za-z]{3}\s+\d{4},\s+\d{1,2}:\d{2}\s*(?:am|pm))",
    re.IGNORECASE,
)


@dataclass
class KPTVesselDeparture:
    name: str
    agent: str
    berth: str
    cargo_type: str
    departure_text: str
    departed_at: Optional[datetime]


def parse_departure_text(raw: str) -> Optional[datetime]:
    if not raw:
        return None
    m = _DEPARTURE_RE.search(str(raw))
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1).strip().lower(), "%d %b %Y, %I:%M %p")
    except ValueError:
        return None


def _pill_text(tile, icon_class: str) -> str:
    for pill in tile.select(".av-pill"):
        icon = pill.select_one("i")
        if icon and icon_class in " ".join(icon.get("class") or []):
            return pill.get_text(" ", strip=True)
    return ""


def parse_ship_departures_html(html: str) -> List[KPTVesselDeparture]:
    soup = BeautifulSoup(html or "", "html.parser")
    out: List[KPTVesselDeparture] = []
    for tile in soup.select("div.av-tile[data-ship]"):
        name = (tile.get("data-name") or "").strip()
        if not name:
            h = tile.select_one(".av-tile-name")
            name = (h.get_text(strip=True) if h else "").strip()
        if not name:
            continue
        agent = (tile.get("data-agent") or "").strip()
        if not agent:
            row = tile.select_one(".av-tile-row")
            agent = row.get_text(" ", strip=True) if row else ""
        berth_el = tile.select_one(".av-badge-berth")
        berth = berth_el.get_text(" ", strip=True) if berth_el else ""
        cargo_type = _pill_text(tile, "fa-gears")
        departure_text = _pill_text(tile, "fa-calendar")
        out.append(
            KPTVesselDeparture(
                name=name,
                agent=agent,
                berth=berth,
                cargo_type=cargo_type,
                departure_text=departure_text,
                departed_at=parse_departure_text(departure_text),
            )
        )
    return out


def parse_departures_total(html: str) -> Optional[int]:
    soup = BeautifulSoup(html or "", "html.parser")
    el = soup.select_one(".av-total-num")
    if not el:
        return None
    try:
        return int(el.get_text(strip=True))
    except ValueError:
        return None


def index_departures(rows: List[KPTVesselDeparture]) -> Dict[str, KPTVesselDeparture]:
    idx: Dict[str, KPTVesselDeparture] = {}
    for row in rows:
        key = normalize_vessel_name(row.name)
        if key and key not in idx:
            idx[key] = row
    return idx


def find_departure_vessel(
    vessel_name: str,
    rows: List[KPTVesselDeparture],
    *,
    index: Optional[Dict[str, KPTVesselDeparture]] = None,
) -> Optional[KPTVesselDeparture]:
    target = normalize_vessel_name(vessel_name)
    if not target:
        return None
    idx = index or index_departures(rows)
    if target in idx:
        return idx[target]
    for key, row in idx.items():
        if target in key or key in target:
            return row
    return None


async def fetch_ship_departures_html(url: str = KPT_SHIP_DEPARTURES_URL) -> str:
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig

    browser = BrowserConfig(headless=True, ignore_https_errors=True)
    run_cfg = CrawlerRunConfig(wait_until="networkidle", page_timeout=90000)
    async with AsyncWebCrawler(config=browser) as crawler:
        result = await crawler.arun(url, config=run_cfg)
    if not result.success or not result.html:
        raise RuntimeError(
            f"KPT departures crawl failed (success={result.success}, "
            f"status={getattr(result, 'status_code', None)})"
        )
    return result.html


async def crawl_ship_departures(url: str = KPT_SHIP_DEPARTURES_URL) -> List[KPTVesselDeparture]:
    html = await fetch_ship_departures_html(url)
    rows = parse_ship_departures_html(html)
    total = parse_departures_total(html)
    logger.info(
        f"[KPT departures crawler] parsed {len(rows)} vessel row(s) "
        f"(page total={total}) from {url}"
    )
    return rows


def crawl_ship_departures_sync(url: str = KPT_SHIP_DEPARTURES_URL) -> List[KPTVesselDeparture]:
    return asyncio.run(crawl_ship_departures(url))

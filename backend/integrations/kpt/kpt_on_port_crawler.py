"""Crawl Karachi Port Trust Ships On Port and extract docked vessels."""

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

KPT_SHIPS_ON_PORT_URL = "https://kpt.gov.pk/pages/54/ship-on-port"
_ARRIVAL_RE = re.compile(
    r"(\d{1,2}\s+[A-Za-z]{3}\s+\d{4},\s+\d{1,2}:\d{2}\s*(?:am|pm))",
    re.IGNORECASE,
)


@dataclass
class KPTVesselOnPort:
    name: str
    agent: str
    berth: str
    cargo_type: str
    arrival_text: str
    arrival_at: Optional[datetime]


def parse_on_port_arrival_text(raw: str) -> Optional[datetime]:
    if not raw:
        return None
    m = _ARRIVAL_RE.search(str(raw))
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


def parse_ships_on_port_html(html: str) -> List[KPTVesselOnPort]:
    soup = BeautifulSoup(html or "", "html.parser")
    out: List[KPTVesselOnPort] = []
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
        arrival_text = _pill_text(tile, "fa-calendar")
        out.append(
            KPTVesselOnPort(
                name=name,
                agent=agent,
                berth=berth,
                cargo_type=cargo_type,
                arrival_text=arrival_text,
                arrival_at=parse_on_port_arrival_text(arrival_text),
            )
        )
    return out


def index_on_port_vessels(rows: List[KPTVesselOnPort]) -> Dict[str, KPTVesselOnPort]:
    """Map normalised vessel name -> first on-port row (duplicates share a berth group)."""
    idx: Dict[str, KPTVesselOnPort] = {}
    for row in rows:
        key = normalize_vessel_name(row.name)
        if key and key not in idx:
            idx[key] = row
    return idx


def find_on_port_vessel(
    vessel_name: str,
    rows: List[KPTVesselOnPort],
    *,
    index: Optional[Dict[str, KPTVesselOnPort]] = None,
) -> Optional[KPTVesselOnPort]:
    target = normalize_vessel_name(vessel_name)
    if not target:
        return None
    idx = index or index_on_port_vessels(rows)
    if target in idx:
        return idx[target]
    for key, row in idx.items():
        if target in key or key in target:
            return row
    return None


async def fetch_ships_on_port_html(url: str = KPT_SHIPS_ON_PORT_URL) -> str:
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig

    browser = BrowserConfig(headless=True, ignore_https_errors=True)
    run_cfg = CrawlerRunConfig(wait_until="networkidle", page_timeout=90000)
    async with AsyncWebCrawler(config=browser) as crawler:
        result = await crawler.arun(url, config=run_cfg)
    if not result.success or not result.html:
        raise RuntimeError(
            f"KPT on-port crawl failed (success={result.success}, "
            f"status={getattr(result, 'status_code', None)})"
        )
    return result.html


async def crawl_ships_on_port(url: str = KPT_SHIPS_ON_PORT_URL) -> List[KPTVesselOnPort]:
    html = await fetch_ships_on_port_html(url)
    rows = parse_ships_on_port_html(html)
    logger.info(f"[KPT on-port crawler] parsed {len(rows)} vessel row(s) from {url}")
    return rows


def crawl_ships_on_port_sync(url: str = KPT_SHIPS_ON_PORT_URL) -> List[KPTVesselOnPort]:
    return asyncio.run(crawl_ships_on_port(url))

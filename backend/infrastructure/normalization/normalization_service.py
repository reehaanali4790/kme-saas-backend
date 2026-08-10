"""
Normalization service — one place for cleaning the noisy free-text values that arrive
from Excel imports and AI document extraction into clean, canonical values used by
filters and reports.

Covers:
  * Company / party names  -> canonical clean name (importers / suppliers masters)
  * Item type descriptions -> short product code (closed set)
  * Vessel names           -> canonical grouping key (moved here from report_endpoints)
  * Issuing-bank names      -> canonical bank group
  * LC payment tenor        -> SIGHT / DA

No third-party fuzzy library is available on the deploy image, so company matching uses
stdlib difflib. Company matching is intentionally aggressive (auto-merges close variants);
genuinely novel names are still saved but flagged (needs_review) for later human review.
"""

import re
import difflib
import logging
from typing import Any, Optional

logger = logging.getLogger("uvicorn")


# ===========================================================================
# Company / party name normalization
# ===========================================================================

# Legal-form / suffix tokens stripped when reducing a company name to its core key.
# Order matters: multi-word forms first so "SMC PVT" is removed before "PVT".
_LEGAL_TOKENS = [
    "SMC-PRIVATE", "SMC PRIVATE", "SMC-PVT", "SMC PVT", "SMC",
    "PRIVATE", "PVT", "LIMITED", "LTD", "LLP", "LLC",
    "INCORPORATED", "INC", "CORPORATION", "CORP",
    "COMPANY", "CO",
]

# Address markers — everything from the marker onward is dropped (it is address text,
# not part of the company name).
_ADDR_MARKERS = [
    "PLOT", "OFFICE", "SUITE", "NEAR", "STREET", "ROAD", "BLOCK", "SECTOR",
    "INDUSTRIAL", "ESTATE", "PHASE", "FLOOR", "BUILDING", "HOUSE NO", "HOUSE",
    "GALI", "MOUZA", "MAUZA", "SURVEY", "KHASRA", "TOWN", "P.O", "P O ",
]

# Aggressive auto-merge threshold on the difflib similarity of two company keys.
_COMPANY_MATCH_CUTOFF = 0.80
# Reporting read-path: only accept fuzzy hits at or above this (avoids false merges in filters).
_COMPANY_READ_CUTOFF = 0.85

UNMATCHED_COMPANY = "Unmatched Company"

# Known importer groups — canonical name + short code + company_key() roots that identify them.
# Aliases like "Perfect Craft Pvt Ltd" collapse via company_key() to "PERFECT CRAFT".
# display_label is the ONE standardized "Name (CODE)" string to show everywhere in the UI
# (tables, filters, reports, exports) instead of the inconsistent mix of full company names
# and bare codes that used to appear in different places.
KNOWN_IMPORTER_PROFILES: list[dict[str, Any]] = [
    {"short_code": "PCL", "canonical": "Perfect Craft", "display_label": "Perfect Craft (PCL)",
     "keys": ["PERFECT CRAFT"]},
    {"short_code": "RIL", "canonical": "Range Industries", "display_label": "Range Industrial (RIL)",
     "keys": ["RANGE INDUSTRIES", "RANGE", "RANGE INDUSTRIES LIMITED", "RANGE INDUSTRIAL"]},
    {"short_code": "MAX", "canonical": "Max Comfort", "display_label": "Max Comfort (Max)",
     "keys": ["MAX COMFORT", "MAX COMFORT PVT LTD"]},
    {"short_code": "MEL", "canonical": "Meen Enterprises", "display_label": "Meen. (MEL)",
     "keys": ["MEEN ENTERPRISES", "MEEN"]},
    {"short_code": "SCL", "canonical": "Steel Craft", "display_label": "Steel Craft (SCL)",
     "keys": ["STEEL CRAFT"]},
    {"short_code": "JMT", "canonical": "JM Traders", "display_label": "JM Traders (JMT)",
     "keys": ["JM TRADERS"]},
    {"short_code": "MAB", "canonical": "MAB Steel", "display_label": "MAB Steel (MAB)",
     "keys": ["MAB STEEL"]},
]


def _profile_for_key(key: str) -> dict | None:
    """Return a known profile dict when key matches a seeded company root."""
    if not key:
        return None
    for p in KNOWN_IMPORTER_PROFILES:
        roots = set(p["keys"]) | {company_key(p["canonical"])}
        if key in roots:
            return p
        for root in roots:
            if key.startswith(root + " ") or root.startswith(key + " "):
                return p
    return None


def _short_code_for_key(key: str) -> str | None:
    p = _profile_for_key(key)
    return p["short_code"] if p else None


def _display_label_for_key(key: str) -> str | None:
    p = _profile_for_key(key)
    return p["display_label"] if p else None


class CompanyResolver:
    """Read-only resolver: raw free-text company name -> short code for reports/filters.

    Does not mutate stored LC/GD/BL values — only maps at read time.
    """

    def __init__(self, db):
        self._by_key: dict[str, dict] = {}
        self._load(db)

    def _load(self, db):
        for p in KNOWN_IMPORTER_PROFILES:
            entry = {
                "short_code": p["short_code"],
                "canonical": p["canonical"],
                "display_label": p.get("display_label"),
                "matched": True,
                "needs_review": False,
            }
            for k in set(p["keys"]) | {company_key(p["canonical"])}:
                if k:
                    self._by_key[k] = entry

        from models.database_models import Importer
        for row in db.query(Importer).all():
            k = company_key(row.name)
            if not k:
                continue
            code = row.short_code or _short_code_for_key(k)
            entry = {
                "short_code": code,
                "canonical": row.name,
                "display_label": _display_label_for_key(k),
                "matched": bool(code),
                "needs_review": bool(row.needs_review),
            }
            if entry["short_code"]:
                self._by_key[k] = entry

    def resolve(self, name) -> dict:
        if not name or not str(name).strip():
            return self._blank()
        raw = str(name).strip()
        key = company_key(raw)
        if not key:
            return self._unmatched(raw)

        hit = self._lookup(key)
        if hit and hit.get("short_code"):
            return self._result(raw, hit["canonical"], hit["short_code"],
                                True, hit.get("needs_review", False), hit.get("display_label"))

        prof = _profile_for_key(key)
        if prof:
            return self._result(raw, prof["canonical"], prof["short_code"], True, False,
                                prof.get("display_label"))

        return self._unmatched(raw)

    def _lookup(self, key: str) -> dict | None:
        if key in self._by_key:
            return self._by_key[key]
        for k, entry in self._by_key.items():
            if key.startswith(k + " ") or k.startswith(key + " "):
                return entry
        close = difflib.get_close_matches(key, list(self._by_key.keys()), n=1,
                                          cutoff=_COMPANY_READ_CUTOFF)
        if close:
            return self._by_key[close[0]]
        return None

    def all_codes(self) -> list[str]:
        codes = {e["short_code"] for e in self._by_key.values() if e.get("short_code")}
        codes.add(UNMATCHED_COMPANY)
        return sorted(codes)

    @staticmethod
    def _blank():
        return {"raw": None, "canonical_name": None, "short_code": None,
                "display": None, "matched": False, "needs_review": False}

    @staticmethod
    def _unmatched(raw: str):
        return {"raw": raw, "canonical_name": None, "short_code": UNMATCHED_COMPANY,
                "display": UNMATCHED_COMPANY, "matched": False, "needs_review": True}

    @staticmethod
    def _result(raw, canonical, code, matched, needs_review, display_label=None):
        return {"raw": raw, "canonical_name": canonical, "short_code": code,
                "display": display_label or code, "matched": matched, "needs_review": needs_review}


def company_resolver(db) -> CompanyResolver:
    return CompanyResolver(db)


def resolve_company(name, db) -> dict:
    return company_resolver(db).resolve(name)


def enrich_company_fields(row: dict, resolver: CompanyResolver,
                          field: str = "importer_name", *, keep_raw: bool = True) -> dict:
    """Attach company_code / company_canonical / company_matched to a report row."""
    raw = row.get(field)
    res = resolver.resolve(raw)
    row["company_code"] = res["short_code"]
    row["company_canonical"] = res["canonical_name"]
    row["company_display"] = res["display"]
    row["company_matched"] = res["matched"]
    row["company_needs_review"] = res["needs_review"]
    if not keep_raw and res["matched"] and res["short_code"]:
        row[field] = res["short_code"]
    return res


def matches_company_code(resolver: CompanyResolver, raw_name: Any, filter_code: str | None) -> bool:
    if not filter_code:
        return True
    return resolver.resolve(raw_name).get("short_code") == filter_code


def company_codes_for_names(resolver: CompanyResolver, names) -> list[str]:
    codes = set()
    for n in names:
        if not n:
            continue
        c = resolver.resolve(n).get("short_code")
        if c:
            codes.add(c)
    return sorted(codes)


def seed_known_importers(db) -> int:
    """Upsert known importer profiles with short codes (idempotent)."""
    from models.database_models import Importer
    import re
    updated = 0
    for p in KNOWN_IMPORTER_PROFILES:
        canon = p["canonical"]
        sc = p["short_code"]
        nn = re.sub(r"\s+", " ", canon.upper()).strip()
        row = db.query(Importer).filter(Importer.name_norm == nn).first()
        if not row:
            for r in db.query(Importer).all():
                rk = company_key(r.name)
                if rk in set(p["keys"]) | {company_key(canon)}:
                    row = r
                    break
        if row:
            if not row.short_code:
                row.short_code = sc
                updated += 1
            if row.needs_review and row.short_code == sc:
                row.needs_review = False
        else:
            db.add(Importer(name=canon, name_norm=nn, short_code=sc, needs_review=False))
            updated += 1
    db.flush()
    return updated


def _strip_address(u: str) -> str:
    """Drop trailing address text (after the first comma, or an address marker word)."""
    u = u.split(",")[0]
    for m in _ADDR_MARKERS:
        idx = u.find(" " + m + " ")
        if idx == -1 and u.startswith(m + " "):
            idx = 0
        if idx != -1:
            u = u[:idx]
    return u


def company_key(name) -> str:
    """Reduce a company name to a canonical comparison key: uppercased, address dropped,
    punctuation and legal-form tokens removed, spaces collapsed.

    'MAX COMFORT (SMC-PVT) LTD.' , 'MAX COMFORT (PVT) LTD' , 'MAX COMFORT SMC PVT LTD., PLOT 5'
    all reduce to 'MAX COMFORT'.
    """
    if not name or not str(name).strip():
        return ""
    u = re.sub(r"\s+", " ", str(name).upper()).strip()
    # remove parenthetical groups (usually the legal form, e.g. "(SMC-PVT)")
    u = re.sub(r"\([^)]*\)", " ", u)
    u = _strip_address(u)
    # drop punctuation -> spaces
    u = re.sub(r"[^A-Z0-9 ]", " ", u)
    u = re.sub(r"\s+", " ", u).strip()
    # remove standalone legal tokens
    for tok in _LEGAL_TOKENS:
        u = re.sub(r"\b" + re.escape(tok) + r"\b", " ", u)
    u = re.sub(r"\s+", " ", u).strip()
    return u


def _title_clean(name) -> str:
    """A presentable fallback clean name for a novel company (title-cased, trimmed)."""
    u = re.sub(r"\s+", " ", str(name or "").strip())
    return u[:290]


def match_company(name, db, model):
    """Match a free-text company name to a canonical master row (importers / suppliers).

    Returns (canonical_name, matched_row_or_None, needs_review).
      * exact key hit               -> that master's clean name, needs_review=False
      * close key (>= cutoff)        -> matched master's clean name, needs_review=False (auto-merge)
      * no confident match           -> creates a NEW master row flagged needs_review=True,
                                        canonical = cleaned original.
    `model` is the SQLAlchemy lookup model (Importer / Supplier). The row is added+flushed
    so the caller's commit persists it.
    """
    if not name or not str(name).strip():
        return None, None, False

    key = company_key(name)
    if not key:
        return _title_clean(name), None, True

    rows = db.query(model).all()
    keyed = [(company_key(r.name), r) for r in rows]

    # 1. exact key
    for k, r in keyed:
        if k and k == key:
            if hasattr(r, "short_code") and not r.short_code:
                sc = _short_code_for_key(key)
                if sc:
                    r.short_code = sc
            return r.name, r, False

    # 2. aggressive fuzzy on keys
    candidates = {k: r for k, r in keyed if k}
    close = difflib.get_close_matches(key, list(candidates.keys()), n=1,
                                      cutoff=_COMPANY_MATCH_CUTOFF)
    if close:
        r = candidates[close[0]]
        if hasattr(r, "short_code") and not r.short_code:
            sc = _short_code_for_key(key)
            if sc:
                r.short_code = sc
        return r.name, r, False

    # 3. novel -> create + flag
    clean = _title_clean(name)
    sc = _short_code_for_key(key)
    row = model(name=clean, name_norm=re.sub(r"\s+", " ", clean.upper()).strip(),
                needs_review=True)
    if sc and hasattr(row, "short_code"):
        row.short_code = sc
    db.add(row)
    db.flush()
    logger.info(f"Normalization: new unmatched company flagged for review: '{clean}'")
    return clean, row, True


# ===========================================================================
# Item type normalization  (closed set of short product codes)
# ===========================================================================

# The complete closed set of valid short codes (existing + the ones requested).
VALID_ITEM_CODES = [
    "GPP", "GPS", "HRP", "HRS", "CRP", "CRS", "PPGIP", "PPGIS",
    "WRLC", "WRHC", "GLP", "EG", "CRNGO", "PUPHRS", "PUPCRS", "PMC",
]


# Second line of defense against pricing/quantity/Incoterm text leaking into a product
# name — a SWIFT LC's F45A field typically bundles the commodity description together
# with quantity, unit price, and delivery terms in one block (e.g. "5000 MT COLD ROLLED
# COILS SECONDARY QUALITY AT USD 593.00 PER M/TON CFR KARACHI"). The extraction prompt is
# instructed to isolate the commodity only, but prompts aren't 100% reliable — this strips
# the same noise defensively before it's stored, wherever a goods/product description is
# extracted (LC creation today; apply the same call anywhere else that changes).
# Matches "QTY 250 MT" or plain "250 MT" quantity prefixes at the start of a goods description.
_QTY_PREFIX_RE = re.compile(
    r'^\s*(?:QTY\s+)?[\d,]+(?:\.\d+)?\s*(?:M/?TONS?|MTS?|KGS?)\b\.?\s*(?:OF)?\s*',
    re.IGNORECASE,
)
# Matches "AT USD NNN PER MT" or "AT THE RATE USD NNN PER MT" price clauses and everything after.
_PRICE_CLAUSE_RE = re.compile(
    r'\s*(?:AT(?:\s+THE\s+RATE)?|@)?\s*(?:USD|US\$|\$|EUR|GBP)\s*[\d,]+(?:\.\d+)?\s*(?:PER\s*)?(?:M/?TON|MT|KG)S?\b.*$',
    re.IGNORECASE,
)
# Matches "AND ALL OTHER DETAILS AS PER ..." boilerplate trailer.
_OTHER_DETAILS_RE = re.compile(
    r'\s*\bAND\s+ALL\s+OTHER\s+DETAILS\b.*$',
    re.IGNORECASE,
)
# Matches Incoterm keyword followed by port/country text.
_INCOTERM_RE = re.compile(
    r'\s+\b(CFR|FOB|CIF|C&F|CPT|CIP|DAP|DDP|EXW|FCA|INCOTERMS?)\b[\s\w,.()/]*$',
    re.IGNORECASE,
)


def clean_goods_description(text: Optional[str]) -> Optional[str]:
    """Strip leaked quantity/price/Incoterm text from a raw SWIFT F45A goods-description block,
    leaving just the commodity description. Falls back to the original (stripped) text if
    cleaning would leave nothing, so an aggressive match never blanks the field."""
    if not text or not str(text).strip():
        return text
    raw = str(text).strip()
    s = _QTY_PREFIX_RE.sub('', raw)
    s = _OTHER_DETAILS_RE.sub('', s)   # strip "AND ALL OTHER DETAILS AS PER ..." first
    s = _PRICE_CLAUSE_RE.sub('', s)
    s = _INCOTERM_RE.sub('', s)
    s = re.sub(r'\s+', ' ', s).strip(' ,.-')
    return s or raw


# ===========================================================================
# Generic extracted-field sanity checks
#
# ONE canonical place to catch a value landing in the wrong-shaped field — a text field
# (country/origin, product/importer name, ...) that's actually a date, or vice versa —
# regardless of whether it came from an Excel import (lc_importer.py) or AI document
# extraction (LC/Contract/Invoice extractors). Import and reuse THIS rather than adding a
# new ad-hoc regex per module, so a fix here covers every caller instead of drifting.
# ===========================================================================
_DATE_SHAPED_RE = re.compile(
    r'^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}:\d{2})?$|^\d{1,2}[/-]\d{1,2}[/-]\d{2,4}$'
)


def looks_like_date_value(value) -> bool:
    """True when `value` is a real date/datetime/Timestamp object, or an obviously
    date-shaped string (e.g. "2024-03-15", "15/03/2024") — never a valid free-text field
    like a country, product, or company name."""
    if value is None:
        return False
    import datetime as _dt
    if isinstance(value, (_dt.datetime, _dt.date)):
        return True
    try:
        import pandas as _pd
        if isinstance(value, _pd.Timestamp):
            return True
    except ImportError:
        pass
    return bool(_DATE_SHAPED_RE.match(str(value).strip()))


def validate_text_field(value: Optional[str], field_label: str) -> tuple[Optional[str], bool]:
    """Sanity-check a free-text extracted/imported field (country, product name, importer,
    supplier, ...). Returns (value, needs_review) — never mutates a non-date-shaped value,
    just flags obviously-wrong ones (e.g. a date landing in a text field) for review instead
    of silently storing/displaying it as if it were valid."""
    if value is None or not str(value).strip():
        return value, False
    if looks_like_date_value(value):
        logger.warning(f"{field_label}: extracted value looks like a date ({value!r}), not text — flagged for review.")
        return value, True
    return value, False


def is_secondary_quality(u: str) -> bool:
    return bool(re.search(r"\bSECONDARY\b|\bSEC\b|\b2ND\b|\bDEFECT|\bREJECT", u))


def detect_quality(text: Optional[str]) -> str:
    """PRIME/SECONDARY from a raw goods-description block (e.g. SWIFT F45A). Same
    keyword rule normalize_item() uses to pick a product code's P/S suffix — kept as a
    separate call so callers that need the quality on its own (LC creation, where not
    every product code carries a P/S suffix — wire rod, galvalume, plastic, ...) don't
    have to re-derive it from the code."""
    if not text or not text.strip():
        return "PRIME"
    return "SECONDARY" if is_secondary_quality(text.upper()) else "PRIME"


def normalize_item(text):
    """Map a (possibly long, commercial) item description to ONE short product code.

    Returns (code_or_None, needs_review). Quantity / rate / CFR / port / LC wording is
    ignored — only the commodity keywords drive the mapping. Closed set: anything that
    can't be confidently mapped returns (None, True) so it is saved-but-flagged, never
    turned into a brand-new code.
    """
    if not text or not str(text).strip():
        return None, True

    raw = str(text).strip()
    u = re.sub(r"\s+", " ", raw.upper())

    # Already a valid short code?
    token = re.sub(r"[^A-Z0-9]", "", u)
    if token in VALID_ITEM_CODES:
        return token, False

    sec = is_secondary_quality(u)

    def has(*words):
        return any(w in u for w in words)

    # Order matters — most specific commodities first.
    # Plastic
    if has("PLASTIC", "PMC"):
        return "PMC", False
    # Galvalume
    if has("GALVALUME", "GALVALUM", "ALUZINC", "ALU-ZINC", "ALUMINIUM ZINC", "ALUMINUM ZINC"):
        return "GLP", False
    # Electro-galvanized
    if has("ELECTRO GALVAN", "ELECTRO-GALVAN", "ELECTROGALVAN", "ELECTRO GALV"):
        return "EG", False
    # Cold-rolled non-grain-oriented / electrical / silicon steel
    if has("NON GRAIN", "NON-GRAIN", "NGO", "GRAIN ORIENTED", "ELECTRICAL STEEL", "SILICON STEEL"):
        return "CRNGO", False
    # Leader / end-cut hot-rolled coils (PUP HRS)
    if has("LEADER END CUT", "END CUT COIL", "LEADER END", "PUP HR") and has("HOT ROLL", "HR "):
        return "PUPHRS", False
    if has("PUPHRS"):
        return "PUPHRS", False
    if has("PUPCRS"):
        return "PUPCRS", False
    # Prepainted / colour-coated galvanized (PPGI)
    if has("PREPAINTED", "PRE-PAINTED", "PRE PAINTED", "PPGI", "COLOR COATED",
           "COLOUR COATED", "COLOR-COATED", "COLOUR-COATED"):
        return ("PPGIS" if sec else "PPGIP"), False
    # Wire rod
    if has("WIRE ROD", "WIRE-ROD", "WIREROD"):
        if has("HIGH CARBON", "HI CARBON", "HIGH-CARBON"):
            return "WRHC", False
        if has("LOW CARBON", "LOW-CARBON"):
            return "WRLC", False
        # bare "wire rod" without a carbon grade -> ambiguous
        return None, True
    # Galvanized plain (GI)
    if has("GALVANIZ", "GALVANIS", "HOT DIPPED GALV", "ZINC COATED", "ZINC-COATED",
           "GALVANIZED PLAIN", "GALVANISED PLAIN") or re.search(r"\bGI\b", u) or re.search(r"\bGP\b", u):
        return ("GPS" if sec else "GPP"), False
    # Cold rolled
    if has("COLD ROLL", "COLD-ROLL", "CRC") or re.search(r"\bCR COIL", u) or re.search(r"\bCR\b", u):
        return ("CRS" if sec else "CRP"), False
    # Hot rolled
    if has("HOT ROLL", "HOT-ROLL", "HRC") or re.search(r"\bHR COIL", u) or re.search(r"\bHR\b", u):
        return ("HRS" if sec else "HRP"), False

    return None, True


# ===========================================================================
# Vessel name normalization  (single source of truth; report_endpoints re-imports)
# ===========================================================================

# Trailing voyage token: " V" / " V." / " V106509" / " Voy 12" / " Voyage 5".
# Strips voyage suffixes (e.g. V 12, VOYAGE 5) but preserves single letter suffixes (e.g. EFFIE V vs EFFIE).
_VOY_RE = re.compile(r"\s+(?:VOYAGE|VOY)(?![A-Z]).*$|\s+V(?:[\s\.-]*\d+.*)$", re.IGNORECASE)


def norm_vessel(name):
    """Canonical key for grouping free-text vessel names (no vessels master).
    Case-insensitive; ignores dots & extra spaces; drops a trailing voyage token,
    preserving standalone suffixes like 'EFFIE V' while collapsing 'EFFIE V.' to 'EFFIE V'."""
    if not name:
        return None
    s = str(name).upper().split(",")[0]
    s = s.replace(".", " ")
    s = re.sub(r"\s+", " ", s).strip()
    s = _VOY_RE.sub("", s).strip()
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


# ===========================================================================
# Issuing-bank normalization
# ===========================================================================

_BANK_RULES = [
    (("MCB ISLAMIC",), "MCB Islamic Bank"),
    (("MCB", "MUSLIM COMMERCIAL"), "MCB Bank"),
    (("HABIB METRO", "HABIBMETRO", "HMBL"), "Habib Metro Bank"),
    (("BANK AL-HABIB", "BANK AL HABIB", "AL-HABIB", "AL HABIB", "BAHL"), "Bank Al Habib"),
    (("HABIB BANK", "HBL"), "HBL"),
    (("SONERI",), "Soneri Bank"),
    (("BANKISLAMI", "BANK ISLAMI", "BIPL"), "BankIslami"),
    (("UBL", "UNITED BANK"), "UBL"),
    (("ALBARAKA", "AL BARAKA"), "Al Baraka Bank"),
    (("ASKARI",), "Askari Bank"),
    (("MEEZAN",), "Meezan Bank"),
    (("ALFALAH", "AL FALAH", "BAFL"), "Bank Alfalah"),
    (("ALLIED", "ABL"), "Allied Bank"),
    (("NATIONAL BANK", "NBP"), "National Bank of Pakistan"),
    (("FAYSAL",), "Faysal Bank"),
    (("STANDARD CHARTERED", "SCB"), "Standard Chartered"),
    (("DUBAI ISLAMIC", "DIB"), "Dubai Islamic Bank"),
    (("JS BANK", "JSBL"), "JS Bank"),
    (("SINDH BANK",), "Sindh Bank"),
    (("BANK OF PUNJAB", "BOP"), "Bank of Punjab"),
    (("BANK OF KHYBER",), "Bank of Khyber"),
    (("SUMMIT",), "Summit Bank"),
    (("SILK",), "Silkbank"),
    (("SAMBA",), "Samba Bank"),
    (("CITI",), "Citibank"),
    (("DEUTSCHE",), "Deutsche Bank"),
    (("ICBC", "INDUSTRIAL AND COMMERCIAL"), "ICBC"),
    (("MOBILINK", "MMBL"), "Mobilink Microfinance Bank"),
]


def norm_bank(name):
    """Group an issuing-bank free-text value to one clean bank name."""
    if not name or not str(name).strip():
        return "(Unknown Bank)"
    u = re.sub(r"\s+", " ", str(name).upper()).strip()
    for keys, canon in _BANK_RULES:
        for k in keys:
            if k in u:
                return canon
    s = u.split("(")[0].split(",")[0]
    s = re.sub(r"\b(LIMITED|LTD|PVT|PRIVATE|PLC|PAKISTAN|BRANCH|BR|HEAD OFFICE)\b", " ", s)
    s = re.sub(r"[^A-Z ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s.title() if s else "(Unknown Bank)"


def _party_models():
    """Lazy import so normalization_service stays importable from model init."""
    from models.database_models import Importer, Supplier, Indentor, BookedBy
    return {
        "importer": Importer,
        "supplier": Supplier,
        "indentor": Indentor,
        "booked_by": BookedBy,
    }


def _bank_key(name):
    """Uppercased canonical key for bank matching (via norm_bank rules)."""
    canon = norm_bank(name)
    if not canon or canon == "(Unknown Bank)":
        return ""
    return re.sub(r"\s+", " ", canon.upper()).strip()


def match_bank(name, db):
    """Match a free-text bank name to the banks master.

    Returns (canonical_name, matched_row_or_None, needs_review).
    Uses norm_bank rules first, then exact/fuzzy key match against existing masters.
    """
    if not name or not str(name).strip():
        return None, None, False

    canon = norm_bank(name)
    if not canon or canon == "(Unknown Bank)":
        return canon, None, True

    from models.database_models import Bank

    key = _bank_key(name)
    rows = db.query(Bank).all()
    keyed = {_bank_key(r.name): r for r in rows if _bank_key(r.name)}

    if key in keyed:
        r = keyed[key]
        return r.name, r, False

    close = difflib.get_close_matches(key, list(keyed.keys()), n=1, cutoff=0.85)
    if close:
        r = keyed[close[0]]
        return r.name, r, False

    nn = re.sub(r"\s+", " ", canon.upper()).strip()
    row = Bank(name=canon, name_norm=nn, needs_review=True)
    db.add(row)
    db.flush()
    logger.info(f"Normalization: new unmatched bank flagged for review: '{canon}'")
    return canon, row, True


def normalize_party_name(name, db, kind):
    """Return the canonical name for a party kind, or the cleaned original if empty.

    kind: importer | supplier | indentor | booked_by | bank
    """
    if kind == "bank":
        canon, _row, _rev = match_bank(name, db)
        return canon
    model = _party_models().get(kind)
    if not model:
        return _title_clean(name) if name else None
    canon, _row, _rev = match_company(name, db, model)
    return canon


def normalize_importer(name, db):
    return normalize_party_name(name, db, "importer")


def normalize_supplier(name, db):
    return normalize_party_name(name, db, "supplier")


def normalize_indentor(name, db):
    return normalize_party_name(name, db, "indentor")


def normalize_booked_by(name, db):
    return normalize_party_name(name, db, "booked_by")


def normalize_bank(name, db):
    """Canonical bank name + ensure banks master row exists."""
    canon, _row, _rev = match_bank(name, db)
    return canon


def normalize_lc_master(lc, db):
    """Overwrite LC header party/bank fields with canonical master names."""
    if lc.importer_name:
        canon = normalize_importer(lc.importer_name, db)
        if canon:
            lc.importer_name = canon
    if lc.supplier_name:
        canon = normalize_supplier(lc.supplier_name, db)
        if canon:
            lc.supplier_name = canon
    if lc.indentor:
        canon = normalize_indentor(lc.indentor, db)
        if canon:
            lc.indentor = canon
    if lc.booked_by:
        canon = normalize_booked_by(lc.booked_by, db)
        if canon:
            lc.booked_by = canon
    if lc.bank_name:
        canon = normalize_bank(lc.bank_name, db)
        if canon and canon != "(Unknown Bank)":
            lc.bank_name = canon


def normalize_contract_parties(c, db):
    """Overwrite contract party + bank fields with canonical master names."""
    if c.buyer_name:
        canon = normalize_importer(c.buyer_name, db)
        if canon:
            c.buyer_name = canon
    if c.supplier_name:
        canon = normalize_supplier(c.supplier_name, db)
        if canon:
            c.supplier_name = canon
    if c.indentor_name:
        canon = normalize_indentor(c.indentor_name, db)
        if canon:
            c.indentor_name = canon
    if c.bank_name and str(c.bank_name).strip():
        canon = normalize_bank(c.bank_name, db)
        if canon and canon != "(Unknown Bank)":
            c.bank_name = canon


def normalize_goods_declaration(gd, db):
    """Overwrite GD importer/exporter with canonical master names."""
    if gd.importer_name and str(gd.importer_name).strip():
        canon = normalize_importer(gd.importer_name, db)
        if canon:
            gd.importer_name = canon
    if gd.exporter_name and str(gd.exporter_name).strip():
        canon = normalize_supplier(gd.exporter_name, db)
        if canon:
            gd.exporter_name = canon


def normalize_invoice_parties(inv, db):
    """Overwrite CI seller/buyer with canonical master names."""
    if inv.seller_name and str(inv.seller_name).strip():
        canon = normalize_supplier(inv.seller_name, db)
        if canon:
            inv.seller_name = canon
    if inv.buyer_name and str(inv.buyer_name).strip():
        canon = normalize_importer(inv.buyer_name, db)
        if canon:
            inv.buyer_name = canon


def normalize_bl_parties(bl, db):
    """Overwrite BL shipper/consignee with canonical master names."""
    if bl.shipper_name and str(bl.shipper_name).strip():
        canon = normalize_supplier(bl.shipper_name, db)
        if canon:
            bl.shipper_name = canon
    if bl.consignee and str(bl.consignee).strip():
        canon = normalize_importer(bl.consignee, db)
        if canon:
            bl.consignee = canon


# ===========================================================================
# LC payment tenor  (SIGHT vs DA)
# ===========================================================================

def payment_tenor(payment_terms):
    """Classify an LC's payment tenor from the free-text 'Drafts At' / payment terms
    (SWIFT F42C). Returns 'SIGHT', 'DA', or None if it can't be told.

    'SIGHT' / 'ON SIGHT' -> SIGHT.  'DA' / usance / acceptance / deferred / '<n> DAYS' -> DA.
    """
    if not payment_terms or not str(payment_terms).strip():
        return None
    u = str(payment_terms).upper()
    if "SIGHT" in u:
        return "SIGHT"
    if (re.search(r"\bDA\b", u) or "USANCE" in u or "ACCEPTANCE" in u
            or "DEFERRED" in u or "TENOR" in u or re.search(r"\d+\s*DAYS", u) or "DAYS" in u):
        return "DA"
    return None

"""Field-level merge policy when uploading a document after manual stub entry."""
from __future__ import annotations

from typing import Any, Optional

SOURCE_MANUAL = "MANUAL"
SOURCE_EXTRACTED = "EXTRACTED"
SOURCE_CONTRACT = "CONTRACT"


def _norm(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, (int, float)):
        return str(val)
    return str(val).strip()


def merge_extracted_with_existing(
    existing: dict,
    extracted: dict,
    field_sources: Optional[dict],
    *,
    scalar_fields: list[str],
    line_items_key: Optional[str] = None,
) -> dict:
    """Preview merge — does not mutate DB. Returns merged preview + conflicts."""
    sources = dict(field_sources or {})
    merged = dict(existing)
    conflicts: list[dict] = []

    for field in scalar_fields:
        new_val = extracted.get(field)
        if new_val in (None, ""):
            continue
        old_val = existing.get(field)
        src = (sources.get(field) or "").upper()
        if old_val not in (None, "") and _norm(old_val) != _norm(new_val):
            if src == SOURCE_MANUAL or src == SOURCE_EXTRACTED:
                conflicts.append({
                    "field": field,
                    "manual": old_val,
                    "extracted": new_val,
                    "source": src or SOURCE_MANUAL,
                })
                continue
        merged[field] = new_val
        sources[field] = SOURCE_EXTRACTED

    line_conflicts: list[dict] = []
    if line_items_key and line_items_key in extracted:
        existing_items = existing.get(line_items_key) or []
        new_items = extracted.get(line_items_key) or []
        merged_items, line_conflicts = merge_line_items(existing_items, new_items)
        merged[line_items_key] = merged_items

    return {
        "extracted": merged,
        "conflicts": conflicts,
        "line_item_conflicts": line_conflicts,
        "field_sources": sources,
        "file_pending": False,
    }


def merge_line_items(existing: list, extracted: list) -> tuple[list, list]:
    """Merge line items by item_number where possible."""
    by_num: dict[Any, dict] = {}
    for row in existing:
        num = row.get("item_number")
        if num is not None:
            by_num[num] = dict(row)

    conflicts: list[dict] = []
    result: list[dict] = []
    seen: set[Any] = set()

    for row in extracted:
        num = row.get("item_number")
        if num is not None and num in by_num:
            old = by_num[num]
            diff_fields = [
                f for f in row
                if f != "item_number" and _norm(old.get(f)) != _norm(row.get(f))
            ]
            if diff_fields:
                conflicts.append({"item_number": num, "fields": diff_fields, "manual": old, "extracted": row})
            merged_row = {**old, **{k: v for k, v in row.items() if v not in (None, "")}}
            result.append(merged_row)
            seen.add(num)
        else:
            result.append(dict(row))

    for num, old in by_num.items():
        if num not in seen:
            result.append(old)
            conflicts.append({"item_number": num, "fields": ["_row"], "manual": old, "extracted": None, "action": "keep_or_drop"})

    return result, conflicts


def apply_merge_overwrites(
    target_sources: dict,
    confirm_overwrites: Optional[list[str]],
) -> set[str]:
    return {f for f in (confirm_overwrites or []) if f}


def build_field_sources_on_save(
    data: dict,
    existing_sources: Optional[dict],
    *,
    staged_file: Optional[str],
    manual_fields: list[str],
) -> dict:
    sources = dict(existing_sources or {})
    if staged_file:
        for field in manual_fields:
            if field in data and data[field] not in (None, ""):
                if field not in sources:
                    sources[field] = SOURCE_EXTRACTED
    else:
        for field in manual_fields:
            if field in data and data[field] not in (None, ""):
                sources[field] = SOURCE_MANUAL
    return sources

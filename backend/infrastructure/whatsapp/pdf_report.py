"""
Branded PDF reports sent to WhatsApp recipients as documents:
  - build_price_alert_pdf  -> LME price alerts affecting our LCs
  - build_lme_rates_pdf    -> the LME rates themselves, after a new bulletin syncs

Branding (logo, company name, band colour) comes from whatever the admin set in
Admin -> Branding; the logo committed in the repo is the fallback when branding
was never configured. Logos are white-on-transparent, so the header renders them
on a dark band rather than directly on the white page background.
"""

import io
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.enums import TA_RIGHT
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image,
)

logger = logging.getLogger("uvicorn")

_REPO_LOGO_PATH = Path(__file__).resolve().parents[3] / "logo" / "perfect-craft-logo-white.png"
_DEFAULT_APP_NAME = "Perfect Craft"
_DEFAULT_BAND_COLOR = "#0F172A"  # slate-900 — dark backdrop for the white logo
_MARGIN = 15 * mm

# Friendly names for the product-group keys used by the rates matrix.
_GROUP_LABELS = {
    "HR": "HR — Hot Rolled",
    "CR": "CR — Cold Rolled",
    "GP": "GP / EG / PPGI",
    "CRNGO": "CRNGO",
    "WR": "Wire Rod",
}

_TITLE_STYLE = ParagraphStyle(
    "WaPdfTitle", fontName="Helvetica-Bold", fontSize=15, textColor=colors.white, leading=18,
)
_SUBTITLE_STYLE = ParagraphStyle(
    "WaPdfSubtitle", fontName="Helvetica", fontSize=9, textColor=colors.HexColor("#cbd5e1"), leading=12,
)
_RECIPIENT_STYLE = ParagraphStyle(
    "WaPdfRecipient", fontName="Helvetica", fontSize=9, textColor=colors.HexColor("#475569"),
)
_FOOTER_STYLE = ParagraphStyle(
    "WaPdfFooter", fontName="Helvetica", fontSize=7.5, textColor=colors.HexColor("#94a3b8"), alignment=TA_RIGHT,
)


def _fmt_money(val) -> str:
    if val is None:
        return "-"
    try:
        return f"{float(val):,.2f}"
    except (TypeError, ValueError):
        return str(val)


def _fmt_pct(val) -> str:
    if val is None:
        return "-"
    try:
        return f"{float(val):+.2f}%"
    except (TypeError, ValueError):
        return str(val)


def resolve_branding(db=None) -> dict:
    """
    Branding to stamp on generated PDFs: whatever the admin set in
    Admin -> Branding, falling back to the logo committed in the repo so PDFs
    still render if branding was never configured. Never raises.

    SVG logos are skipped — the admin upload accepts them but neither Pillow nor
    reportlab can rasterise SVG, so we fall back rather than render nothing.
    """
    app_name = _DEFAULT_APP_NAME
    bg_color = _DEFAULT_BAND_COLOR
    logo_path = _REPO_LOGO_PATH if _REPO_LOGO_PATH.exists() else None

    if db is not None:
        try:
            from models.database_models import BrandingConfig
            cfg = db.query(BrandingConfig).order_by(BrandingConfig.config_id).first()
            if cfg:
                if cfg.app_name:
                    app_name = cfg.app_name
                if cfg.logo_bg_color:
                    bg_color = cfg.logo_bg_color
                if cfg.logo_path:
                    uploaded = Path(cfg.logo_path)
                    if uploaded.suffix.lower() == ".svg":
                        logger.info("PDF branding: uploaded logo is SVG (not rasterisable) — using repo logo")
                    elif uploaded.exists():
                        logo_path = uploaded
                    else:
                        logger.warning("PDF branding: uploaded logo missing at %s — using repo logo", uploaded)
        except Exception:
            logger.warning("PDF branding: could not read BrandingConfig — using defaults", exc_info=True)

    return {"app_name": app_name, "bg_color": bg_color, "logo_path": logo_path}


def _cropped_logo_bytes(logo_path: Path) -> Optional[bytes]:
    """
    Logo files typically carry transparent padding around the mark (the repo one
    is ~395x136 of content inside a 400x190 canvas) — scaling the raw file
    stretches that dead space too, rendering the logo small and off-balance.
    Crop to the actual visible content first.
    """
    try:
        from PIL import Image as PILImage
        img = PILImage.open(logo_path)
        bbox = img.split()[-1].getbbox() if img.mode == "RGBA" else None
        if bbox:
            img = img.crop(bbox)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        logger.warning("PDF: failed to crop logo at %s", logo_path, exc_info=True)
        return None


def _logo_cell(branding: dict):
    logo_path = branding.get("logo_path")
    if logo_path and Path(logo_path).exists():
        cropped = _cropped_logo_bytes(Path(logo_path))
        try:
            logo = Image(io.BytesIO(cropped)) if cropped else Image(str(logo_path))
            logo.drawHeight = 15 * mm
            logo.drawWidth = logo.drawHeight * (logo.imageWidth / logo.imageHeight)
            return logo
        except Exception:
            logger.warning("PDF: failed to load logo at %s", logo_path, exc_info=True)
    return Paragraph(branding.get("app_name", _DEFAULT_APP_NAME).upper(), _TITLE_STYLE)


def _header_table(width: float, branding: dict, title: str, subtitle: str) -> Table:
    """Dark band containing the logo (left) and title/subtitle (right)."""
    title_block = [
        Paragraph(title, _TITLE_STYLE),
        Paragraph(subtitle, _SUBTITLE_STYLE),
    ]
    header = Table([[_logo_cell(branding), title_block]], colWidths=[width * 0.32, width * 0.68])
    header.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(branding.get("bg_color", _DEFAULT_BAND_COLOR))),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (0, 0), "LEFT"),
        ("LEFTPADDING", (0, 0), (0, 0), 16),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (1, 0), (1, 0), 16),
    ]))
    return header


def _alert_row(alert) -> list:
    lc_number = alert.lc.lc_number if alert.lc else f"LC#{alert.lc_id}"
    product = alert.product.product_code if alert.product else "?"
    date_str = alert.alert_date.strftime("%d-%b-%Y") if alert.alert_date else "-"
    arrow = "UP" if alert.alert_type == "INCREASE" else "DOWN"
    impact = alert.total_impact
    return [
        date_str,
        lc_number,
        product,
        f"{arrow} {alert.alert_type}",
        _fmt_money(alert.old_lme_price),
        _fmt_money(alert.new_lme_price),
        _fmt_pct(alert.difference_percent),
        _fmt_money(impact) if impact is not None else "-",
    ]


def build_price_alert_pdf(alerts: list, recipient_name: Optional[str] = None,
                          branding: Optional[dict] = None) -> bytes:
    """
    Build a branded, tabular PDF summarizing a batch of LME PriceAlert rows.
    Returns raw PDF bytes, ready to upload to Meta's media endpoint.
    """
    branding = branding or resolve_branding()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=_MARGIN, rightMargin=_MARGIN, topMargin=_MARGIN, bottomMargin=_MARGIN,
    )
    content_width = A4[0] - 2 * _MARGIN

    story: list = [
        _header_table(content_width, branding, "LME Price Alert",
                      f"Generated {datetime.now():%d %b %Y, %H:%M}"),
        Spacer(1, 8 * mm),
    ]

    if recipient_name:
        story.append(Paragraph(f"Prepared for: <b>{recipient_name}</b>", _RECIPIENT_STYLE))
        story.append(Spacer(1, 4 * mm))

    table_data = [["Date", "LC #", "Product", "Change", "Old Price", "New Price", "Diff %", "Impact"]]
    table_data.extend(_alert_row(a) for a in alerts)

    col_widths = [w * content_width for w in (0.11, 0.16, 0.13, 0.13, 0.13, 0.13, 0.10, 0.11)]
    table = Table(table_data, colWidths=col_widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e293b")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("ALIGN", (4, 0), (-1, -1), "RIGHT"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ]))

    story.append(table)
    story.append(Spacer(1, 6 * mm))
    story.append(Paragraph(f"{branding['app_name']} — LME Monitoring System", _FOOTER_STYLE))

    doc.build(story)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# LME rates report — the rates themselves, sent after a new bulletin syncs
# --------------------------------------------------------------------------- #

_SECTION_STYLE = ParagraphStyle(
    "WaPdfSection", fontName="Helvetica-Bold", fontSize=10, textColor=colors.HexColor("#0f172a"),
    spaceAfter=3,
)
_NOTE_STYLE = ParagraphStyle(
    "WaPdfNote", fontName="Helvetica", fontSize=8.5, textColor=colors.HexColor("#64748b"),
)


def _rate_cell(value, change) -> str:
    """'500.34  (+1.20)' — the change vs the previous bulletin, when known."""
    if value is None:
        return "-"
    text = f"{float(value):,.2f}"
    if change is not None and float(change) != 0:
        text += f"   ({float(change):+.2f})"
    return text


def _group_table(group_result: dict, content_width: float) -> Optional[Table]:
    """One region-by-column table of the latest rates for a single product group."""
    matrix = group_result.get("matrix") or []
    if not matrix:
        return None
    latest = matrix[0]  # build_rates_matrix() returns newest-first

    col1_label = group_result.get("col1_label") or "Rate"
    col2_label = group_result.get("col2_label")

    header = ["Region", col1_label] + ([col2_label] if col2_label else [])
    rows = [header]
    has_any_value = False

    for region, region_label in (("CHINA", "China"), ("AFRICA", "Africa"),
                                 ("EUROPE", "Europe"), ("UAE", "UAE")):
        key = region.lower()
        v1 = latest.get(f"{key}_col1")
        row = [region_label, _rate_cell(v1, latest.get(f"{key}_col1_chg"))]
        if col2_label:
            v2 = latest.get(f"{key}_col2")
            row.append(_rate_cell(v2, latest.get(f"{key}_col2_chg")))
            has_any_value = has_any_value or v2 is not None
        has_any_value = has_any_value or v1 is not None
        rows.append(row)

    if not has_any_value:
        return None  # nothing computed for this group — omit rather than print a table of dashes

    n_cols = len(header)
    widths = [content_width * 0.22] + [content_width * (0.78 / (n_cols - 1))] * (n_cols - 1)
    table = Table(rows, colWidths=widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e293b")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
    ]))
    return table


def build_lme_rates_pdf(rates_by_group: list, bulletin_date: Optional[str] = None,
                        branding: Optional[dict] = None) -> bytes:
    """
    Build the branded LME rates report: the latest calculated rate per region for
    each product group, with the change against the previous bulletin.

    `rates_by_group` is a list of build_rates_matrix() results (one per group).
    `bulletin_date` is the LME bulletin's own date — the date the rates are FOR,
    not when we processed them.
    Returns raw PDF bytes, ready to upload to Meta's media endpoint.
    """
    branding = branding or resolve_branding()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=_MARGIN, rightMargin=_MARGIN, topMargin=_MARGIN, bottomMargin=_MARGIN,
    )
    content_width = A4[0] - 2 * _MARGIN

    if bulletin_date:
        try:
            pretty_date = datetime.strptime(bulletin_date, "%Y-%m-%d").strftime("%d %b %Y")
        except (ValueError, TypeError):
            pretty_date = str(bulletin_date)
        subtitle = f"Bulletin dated {pretty_date}"
    else:
        subtitle = f"Generated {datetime.now():%d %b %Y, %H:%M}"

    story: list = [
        _header_table(content_width, branding, "LME Rates Report", subtitle),
        Spacer(1, 8 * mm),
    ]

    rendered = 0
    for group_result in rates_by_group:
        table = _group_table(group_result, content_width)
        if table is None:
            continue
        group_key = (group_result.get("group") or "").upper()
        story.append(Paragraph(_GROUP_LABELS.get(group_key, group_key), _SECTION_STYLE))
        story.append(table)
        story.append(Spacer(1, 6 * mm))
        rendered += 1

    if rendered == 0:
        story.append(Paragraph("No calculated rates available for this bulletin.", _NOTE_STYLE))
        story.append(Spacer(1, 4 * mm))
    else:
        story.append(Paragraph(
            "Figures in brackets show the change against the previous bulletin.", _NOTE_STYLE))
        story.append(Spacer(1, 4 * mm))

    story.append(Paragraph(f"{branding['app_name']} — LME Monitoring System", _FOOTER_STYLE))

    doc.build(story)
    return buf.getvalue()

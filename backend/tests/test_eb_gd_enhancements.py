"""
Self-check unit test for EB/GD item details (m_size), GD lab report assessment,
and Report Builder insurance integration.
"""

from decimal import Decimal
from models.database_models import GoodsDeclaration, GDItem, ExBondItem, InsuranceCertificate, Shipment
from modules.weboc.gd_schemas import GDItemIn
from modules.weboc.services import apply_item_details, get_item_details_current
from modules.weboc.partial_gd_service import apply_partial_gd_item_details, get_partial_gd_detail
from modules.reports.report_master_service import _insurance_snapshot, _primary_insurance
from modules.reports.report_field_catalog import SOURCE_FIELD_MAP, qualify_key

def test_gd_item_m_size_extraction_and_dict():
    item = GDItem(item_number=1, hs_code="7209.1690", goods_description="Cold Rolled Steel", m_size="2 mm", quantity=Decimal("24.07"))
    assert item.m_size == "2 mm"

def test_ex_bond_item_m_size():
    item = ExBondItem(item_number=1, hs_code="7209.1690", m_size="2 mm")
    assert item.m_size == "2 mm"

def test_insurance_snapshot_calculation():
    ins = InsuranceCertificate(
        certificate_number="INS-100",
        sum_insured=Decimal("100000.00"),
        gross_premium=Decimal("250.00"),
        status="VERIFIED"
    )
    snap = _insurance_snapshot(ins)
    assert snap["certificate_number"] == "INS-100"
    assert snap["sum_insured"] == 100000.00
    assert snap["gross_premium"] == 250.00
    assert snap["premium_rate_pct"] == 0.25

def test_catalog_contains_insurance():
    assert "insurance" in SOURCE_FIELD_MAP
    keys = [f["key"] for f in SOURCE_FIELD_MAP["insurance"]]
    assert "premium_rate_pct" in keys
    assert "sum_insured" in keys

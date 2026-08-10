"""Tests for the Commercial Invoice module migration (modules/documents/invoice_router.py,
invoice_service.py, invoice_schemas.py)."""
from modules.documents.invoice_schemas import InvoiceSave


# ---------------------------------------------------------------------------
# Schema-level
# ---------------------------------------------------------------------------

def test_schema_tolerates_malformed_decimal():
    data = InvoiceSave(total_amount_usd="garbage")
    assert data.total_amount_usd is None


def test_schema_tolerates_malformed_date():
    data = InvoiceSave(invoice_date="garbage")
    assert data.invoice_date is None


def test_decimal_present_key_clears_semantics():
    data = InvoiceSave(total_amount_usd=None)
    assert "total_amount_usd" in data.model_fields_set


def test_line_items_tolerate_malformed_decimal():
    data = InvoiceSave(line_items=[{"item_number": "1", "quantity_mt": "garbage"}])
    assert data.line_items[0].item_number == 1
    assert data.line_items[0].quantity_mt is None


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------

def test_viewer_can_read_but_not_write(client, make_user):
    user, password = make_user(role_name="VIEWER")
    login = client.post("/api/auth/login", data={"username": user.username, "password": password})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    resp = client.post("/api/invoices/", headers=headers, json={"invoice_number": "X"})
    assert resp.status_code == 403


def test_create_get_update_delete_lifecycle(authenticated_client):
    create = authenticated_client.post("/api/invoices/", json={
        "invoice_number": "INV-TEST-001", "total_amount_usd": "5000.00",
        "total_net_weight_mt": "10.5", "invoice_date": "2026-01-15",
        "line_items": [
            {"item_number": 1, "quantity_mt": "5.0", "unit_price_usd": "500.00"},
            {"item_number": 2, "quantity_mt": "5.5", "unit_price_usd": "500.00"},
        ],
    })
    assert create.status_code == 200, create.text
    invoice_id = create.json()["invoice_id"]

    got = authenticated_client.get(f"/api/invoices/{invoice_id}")
    assert got.status_code == 200
    assert got.json()["invoice_number"] == "INV-TEST-001"
    assert got.json()["total_amount_usd"] == 5000.0
    assert len(got.json()["line_items"]) == 2
    # save_invoice always force-sets VERIFIED regardless of prior status
    assert got.json()["status"] == "VERIFIED"

    updated = authenticated_client.put(f"/api/invoices/{invoice_id}", json={"grade": "Grade A"})
    assert updated.status_code == 200
    reget = authenticated_client.get(f"/api/invoices/{invoice_id}")
    assert reget.json()["grade"] == "Grade A"
    # line_items absent from the PUT payload -> untouched (still 2)
    assert len(reget.json()["line_items"]) == 2

    replaced = authenticated_client.put(f"/api/invoices/{invoice_id}", json={
        "line_items": [{"item_number": 1, "quantity_mt": "9.0"}],
    })
    assert replaced.status_code == 200
    assert len(authenticated_client.get(f"/api/invoices/{invoice_id}").json()["line_items"]) == 1

    deleted = authenticated_client.delete(f"/api/invoices/{invoice_id}")
    assert deleted.status_code == 200

    missing = authenticated_client.get(f"/api/invoices/{invoice_id}")
    assert missing.status_code == 404


def test_str_field_explicit_null_does_not_clear(authenticated_client):
    create = authenticated_client.post("/api/invoices/", json={"invoice_number": "INV-KEEP-001"})
    invoice_id = create.json()["invoice_id"]

    resp = authenticated_client.put(f"/api/invoices/{invoice_id}", json={"invoice_number": None})
    assert resp.status_code == 200
    assert authenticated_client.get(f"/api/invoices/{invoice_id}").json()["invoice_number"] == "INV-KEEP-001"

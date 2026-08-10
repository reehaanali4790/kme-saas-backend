"""Tests for GD examination report availability used by Report Builder."""
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

from modules.weboc.gd_service import examination_report_available


def _gd(**kwargs):
    base = dict(
        gd_id=1,
        examined_date=None,
        vir_no=None,
        index_no=None,
        examination_report=None,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_examination_report_available_false_when_empty():
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    assert examination_report_available(_gd(), db) is False


def test_examination_report_available_true_for_note():
    db = MagicMock()
    assert examination_report_available(_gd(examination_report="Examined OK"), db) is True


def test_examination_report_available_true_for_date_and_vir():
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    gd = _gd(examined_date=date(2026, 3, 1), vir_no="VIR-99")
    assert examination_report_available(gd, db) is True


def test_examination_report_available_false_for_date_only():
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    assert examination_report_available(_gd(examined_date=date(2026, 3, 1)), db) is False

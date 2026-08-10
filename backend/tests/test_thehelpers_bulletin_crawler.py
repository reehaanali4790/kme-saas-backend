"""Tests for integrations/thehelpers/thehelpers_bulletin_crawler.py - the automated
crawler that feeds thehelpers.pk bulletins through the existing manual-upload
pipeline. Network/PDF-parsing itself is exercised manually against the real site
(see the task notes); these tests cover the logic that's cheap and safe to run in
CI: link/date filtering, dedup, currency-rate fallback, and the orchestration's
error handling.
"""
from datetime import date, timedelta
from decimal import Decimal

from integrations.thehelpers.thehelpers_bulletin_crawler import (
    _already_imported, _find_recent_pdf_links, _pick_currency_rate,
    crawl_and_import_bulletins,
)
from models.database_models import CurrencyRate, LMEBulletin, LMEBulletinCrawlLog


SAMPLE_INDEX_HTML = """
<div class="sidebar">
  <a href="/uploads/pdf/LMB%2029-07-2026%20The%20Helpers.pdf">29-Jul-2026</a>
  <a href="/uploads/pdf/LMB%2022-07-2026%20The%20Helpers.pdf">22-Jul-2026</a>
  <a href="/uploads/pdf/LMB%2015-07-2026%20The%20Helpers.pdf">15-Jul-2026</a>
  <a href="/uploads/pdf/LMB%20undated%20The%20Helpers.pdf">not a date</a>
  <a href="/some/other/page.html">Not a PDF</a>
</div>
"""


def test_find_recent_pdf_links_filters_by_date_and_keeps_undated():
    links = _find_recent_pdf_links(SAMPLE_INDEX_HTML, "https://thehelpers.pk/", since=date(2026, 7, 20))
    urls = [u for u, _ in links]
    assert any("29-07-2026" in u for u in urls)
    assert any("22-07-2026" in u for u in urls)
    assert not any("15-07-2026" in u for u in urls)  # before the `since` cutoff
    assert any("undated" in u for u in urls)  # unparseable anchor date -> kept, not silently dropped
    assert not any("other/page.html" in u for u in urls)  # not a PDF link at all


def test_find_recent_pdf_links_empty_when_nothing_in_window():
    links = _find_recent_pdf_links(SAMPLE_INDEX_HTML, "https://thehelpers.pk/", since=date(2026, 8, 1))
    urls = [u for u, _ in links]
    # Only the undated one survives - its date is unknown, so it can't be excluded by the window.
    assert all("undated" in u for u in urls)


def test_already_imported_true_for_existing_manual_bulletin(db_session, make_user):
    user, _ = make_user()
    db_session.add(LMEBulletin(
        bulletin_date=date(2026, 7, 22), file_name="x.pdf", uploaded_by=user.user_id,
        source="MANUAL",
    ))
    db_session.commit()
    assert _already_imported(db_session, date(2026, 7, 22)) is True
    assert _already_imported(db_session, date(2026, 7, 29)) is False


def test_already_imported_ignores_web_source_bulletins(db_session):
    db_session.add(LMEBulletin(
        bulletin_date=date(2026, 7, 29), file_name="x.pdf", source="WEB",
    ))
    db_session.commit()
    # A WEB-source bulletin for this date doesn't count - the crawler/matrix pipeline
    # only considers MANUAL and CRAWLER (both carry full FastMarkets symbol data).
    assert _already_imported(db_session, date(2026, 7, 29)) is False


def test_already_imported_true_for_existing_crawler_bulletin(db_session):
    db_session.add(LMEBulletin(
        bulletin_date=date(2026, 7, 29), file_name="x.pdf", source="CRAWLER",
    ))
    db_session.commit()
    # A prior crawler run already covered this date - a later run must not re-import it.
    assert _already_imported(db_session, date(2026, 7, 29)) is True


def test_pick_currency_rate_prefers_exact_date(db_session, make_user):
    user, _ = make_user()
    db_session.add(CurrencyRate(rate_date=date(2026, 7, 20), usd_rate=Decimal("278.0"),
                                eur_rate=Decimal("318.0"), source="MANUAL", created_by=user.user_id))
    db_session.add(CurrencyRate(rate_date=date(2026, 7, 29), usd_rate=Decimal("279.0"),
                                eur_rate=Decimal("319.0"), source="MANUAL", created_by=user.user_id))
    db_session.commit()
    rate = _pick_currency_rate(db_session, date(2026, 7, 29))
    assert rate.rate_date == date(2026, 7, 29)


def test_pick_currency_rate_falls_back_to_earlier_then_latest(db_session, make_user):
    user, _ = make_user()
    db_session.add(CurrencyRate(rate_date=date(2026, 7, 20), usd_rate=Decimal("278.0"),
                                eur_rate=Decimal("318.0"), source="MANUAL", created_by=user.user_id))
    db_session.commit()
    # No exact match for 07-29, but an earlier rate exists -> use it.
    rate = _pick_currency_rate(db_session, date(2026, 7, 29))
    assert rate.rate_date == date(2026, 7, 20)
    # No rate at all older than the bulletin either -> falls back to the latest available.
    rate2 = _pick_currency_rate(db_session, date(2020, 1, 1))
    assert rate2.rate_date == date(2026, 7, 20)


def test_pick_currency_rate_none_when_table_empty(db_session):
    assert _pick_currency_rate(db_session, date(2026, 7, 29)) is None


def test_crawl_skips_already_imported_bulletin(db_session, make_user, monkeypatch):
    user, _ = make_user()
    db_session.add(LMEBulletin(
        bulletin_date=date(2026, 7, 29), file_name="existing.pdf",
        uploaded_by=user.user_id, source="MANUAL",
    ))
    db_session.commit()

    # crawl_and_import_bulletins() calls db.close() in its finally block - neutralize
    # that so it doesn't tear down the shared fixture session out from under the
    # assertions this test still needs to make afterward.
    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr(
        "integrations.thehelpers.thehelpers_bulletin_crawler.SessionLocal",
        lambda: db_session,
    )
    monkeypatch.setattr(
        "integrations.thehelpers.thehelpers_bulletin_crawler._try_acquire_lock",
        lambda conn: True,
    )
    monkeypatch.setattr(
        "integrations.thehelpers.thehelpers_bulletin_crawler._release_lock",
        lambda conn: None,
    )

    class _FakeResp:
        text = SAMPLE_INDEX_HTML

        def raise_for_status(self):
            pass

    import httpx as _httpx
    monkeypatch.setattr(_httpx, "get", lambda *a, **k: _FakeResp())
    monkeypatch.setattr(
        "integrations.thehelpers.thehelpers_bulletin_crawler.extract_prices_from_pdf",
        lambda path: {"bulletin_date": "2026-07-29", "prices": [{"symbol": "x", "low": 1, "high": 1, "avg": 1}],
                      "symbols_found": ["x"]},
    )
    monkeypatch.setattr(
        "integrations.thehelpers.thehelpers_bulletin_crawler._download_with_retry",
        lambda url: b"%PDF-fake",
    )

    result = crawl_and_import_bulletins(trigger="MANUAL")
    assert result["found"] >= 1
    assert result["skipped"] >= 1
    assert result["imported"] == 0

    # Still exactly one MANUAL bulletin for that date - the existing row wasn't touched or duplicated.
    rows = db_session.query(LMEBulletin).filter(
        LMEBulletin.bulletin_date == date(2026, 7, 29), LMEBulletin.source == "MANUAL").all()
    assert len(rows) == 1
    assert rows[0].file_name == "existing.pdf"

    log_rows = db_session.query(LMEBulletinCrawlLog).filter(
        LMEBulletinCrawlLog.bulletin_date_found == date(2026, 7, 29)).all()
    assert any(r.status == "SKIPPED_ALREADY_IMPORTED" for r in log_rows)


def test_crawl_imports_new_bulletin_tagged_as_crawler_source(db_session, make_user, monkeypatch):
    user, _ = make_user()
    db_session.add(CurrencyRate(rate_date=date(2026, 7, 29), usd_rate=Decimal("278.0"),
                                eur_rate=Decimal("318.0"), source="MANUAL", created_by=user.user_id))
    db_session.commit()

    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr(
        "integrations.thehelpers.thehelpers_bulletin_crawler.SessionLocal",
        lambda: db_session,
    )
    monkeypatch.setattr(
        "integrations.thehelpers.thehelpers_bulletin_crawler._try_acquire_lock",
        lambda conn: True,
    )
    monkeypatch.setattr(
        "integrations.thehelpers.thehelpers_bulletin_crawler._release_lock",
        lambda conn: None,
    )

    class _FakeResp:
        text = SAMPLE_INDEX_HTML

        def raise_for_status(self):
            pass

    import httpx as _httpx
    monkeypatch.setattr(_httpx, "get", lambda *a, **k: _FakeResp())
    monkeypatch.setattr(
        "integrations.thehelpers.thehelpers_bulletin_crawler.extract_prices_from_pdf",
        lambda path: {"bulletin_date": "2026-07-29", "prices": [{"symbol": "MB-STE-0009", "low": 1, "high": 1, "avg": 1}],
                      "symbols_found": ["MB-STE-0009"]},
    )
    monkeypatch.setattr(
        "integrations.thehelpers.thehelpers_bulletin_crawler._download_with_retry",
        lambda url: b"%PDF-fake",
    )

    result = crawl_and_import_bulletins(trigger="CRON")
    assert result["imported"] == 1

    # No prior bulletin existed for this date - the new one must be tagged CRAWLER, not
    # MANUAL, so the UI can tell it apart from a real human upload while it still feeds
    # the same Rate Matrix query (which now includes both sources).
    rows = db_session.query(LMEBulletin).filter(LMEBulletin.bulletin_date == date(2026, 7, 29)).all()
    assert len(rows) == 1
    assert rows[0].source == "CRAWLER"
    assert rows[0].uploaded_by is None


def test_crawl_handles_index_fetch_failure_without_raising(monkeypatch, db_session):
    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr(
        "integrations.thehelpers.thehelpers_bulletin_crawler.SessionLocal",
        lambda: db_session,
    )
    monkeypatch.setattr(
        "integrations.thehelpers.thehelpers_bulletin_crawler._try_acquire_lock",
        lambda conn: True,
    )
    monkeypatch.setattr(
        "integrations.thehelpers.thehelpers_bulletin_crawler._release_lock",
        lambda conn: None,
    )

    import httpx as _httpx

    def _raise(*a, **k):
        raise _httpx.ConnectError("boom")

    monkeypatch.setattr(_httpx, "get", _raise)

    result = crawl_and_import_bulletins(trigger="CRON")
    assert result["status"] == "FAILED"

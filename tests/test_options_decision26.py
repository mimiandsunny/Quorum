"""Decision 26 — long-term-validation infra: feedback table, panel view
counter, wave_2_started_at annotation.

Storage layer is monkey-patched (Postgres-free). The tests verify:
- sentiment validation rejects unexpected values.
- failed inserts never raise (fail-open posture).
- the metric aggregators produce the Prometheus-shaped key names that
  /api/metrics scrapers expect.
"""
from __future__ import annotations

import pytest

import data.storage as storage_mod


# ─── record_option_thesis_feedback ──────────────────────


def test_record_option_thesis_feedback_rejects_bad_sentiment(caplog):
    caplog.set_level("WARNING")
    result = storage_mod.record_option_thesis_feedback(
        ticker="AAPL",
        strategy="bullish_debit_spread",
        recommendation_id="rec-1",
        sentiment="meh",
    )
    assert result is None
    assert any(
        "bad sentiment" in record.message for record in caplog.records
    )


def test_record_option_thesis_feedback_fails_open_on_db_error(monkeypatch, caplog):
    """A DB outage during feedback insert must NEVER raise — the UX
    impact of a dropped click is smaller than a 500 on a thumbs button.
    """
    def _boom(*args, **kwargs):
        raise RuntimeError("postgres exploded")

    monkeypatch.setattr(storage_mod, "get_connection", _boom)
    caplog.set_level("WARNING")
    result = storage_mod.record_option_thesis_feedback(
        ticker="AAPL", strategy="bullish_debit_spread",
        recommendation_id=None, sentiment="up",
    )
    assert result is None
    assert any("postgres exploded" in r.message for r in caplog.records)


# ─── record_cockpit_view ────────────────────────────────


def test_record_cockpit_view_skips_empty_panel():
    """Empty panel name = no-op. Avoids polluting the log with phantom
    rows when a UI bug fires with an unset panel field.
    """
    # Should not raise even if get_connection is unreachable, because we
    # short-circuit before touching it.
    storage_mod.record_cockpit_view("")


def test_record_cockpit_view_fails_open(monkeypatch, caplog):
    def _boom(*args, **kwargs):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(storage_mod, "get_connection", _boom)
    caplog.set_level("WARNING")
    # Must not raise.
    storage_mod.record_cockpit_view("screener")
    assert any("connection refused" in r.message for r in caplog.records)


# ─── metric aggregator key shapes ───────────────────────


def test_get_cockpit_view_metrics_emits_per_panel_total(monkeypatch):
    """The /api/metrics scraper expects Prometheus-shaped key names like
    `cockpit_view_total_screener`. Pinning the key shape here so a future
    refactor can't silently rename them.
    """
    fake_rows = [
        {"panel": "screener", "n": 12},
        {"panel": "thesis", "n": 4},
    ]
    monkeypatch.setattr(
        storage_mod, "get_connection",
        lambda: _ConnStub(fetchall_result=fake_rows),
    )
    counters = storage_mod.get_cockpit_view_metrics()
    assert counters == {
        "cockpit_view_total_screener": 12,
        "cockpit_view_total_thesis": 4,
    }


def test_get_option_thesis_feedback_metrics_lifetime_plus_breakdown(monkeypatch):
    """Lifetime up/down totals + per-strategy breakdown. The lifetime
    key always exists (zero when empty) so scrapers don't have to
    conditionally check for the key.
    """
    fake_rows = [
        {"strategy": "bullish_debit_spread", "sentiment": "up", "n": 3},
        {"strategy": "bullish_debit_spread", "sentiment": "down", "n": 1},
        {"strategy": "neutral_iron_condor", "sentiment": "up", "n": 2},
    ]
    monkeypatch.setattr(
        storage_mod, "get_connection",
        lambda: _ConnStub(fetchall_result=fake_rows),
    )
    counters = storage_mod.get_option_thesis_feedback_metrics()
    # Lifetime totals sum across strategies.
    assert counters["option_thesis_feedback_up_total"] == 5
    assert counters["option_thesis_feedback_down_total"] == 1
    # Per-strategy keys exist.
    assert counters["option_thesis_feedback_bullish_debit_spread_up_total"] == 3
    assert counters["option_thesis_feedback_bullish_debit_spread_down_total"] == 1
    assert counters["option_thesis_feedback_neutral_iron_condor_up_total"] == 2


def test_get_option_thesis_feedback_metrics_zero_baseline(monkeypatch):
    """Empty table: lifetime keys exist with value 0 (scraper contract)."""
    monkeypatch.setattr(
        storage_mod, "get_connection",
        lambda: _ConnStub(fetchall_result=[]),
    )
    counters = storage_mod.get_option_thesis_feedback_metrics()
    assert counters == {
        "option_thesis_feedback_up_total": 0,
        "option_thesis_feedback_down_total": 0,
    }


# ─── deploy annotation idempotency ──────────────────────


def test_upsert_deploy_annotation_uses_on_conflict_do_nothing(monkeypatch):
    """Idempotent insert preserves the original timestamp. The SQL must
    use ON CONFLICT DO NOTHING — using DO UPDATE would reset the
    `recorded_at` timestamp and break /retro's wave-boundary filter.
    """
    executed_sql: list[str] = []
    monkeypatch.setattr(
        storage_mod, "get_connection",
        lambda: _ConnStub(record_sql=executed_sql),
    )
    storage_mod.upsert_deploy_annotation("wave_2_started_at", "2026-05-17T10:00:00")
    assert len(executed_sql) == 1
    assert "ON CONFLICT (key) DO NOTHING" in executed_sql[0]


# ─── Stub helpers ───────────────────────────────────────


class _CursorStub:
    def __init__(self, fetchall_result=None, fetchone_result=None, record_sql=None):
        self._fetchall = fetchall_result or []
        self._fetchone = fetchone_result
        self._record_sql = record_sql

    def fetchall(self):
        return self._fetchall

    def fetchone(self):
        return self._fetchone


class _ConnStub:
    """Minimal psycopg-shaped connection stub for unit tests. Captures the
    SQL string if `record_sql` is provided so tests can assert on shape.
    """

    def __init__(self, fetchall_result=None, fetchone_result=None, record_sql=None):
        self._fetchall = fetchall_result or []
        self._fetchone = fetchone_result
        self._record_sql = record_sql

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        if self._record_sql is not None:
            self._record_sql.append(sql)
        return _CursorStub(
            fetchall_result=self._fetchall,
            fetchone_result=self._fetchone,
        )

    def commit(self):
        pass

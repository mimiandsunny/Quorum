"""Tests for D3 — IV-rank screener (Wave 2 plan rev 4).

Storage layer is monkey-patched so the suite stays Postgres-free.
The screener's contract: cold-start tickers always come back as a
separate bucket, and `ranked` is sorted iv_rank_30d desc.
"""
from __future__ import annotations

from datetime import datetime

from data.models import OptionIVHistory, OptionIVLabel
from options.screener import build_iv_screener


def _iv(
    ticker: str,
    *,
    rank: float | None = None,
    label: OptionIVLabel | None = OptionIVLabel.FAIR,
    captured_at: datetime = datetime(2026, 5, 15, 18, 0),
) -> OptionIVHistory:
    return OptionIVHistory(
        ticker=ticker,
        captured_at=captured_at,
        underlying_price=100.0,
        atm_iv_30d=0.30,
        iv_rank_30d=rank,
        iv_label=label,
    )


def test_build_iv_screener_sorts_ranked_descending(monkeypatch):
    rows = [
        _iv("AAPL", rank=0.42, label=OptionIVLabel.FAIR),
        _iv("TSLA", rank=0.92, label=OptionIVLabel.RICH),
        _iv("MSFT", rank=0.15, label=OptionIVLabel.CHEAP),
    ]
    import data.storage as storage_mod

    monkeypatch.setattr(storage_mod, "get_iv_screener_rows", lambda **kw: list(rows))
    monkeypatch.setattr(storage_mod, "get_iv_cold_start_tickers", lambda: [])

    result = build_iv_screener()
    assert [r.ticker for r in result.ranked] == ["TSLA", "AAPL", "MSFT"]
    assert result.cold_start == []
    assert result.total == 3


def test_build_iv_screener_separates_cold_start(monkeypatch):
    import data.storage as storage_mod

    monkeypatch.setattr(
        storage_mod, "get_iv_screener_rows",
        lambda **kw: [_iv("AAPL", rank=0.7, label=OptionIVLabel.RICH)],
    )
    monkeypatch.setattr(
        storage_mod, "get_iv_cold_start_tickers", lambda: ["SPCE", "RIVN"]
    )

    result = build_iv_screener()
    assert [r.ticker for r in result.ranked] == ["AAPL"]
    # Cold-start bucket is alphabetized for DR8 wireframe.
    assert result.cold_start == ["RIVN", "SPCE"]
    assert result.total == 3


def test_build_iv_screener_filters_cold_start_by_tickers(monkeypatch):
    """When the caller passes a ticker filter, the cold-start bucket
    must respect it too (otherwise the cockpit shows excluded universe
    tickers in the 'building history' subsection for an unrelated ticker
    filter)."""
    import data.storage as storage_mod

    monkeypatch.setattr(storage_mod, "get_iv_screener_rows", lambda **kw: [])
    monkeypatch.setattr(
        storage_mod, "get_iv_cold_start_tickers",
        lambda: ["SPCE", "RIVN", "PLTR"],
    )

    result = build_iv_screener(tickers=["RIVN", "AAPL"])
    assert result.ranked == []
    assert result.cold_start == ["RIVN"]


def test_build_iv_screener_pushes_none_rank_to_bottom(monkeypatch):
    """Defensive: if a row sneaks through with iv_rank_30d=None but a
    non-'insufficient' label, the comparator must not crash and must
    push it below the ranked rows."""
    rows = [
        _iv("AAPL", rank=None, label=OptionIVLabel.FAIR),
        _iv("TSLA", rank=0.9, label=OptionIVLabel.RICH),
    ]
    import data.storage as storage_mod

    monkeypatch.setattr(storage_mod, "get_iv_screener_rows", lambda **kw: list(rows))
    monkeypatch.setattr(storage_mod, "get_iv_cold_start_tickers", lambda: [])

    result = build_iv_screener()
    assert [r.ticker for r in result.ranked] == ["TSLA", "AAPL"]

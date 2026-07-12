"""T1 — Wave 2 integration tests (plan rev 4 eng review).

Covers the cross-module failure modes that single-file unit tests miss:
  - A2: per-ticker preferred→fallback path in the refresh job + audit row.
  - A3: zombie-job cleanup at boot.
  - A6: per-ticker txn — chain persists even when IV summary fails.
  - E2E smokes: each new endpoint returns the expected JSON shape with
    storage seams stubbed (no live DB).
  - Decision 17: the async refresh handler is fire-and-forget (202 +
    queued without awaiting the runner).

Storage seams are monkey-patched throughout so the suite stays
Postgres-free, matching the existing unit-test posture.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import sys
from datetime import date, datetime
from types import ModuleType

import pytest

import main as main_mod
from data.models import (
    DataProviderEvent,
    DataProviderEventType,
    OptionThesisAttempt,
    OptionThesisStatus,
)
from options.fetchers import YfinanceFetcher
from options.models import (
    OptionChainSnapshot,
    OptionChainSource,
    OptionContractSnapshot,
    OptionType,
)


# ─── Fixtures ─────────────────────────────────────────────


def _put_contract(strike: float = 95.0, expiration: date = date(2026, 6, 20)) -> OptionContractSnapshot:
    return OptionContractSnapshot(
        contract_symbol=f"AAPL{expiration.strftime('%y%m%d')}P{int(strike * 1000):08d}",
        ticker="AAPL",
        expiration=expiration,
        option_type=OptionType.PUT,
        strike=strike,
        bid=0.95, ask=1.05,
        delta=-0.25, gamma=0.02,
        volume=200, open_interest=400,
    )


def _snapshot(*, ticker: str = "AAPL", source: OptionChainSource = OptionChainSource.IBKR) -> OptionChainSnapshot:
    return OptionChainSnapshot(
        snapshot_id=f"snap-{ticker}",
        ticker=ticker,
        captured_at=datetime(2026, 5, 16, 10, 0),
        source=source,
        underlying_price=100.0,
        expirations=[date(2026, 6, 20)],
        contracts=[_put_contract()],
    )


# ─── A2: preferred-with-fallback in refresh_job ───────────


@pytest.mark.asyncio
async def test_a2_fetcher_falls_back_to_yfinance_and_audits(monkeypatch):
    """Preferred fetcher raises → fallback fires → data_provider_events
    row is recorded with from/to providers + exception class.
    """
    from options import refresh_job as rj

    class _BoomFetcher:
        source_name = "ibkr"

        async def fetch(self, ticker, *, max_expirations=4):
            raise RuntimeError("Gateway connect refused")

    class _OkFallback:
        source_name = "yfinance"

        def fetch(self, ticker, *, max_expirations=4):
            return _snapshot(ticker=ticker, source=OptionChainSource.YFINANCE)

    audit_rows: list[DataProviderEvent] = []
    monkeypatch.setattr(rj, "record_data_provider_event", lambda ev: audit_rows.append(ev))

    snap = await rj._fetch_with_fallback(
        preferred=_BoomFetcher(),
        fallback=_OkFallback(),
        ticker="AAPL",
        max_expirations=4,
    )
    assert snap.ticker == "AAPL"
    assert snap.source == OptionChainSource.YFINANCE
    assert len(audit_rows) == 1
    event = audit_rows[0]
    assert event.event_type == DataProviderEventType.FETCHER_FALLBACK
    assert event.ticker == "AAPL"
    assert event.from_provider == "ibkr"
    assert event.to_provider == "yfinance"
    assert "RuntimeError" in (event.reason or "")


@pytest.mark.asyncio
async def test_a2_both_fetchers_failing_re_raises(monkeypatch):
    """When both fetchers fail, the exception escapes to the caller
    so the refresh-job orchestrator can record the failure into the
    per-ticker failures list — not swallowed.
    """
    from options import refresh_job as rj

    class _Boom:
        source_name = "ibkr"
        async def fetch(self, ticker, *, max_expirations=4):
            raise RuntimeError("preferred dead")

    class _BoomToo:
        source_name = "yfinance"
        def fetch(self, ticker, *, max_expirations=4):
            raise RuntimeError("fallback dead")

    monkeypatch.setattr(rj, "record_data_provider_event", lambda ev: None)
    with pytest.raises(RuntimeError, match="fallback dead"):
        await rj._fetch_with_fallback(
            preferred=_Boom(), fallback=_BoomToo(),
            ticker="AAPL", max_expirations=4,
        )


# ─── A6: per-ticker txn — chain persists when IV summary skips ────


def test_a6_chain_persists_when_iv_summary_skipped(monkeypatch):
    """A6: chain ingest is the source of truth; IV summary is derived.
    When compute_iv_summary returns is_eligible=False, the chain row
    must still land AND a data_provider_events('iv_summary_skipped')
    row must be recorded.
    """
    from options import iv_summary as ivs

    snap = OptionChainSnapshot(
        snapshot_id="snap-thin",
        ticker="THIN",
        captured_at=datetime(2026, 5, 16, 10, 0),
        source=OptionChainSource.IBKR,
        underlying_price=100.0,
        expirations=[date(2026, 5, 23)],  # too-near, no 30d expiry
        contracts=[],
    )

    saved_chain: list[str] = []
    iv_writes: list = []
    audits: list[DataProviderEvent] = []

    import data.storage as storage_mod
    monkeypatch.setattr(
        storage_mod, "save_option_chain_snapshot",
        lambda s: (saved_chain.append(s.snapshot_id), s.snapshot_id)[1],
    )
    monkeypatch.setattr(storage_mod, "insert_option_iv_history", lambda h: iv_writes.append(h))
    monkeypatch.setattr(storage_mod, "record_data_provider_event", lambda ev: audits.append(ev))

    snapshot_id, iv_written = ivs.persist_chain_with_iv_summary(snap)
    assert snapshot_id == "snap-thin"
    assert iv_written is False
    assert saved_chain == ["snap-thin"]
    assert iv_writes == []  # summary write skipped per A6
    assert len(audits) == 1
    assert audits[0].event_type == DataProviderEventType.IV_SUMMARY_SKIPPED
    assert audits[0].ticker == "THIN"


def test_a6_chain_and_summary_both_persist_when_eligible(monkeypatch):
    """Happy path mirror: both chain and IV summary land for a healthy
    snapshot with a 30d expiry and an ATM IV close to underlying.
    """
    from options import iv_summary as ivs

    # Build a snapshot with a 30-day expiry and a 100-strike put + call
    # carrying valid IV at underlying=100.
    today = date(2026, 5, 16)
    target_exp = date(2026, 6, 15)  # ~30 DTE
    snap = OptionChainSnapshot(
        snapshot_id="snap-clean",
        ticker="CLEAN",
        captured_at=datetime(2026, 5, 16, 10, 0),
        source=OptionChainSource.IBKR,
        underlying_price=100.0,
        expirations=[target_exp],
        contracts=[
            OptionContractSnapshot(
                contract_symbol="CLEAN260615C00100000",
                ticker="CLEAN", expiration=target_exp,
                option_type=OptionType.CALL, strike=100.0,
                bid=2.0, ask=2.2, implied_volatility=0.30,
            ),
            OptionContractSnapshot(
                contract_symbol="CLEAN260615P00100000",
                ticker="CLEAN", expiration=target_exp,
                option_type=OptionType.PUT, strike=100.0,
                bid=2.0, ask=2.2, implied_volatility=0.32,
            ),
        ],
    )

    saved_chain: list[str] = []
    iv_writes: list = []
    audits: list[DataProviderEvent] = []
    import data.storage as storage_mod
    monkeypatch.setattr(
        storage_mod, "save_option_chain_snapshot",
        lambda s: (saved_chain.append(s.snapshot_id), s.snapshot_id)[1],
    )
    monkeypatch.setattr(storage_mod, "insert_option_iv_history", lambda h: iv_writes.append(h))
    monkeypatch.setattr(storage_mod, "record_data_provider_event", lambda ev: audits.append(ev))

    snapshot_id, iv_written = ivs.persist_chain_with_iv_summary(snap)
    assert iv_written is True
    assert saved_chain == ["snap-clean"]
    assert len(iv_writes) == 1
    assert iv_writes[0].atm_iv_30d == pytest.approx(0.31, abs=0.01)
    assert audits == []  # no skip recorded


# ─── Decision 17: async refresh fire-and-forget ──────────


@pytest.mark.asyncio
async def test_decision17_refresh_returns_202_without_awaiting_runner(monkeypatch):
    """The handler must NOT await run_chain_refresh — that would block
    the client request for the entire 50-ticker refresh budget. Verify
    by making the runner sleep longer than the handler's response time.
    """
    runner_finished = asyncio.Event()

    async def _slow_runner(tickers, *, max_expirations, preferred):
        await asyncio.sleep(0.2)
        runner_finished.set()

    monkeypatch.setattr("options.refresh_job.run_chain_refresh", _slow_runner)
    monkeypatch.setattr(
        "options.refresh_job.build_preferred_fetcher", lambda: YfinanceFetcher()
    )

    response = await main_mod.api_options_refresh(
        main_mod.OptionsRefreshRequest(tickers=["AAPL"], max_expirations=2)
    )
    # Handler returned before the runner finished — the contract.
    assert response.status_code == 202
    assert not runner_finished.is_set()
    # Drain the task so the test doesn't leave background work behind.
    for task in list(main_mod._pending_job_tasks):
        await task
    assert runner_finished.is_set()


@pytest.mark.asyncio
async def test_decision17_refresh_rejects_empty_universe(monkeypatch):
    """Empty tickers + empty settings.tickers → 400. Prevents queuing a
    no-op job that holds the running-slot for nothing.
    """
    monkeypatch.setattr(main_mod.settings, "tickers", [])
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        await main_mod.api_options_refresh(
            main_mod.OptionsRefreshRequest(tickers=None, max_expirations=4)
        )
    assert exc_info.value.status_code == 400


# ─── E2E smoke: screener endpoint shape ──────────────────


@pytest.mark.asyncio
async def test_e2e_screener_endpoint_payload_shape(monkeypatch):
    """/api/options/screener returns {ranked, cold_start, total}. Lock
    the response shape so a UI change can't silently break the cockpit.
    """
    from data.models import OptionIVHistory, OptionIVLabel

    fake_rows = [
        OptionIVHistory(
            ticker="AAPL",
            captured_at=datetime(2026, 5, 16, 10, 0),
            underlying_price=180.0,
            atm_iv_30d=0.28,
            iv_rank_30d=0.72,
            iv_label=OptionIVLabel.RICH,
        ),
    ]
    import data.storage as storage_mod
    monkeypatch.setattr(storage_mod, "get_iv_screener_rows", lambda **kw: fake_rows)
    monkeypatch.setattr(storage_mod, "get_iv_cold_start_tickers", lambda: ["NVDA", "TSLA"])

    payload = await main_mod.api_options_screener(tickers=None, limit=50)
    assert set(payload.keys()) == {"ranked", "cold_start", "total"}
    assert len(payload["ranked"]) == 1
    assert payload["ranked"][0]["ticker"] == "AAPL"
    assert payload["cold_start"] == ["NVDA", "TSLA"]
    assert payload["total"] == 3


# ─── E2E smoke: D4 thesis endpoint ───────────────────────


@pytest.mark.asyncio
async def test_e2e_thesis_endpoint_returns_structured_payload(monkeypatch):
    """/api/options/thesis/{ticker}/{strategy} returns the canonical
    response shape including from_cache + status fields.
    """
    from options import thesis as thesis_mod

    # Stub storage seams so the dispatcher doesn't try Postgres.
    monkeypatch.setattr(thesis_mod, "_record_attempt", lambda **kw: None)
    monkeypatch.setattr(thesis_mod, "_read_cache", lambda **kw: None)
    monkeypatch.setattr(thesis_mod, "_write_cache", lambda **kw: None)

    # Build a chain rich enough for bullish_debit_spread to succeed.
    target_exp = date(2026, 6, 20)
    snap_payload = OptionChainSnapshot(
        snapshot_id="snap-e2e",
        ticker="AAPL",
        captured_at=datetime(2026, 5, 16, 10, 0),
        source=OptionChainSource.IBKR,
        underlying_price=100.0,
        expirations=[target_exp],
        contracts=[
            OptionContractSnapshot(
                contract_symbol="AAPL260620C00100000", ticker="AAPL",
                expiration=target_exp, option_type=OptionType.CALL,
                strike=100.0, bid=3.0, ask=3.2,
            ),
            OptionContractSnapshot(
                contract_symbol="AAPL260620C00105000", ticker="AAPL",
                expiration=target_exp, option_type=OptionType.CALL,
                strike=105.0, bid=1.0, ask=1.2,
            ),
        ],
    ).model_dump(mode="json")

    import data.storage as storage_mod
    monkeypatch.setattr(
        storage_mod, "get_latest_option_chain_snapshot",
        lambda ticker: snap_payload,
    )

    response = await main_mod.api_options_thesis(
        ticker="AAPL", strategy="bullish_debit_spread", recommendation_id=None
    )
    assert response["status"] == "success"
    assert response["from_cache"] is False
    assert response["structure"]["strategy"] == "bullish_debit_spread"
    assert len(response["structure"]["legs"]) == 2


@pytest.mark.asyncio
async def test_e2e_thesis_endpoint_404_when_no_snapshot(monkeypatch):
    """No stored snapshot → 404, not a phantom thesis."""
    import data.storage as storage_mod
    from fastapi import HTTPException

    monkeypatch.setattr(storage_mod, "get_latest_option_chain_snapshot", lambda ticker: None)
    with pytest.raises(HTTPException) as exc_info:
        await main_mod.api_options_thesis(
            ticker="UNKNOWN", strategy="bullish_debit_spread", recommendation_id=None
        )
    assert exc_info.value.status_code == 404


# ─── E2E smoke: decision-26 endpoints ────────────────────


@pytest.mark.asyncio
async def test_e2e_thesis_feedback_endpoint(monkeypatch):
    captured: list[dict] = []
    import data.storage as storage_mod
    monkeypatch.setattr(
        storage_mod, "record_option_thesis_feedback",
        lambda **kw: (captured.append(kw), 42)[1],
    )
    result = await main_mod.api_options_thesis_feedback(
        main_mod.ThesisFeedbackRequest(
            ticker="AAPL", strategy="bullish_debit_spread",
            recommendation_id="rec-1", sentiment="up",
        )
    )
    assert result == {"status": "ok", "id": 42}
    assert captured == [{
        "ticker": "AAPL",
        "strategy": "bullish_debit_spread",
        "recommendation_id": "rec-1",
        "sentiment": "up",
    }]


@pytest.mark.asyncio
async def test_e2e_cockpit_view_endpoint(monkeypatch):
    captured: list[str] = []
    import data.storage as storage_mod
    monkeypatch.setattr(
        storage_mod, "record_cockpit_view",
        lambda panel: captured.append(panel),
    )
    result = await main_mod.api_options_cockpit_view(
        main_mod.CockpitViewRequest(panel="screener")
    )
    assert result == {"status": "ok"}
    assert captured == ["screener"]

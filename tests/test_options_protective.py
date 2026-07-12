"""Tests for D2 — protective-put compute (Wave 2 plan rev 4).

The compute path is pure given a snapshot + position dict, so all tests
work on synthetic in-memory chains. The `refresh_protective_costs`
orchestrator is exercised via dependency injection over the storage
imports — keeps the suite Postgres-free.
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from data.models import OptionGreeksSource
from options import protective
from options.models import (
    OptionChainSnapshot,
    OptionChainSource,
    OptionContractSnapshot,
    OptionType,
)
from options.protective import (
    _MAX_DTE,
    _MIN_DTE,
    _select_protective_put,
    compute_protective_cost,
    refresh_protective_costs,
)


def _put(
    symbol: str,
    strike: float,
    *,
    bid: float = 0.95,
    ask: float = 1.05,
    delta: float | None = -0.25,
    gamma: float | None = 0.02,
    volume: int = 200,
    oi: int = 400,
    expiration: date = date(2026, 6, 15),
) -> OptionContractSnapshot:
    return OptionContractSnapshot(
        contract_symbol=symbol,
        ticker="AAPL",
        expiration=expiration,
        option_type=OptionType.PUT,
        strike=strike,
        bid=bid,
        ask=ask,
        delta=delta,
        gamma=gamma,
        volume=volume,
        open_interest=oi,
    )


def _snapshot(
    *,
    contracts: list[OptionContractSnapshot] | None = None,
    captured_at: datetime = datetime(2026, 5, 15, 10, 0),
    underlying: float = 100.0,
    source: OptionChainSource = OptionChainSource.IBKR,
) -> OptionChainSnapshot:
    return OptionChainSnapshot(
        snapshot_id="snap-d2-1",
        ticker="AAPL",
        captured_at=captured_at,
        source=source,
        underlying_price=underlying,
        expirations=[date(2026, 6, 15)],
        contracts=contracts or [_put("AAPL260615P00095000", 95.0)],
    )


def _position(*, paper_trade_id: int = 1, qty: float = 100.0, avg_entry: float = 100.0) -> dict:
    return {
        "paper_trade_id": paper_trade_id,
        "ticker": "AAPL",
        "qty": qty,
        "avg_entry": avg_entry,
        "strategy": "balanced",
    }


# ─── _select_protective_put ───────────────────────────────


def test_select_protective_put_picks_target_delta_within_window():
    contracts = [
        _put("P-095", 95.0, delta=-0.20, bid=0.80, ask=0.90),
        _put("P-093", 93.0, delta=-0.25, bid=0.55, ask=0.65),  # target match
        _put("P-090", 90.0, delta=-0.40, bid=0.30, ask=0.40),
    ]
    chosen = _select_protective_put(_snapshot(contracts=contracts))
    assert chosen is not None
    assert chosen.contract.contract_symbol == "P-093"
    assert chosen.delta == -0.25


def test_select_protective_put_breaks_delta_ties_on_volume():
    contracts = [
        _put("P-thin", 95.0, delta=-0.25, volume=10, bid=0.50, ask=0.60),
        _put("P-deep", 95.0, delta=-0.25, volume=2000, bid=0.50, ask=0.60),
    ]
    chosen = _select_protective_put(_snapshot(contracts=contracts))
    assert chosen is not None
    assert chosen.contract.contract_symbol == "P-deep"


def test_select_protective_put_excludes_dte_outside_window():
    too_short = _put(
        "P-near", 95.0, delta=-0.25, expiration=date(2026, 5, 16)  # 1 DTE
    )
    too_long = _put(
        "P-far", 95.0, delta=-0.25, expiration=date(2026, 12, 15)  # ~210 DTE
    )
    contracts = [too_short, too_long]
    assert _select_protective_put(_snapshot(contracts=contracts)) is None


def test_select_protective_put_excludes_delta_outside_tolerance():
    far_otm = _put("P-far-otm", 70.0, delta=-0.03)  # 22pts away from target
    contracts = [far_otm]
    assert _select_protective_put(_snapshot(contracts=contracts)) is None


def test_select_protective_put_handles_missing_greeks_via_moneyness():
    """yfinance path: delta=None; moneyness-based pick keeps D2 alive during
    the IBKR-waiting window per OV4 + the local_bs fallback design."""
    contracts = [
        # 5% OTM put — in the 3-10% band
        _put("P-95", 95.0, delta=None, gamma=None, bid=0.80, ask=0.90),
        # 15% OTM — outside the band
        _put("P-85", 85.0, delta=None, gamma=None, bid=0.20, ask=0.25),
    ]
    chosen = _select_protective_put(_snapshot(contracts=contracts))
    assert chosen is not None
    assert chosen.contract.contract_symbol == "P-95"
    assert chosen.delta is None


def test_select_protective_put_returns_none_when_no_puts():
    call = OptionContractSnapshot(
        contract_symbol="C-100",
        ticker="AAPL",
        expiration=date(2026, 6, 15),
        option_type=OptionType.CALL,
        strike=100.0,
        bid=1.0,
        ask=1.1,
        delta=0.50,
    )
    assert _select_protective_put(_snapshot(contracts=[call])) is None


def test_select_protective_put_dte_window_boundaries():
    from datetime import timedelta

    as_of = date(2026, 5, 15)
    at_floor = _put("P-floor", 95.0, expiration=as_of + timedelta(days=_MIN_DTE))
    at_ceiling = _put("P-ceil", 95.0, expiration=as_of + timedelta(days=_MAX_DTE))
    snapshot = _snapshot(contracts=[at_floor, at_ceiling])
    chosen = _select_protective_put(snapshot, as_of=as_of)
    assert chosen is not None
    # Tied delta + volume; tie-break on smaller DTE.
    assert chosen.contract.contract_symbol == "P-floor"


# ─── compute_protective_cost ──────────────────────────────


def test_compute_protective_cost_happy_path():
    snapshot = _snapshot(
        contracts=[_put("P-095", 95.0, bid=0.95, ask=1.05, delta=-0.25)],
    )
    cost = compute_protective_cost(_position(qty=100, avg_entry=100.0), snapshot)
    assert cost is not None
    assert cost.position_id == 1
    assert cost.contract_symbol == "P-095"
    assert cost.cost_per_share == 1.0  # mid of (0.95, 1.05)
    assert cost.cost_pct_of_position == 0.01  # 1.00 / 100.00
    assert cost.delta == -0.25
    # IBKR source + delta present → PROVIDER.
    assert cost.greeks_source == OptionGreeksSource.PROVIDER


def test_compute_protective_cost_yfinance_source_no_greeks():
    contracts = [_put("P-095", 95.0, bid=0.95, ask=1.05, delta=None, gamma=None)]
    snapshot = _snapshot(contracts=contracts, source=OptionChainSource.YFINANCE)
    cost = compute_protective_cost(_position(), snapshot)
    assert cost is not None
    assert cost.greeks_source == OptionGreeksSource.NONE
    assert cost.delta is None


def test_compute_protective_cost_local_bs_when_yf_with_derived_greeks():
    """A yfinance snapshot carrying derived Greeks (the local_bs path) gets
    `LOCAL_BS` tagged, not `PROVIDER` — keeps the audit trail honest."""
    contracts = [_put("P-095", 95.0, bid=0.95, ask=1.05, delta=-0.25, gamma=0.02)]
    snapshot = _snapshot(contracts=contracts, source=OptionChainSource.YFINANCE)
    cost = compute_protective_cost(_position(), snapshot)
    assert cost is not None
    assert cost.greeks_source == OptionGreeksSource.LOCAL_BS


def test_compute_protective_cost_provider_nan_when_partial_greeks():
    """IBKR row has gamma but the specific delta we need is None → PROVIDER_NAN
    so the UI knows to badge the row 'estimated' (per A2 / OV4)."""
    contracts = [_put("P-095", 95.0, bid=0.95, ask=1.05, delta=None, gamma=0.02)]
    # Fits the 5% OTM moneyness fallback so candidate is non-None.
    snapshot = _snapshot(contracts=contracts, source=OptionChainSource.IBKR)
    cost = compute_protective_cost(_position(), snapshot)
    assert cost is not None
    assert cost.greeks_source == OptionGreeksSource.PROVIDER_NAN


def test_compute_protective_cost_returns_none_for_short_position():
    snapshot = _snapshot()
    cost = compute_protective_cost(_position(qty=-100), snapshot)
    assert cost is None


def test_compute_protective_cost_returns_none_for_zero_avg_entry():
    snapshot = _snapshot()
    cost = compute_protective_cost(_position(avg_entry=0.0), snapshot)
    assert cost is None


def test_compute_protective_cost_returns_none_when_no_eligible_put():
    # Only a far-OTM put outside delta tolerance.
    contracts = [_put("P-70", 70.0, delta=-0.05)]
    snapshot = _snapshot(contracts=contracts)
    cost = compute_protective_cost(_position(), snapshot)
    assert cost is None


# ─── refresh_protective_costs orchestrator ────────────────


def test_refresh_protective_costs_counts_outcomes(monkeypatch):
    snapshot_payload_aapl = _snapshot()
    snapshot_payload_msft = _snapshot(
        contracts=[_put("P-far", 70.0, delta=-0.05)]
    ).model_copy(update={"ticker": "MSFT"})
    # Monkey-patch the storage module's symbols. refresh_protective_costs
    # imports inside the function so the patch lands at call time.
    positions = [
        {"paper_trade_id": 1, "ticker": "AAPL", "qty": 100, "avg_entry": 100.0},
        {"paper_trade_id": 2, "ticker": "MSFT", "qty": 50, "avg_entry": 200.0},
        {"paper_trade_id": 3, "ticker": "TSLA", "qty": 10, "avg_entry": 300.0},
    ]
    chain_rows = {
        "AAPL": snapshot_payload_aapl.model_dump(mode="json"),
        "MSFT": snapshot_payload_msft.model_dump(mode="json"),
        # TSLA missing → skipped_no_chain
    }
    upserted: list = []

    import data.storage as storage_mod

    monkeypatch.setattr(storage_mod, "get_open_paper_positions", lambda: positions)
    monkeypatch.setattr(
        storage_mod,
        "get_latest_option_chain_snapshot",
        lambda ticker: chain_rows.get(ticker.upper()),
    )
    monkeypatch.setattr(
        storage_mod,
        "upsert_option_protective_cost",
        lambda cost: upserted.append(cost),
    )

    summary = refresh_protective_costs()

    assert summary == {
        "computed": 1,         # AAPL
        "skipped_no_chain": 1, # TSLA
        "skipped_no_put": 1,   # MSFT (delta outside tolerance)
    }
    assert len(upserted) == 1
    assert upserted[0].position_id == 1


def test_refresh_protective_costs_caches_snapshot_per_ticker(monkeypatch):
    """Three positions sharing AAPL must produce one chain read, not three."""
    snapshot_payload = _snapshot().model_dump(mode="json")
    positions = [
        {"paper_trade_id": i, "ticker": "AAPL", "qty": 100, "avg_entry": 100.0}
        for i in range(1, 4)
    ]
    read_count = {"n": 0}

    def _fake_snap_read(ticker: str):
        read_count["n"] += 1
        return snapshot_payload

    import data.storage as storage_mod

    monkeypatch.setattr(storage_mod, "get_open_paper_positions", lambda: positions)
    monkeypatch.setattr(storage_mod, "get_latest_option_chain_snapshot", _fake_snap_read)
    monkeypatch.setattr(storage_mod, "upsert_option_protective_cost", lambda cost: None)

    refresh_protective_costs()
    assert read_count["n"] == 1

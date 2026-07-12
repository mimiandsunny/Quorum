"""Regression tests for the Wave 2 cleanup PR (K1-K15 + D8/D9/D10).

Each test names the issue it guards against. Plan rev 4 IRON RULE: any
regression of these fixes must fail the test suite — they do not require
AskUserQuestion to add to the plan.
"""

from datetime import date, datetime

import pytest

from options.chain import _YFINANCE_INTER_CALL_SLEEP_S
from options.features import (
    LiquidityThresholds,
    _flow_score,
    _iv_percentiles,
    _liquidity_score,
    compute_tags,
    moneyness_pct,
    option_mid,
    option_spread_pct,
    position_gamma_dollars,
    rank_options_snapshot,
)
from options.flow import scan_unusual_flow
from options.models import (
    OptionChainSnapshot,
    OptionChainSource,
    OptionContractSnapshot,
    OptionRank,
    OptionsDashboard,
    OptionsSnapshotSummary,
    OptionType,
)
from options.service import OptionChainSnapshotRow, _snapshot_from_row


def _contract(
    symbol: str,
    option_type: OptionType,
    strike: float,
    *,
    bid: float | None = 1.0,
    ask: float | None = 1.1,
    iv: float | None = 0.30,
    volume: int | None = 100,
    oi: int | None = 200,
    gamma: float | None = 0.02,
    expiration: date = date(2026, 5, 15),
) -> OptionContractSnapshot:
    return OptionContractSnapshot(
        contract_symbol=symbol,
        ticker="NVDA",
        expiration=expiration,
        option_type=option_type,
        strike=strike,
        bid=bid,
        ask=ask,
        implied_volatility=iv,
        volume=volume,
        open_interest=oi,
        gamma=gamma,
    )


def _snapshot(contracts: list[OptionContractSnapshot]) -> OptionChainSnapshot:
    return OptionChainSnapshot(
        snapshot_id="opt-cleanup-1",
        ticker="NVDA",
        captured_at=datetime(2026, 4, 29, 10, 0),
        source=OptionChainSource.TEST,
        underlying_price=100.0,
        expirations=[date(2026, 5, 15)],
        contracts=contracts,
    )


# ── K1: position_gamma_dollars rename ────────────────────────────────────


def test_k1_position_gamma_dollars_replaces_gamma_exposure():
    """OptionRank exposes position_gamma_dollars, never gamma_exposure (K1)."""
    fields = OptionRank.model_fields
    assert "position_gamma_dollars" in fields
    assert "gamma_exposure" not in fields


def test_k1_position_gamma_dollars_compute_matches_long_position():
    """Long-position gamma is positive for calls, negative for puts. Value
    follows the formula gamma * oi * 100 * S * S * 0.01 (dollar gamma per 1%
    move, not dealer GEX — see TODOS.md TD-1)."""
    call = _contract("NVDA260515C00100000", OptionType.CALL, 100, gamma=0.02, oi=500)
    put = _contract("NVDA260515P00100000", OptionType.PUT, 100, gamma=0.02, oi=500)
    # 0.02 * 500 * 100 * 100 * 100 * 0.01 = 100_000
    assert position_gamma_dollars(call, 100.0) == pytest.approx(100_000.0, rel=1e-3)
    assert position_gamma_dollars(put, 100.0) == pytest.approx(-100_000.0, rel=1e-3)


# ── K2 + K10: bid=0 with ask>0 is a real OTM market ──────────────────────


def test_k2_option_mid_accepts_bid_zero():
    """OTM contract with bid=0, ask=0.05 has a real mid — not a stale last_price."""
    otm = _contract("NVDA260515C00200000", OptionType.CALL, 200, bid=0.0, ask=0.05, iv=0.50)
    assert option_mid(otm) == 0.025


def test_k10_option_spread_pct_accepts_bid_zero():
    """Spread is computable when bid=0, ask>0 (was None pre-K10)."""
    otm = _contract("NVDA260515C00200000", OptionType.CALL, 200, bid=0.0, ask=0.05, iv=0.50)
    spread = option_spread_pct(otm)
    assert spread is not None
    assert spread == pytest.approx(2.0, rel=1e-3)  # 0.05 / 0.025


def test_k2_option_mid_rejects_negative_bid():
    """Negative bid is not a real market and must not return a mid."""
    contract = _contract("NVDA260515C00200000", OptionType.CALL, 200, bid=-0.05, ask=0.05)
    assert option_mid(contract) is None


# ── K4: average-rank tie handling ────────────────────────────────────────


def test_k4_iv_percentile_uses_average_rank_for_ties():
    """Two tied IVs share the average of their positions, not the upper rank."""
    contracts = [
        _contract("AAA", OptionType.CALL, 100, iv=0.20),
        _contract("BBB", OptionType.CALL, 105, iv=0.30),
        _contract("CCC", OptionType.CALL, 110, iv=0.30),
        _contract("DDD", OptionType.CALL, 115, iv=0.40),
    ]
    pcts = _iv_percentiles(contracts)
    # AAA rank 1, DDD rank 4. BBB/CCC tied → average rank (2+3)/2 = 2.5.
    # n=4, denominator = n-1 = 3.
    assert pcts["AAA"] == pytest.approx(0.0, abs=1e-4)
    assert pcts["DDD"] == pytest.approx(1.0, abs=1e-4)
    assert pcts["BBB"] == pytest.approx((2.5 - 1) / 3, abs=1e-4)
    assert pcts["CCC"] == pytest.approx((2.5 - 1) / 3, abs=1e-4)


# ── K5: yfinance backoff sleeps between expiration calls ─────────────────


def test_k5_yfinance_backoff_sleep_is_set():
    """The throttle constant is wired (sleep call uses it between expirations)."""
    assert _YFINANCE_INTER_CALL_SLEEP_S > 0


def test_k5_yfinance_chain_sleeps_between_expirations(monkeypatch):
    """Two expirations → exactly one sleep between them; one expiration → no sleep."""
    import options.chain as chain_module

    sleep_calls: list[float] = []
    monkeypatch.setattr(chain_module.time, "sleep", lambda s: sleep_calls.append(s))

    class FakeChain:
        def __init__(self):
            import pandas as pd

            self.calls = pd.DataFrame()
            self.puts = pd.DataFrame()

    class FakeTicker:
        options = ("2026-05-15", "2026-05-22")
        fast_info = {"last_price": 100.0}

        def history(self, *_, **__):
            import pandas as pd

            return pd.DataFrame()

        def option_chain(self, _):
            return FakeChain()

    class FakeYF:
        @staticmethod
        def Ticker(_):
            return FakeTicker()

    monkeypatch.setitem(__import__("sys").modules, "yfinance", FakeYF)
    chain_module.fetch_yfinance_option_chain("NVDA", max_expirations=2)
    assert sleep_calls == [_YFINANCE_INTER_CALL_SLEEP_S]


# ── K11: moneyness_pct returns None on bad input, never 0.0 ──────────────


def test_k11_moneyness_pct_returns_none_for_zero_underlying():
    """Zero underlying is not 'at the money' — must signal missing, not 0.0."""
    contract = _contract("NVDA260515C00100000", OptionType.CALL, 100)
    assert moneyness_pct(contract, 0.0) is None
    assert moneyness_pct(contract, -1.0) is None


def test_k11_moneyness_pct_returns_real_value_for_valid_input():
    contract = _contract("NVDA260515C00105000", OptionType.CALL, 105)
    # Call moneyness = (underlying - strike) / underlying = (100 - 105)/100 = -0.05
    assert moneyness_pct(contract, 100.0) == pytest.approx(-0.05, abs=1e-4)


# ── K12: single-element IV percentile returns empty (not all-zero) ───────


def test_k12_iv_percentile_single_element_returns_empty():
    """A single eligible IV produces no meaningful percentile — return empty dict."""
    contracts = [_contract("ONLY", OptionType.CALL, 100, iv=0.30)]
    assert _iv_percentiles(contracts) == {}


def test_k12_iv_percentile_zero_eligible_returns_empty():
    contracts = [_contract("X", OptionType.CALL, 100, iv=None)]
    assert _iv_percentiles(contracts) == {}


# ── K13: flow score uses the SAME volume/OI ratio shown to the user ──────


def test_k13_flow_score_uses_displayed_volume_oi_ratio():
    """When OI=0, displayed ratio is None and the score must NOT silently use
    `volume / max(OI, 1)` behind the scenes."""
    contract = _contract("OI_ZERO", OptionType.CALL, 100, volume=500, oi=0)
    score, displayed_ratio, _ = _flow_score(contract, mid=1.0)
    assert displayed_ratio is None
    # With ratio_for_score = 0.0, score = volume/1000 * 0.20 + premium_dollars/1M * 0.40
    # premium_dollars = 1.0 * 500 * 100 = 50000 → 50000/1_000_000 * 0.40 = 0.02
    # volume/1000 = 0.5 * 0.20 = 0.10 → score = 0.12
    assert score == pytest.approx(0.12, abs=1e-3)


# ── K14: source is an Enum, not a free-form string ───────────────────────


def test_k14_chain_snapshot_source_is_enum():
    snap = OptionChainSnapshot(
        ticker="NVDA",
        underlying_price=100.0,
        source=OptionChainSource.IBKR,
    )
    assert snap.source is OptionChainSource.IBKR
    assert snap.source.value == "ibkr"


def test_k14_chain_snapshot_source_rejects_unknown_strings():
    with pytest.raises(Exception):
        OptionChainSnapshot(ticker="NVDA", underlying_price=100.0, source="not-a-real-source")


def test_k14_chain_snapshot_source_default_is_manual():
    snap = OptionChainSnapshot(ticker="NVDA", underlying_price=100.0)
    assert snap.source is OptionChainSource.MANUAL


# ── K15: storage row schema is named ─────────────────────────────────────


def test_k15_option_chain_snapshot_row_typed_dict_exists():
    """The row shape is a named contract, not an anonymous dict."""
    annotations = OptionChainSnapshotRow.__annotations__
    assert {"snapshot_id", "ticker", "captured_at", "source", "underlying_price",
            "expirations", "contracts", "metadata"} <= annotations.keys()


def test_k15_snapshot_from_row_round_trips():
    row: OptionChainSnapshotRow = {
        "snapshot_id": "row-1",
        "ticker": "NVDA",
        "captured_at": datetime(2026, 4, 29, 10, 0),
        "source": "yfinance",
        "underlying_price": 100.0,
        "expirations": ["2026-05-15"],
        "contracts": [],
        "metadata": {},
    }
    snap = _snapshot_from_row(row)
    assert snap.ticker == "NVDA"
    assert snap.source is OptionChainSource.YFINANCE


# ── D8: typed OptionsSnapshotSummary, no list[dict] ──────────────────────


def test_d8_options_dashboard_snapshots_is_typed():
    """OptionsDashboard.snapshots is list[OptionsSnapshotSummary], not list[dict]."""
    field = OptionsDashboard.model_fields["snapshots"]
    # Pydantic v2 stores the annotation; verify it parameterizes on OptionsSnapshotSummary
    assert "OptionsSnapshotSummary" in str(field.annotation)


def test_d8_summary_validates_required_fields():
    summary = OptionsSnapshotSummary(
        ticker="NVDA",
        snapshot_id="x",
        captured_at=datetime(2026, 4, 29),
        underlying_price=100.0,
        expirations=2,
        contracts=10,
    )
    assert summary.cheap_vol == 0
    assert summary.unusual_flow == 0
    assert summary.net_position_gamma_dollars is None


# ── D9: liquidity thresholds are config-driven ───────────────────────────


def test_d9_liquidity_score_uses_injected_thresholds():
    """A small cap with looser threshold scores higher than under defaults."""
    contract = _contract("SMALL", OptionType.CALL, 100, volume=50, oi=100)
    default_score = _liquidity_score(contract, spread_pct=0.10, thresholds=LiquidityThresholds())
    loose_score = _liquidity_score(
        contract,
        spread_pct=0.10,
        thresholds=LiquidityThresholds(volume_target=50, oi_target=100, spread_target=0.50),
    )
    assert loose_score > default_score


def test_d9_default_thresholds_match_wave_1_magic_numbers():
    """Defaults must match the pre-D9 hard-coded values to avoid silent re-ranking."""
    defaults = LiquidityThresholds()
    assert defaults.volume_target == 500
    assert defaults.oi_target == 1000
    assert defaults.spread_target == 0.25


# ── D10: compute_tags is callable independently of rank composition ──────


def test_d10_compute_tags_runs_without_rank_pipeline():
    """Tags can be recomputed without rerunning the full ranker."""
    contract = _contract("NVDA260515C00105000", OptionType.CALL, 105, volume=500, oi=200)
    tags = compute_tags(
        contract=contract,
        iv_label="rich",
        spread_pct=0.05,
        flow_score=0.70,
        volume_oi_ratio=2.5,
        dte=20,
        moneyness=-0.05,
    )
    assert "rich_vol" in tags
    assert "unusual_flow" in tags
    assert "tight_spread" in tags
    assert "near_money" not in tags  # |-0.05| > 0.03


def test_d10_compute_tags_handles_none_moneyness():
    """If moneyness is None (per K11), no near_money tag."""
    contract = _contract("X", OptionType.CALL, 100)
    tags = compute_tags(
        contract=contract,
        iv_label="fair",
        spread_pct=None,
        flow_score=0.0,
        volume_oi_ratio=None,
        dte=30,
        moneyness=None,
    )
    assert "near_money" not in tags


# ── K3: scanner sees the full snapshot before filtering ──────────────────


def test_k3_scan_unusual_flow_finds_high_flow_on_illiquid_strikes():
    """A high-flow but otherwise low-rank contract must survive the scan; the
    pre-K3 implementation sliced rank_options_snapshot down to ~80 BEFORE the
    flow filter ran, hiding contracts on illiquid strikes."""
    contracts = [
        _contract(f"NOISE_{i:03d}", OptionType.CALL, 100 + i, volume=10, oi=1000, iv=0.20)
        for i in range(120)
    ]
    # The hidden gem: small OI but huge volume relative to it (vol/oi=20.0)
    contracts.append(
        _contract("HIDDEN_GEM", OptionType.PUT, 95, volume=400, oi=20, iv=0.50)
    )
    snap = _snapshot(contracts)
    flow = scan_unusual_flow(snap, limit=5)
    assert any(row.contract_symbol == "HIDDEN_GEM" for row in flow)


# ── Integration: a full rank pipeline still works after all the fixes ────


def test_integration_full_pipeline_still_ranks_known_winner():
    """Smoke test: nothing in the cleanup PR broke end-to-end ranking."""
    contracts = [
        _contract("NVDA260515C00100000", OptionType.CALL, 100, iv=0.20, volume=80, oi=1000),
        _contract("NVDA260515C00105000", OptionType.CALL, 105, iv=0.55, volume=1500, oi=200, bid=2.0, ask=2.1),
        _contract("NVDA260515P00095000", OptionType.PUT, 95, iv=0.35, volume=20, oi=400),
    ]
    snap = _snapshot(contracts)
    ranks = rank_options_snapshot(snap, as_of=date(2026, 4, 29), limit=3)
    assert ranks[0].contract_symbol == "NVDA260515C00105000"
    assert ranks[0].iv_label == "rich"
    assert ranks[0].position_gamma_dollars is not None

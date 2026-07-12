"""Unit tests for agents/paper_trader.py — wave 1.5.

Mocks the Alpaca client and DB layer via monkeypatch — no network, no DB.
Covers the validation order, ISOLATION boundary, retry/error classification,
qty math, multi-strategy idempotency, and the SELL-via-margin path.

All tests pass a Strategy explicitly (wave 1.5 signature change).
"""
from datetime import date

import pytest

from config import settings as live_settings
from data.models import Decision, FinalSignal, PaperTradeStatus, RiskVerdict
from tests.conftest import _signal


@pytest.fixture
def paper_enabled(monkeypatch):
    monkeypatch.setattr(live_settings, "paper_trading_enabled", True)
    monkeypatch.setattr(live_settings, "paper_account_starting_value", 100_000)


@pytest.fixture(autouse=True)
def _reset_alpaca_clients():
    """Each test starts with empty per-strategy alpaca client cache."""
    from agents.paper_trader import _reset_alpaca_clients
    _reset_alpaca_clients()
    yield
    _reset_alpaca_clients()


# ─── Feature flag ────────────────────────────────────────

def test_paper_trading_disabled_returns_none(monkeypatch, storage_capture, mock_balanced_strategy):
    monkeypatch.setattr(live_settings, "paper_trading_enabled", False)
    from agents.paper_trader import execute_paper_trade
    assert execute_paper_trade(_signal(), mock_balanced_strategy) is None
    assert storage_capture["inserted"] == []


# ─── Idempotency (wave 1.5: key includes :strategy) ─────

def test_duplicate_with_alpaca_order_id_is_blocked(
    paper_enabled, storage_capture, mock_balanced_strategy,
):
    """Existing row already linked to a real Alpaca order → must NOT re-submit."""
    from agents.paper_trader import execute_paper_trade
    storage_capture["trades_by_key"]["NVDA:2026-04-25:balanced"] = {
        "id": 99,
        "alpaca_order_id": "alpaca-existing-id",
        "status": "submitted",
        "status_reason": None,
    }
    result = execute_paper_trade(_signal(), mock_balanced_strategy)
    assert result == {"status": "duplicate_blocked"}
    assert storage_capture["inserted"] == []
    assert storage_capture["deleted_keys"] == []


def test_audit_only_row_cleared_and_resubmitted(
    paper_enabled, storage_capture, mock_balanced_strategy, monkeypatch,
):
    """Existing row with no alpaca_order_id (audit-only) → delete + re-submit."""
    from agents import paper_trader
    monkeypatch.setattr(paper_trader, "_get_alpaca_client", lambda strategy: object())
    monkeypatch.setattr(
        paper_trader, "_submit_bracket",
        lambda *a, **kw: ("alpaca-fresh-id", None),
    )

    storage_capture["trades_by_key"]["NVDA:2026-04-25:balanced"] = {
        "id": 42,
        "alpaca_order_id": None,
        "status": "execution_skipped",
        "status_reason": "risk_rejected",
    }
    result = paper_trader.execute_paper_trade(_signal(), mock_balanced_strategy)
    assert result["status"] == "submitted"
    assert result["alpaca_order_id"] == "alpaca-fresh-id"
    assert "NVDA:2026-04-25:balanced" in storage_capture["deleted_keys"]
    assert len(storage_capture["inserted"]) == 1
    assert storage_capture["inserted"][0].idempotency_key == "NVDA:2026-04-25:balanced"


def test_idempotency_key_includes_strategy(mock_aggressive_strategy, mock_balanced_strategy):
    """The same signal under different strategies must produce DIFFERENT keys
    (regression test for wave-1.5 multi-strategy schema).
    """
    from agents.paper_trader import _idempotency_key
    sig = _signal()
    assert _idempotency_key(sig, mock_aggressive_strategy) == "NVDA:2026-04-25:aggressive"
    assert _idempotency_key(sig, mock_balanced_strategy) == "NVDA:2026-04-25:balanced"
    # Distinct → 3 strategies on the same signal produce 3 distinct rows
    assert _idempotency_key(sig, mock_aggressive_strategy) != _idempotency_key(sig, mock_balanced_strategy)


def test_idempotency_key_ignores_recommendation_id_for_same_day_dedup(mock_balanced_strategy):
    """Paper execution dedupes by logical ticker/date/strategy, not v2 run id."""
    from agents.paper_trader import _idempotency_key
    sig = _signal()
    sig.recommendation_id = "rec-abc123"
    assert _idempotency_key(sig, mock_balanced_strategy) == "NVDA:2026-04-25:balanced"


def test_duplicate_with_legacy_v2_key_is_blocked(
    paper_enabled, storage_capture, mock_balanced_strategy,
):
    """Old rec_id-based keys still block if they already reached Alpaca."""
    from agents.paper_trader import execute_paper_trade
    storage_capture["trades_by_key"]["rec-abc123:balanced"] = {
        "id": 99,
        "recommendation_id": "rec-abc123",
        "ticker": "NVDA",
        "signal_date": date(2026, 4, 25),
        "strategy": "balanced",
        "alpaca_order_id": "alpaca-existing-id",
        "status": "submitted",
        "status_reason": None,
    }
    sig = _signal()
    sig.recommendation_id = "rec-new456"

    result = execute_paper_trade(sig, mock_balanced_strategy)

    assert result == {"status": "duplicate_blocked"}
    assert storage_capture["inserted"] == []


# ─── Validation order ────────────────────────────────────

def test_hold_decision_records_skip(paper_enabled, storage_capture, mock_balanced_strategy):
    from agents.paper_trader import execute_paper_trade
    result = execute_paper_trade(_signal(decision=Decision.HOLD), mock_balanced_strategy)
    assert result == {"status": "execution_skipped", "reason": "hold_decision"}
    assert len(storage_capture["inserted"]) == 1
    inserted = storage_capture["inserted"][0]
    assert inserted.status == PaperTradeStatus.EXECUTION_SKIPPED
    assert inserted.status_reason == "hold_decision"
    assert inserted.strategy == "balanced"


def test_empty_entry_zone_records_skip(paper_enabled, storage_capture, mock_balanced_strategy):
    from agents.paper_trader import execute_paper_trade
    sig = _signal()
    sig.entry_zone = []
    result = execute_paper_trade(sig, mock_balanced_strategy)
    assert result == {"status": "execution_skipped", "reason": "invalid_entry_zone"}
    assert storage_capture["inserted"][0].status_reason == "invalid_entry_zone"


def test_missing_targets_records_skip(paper_enabled, storage_capture, mock_balanced_strategy):
    from agents.paper_trader import execute_paper_trade
    sig = _signal(targets=())
    result = execute_paper_trade(sig, mock_balanced_strategy)
    assert result == {"status": "execution_skipped", "reason": "missing_targets"}


def test_invalid_buy_bracket_stop_above_entry(paper_enabled, storage_capture, mock_balanced_strategy):
    """BUY: must have stop < entry < target. stop > entry should be rejected."""
    from agents.paper_trader import execute_paper_trade
    sig = _signal(stop_loss=510.0, targets=(520.0,))
    result = execute_paper_trade(sig, mock_balanced_strategy)
    assert result == {"status": "execution_skipped", "reason": "invalid_bracket"}
    assert "BUY bracket invalid" in storage_capture["inserted"][0].status_reason


def test_invalid_buy_bracket_target_below_entry(paper_enabled, storage_capture, mock_balanced_strategy):
    from agents.paper_trader import execute_paper_trade
    sig = _signal(stop_loss=478.0, targets=(480.0,))
    result = execute_paper_trade(sig, mock_balanced_strategy)
    assert result == {"status": "execution_skipped", "reason": "invalid_bracket"}


# ─── Per-strategy R/R cap (hallucinated-target gate) ────

def test_rr_cap_blocks_when_signal_rr_above_strategy_max(
    paper_enabled, storage_capture, mock_balanced_strategy,
):
    """AMD-style: R/R 22.4x signal hits BAL's cap of 8.0 → execution_skipped."""
    from agents.paper_trader import execute_paper_trade
    from agents.strategies import Strategy
    bal_with_cap = Strategy(
        name="balanced", notional_pct=0.015, drawdown_pause_threshold=-0.10,
        api_key="k", secret_key="s", enabled=True, max_reward_risk=8.0,
    )
    sig = _signal(reward_risk_ratio=22.4)
    result = execute_paper_trade(sig, bal_with_cap)
    assert result == {"status": "execution_skipped", "reason": "rr_above_strategy_cap"}
    inserted = storage_capture["inserted"][0]
    assert "22.4x exceeds balanced cap of 8.0x" in inserted.status_reason
    assert inserted.strategy == "balanced"


def test_rr_cap_per_strategy_differentiation(paper_enabled, storage_capture):
    """Same R/R-9 signal: AGG (cap 12) accepts, BAL (cap 8) rejects, CON (cap 6) rejects."""
    from agents import paper_trader
    from agents.strategies import Strategy

    sig = _signal(reward_risk_ratio=9.0)

    agg = Strategy(name="aggressive", notional_pct=0.03, drawdown_pause_threshold=-0.15,
                   api_key="k", secret_key="s", enabled=True, max_reward_risk=12.0)
    bal = Strategy(name="balanced", notional_pct=0.015, drawdown_pause_threshold=-0.10,
                   api_key="k", secret_key="s", enabled=True, max_reward_risk=8.0)
    con = Strategy(name="conservative", notional_pct=0.005, drawdown_pause_threshold=-0.08,
                   api_key="k", secret_key="s", enabled=True, max_reward_risk=6.0)

    paper_trader._reset_alpaca_clients()
    # Stub alpaca client + submission so AGG actually progresses past the cap
    captured = []

    def _fake_submit(*args, **kwargs):
        captured.append(args)
        return ("alpaca-fake-id", None)

    import pytest
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(paper_trader, "_get_alpaca_client", lambda s: object())
        monkeypatch.setattr(paper_trader, "_submit_bracket", _fake_submit)

        agg_result = paper_trader.execute_paper_trade(sig, agg)
        bal_result = paper_trader.execute_paper_trade(sig, bal)
        con_result = paper_trader.execute_paper_trade(sig, con)
    finally:
        monkeypatch.undo()

    assert agg_result["status"] == "submitted"
    assert bal_result == {"status": "execution_skipped", "reason": "rr_above_strategy_cap"}
    assert con_result == {"status": "execution_skipped", "reason": "rr_above_strategy_cap"}


def test_rr_cap_zero_disables_gate(paper_enabled, storage_capture, monkeypatch):
    """Operator escape hatch: max_reward_risk=0 turns the cap off."""
    from agents import paper_trader
    from agents.strategies import Strategy

    no_cap = Strategy(
        name="balanced", notional_pct=0.015, drawdown_pause_threshold=-0.10,
        api_key="k", secret_key="s", enabled=True, max_reward_risk=0.0,
    )
    monkeypatch.setattr(paper_trader, "_get_alpaca_client", lambda s: object())
    monkeypatch.setattr(paper_trader, "_submit_bracket", lambda *a, **kw: ("id", None))

    sig = _signal(reward_risk_ratio=50.0)
    result = paper_trader.execute_paper_trade(sig, no_cap)
    assert result["status"] == "submitted"


def test_valid_sell_bracket_passes_validation(
    paper_enabled, storage_capture, mock_balanced_strategy, monkeypatch,
):
    """SELL: stop > entry > target. Should NOT skip on bracket sanity."""
    from agents import paper_trader
    monkeypatch.setattr(
        paper_trader, "_submit_bracket",
        lambda *a, **kw: ("alpaca-fake-id", None),
    )
    monkeypatch.setattr(paper_trader, "_get_alpaca_client", lambda strategy: object())
    sig = _signal(decision=Decision.SELL, entry_zone=(485.0, 489.0), stop_loss=500.0, targets=(470.0,))
    result = paper_trader.execute_paper_trade(sig, mock_balanced_strategy)
    assert result["status"] == "submitted"
    inserted = storage_capture["inserted"][0]
    assert inserted.side == "sell_short"


# ─── Position sizing (E2 dropped: flat notional per strategy) ──

def test_compute_qty_basic(mock_balanced_strategy):
    """1.5% of 100k = $1500. At $487 → 3 shares (1500/487 = 3.08, floor)."""
    from agents.paper_trader import _compute_qty
    assert _compute_qty(mock_balanced_strategy, 487.0) == 3


def test_compute_qty_aggressive_uses_3pct(mock_aggressive_strategy, monkeypatch):
    """Aggressive 3.0% of 100k = $3000. At $487 → 6 shares (3000/487 = 6.16)."""
    monkeypatch.setattr(live_settings, "paper_account_starting_value", 100_000)
    from agents.paper_trader import _compute_qty
    assert _compute_qty(mock_aggressive_strategy, 487.0) == 6


def test_compute_qty_conservative_uses_05pct(mock_conservative_strategy, monkeypatch):
    """Conservative 0.5% of 100k = $500. At $487 → 1 share (500/487 = 1.03)."""
    monkeypatch.setattr(live_settings, "paper_account_starting_value", 100_000)
    from agents.paper_trader import _compute_qty
    assert _compute_qty(mock_conservative_strategy, 487.0) == 1


def test_compute_qty_floors_to_at_least_one(mock_balanced_strategy, monkeypatch):
    """Tiny notional → still at least 1 share (no zero-share orders)."""
    from agents.paper_trader import _compute_qty
    # Use a strategy with a near-zero notional to test the floor
    from agents.strategies import Strategy
    tiny = Strategy("tiny", 0.0001, -0.10, "k", "s", enabled=True)
    assert _compute_qty(tiny, 100_000.0) == 1


# ─── Bracket sanity helper (pure) ───────────────────────

def test_validate_bracket_sanity_valid_buy():
    from agents.paper_trader import _validate_bracket_sanity
    assert _validate_bracket_sanity(Decision.BUY, 487, 478, 510) is None


def test_validate_bracket_sanity_valid_sell():
    from agents.paper_trader import _validate_bracket_sanity
    assert _validate_bracket_sanity(Decision.SELL, 487, 500, 470) is None


def test_validate_bracket_sanity_invalid_buy():
    from agents.paper_trader import _validate_bracket_sanity
    msg = _validate_bracket_sanity(Decision.BUY, 487, 510, 478)
    assert msg is not None
    assert "BUY bracket invalid" in msg


# ─── Per-strategy isolation ────────────────────────────

def test_per_strategy_alpaca_clients_cached_independently(
    paper_enabled, mock_aggressive_strategy, mock_balanced_strategy, monkeypatch,
):
    """Each strategy's TradingClient is cached separately (3 clients, not 1)."""
    from agents import paper_trader

    # Stub TradingClient so we don't need real alpaca-py wiring
    captured = []

    class FakeTC:
        def __init__(self, **kwargs):
            captured.append(kwargs)

    monkeypatch.setattr("alpaca.trading.client.TradingClient", FakeTC)

    c1 = paper_trader._get_alpaca_client(mock_aggressive_strategy)
    c2 = paper_trader._get_alpaca_client(mock_balanced_strategy)
    c3 = paper_trader._get_alpaca_client(mock_aggressive_strategy)  # cached

    assert c1 is not c2          # different strategies → different instances
    assert c1 is c3              # same strategy → same cached instance
    # Two distinct TradingClient constructions (one per unique strategy)
    assert len(captured) == 2
    assert captured[0]["api_key"] == mock_aggressive_strategy.api_key
    assert captured[1]["api_key"] == mock_balanced_strategy.api_key

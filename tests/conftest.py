"""Shared pytest fixtures for wave 1.5.

The DB layer is mocked via monkeypatch in most tests — no real Postgres,
no real Alpaca. See tests/README.md for the testing model.
"""
from datetime import date

import pytest

from agents.strategies import Strategy
from data.models import Decision, FinalSignal, RiskVerdict


# ─── Strategy fixtures (wave 1.5) ──────────────────────

@pytest.fixture
def mock_balanced_strategy():
    """A 'balanced' Strategy with placeholder creds. Used as the default
    single-strategy test target — matches wave-1 behavior."""
    return Strategy(
        name="balanced",
        notional_pct=0.015,
        drawdown_pause_threshold=-0.10,
        api_key="test_balanced_key",
        secret_key="test_balanced_secret",
        enabled=True,
    )


@pytest.fixture
def mock_aggressive_strategy():
    return Strategy(
        name="aggressive",
        notional_pct=0.030,
        drawdown_pause_threshold=-0.15,
        api_key="test_aggressive_key",
        secret_key="test_aggressive_secret",
        enabled=True,
    )


@pytest.fixture
def mock_conservative_strategy():
    return Strategy(
        name="conservative",
        notional_pct=0.005,
        drawdown_pause_threshold=-0.08,
        api_key="test_conservative_key",
        secret_key="test_conservative_secret",
        enabled=True,
    )


@pytest.fixture
def mock_strategies(mock_aggressive_strategy, mock_balanced_strategy, mock_conservative_strategy):
    """All 3 strategies in canonical AGG/BAL/CON order."""
    return [mock_aggressive_strategy, mock_balanced_strategy, mock_conservative_strategy]


# ─── Signal factory ────────────────────────────────────

def _signal(
    decision=Decision.BUY,
    entry_zone=(485.0, 489.0),
    stop_loss=478.0,
    targets=(510.0,),
    confidence=0.75,
    ticker="NVDA",
    risk_verdict=RiskVerdict.APPROVED,
    reward_risk_ratio=2.5,
):
    return FinalSignal(
        ticker=ticker,
        date=date(2026, 4, 25),
        decision=decision,
        confidence=confidence,
        entry_zone=list(entry_zone),
        stop_loss=stop_loss,
        targets=list(targets),
        invalidation="break of support",
        holding_period_days=5,
        thesis="strong setup",
        bull_case="upside thesis",
        bear_case="downside risk",
        risk_verdict=risk_verdict,
        risk_reasons=[],
        position_size_pct=0.015,
        reward_risk_ratio=reward_risk_ratio,
    )


@pytest.fixture
def signal():
    return _signal()


@pytest.fixture
def hold_signal():
    return _signal(decision=Decision.HOLD)


@pytest.fixture
def rejected_signal():
    return _signal(risk_verdict=RiskVerdict.REJECTED)


# ─── Storage capture (in-memory mock) ─────────────────

@pytest.fixture
def storage_capture(monkeypatch):
    """In-memory mock of the paper_trades + pause + snapshot helpers.

    Used by tests that exercise execute_paper_trade / execute_paper_trades
    without touching a real DB. Each test gets a fresh dict.
    """
    state = {
        "trades_by_key": {},        # idempotency_key -> dict (mock paper_trade row)
        "trade_counter": [0],       # mutable so closures can increment
        "inserted": [],             # list[PaperTrade] inserted via insert_paper_trade
        "upserted": [],             # list[PaperTrade] upserted via upsert_paper_trade
        "deleted_keys": [],
        "snapshots": [],            # list[EquitySnapshot]
        "pause_state": {},          # strategy -> dict
        "pause_inserts": [],        # list[(strategy, reason)] every call
    }

    def _insert(trade):
        state["trade_counter"][0] += 1
        new_id = state["trade_counter"][0]
        row = trade.model_dump()
        row["id"] = new_id
        state["trades_by_key"][trade.idempotency_key] = row
        state["inserted"].append(trade)
        return new_id

    def _upsert(trade):
        existing = state["trades_by_key"].get(trade.idempotency_key)
        if existing:
            new_id = existing["id"]
        else:
            state["trade_counter"][0] += 1
            new_id = state["trade_counter"][0]
        row = trade.model_dump()
        row["id"] = new_id
        state["trades_by_key"][trade.idempotency_key] = row
        state["upserted"].append(trade)
        return new_id

    def _get_by_key(key):
        return state["trades_by_key"].get(key)

    def _get_by_signal_strategy(ticker, signal_date, strategy):
        matches = [
            row for row in state["trades_by_key"].values()
            if row.get("ticker") == ticker
            and row.get("signal_date") == signal_date
            and row.get("strategy") == strategy
        ]
        if not matches:
            return None
        matches.sort(
            key=lambda row: (
                0 if row.get("alpaca_order_id") else 1,
                -row.get("id", 0),
            )
        )
        return matches[0]

    def _delete_by_key(key):
        existing = state["trades_by_key"].get(key)
        if existing and existing.get("alpaca_order_id"):
            return False
        if key in state["trades_by_key"]:
            del state["trades_by_key"][key]
            state["deleted_keys"].append(key)
            return True
        return False

    def _update_status(trade_id, status, status_reason=None, alpaca_order_id=None):
        for k, row in state["trades_by_key"].items():
            if row.get("id") == trade_id:
                row["status"] = status.value if hasattr(status, "value") else status
                if status_reason is not None:
                    row["status_reason"] = status_reason
                if alpaca_order_id is not None:
                    row["alpaca_order_id"] = alpaca_order_id
                return

    def _insert_pause_if_absent(strategy, reason, paused_drawdown=None, unpause_after=None):
        state["pause_inserts"].append((strategy, reason))
        if strategy in state["pause_state"]:
            return False
        state["pause_state"][strategy] = {
            "strategy": strategy,
            "paused_reason": reason,
            "paused_drawdown": paused_drawdown,
            "unpause_after": unpause_after,
        }
        return True

    def _get_strategy_pause(strategy):
        return state["pause_state"].get(strategy)

    def _get_recent_snapshots(strategy, limit=30):
        return [s for s in reversed(state["snapshots"]) if s.get("strategy") == strategy][:limit]

    def _insert_snapshot(snap):
        state["snapshots"].append(snap.model_dump() if hasattr(snap, "model_dump") else dict(snap))
        return len(state["snapshots"])

    # Patch storage layer
    monkeypatch.setattr("data.storage.insert_paper_trade", _insert)
    monkeypatch.setattr("data.storage.upsert_paper_trade", _upsert)
    monkeypatch.setattr("data.storage.get_paper_trade_by_key", _get_by_key)
    monkeypatch.setattr("data.storage.get_paper_trade_by_signal_strategy", _get_by_signal_strategy)
    monkeypatch.setattr("data.storage.delete_paper_trade_by_key", _delete_by_key)
    monkeypatch.setattr("data.storage.update_paper_trade_status", _update_status)
    monkeypatch.setattr("data.storage.insert_pause_if_absent", _insert_pause_if_absent)
    monkeypatch.setattr("data.storage.get_strategy_pause", _get_strategy_pause)
    monkeypatch.setattr("data.storage.get_recent_snapshots", _get_recent_snapshots)
    monkeypatch.setattr("data.storage.insert_equity_snapshot", _insert_snapshot)

    # Also patch import-site names used by paper_trader (it imports from data.storage)
    monkeypatch.setattr("agents.paper_trader.insert_paper_trade", _insert)
    monkeypatch.setattr("agents.paper_trader.get_paper_trade_by_key", _get_by_key)
    monkeypatch.setattr("agents.paper_trader.get_paper_trade_by_signal_strategy", _get_by_signal_strategy)
    monkeypatch.setattr("agents.paper_trader.delete_paper_trade_by_key", _delete_by_key)
    monkeypatch.setattr("agents.paper_trader.update_paper_trade_status", _update_status)

    return state

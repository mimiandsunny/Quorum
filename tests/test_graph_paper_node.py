"""Unit tests for the execute_paper_trades graph node.

These tests keep the node at the wave 1.5 async fan-out boundary:
APPROVED signals fan out across enabled strategies, while REJECTED signals
write one audit row per strategy and never touch Alpaca.
"""

from datetime import date

import pytest

from config import settings as live_settings
from data.models import (
    Decision,
    FinalSignal,
    PaperTradeStatus,
    RiskAssessment,
    RiskVerdict,
)


def _signal(verdict=RiskVerdict.APPROVED, decision=Decision.BUY):
    return FinalSignal(
        ticker="NVDA",
        date=date(2026, 4, 25),
        decision=decision,
        confidence=0.75,
        entry_zone=[485.0, 489.0],
        stop_loss=478.0,
        targets=[510.0],
        invalidation="x",
        holding_period_days=5,
        thesis="t",
        bull_case="b",
        bear_case="b",
        risk_verdict=verdict,
        risk_reasons=[],
        position_size_pct=0.015,
        reward_risk_ratio=2.5,
    )


def _state(signal=None, risk_verdict=RiskVerdict.APPROVED, error=None):
    sig = signal or _signal(verdict=risk_verdict)
    return {
        "ticker": "NVDA",
        "final_signal": sig,
        "risk_assessment": RiskAssessment(
            ticker="NVDA",
            verdict=risk_verdict,
            position_size_pct=0.015,
            reward_risk_ratio=2.5,
        ),
        **({"error": error} if error else {}),
    }


@pytest.fixture
def inline_to_thread(monkeypatch):
    """Run asyncio.to_thread payloads inline but still through gather."""
    scheduled = []

    async def _to_thread(fn, *args, **kwargs):
        scheduled.append((fn, args, kwargs))
        return fn(*args, **kwargs)

    monkeypatch.setattr("asyncio.to_thread", _to_thread)
    return scheduled


@pytest.fixture
def graph_storage_capture(monkeypatch):
    """Capture REJECTED-branch DB writes without touching Postgres."""
    state = {
        "existing_rows": {},
        "upserted": [],
    }

    def _get_by_key(key):
        return state["existing_rows"].get(key)

    def _get_by_signal_strategy(ticker, signal_date, strategy):
        matches = [
            row for row in state["existing_rows"].values()
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

    def _upsert(trade):
        existing = state["existing_rows"].get(trade.idempotency_key)
        new_id = (existing or {}).get("id", len(state["upserted"]) + 1)
        state["existing_rows"][trade.idempotency_key] = {
            "id": new_id,
            "ticker": trade.ticker,
            "signal_date": trade.signal_date,
            "strategy": trade.strategy,
            "alpaca_order_id": trade.alpaca_order_id,
            "status": trade.status.value,
            "status_reason": trade.status_reason,
        }
        state["upserted"].append(trade)
        return new_id

    monkeypatch.setattr("data.storage.get_paper_trade_by_key", _get_by_key)
    monkeypatch.setattr("data.storage.get_paper_trade_by_signal_strategy", _get_by_signal_strategy)
    monkeypatch.setattr("data.storage.upsert_paper_trade", _upsert)
    return state


def test_disabled_no_op(monkeypatch, graph_storage_capture):
    from agents.graph import execute_paper_trades

    monkeypatch.setattr(live_settings, "paper_trading_enabled", False)

    out = execute_paper_trades(_state())

    assert out == _state()
    assert graph_storage_capture["upserted"] == []


def test_no_final_signal_no_op(monkeypatch, graph_storage_capture):
    from agents.graph import execute_paper_trades

    monkeypatch.setattr(live_settings, "paper_trading_enabled", True)

    out = execute_paper_trades({"ticker": "NVDA"})

    assert out == {"ticker": "NVDA"}
    assert graph_storage_capture["upserted"] == []


def test_error_in_state_no_op(monkeypatch, graph_storage_capture):
    from agents.graph import execute_paper_trades

    monkeypatch.setattr(live_settings, "paper_trading_enabled", True)

    out = execute_paper_trades(_state(error="upstream failed"))

    assert out["error"] == "upstream failed"
    assert graph_storage_capture["upserted"] == []


def test_rejected_signal_writes_audit_row_for_each_strategy(
    monkeypatch, graph_storage_capture, mock_strategies
):
    from agents import graph as graph_mod

    monkeypatch.setattr(live_settings, "paper_trading_enabled", True)
    monkeypatch.setattr(graph_mod, "enabled_strategies", lambda: mock_strategies)

    graph_mod.execute_paper_trades(_state(risk_verdict=RiskVerdict.REJECTED))

    keys = [t.idempotency_key for t in graph_storage_capture["upserted"]]
    assert keys == [
        "NVDA:2026-04-25:aggressive",
        "NVDA:2026-04-25:balanced",
        "NVDA:2026-04-25:conservative",
    ]
    assert {t.status for t in graph_storage_capture["upserted"]} == {
        PaperTradeStatus.EXECUTION_SKIPPED
    }
    assert {t.status_reason for t in graph_storage_capture["upserted"]} == {
        "risk_rejected"
    }


def test_rejected_signal_refreshes_stale_audit_row(
    monkeypatch, graph_storage_capture, mock_balanced_strategy
):
    from agents import graph as graph_mod

    monkeypatch.setattr(live_settings, "paper_trading_enabled", True)
    monkeypatch.setattr(graph_mod, "enabled_strategies", lambda: [mock_balanced_strategy])
    graph_storage_capture["existing_rows"]["NVDA:2026-04-25:balanced"] = {
        "id": 7,
        "alpaca_order_id": None,
        "status": "execution_skipped",
        "status_reason": "risk_rejected",
    }

    graph_mod.execute_paper_trades(_state(risk_verdict=RiskVerdict.REJECTED))

    refreshed = graph_storage_capture["upserted"][0]
    assert refreshed.idempotency_key == "NVDA:2026-04-25:balanced"
    assert refreshed.status == PaperTradeStatus.EXECUTION_SKIPPED
    assert refreshed.status_reason == "risk_rejected"


def test_rejected_signal_preserves_alpaca_linked_row(
    monkeypatch, graph_storage_capture, mock_balanced_strategy
):
    from agents import graph as graph_mod

    monkeypatch.setattr(live_settings, "paper_trading_enabled", True)
    monkeypatch.setattr(graph_mod, "enabled_strategies", lambda: [mock_balanced_strategy])
    graph_storage_capture["existing_rows"]["NVDA:2026-04-25:balanced"] = {
        "id": 7,
        "alpaca_order_id": "alpaca-real-id",
        "status": "submitted",
        "status_reason": None,
    }

    graph_mod.execute_paper_trades(_state(risk_verdict=RiskVerdict.REJECTED))

    assert graph_storage_capture["upserted"] == []


def test_rejected_signal_preserves_legacy_v2_alpaca_linked_row(
    monkeypatch, graph_storage_capture, mock_balanced_strategy
):
    from agents import graph as graph_mod

    monkeypatch.setattr(live_settings, "paper_trading_enabled", True)
    monkeypatch.setattr(graph_mod, "enabled_strategies", lambda: [mock_balanced_strategy])
    graph_storage_capture["existing_rows"]["rec-abc123:balanced"] = {
        "id": 7,
        "ticker": "NVDA",
        "signal_date": date(2026, 4, 25),
        "strategy": "balanced",
        "alpaca_order_id": "alpaca-real-id",
        "status": "submitted",
        "status_reason": None,
    }
    signal = _signal(verdict=RiskVerdict.REJECTED)
    signal.recommendation_id = "rec-new456"

    graph_mod.execute_paper_trades(_state(signal=signal, risk_verdict=RiskVerdict.REJECTED))

    assert graph_storage_capture["upserted"] == []


@pytest.mark.asyncio
async def test_async_fanout_schedules_one_task_per_enabled_strategy(
    monkeypatch, inline_to_thread, mock_strategies
):
    from agents import graph as graph_mod

    calls = []
    monkeypatch.setattr(graph_mod, "is_paused", lambda strategy: False)
    monkeypatch.setattr(
        graph_mod,
        "execute_paper_trade",
        lambda signal, strategy: calls.append(strategy.name) or {"status": "submitted"},
    )

    await graph_mod._execute_paper_trades_async(_signal(), mock_strategies)

    assert calls == ["aggressive", "balanced", "conservative"]
    assert len(inline_to_thread) == 3


@pytest.mark.asyncio
async def test_async_fanout_skips_paused_strategy(
    monkeypatch, inline_to_thread, mock_strategies
):
    from agents import graph as graph_mod

    calls = []
    monkeypatch.setattr(
        graph_mod,
        "is_paused",
        lambda strategy: strategy.name == "balanced",
    )
    monkeypatch.setattr(
        graph_mod,
        "execute_paper_trade",
        lambda signal, strategy: calls.append(strategy.name),
    )

    await graph_mod._execute_paper_trades_async(_signal(), mock_strategies)

    assert calls == ["aggressive", "conservative"]
    assert len(inline_to_thread) == 2


def test_approved_signal_uses_async_fanout_from_sync_node(
    monkeypatch, inline_to_thread, mock_strategies
):
    from agents import graph as graph_mod

    calls = []
    monkeypatch.setattr(live_settings, "paper_trading_enabled", True)
    monkeypatch.setattr(graph_mod, "enabled_strategies", lambda: mock_strategies)
    monkeypatch.setattr(graph_mod, "is_paused", lambda strategy: False)
    monkeypatch.setattr(
        graph_mod,
        "execute_paper_trade",
        lambda signal, strategy: calls.append((signal.ticker, strategy.name)),
    )

    out = graph_mod.execute_paper_trades(_state())

    assert [name for _, name in calls] == ["aggressive", "balanced", "conservative"]
    assert len(inline_to_thread) == 3
    assert out["final_signal"].ticker == "NVDA"


def test_paper_trader_exception_isolated_in_one_strategy(
    monkeypatch, inline_to_thread, mock_strategies
):
    from agents import graph as graph_mod

    calls = []
    monkeypatch.setattr(live_settings, "paper_trading_enabled", True)
    monkeypatch.setattr(graph_mod, "enabled_strategies", lambda: mock_strategies)
    monkeypatch.setattr(graph_mod, "is_paused", lambda strategy: False)

    def _execute(signal, strategy):
        calls.append(strategy.name)
        if strategy.name == "balanced":
            raise RuntimeError("alpaca exploded unexpectedly")
        return {"status": "submitted"}

    monkeypatch.setattr(graph_mod, "execute_paper_trade", _execute)

    out = graph_mod.execute_paper_trades(_state())

    assert calls == ["aggressive", "balanced", "conservative"]
    assert out["final_signal"].ticker == "NVDA"


def test_hold_signal_with_approved_verdict_routes_through_fanout(
    monkeypatch, inline_to_thread, mock_strategies, graph_storage_capture
):
    from agents import graph as graph_mod

    calls = []
    monkeypatch.setattr(live_settings, "paper_trading_enabled", True)
    monkeypatch.setattr(graph_mod, "enabled_strategies", lambda: mock_strategies)
    monkeypatch.setattr(graph_mod, "is_paused", lambda strategy: False)
    monkeypatch.setattr(
        graph_mod,
        "execute_paper_trade",
        lambda signal, strategy: calls.append((signal.decision, strategy.name)),
    )

    graph_mod.execute_paper_trades(_state(signal=_signal(decision=Decision.HOLD)))

    assert [name for _, name in calls] == ["aggressive", "balanced", "conservative"]
    assert graph_storage_capture["upserted"] == []

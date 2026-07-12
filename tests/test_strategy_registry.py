"""Strategy registry + circuit breaker tests (wave 1.5).

Covers:
  - Registry construction from settings
  - enabled_strategies filter (creds gate)
  - is_paused branches: explicit pause row, drawdown threshold, insufficient data
  - insert_pause_if_absent race semantics (NEW5 fix from CEO eng-review)
  - Color token mapping
"""
from datetime import date, datetime, timezone

import pytest

from agents import strategies as strategies_mod
from agents.strategies import (
    Strategy,
    enabled_strategies,
    get_strategy,
    is_paused,
    reload_strategies,
    strategy_color_token,
)
from data.models import EquitySnapshot


# ─── Registry ─────────────────────────────────────────

def test_registry_has_3_strategies():
    """Wave 1.5 ships exactly 3 strategies in canonical order."""
    reload_strategies()
    names = [s.name for s in strategies_mod.STRATEGIES]
    assert names == ["aggressive", "balanced", "conservative"]


def test_strategy_notional_pct_per_strategy():
    """Each strategy has a distinct notional fraction."""
    reload_strategies()
    notionals = {s.name: s.notional_pct for s in strategies_mod.STRATEGIES}
    assert notionals["aggressive"] == 0.030
    assert notionals["balanced"] == 0.015
    assert notionals["conservative"] == 0.005


def test_strategy_drawdown_thresholds_per_strategy():
    """Aggressive has the loosest threshold; conservative the tightest."""
    reload_strategies()
    thresholds = {s.name: s.drawdown_pause_threshold for s in strategies_mod.STRATEGIES}
    assert thresholds["aggressive"] == -0.15
    assert thresholds["balanced"] == -0.10
    assert thresholds["conservative"] == -0.08


def test_strategy_max_rr_caps_per_strategy():
    """Per-strategy R/R cap: AGG most permissive, CON strictest. Caps the
    hallucinated-target class — AMD-style 22x signals get filtered."""
    reload_strategies()
    caps = {s.name: s.max_reward_risk for s in strategies_mod.STRATEGIES}
    assert caps["aggressive"] == 12.0
    assert caps["balanced"] == 8.0
    assert caps["conservative"] == 6.0
    assert caps["aggressive"] > caps["balanced"] > caps["conservative"]


def test_get_strategy_lookup():
    """get_strategy returns the matching Strategy or None."""
    reload_strategies()
    s = get_strategy("balanced")
    assert s is not None and s.name == "balanced"
    assert get_strategy("nonexistent") is None


def test_strategy_color_tokens_distinct():
    """All 3 strategies map to distinct CSS variables. No P&L color collision."""
    tokens = {strategy_color_token(n) for n in ("aggressive", "balanced", "conservative")}
    assert len(tokens) == 3
    # Strategy colors must NOT be green or red (those are P&L semantics)
    assert "var(--green)" not in tokens
    assert "var(--red)" not in tokens


# ─── enabled_strategies (creds gate) ──────────────────

def test_enabled_strategies_only_those_with_creds(monkeypatch):
    """A strategy missing creds is filtered out by enabled_strategies."""
    fake = [
        Strategy("aggressive", 0.03, -0.15, "k1", "s1", enabled=True),
        Strategy("balanced", 0.015, -0.10, "", "", enabled=False),  # missing
        Strategy("conservative", 0.005, -0.08, "k3", "s3", enabled=True),
    ]
    monkeypatch.setattr(strategies_mod, "STRATEGIES", fake)
    enabled = enabled_strategies()
    assert [s.name for s in enabled] == ["aggressive", "conservative"]


def test_enabled_strategies_does_not_call_is_paused(monkeypatch):
    """CL3 fix: enabled_strategies() must not call is_paused() (avoids
    double-query when graph node calls is_paused per-signal).

    Verified by monkeypatching is_paused to raise — if enabled_strategies
    invoked it, the test would error.
    """
    def boom(_strategy):
        raise AssertionError("enabled_strategies must NOT call is_paused")
    monkeypatch.setattr(strategies_mod, "is_paused", boom)
    # Should succeed without invoking is_paused
    result = enabled_strategies()
    assert isinstance(result, list)


# ─── is_paused: explicit pause state ──────────────────

def test_is_paused_returns_true_when_pause_row_exists(monkeypatch, mock_balanced_strategy):
    """A row in strategy_pause_state with no unpause_after = paused indefinitely."""
    monkeypatch.setattr(
        "data.storage.get_strategy_pause",
        lambda name: {"strategy": name, "paused_reason": "manual", "unpause_after": None},
    )
    monkeypatch.setattr("data.storage.get_recent_snapshots", lambda name, limit=30: [])
    assert is_paused(mock_balanced_strategy) is True


def test_is_paused_returns_false_when_no_pause_row_and_no_snapshots(monkeypatch, mock_balanced_strategy):
    """No pause row + no snapshots → not paused (not enough data)."""
    monkeypatch.setattr("data.storage.get_strategy_pause", lambda name: None)
    monkeypatch.setattr("data.storage.get_recent_snapshots", lambda name, limit=30: [])
    assert is_paused(mock_balanced_strategy) is False


def test_is_paused_returns_false_with_fewer_than_5_snapshots(monkeypatch, mock_balanced_strategy):
    """Insufficient snapshots → fail-open (don't pause on flimsy signal)."""
    monkeypatch.setattr("data.storage.get_strategy_pause", lambda name: None)
    snaps = [{"account_equity": 100_000} for _ in range(4)]
    monkeypatch.setattr("data.storage.get_recent_snapshots", lambda name, limit=30: snaps)
    assert is_paused(mock_balanced_strategy) is False


# ─── is_paused: drawdown trigger ──────────────────────

def test_is_paused_trips_when_drawdown_below_threshold(
    monkeypatch, mock_balanced_strategy
):
    """Rolling-30-day drawdown of -12% trips the -10% threshold for balanced."""
    monkeypatch.setattr("data.storage.get_strategy_pause", lambda name: None)
    # peak=100k, current=88k → drawdown = -0.12 (below -0.10 threshold)
    snaps = [
        {"account_equity": 88_000},   # newest first
        {"account_equity": 92_000},
        {"account_equity": 95_000},
        {"account_equity": 100_000},  # peak
        {"account_equity": 99_000},
    ]
    monkeypatch.setattr("data.storage.get_recent_snapshots", lambda name, limit=30: snaps)
    inserted = {"called": 0}

    def _insert_pause(strategy, reason, paused_drawdown=None, unpause_after=None):
        inserted["called"] += 1
        return True  # first insert wins
    monkeypatch.setattr("data.storage.insert_pause_if_absent", _insert_pause)

    assert is_paused(mock_balanced_strategy) is True
    assert inserted["called"] == 1


def test_is_paused_does_not_trip_within_threshold(monkeypatch, mock_balanced_strategy):
    """Drawdown of -8% does NOT trip the -10% balanced threshold."""
    monkeypatch.setattr("data.storage.get_strategy_pause", lambda name: None)
    snaps = [
        {"account_equity": 92_000},   # newest, -8% from 100k peak
        {"account_equity": 95_000},
        {"account_equity": 98_000},
        {"account_equity": 100_000},  # peak
        {"account_equity": 99_000},
    ]
    monkeypatch.setattr("data.storage.get_recent_snapshots", lambda name, limit=30: snaps)
    monkeypatch.setattr("data.storage.insert_pause_if_absent",
                        lambda *a, **kw: pytest.fail("should not insert pause"))
    assert is_paused(mock_balanced_strategy) is False


# ─── insert_pause_if_absent race (NEW5 fix) ───────────

def test_insert_pause_if_absent_only_first_returns_true(storage_capture, mock_aggressive_strategy):
    """Concurrent callers all observe pre-pause state, all attempt INSERT.
    Only one returns True — the others see ON CONFLICT and return False.
    Ensures log_circuit_breaker fires exactly once.
    """
    from data.storage import insert_pause_if_absent
    first = insert_pause_if_absent("aggressive", "drawdown_threshold", paused_drawdown=-0.16)
    second = insert_pause_if_absent("aggressive", "drawdown_threshold", paused_drawdown=-0.16)
    third = insert_pause_if_absent("aggressive", "drawdown_threshold", paused_drawdown=-0.16)
    assert first is True
    assert second is False
    assert third is False
    # The pause row is in place
    assert storage_capture["pause_state"]["aggressive"]["paused_reason"] == "drawdown_threshold"

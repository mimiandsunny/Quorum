"""Circuit breaker tests for per-strategy drawdown pauses."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import threading

import pytest

from agents.strategies import is_paused


def test_manual_pause_row_blocks_strategy(monkeypatch, mock_balanced_strategy):
    monkeypatch.setattr(
        "data.storage.get_strategy_pause",
        lambda name: {"strategy": name, "paused_reason": "manual", "unpause_after": None},
    )
    monkeypatch.setattr("data.storage.get_recent_snapshots", lambda name, limit=30: [])

    assert is_paused(mock_balanced_strategy) is True


def test_expired_pause_row_falls_through_to_snapshot_check(monkeypatch, mock_balanced_strategy):
    expired = datetime.now(timezone.utc) - timedelta(minutes=1)
    monkeypatch.setattr(
        "data.storage.get_strategy_pause",
        lambda name: {"strategy": name, "paused_reason": "manual", "unpause_after": expired},
    )
    monkeypatch.setattr("data.storage.get_recent_snapshots", lambda name, limit=30: [])

    assert is_paused(mock_balanced_strategy) is False


def test_insufficient_snapshots_fail_open(monkeypatch, mock_balanced_strategy):
    monkeypatch.setattr("data.storage.get_strategy_pause", lambda name: None)
    monkeypatch.setattr(
        "data.storage.get_recent_snapshots",
        lambda name, limit=30: [{"account_equity": 100_000} for _ in range(4)],
    )

    assert is_paused(mock_balanced_strategy) is False


def test_drawdown_below_threshold_inserts_pause(monkeypatch, mock_balanced_strategy):
    inserted = []
    monkeypatch.setattr("data.storage.get_strategy_pause", lambda name: None)
    monkeypatch.setattr(
        "data.storage.get_recent_snapshots",
        lambda name, limit=30: [
            {"account_equity": 88_000},
            {"account_equity": 92_000},
            {"account_equity": 95_000},
            {"account_equity": 100_000},
            {"account_equity": 99_000},
        ],
    )

    def _insert_pause(strategy, reason, paused_drawdown=None, unpause_after=None):
        inserted.append((strategy, reason, paused_drawdown, unpause_after))
        return True

    monkeypatch.setattr("data.storage.insert_pause_if_absent", _insert_pause)

    assert is_paused(mock_balanced_strategy) is True
    assert inserted == [
        ("balanced", "drawdown_threshold", pytest.approx(-0.12), None)
    ]


def test_drawdown_within_threshold_does_not_insert(monkeypatch, mock_balanced_strategy):
    monkeypatch.setattr("data.storage.get_strategy_pause", lambda name: None)
    monkeypatch.setattr(
        "data.storage.get_recent_snapshots",
        lambda name, limit=30: [
            {"account_equity": 92_000},
            {"account_equity": 95_000},
            {"account_equity": 98_000},
            {"account_equity": 100_000},
            {"account_equity": 99_000},
        ],
    )
    monkeypatch.setattr(
        "data.storage.insert_pause_if_absent",
        lambda *args, **kwargs: pytest.fail("pause insert should not run"),
    )

    assert is_paused(mock_balanced_strategy) is False


def test_losing_insert_race_still_reports_paused(monkeypatch, mock_balanced_strategy):
    monkeypatch.setattr("data.storage.get_strategy_pause", lambda name: None)
    monkeypatch.setattr(
        "data.storage.get_recent_snapshots",
        lambda name, limit=30: [
            {"account_equity": 88_000},
            {"account_equity": 92_000},
            {"account_equity": 95_000},
            {"account_equity": 100_000},
            {"account_equity": 99_000},
        ],
    )
    monkeypatch.setattr("data.storage.insert_pause_if_absent", lambda *a, **kw: False)

    assert is_paused(mock_balanced_strategy) is True


class _Result:
    def __init__(self, row=None):
        self._row = row

    def fetchone(self):
        return self._row


class _PauseRaceState:
    def __init__(self, parties: int):
        self.barrier = threading.Barrier(parties, timeout=5)
        self.lock = threading.Lock()
        self.inserted = set()
        self.attempts = []
        self.commits = 0


class _PauseRaceConnection:
    def __init__(self, state: _PauseRaceState):
        self.state = state

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, statement, params=None):
        if "INSERT INTO strategy_pause_state" not in statement:
            raise AssertionError(f"unexpected SQL: {statement}")

        strategy = params[0]
        self.state.attempts.append(params)
        self.state.barrier.wait()
        with self.state.lock:
            if strategy in self.state.inserted:
                return _Result(None)
            self.state.inserted.add(strategy)
            return _Result({"strategy": strategy})

    def commit(self):
        with self.state.lock:
            self.state.commits += 1


def test_insert_pause_if_absent_three_way_concurrent_insert_race(monkeypatch):
    """Three simultaneous INSERT attempts should produce exactly one winner."""
    from data import storage

    race_state = _PauseRaceState(parties=3)
    monkeypatch.setattr(
        storage,
        "get_connection",
        lambda: _PauseRaceConnection(race_state),
    )

    def _attempt():
        return storage.insert_pause_if_absent(
            "balanced",
            "drawdown_threshold",
            paused_drawdown=-0.12,
        )

    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(lambda _: _attempt(), range(3)))

    assert sorted(results) == [False, False, True]
    assert len(race_state.attempts) == 3
    assert race_state.inserted == {"balanced"}
    assert race_state.commits == 3

"""Equity snapshot tests for the paper-trade reconciler."""

from datetime import date
from types import SimpleNamespace


def _account(equity=101_250, cash=80_000, long_value=20_000, short_value=-1_250):
    return SimpleNamespace(
        equity=equity,
        cash=cash,
        long_market_value=long_value,
        short_market_value=short_value,
    )


def _client(account):
    return SimpleNamespace(get_account=lambda: account)


def test_first_snapshot_seeds_yesterday_then_today(monkeypatch, mock_balanced_strategy):
    from agents import paper_reconciler

    inserted = []
    today = date(2026, 4, 27)
    monkeypatch.setattr(
        paper_reconciler,
        "_get_client_for",
        lambda strategy: _client(_account(equity=100_000, cash=90_000, long_value=8_000, short_value=-2_000)),
    )
    monkeypatch.setattr(paper_reconciler, "get_latest_snapshot", lambda strategy: None)
    monkeypatch.setattr(
        paper_reconciler,
        "insert_equity_snapshot",
        lambda snapshot: inserted.append(snapshot) or len(inserted),
    )

    paper_reconciler._snapshot_one_strategy(mock_balanced_strategy, today)

    assert [s.snapshot_date for s in inserted] == [date(2026, 4, 26), today]
    assert [s.strategy for s in inserted] == ["balanced", "balanced"]
    assert [s.account_equity for s in inserted] == [100_000, 100_000]
    assert [s.positions_value for s in inserted] == [10_000, 10_000]
    assert [s.daily_pnl for s in inserted] == [0.0, 0.0]


def test_subsequent_snapshot_uses_prior_equity_for_daily_pnl(
    monkeypatch, mock_aggressive_strategy
):
    from agents import paper_reconciler

    inserted = []
    today = date(2026, 4, 27)
    monkeypatch.setattr(
        paper_reconciler,
        "_get_client_for",
        lambda strategy: _client(_account(equity=101_250, cash=80_000, long_value=20_000, short_value=-1_250)),
    )
    monkeypatch.setattr(
        paper_reconciler,
        "get_latest_snapshot",
        lambda strategy: {"account_equity": 100_000},
    )
    monkeypatch.setattr(
        paper_reconciler,
        "insert_equity_snapshot",
        lambda snapshot: inserted.append(snapshot) or len(inserted),
    )

    paper_reconciler._snapshot_one_strategy(mock_aggressive_strategy, today)

    assert len(inserted) == 1
    snapshot = inserted[0]
    assert snapshot.strategy == "aggressive"
    assert snapshot.snapshot_date == today
    assert snapshot.account_equity == 101_250
    assert snapshot.positions_value == 21_250
    assert snapshot.daily_pnl == 1_250


def test_duplicate_today_snapshot_does_not_error(monkeypatch, mock_balanced_strategy):
    from agents import paper_reconciler

    inserted = []
    monkeypatch.setattr(
        paper_reconciler,
        "_get_client_for",
        lambda strategy: _client(_account(equity=100_500)),
    )
    monkeypatch.setattr(
        paper_reconciler,
        "get_latest_snapshot",
        lambda strategy: {"account_equity": 100_000},
    )
    monkeypatch.setattr(
        paper_reconciler,
        "insert_equity_snapshot",
        lambda snapshot: inserted.append(snapshot) and None,
    )

    paper_reconciler._snapshot_one_strategy(mock_balanced_strategy, date(2026, 4, 27))

    assert len(inserted) == 1
    assert inserted[0].daily_pnl == 500


def test_snapshot_equity_isolates_strategy_failures(monkeypatch, mock_strategies):
    from agents import paper_reconciler

    calls = []
    monkeypatch.setattr(paper_reconciler, "enabled_strategies", lambda: mock_strategies)

    def _snapshot(strategy, snapshot_date):
        calls.append(strategy.name)
        if strategy.name == "balanced":
            raise RuntimeError("account unavailable")

    monkeypatch.setattr(paper_reconciler, "_snapshot_one_strategy", _snapshot)

    paper_reconciler.snapshot_equity()

    assert calls == ["aggressive", "balanced", "conservative"]


def test_maybe_seed_snapshots_on_boot_only_seeds_missing_strategies(
    monkeypatch, mock_strategies
):
    from agents import paper_reconciler

    seeded = []
    monkeypatch.setattr(paper_reconciler, "enabled_strategies", lambda: mock_strategies)
    monkeypatch.setattr(
        paper_reconciler,
        "get_latest_snapshot",
        lambda strategy: None if strategy in {"aggressive", "conservative"} else {"id": 1},
    )
    monkeypatch.setattr(
        paper_reconciler,
        "_snapshot_one_strategy",
        lambda strategy, snapshot_date: seeded.append(strategy.name),
    )

    paper_reconciler.maybe_seed_snapshots_on_boot()

    assert seeded == ["aggressive", "conservative"]

from datetime import date, datetime
import json

from data import storage
from data.models import (
    DataSnapshot,
    Decision,
    Recommendation,
    RecommendationScore,
    RecommendationSide,
    RiskVerdict,
)


class _FakeConnection:
    def __init__(self, rows=None):
        self.calls = []
        self.committed = False
        self.rows = rows or []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, statement, params=None):
        self.calls.append((statement, params))
        return self

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def commit(self):
        self.committed = True


class _SequentialConnection:
    def __init__(self, result_sets):
        self.calls = []
        self.result_sets = list(result_sets)
        self.current = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, statement, params=None):
        self.calls.append((statement, params))
        self.current = self.result_sets.pop(0) if self.result_sets else []
        return self

    def fetchone(self):
        return self.current[0] if self.current else None

    def fetchall(self):
        return self.current


def test_save_data_snapshot_persists_immutable_payload(monkeypatch):
    conn = _FakeConnection()
    monkeypatch.setattr(storage, "get_connection", lambda: conn)

    snapshot = DataSnapshot(
        snapshot_id="snap-1",
        run_id="run-1",
        ticker="NVDA",
        captured_at=datetime(2026, 4, 28, 9, 30),
        price_payload={"technicals": {"current_price": 100}},
        data_quality_flags=["missing_sector"],
    )

    assert storage.save_data_snapshot(snapshot) == "snap-1"

    statement, params = conn.calls[0]
    assert "INSERT INTO data_snapshots" in statement
    assert "ON CONFLICT (snapshot_id) DO NOTHING" in statement
    assert params[0] == "snap-1"
    assert params[2] == "NVDA"
    assert conn.committed is True


def test_get_analyst_reports_returns_decoded_payload(monkeypatch):
    payload = {"technical": {"trend": "bullish"}}
    conn = _FakeConnection(rows=[{"reports": json.dumps(payload)}])
    monkeypatch.setattr(storage, "get_connection", lambda: conn)

    result = storage.get_analyst_reports("NVDA", date(2026, 4, 28))

    statement, params = conn.calls[0]
    assert "SELECT reports FROM analyst_reports" in statement
    assert params == ("NVDA", date(2026, 4, 28))
    assert result == payload


def test_save_recommendation_is_insert_only(monkeypatch):
    conn = _FakeConnection()
    monkeypatch.setattr(storage, "get_connection", lambda: conn)

    recommendation = Recommendation(
        recommendation_id="rec-1",
        run_id="run-1",
        snapshot_id="snap-1",
        ticker="NVDA",
        created_at=datetime(2026, 4, 28, 10, 0),
        horizon_days=5,
        decision=Decision.BUY,
        side=RecommendationSide.LONG,
        confidence=0.72,
        entry_zone=[100.0, 102.0],
        stop_loss=96.0,
        targets=[112.0],
        thesis="Momentum with clean risk.",
        invalidation="Close below support.",
        risk_verdict=RiskVerdict.APPROVED,
        risk_reasons=[],
    )

    assert storage.save_recommendation(recommendation) == "rec-1"

    statement, params = conn.calls[0]
    assert "INSERT INTO recommendations" in statement
    assert "ON CONFLICT" not in statement
    assert params[0] == "rec-1"
    assert params[7] == "BUY"
    assert params[8] == "long"
    assert conn.committed is True


def test_get_unscored_recommendations_reads_missing_scores(monkeypatch):
    row = {
        "recommendation_id": "rec-1",
        "ticker": "NVDA",
        "created_at": datetime(2026, 4, 28, 10, 0),
    }
    conn = _FakeConnection(rows=[row])
    monkeypatch.setattr(storage, "get_connection", lambda: conn)

    result = storage.get_unscored_recommendations(days_back=12)

    statement, params = conn.calls[0]
    assert "FROM recommendations r" in statement
    assert "LEFT JOIN recommendation_scores rs" in statement
    assert "rs.recommendation_id IS NULL" in statement
    assert params == (12,)
    assert result == [row]


def test_get_latest_recommendations_left_joins_scores(monkeypatch):
    row = {
        "recommendation_id": "rec-1",
        "ticker": "NVDA",
        "outcome_score": 0.8,
    }
    conn = _FakeConnection(rows=[row])
    monkeypatch.setattr(storage, "get_connection", lambda: conn)

    result = storage.get_latest_recommendations(limit=25, ticker="NVDA")

    statement, params = conn.calls[0]
    assert "FROM recommendations r" in statement
    assert "LEFT JOIN recommendation_scores rs" in statement
    assert "rs.score AS outcome_score" in statement
    assert "WHERE (%s::text IS NULL OR r.ticker = %s)" in statement
    assert params == ("NVDA", "NVDA", 25)
    assert result == [row]


def test_get_latest_recommendations_no_ticker_filter_casts_param(monkeypatch):
    """Regression: psycopg fails with IndeterminateDatatype if NULL is sent
    without a type cast against `IS NULL`. The default API call (no ticker
    filter) hit /api/recommendations 500 in production until ::text was added.
    """
    conn = _FakeConnection(rows=[])
    monkeypatch.setattr(storage, "get_connection", lambda: conn)

    storage.get_latest_recommendations(limit=10, ticker=None)

    statement, params = conn.calls[0]
    assert "%s::text IS NULL" in statement
    assert params == (None, None, 10)


def test_get_recommendations_by_date_returns_normalized_v2_rows(monkeypatch):
    row = {
        "recommendation_id": "rec-1",
        "ticker": "NVDA",
        "decision": "BUY",
        "risk_verdict": "APPROVED",
        "entry_zone": "[100, 101]",
        "targets": "[110, 120]",
        "alpha_outputs": '[{"strategy_type": "short_term", "direction": "long", "confidence": 0.7}]',
        "committee_outputs": "{}",
        "risk_reasons": "[]",
        "model_versions": "{}",
        "target_hit": "[true]",
    }
    conn = _FakeConnection(rows=[row])
    monkeypatch.setattr(storage, "get_connection", lambda: conn)

    result = storage.get_recommendations_by_date(date(2026, 4, 28))

    statement, params = conn.calls[0]
    assert "WHERE r.created_at::date = %s" in statement
    assert "PARTITION BY r.ticker" in statement
    assert "WHERE ticker_rank = 1" in statement
    assert params == (date(2026, 4, 28),)
    assert result[0]["entry_zone"] == [100, 101]
    assert result[0]["targets"] == [110, 120]
    assert result[0]["alpha_outputs"][0]["direction"] == "long"
    assert result[0]["target_hit"] == [True]


def test_get_recommendation_score_history_joins_recommendations(monkeypatch):
    row = {
        "recommendation_id": "rec-1",
        "ticker": "NVDA",
        "score": 0.8,
    }
    conn = _FakeConnection(rows=[row])
    monkeypatch.setattr(storage, "get_connection", lambda: conn)

    result = storage.get_recommendation_score_history(days=30, limit=75)

    statement, params = conn.calls[0]
    assert "FROM recommendation_scores rs" in statement
    assert "JOIN recommendations r" in statement
    assert "WHERE rs.score_date >= CURRENT_DATE - %s" in statement
    assert "LIMIT %s" in statement
    assert params == (30, 75)
    assert result == [row]


def test_get_recommendation_audit_detail_assembles_nested_payload(monkeypatch):
    recommendation = {
        "recommendation_id": "rec-1",
        "snapshot_id": "snap-1",
        "ticker": "NVDA",
    }
    score = {"recommendation_id": "rec-1", "score": 0.8}
    snapshot = {"snapshot_id": "snap-1", "feature_payload": {"returns": {"20d": 0.1}}}
    paper_trade = {"recommendation_id": "rec-1", "strategy": "balanced"}
    conn = _SequentialConnection([
        [recommendation],
        [score],
        [snapshot],
        [paper_trade],
    ])
    monkeypatch.setattr(storage, "get_connection", lambda: conn)

    detail = storage.get_recommendation_audit_detail("rec-1")

    assert detail == {
        "recommendation": recommendation,
        "snapshot": snapshot,
        "score": score,
        "paper_trades": [paper_trade],
    }
    assert "FROM recommendations" in conn.calls[0][0]
    assert "FROM recommendation_scores" in conn.calls[1][0]
    assert "FROM data_snapshots" in conn.calls[2][0]
    assert "FROM paper_trades pt" in conn.calls[3][0]


def test_get_recommendation_audit_detail_returns_none_when_missing(monkeypatch):
    conn = _SequentialConnection([[]])
    monkeypatch.setattr(storage, "get_connection", lambda: conn)

    assert storage.get_recommendation_audit_detail("missing") is None
    assert len(conn.calls) == 1


def test_save_recommendation_score_upserts_outcome(monkeypatch):
    conn = _FakeConnection()
    monkeypatch.setattr(storage, "get_connection", lambda: conn)

    score = RecommendationScore(
        recommendation_id="rec-1",
        score_date=date(2026, 5, 3),
        side_return_pct=0.08,
        benchmark_return_pct=0.02,
        sector_return_pct=0.03,
        excess_return_pct=0.06,
        mae_pct=-0.01,
        mfe_pct=0.11,
        stop_hit=False,
        target_hit=[True, False],
        confidence_bucket="65-75",
        score=0.8,
    )

    storage.save_recommendation_score(score)

    statement, params = conn.calls[0]
    assert "INSERT INTO recommendation_scores" in statement
    assert "ON CONFLICT (recommendation_id) DO UPDATE" in statement
    assert params[0] == "rec-1"
    assert params[1] == date(2026, 5, 3)
    assert params[9] == "[true, false]"
    assert params[10] == "65-75"
    assert conn.committed is True


def test_get_paper_trades_by_recommendation_returns_execution_rows(monkeypatch):
    row = {
        "recommendation_id": "rec-1",
        "strategy": "balanced",
        "status": "filled",
        "avg_entry": 99,
    }
    conn = _FakeConnection(rows=[row])
    monkeypatch.setattr(storage, "get_connection", lambda: conn)

    result = storage.get_paper_trades_by_recommendation("rec-1")

    statement, params = conn.calls[0]
    assert "FROM paper_trades pt" in statement
    assert "LEFT JOIN paper_positions pp" in statement
    assert "WHERE pt.recommendation_id = %s" in statement
    assert params == ("rec-1",)
    assert result == [row]


def test_get_paper_trade_by_signal_strategy_prefers_broker_linked_row(monkeypatch):
    row = {
        "id": 2,
        "ticker": "NVDA",
        "signal_date": date(2026, 4, 28),
        "strategy": "balanced",
        "alpaca_order_id": "alpaca-1",
    }
    conn = _FakeConnection(rows=[row])
    monkeypatch.setattr(storage, "get_connection", lambda: conn)

    result = storage.get_paper_trade_by_signal_strategy(
        "NVDA",
        date(2026, 4, 28),
        "balanced",
    )

    statement, params = conn.calls[0]
    assert "WHERE ticker = %s" in statement
    assert "CASE WHEN alpaca_order_id IS NOT NULL THEN 0 ELSE 1 END" in statement
    assert params == ("NVDA", date(2026, 4, 28), "balanced")
    assert result == row


def test_get_recommendation_calibration_aggregates_score_buckets(monkeypatch):
    row = {
        "strategy_type": "short_term",
        "horizon_days": 5,
        "side": "long",
        "confidence_bucket": "65-75",
        "total": 4,
        "avg_confidence": 0.7,
        "win_rate": 0.75,
        "outperform_rate": 0.5,
        "avg_side_return_pct": 0.04,
        "avg_excess_return_pct": 0.02,
        "avg_score": 0.7,
    }
    conn = _FakeConnection(rows=[row])
    monkeypatch.setattr(storage, "get_connection", lambda: conn)

    result = storage.get_recommendation_calibration(days=45, min_samples=3)

    statement, params = conn.calls[0]
    assert "FROM recommendation_scores rs" in statement
    assert "JOIN recommendations r" in statement
    assert "GROUP BY" in statement
    assert "HAVING COUNT(*) >= %s" in statement
    assert params == (45, 3)
    assert result == [row]


def test_get_recommendation_dashboard_summary_normalizes_row(monkeypatch):
    row = {
        "total": 5,
        "approved": 4,
        "rejected": 1,
        "scored": 3,
        "filled": 2,
        "failed": 1,
        "skipped": 4,
        "avg_confidence": 0.72,
        "avg_target_weight": 0.018,
        "avg_score": 0.81,
        "avg_side_return_pct": 0.03,
        "avg_excess_return_pct": 0.02,
    }
    conn = _FakeConnection(rows=[row])
    monkeypatch.setattr(storage, "get_connection", lambda: conn)

    result = storage.get_recommendation_dashboard_summary(days=21)

    statement, params = conn.calls[0]
    assert "WITH recent AS" in statement
    assert "execution_summary" in statement
    assert params == (21,)
    assert result["days"] == 21
    assert result["total"] == 5
    assert result["avg_excess_return_pct"] == 0.02


def test_get_recommendation_track_records_groups_by_regime(monkeypatch):
    row = {
        "ticker": "NVDA",
        "strategy_type": "short_term",
        "side": "long",
        "regime": "risk-on",
        "total": 4,
        "avg_confidence": 0.7,
        "win_rate": 0.75,
        "outperform_rate": 0.5,
        "avg_side_return_pct": 0.04,
        "avg_excess_return_pct": 0.02,
        "avg_score": 0.8,
    }
    conn = _FakeConnection(rows=[row])
    monkeypatch.setattr(storage, "get_connection", lambda: conn)

    result = storage.get_recommendation_track_records(days=90, min_samples=4)

    statement, params = conn.calls[0]
    assert "LEFT JOIN data_snapshots ds" in statement
    assert "macro_payload->'regime'->>'regime'" in statement
    assert "HAVING COUNT(*) >= %s" in statement
    assert params == (90, 4)
    assert result == [row]

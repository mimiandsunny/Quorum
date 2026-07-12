from datetime import date

import pytest
from fastapi import HTTPException

import main
from data.models import Decision, FinalSignal, RiskVerdict


def test_dashboard_recommendation_filter_matches_signal_filters():
    rows = [
        {"ticker": "NVDA", "decision": "BUY", "risk_verdict": "APPROVED"},
        {"ticker": "AMD", "decision": "BUY", "risk_verdict": "REJECTED"},
        {"ticker": "TSLA", "decision": "SELL", "risk_verdict": "APPROVED"},
        {"ticker": "PLTR", "decision": "HOLD", "risk_verdict": "REJECTED"},
    ]

    assert [row["ticker"] for row in main._filter_recommendations(rows, "BUY")] == ["NVDA", "AMD"]
    assert [row["ticker"] for row in main._filter_recommendations(rows, "APPROVED")] == ["NVDA", "TSLA"]
    assert [row["ticker"] for row in main._filter_recommendations(rows, "REJECTED")] == ["AMD", "PLTR"]
    assert main._filter_recommendations(rows, "all") == rows


def test_dashboard_template_renders_v2_cards_without_legacy_signals():
    html = main.templates.get_template("dashboard.html").render(
        signals=[],
        recommendations=[{
            "recommendation_id": "rec-123456789",
            "ticker": "NVDA",
            "decision": "BUY",
            "side": "long",
            "risk_verdict": "APPROVED",
            "strategy_type": "short_term",
            "horizon_days": 5,
            "confidence": 0.72,
            "portfolio_target_weight": 0.02,
            "expected_return": 0.05,
            "expected_drawdown": -0.02,
            "entry_zone": [100.0, 101.0],
            "stop_loss": 96.0,
            "targets": [110.0],
            "thesis": "Momentum setup with controlled risk.",
            "risk_reasons": [],
            "execution_status": None,
            "execution_return_pct": None,
            "paper_trades_by_strategy": {
                "balanced": {"status": "submitted", "status_reason": None},
            },
            "alpha_outputs": [
                {
                    "strategy_type": "short_term",
                    "direction": "long",
                    "confidence": 0.7,
                }
            ],
            "outcome_score": None,
        }],
        report_date="2026-04-28",
        filter="all",
        score_map={},
        leaderboard=[],
        changelog=[],
        concentration_warnings=[],
        run_status=None,
        exec_stats={},
        recommendation_summary={},
        paper_trades_by_ticker={},
        strategy_summary={},
        strategies_meta=[],
        debate_by_ticker={},
        options_dashboard={"snapshots": [], "candidates": [], "summary": {}},
        option_refresh_tickers="NVDA",
        has_digest=False,
        digest_date=None,
    )

    assert "V2 Recommendation Ledger (1)" in html
    assert "rec-1234" in html
    assert "Risk Gate" in html
    assert "BAL submitted" in html
    assert "Momentum setup with controlled risk." in html
    assert "?report_date=2026-04-28&filter=BUY" in html
    assert "No recommendations for 2026-04-28" not in html


def test_dashboard_template_keeps_rejected_signal_geometry_visible():
    signal = FinalSignal(
        ticker="BEAM",
        date=date(2026, 4, 29),
        decision=Decision.SELL,
        confidence=0.54,
        entry_zone=[30.50, 30.60],
        stop_loss=32.60,
        targets=[24.73, 21.90],
        invalidation="Above resistance.",
        holding_period_days=5,
        thesis="Short setup near nearby resistance.",
        bull_case="Bull",
        bear_case="Bear",
        risk_verdict=RiskVerdict.REJECTED,
        risk_reasons=["Confidence 0.54 below minimum 0.65"],
        position_size_pct=0.0,
        reward_risk_ratio=2.8,
    )

    html = main.templates.get_template("dashboard.html").render(
        signals=[signal],
        recommendations=[],
        report_date="2026-04-29",
        filter="all",
        score_map={},
        leaderboard=[],
        changelog=[],
        concentration_warnings=[],
        run_status=None,
        exec_stats={},
        recommendation_summary={},
        paper_trades_by_ticker={},
        strategy_summary={},
        strategies_meta=[],
        debate_by_ticker={},
        options_dashboard={"snapshots": [], "candidates": [], "summary": {}},
        option_refresh_tickers="BEAM",
        has_digest=False,
        digest_date=None,
    )

    assert "SELL blocked" in html
    assert "$30.50–$30.60" in html
    assert "$32.60" in html
    assert "$24.73, $21.90" in html
    assert "2.8x" in html


def test_dashboard_template_renders_options_cockpit():
    html = main.templates.get_template("dashboard.html").render(
        signals=[],
        recommendations=[],
        report_date="2026-04-29",
        filter="all",
        score_map={},
        leaderboard=[],
        changelog=[],
        concentration_warnings=[],
        run_status=None,
        exec_stats={},
        recommendation_summary={},
        paper_trades_by_ticker={},
        strategy_summary={},
        strategies_meta=[],
        debate_by_ticker={},
        options_dashboard={
            "snapshots": [{"ticker": "NVDA"}],
            "summary": {
                "snapshot_count": 1,
                "candidate_count": 1,
                "unusual_flow_count": 1,
                "cheap_vol_count": 0,
                "rich_vol_count": 1,
            },
            "candidates": [{
                "ticker": "NVDA",
                "contract_symbol": "NVDA260515C00105000",
                "option_type": "call",
                "dte": 16,
                "strike": 105.0,
                "mid": 2.05,
                "implied_volatility": 0.55,
                "iv_label": "rich",
                "spread_pct": 0.05,
                "volume_oi_ratio": 7.5,
                "premium_dollars": 307500.0,
                "rank_score": 0.82,
                "tags": ["unusual_flow", "rich_vol"],
            }],
        },
        option_refresh_tickers="NVDA",
        has_digest=False,
        digest_date=None,
    )

    assert "Options Cockpit" in html
    assert "NVDA260515C00105000" in html
    assert "unusual flow" in html
    assert "rich vol" in html


@pytest.mark.asyncio
async def test_api_recommendations_returns_latest_rows(monkeypatch):
    calls = {}

    def _latest(limit, ticker):
        calls["limit"] = limit
        calls["ticker"] = ticker
        return [{"recommendation_id": "rec-1", "ticker": ticker}]

    monkeypatch.setattr("data.storage.get_latest_recommendations", _latest)

    result = await main.api_recommendations(ticker="NVDA", limit=25)

    assert calls == {"limit": 25, "ticker": "NVDA"}
    assert result == [{"recommendation_id": "rec-1", "ticker": "NVDA"}]


@pytest.mark.asyncio
async def test_api_recommendation_detail_returns_audit_payload(monkeypatch):
    payload = {
        "recommendation": {"recommendation_id": "rec-1"},
        "snapshot": {"snapshot_id": "snap-1"},
        "score": {"score": 0.8},
        "paper_trades": [],
    }
    calls = {}

    def _detail(recommendation_id):
        calls["recommendation_id"] = recommendation_id
        return payload

    monkeypatch.setattr("data.storage.get_recommendation_audit_detail", _detail)

    result = await main.api_recommendation_detail("rec-1")

    assert calls == {"recommendation_id": "rec-1"}
    assert result == payload


@pytest.mark.asyncio
async def test_api_recommendation_detail_404_when_missing(monkeypatch):
    monkeypatch.setattr("data.storage.get_recommendation_audit_detail", lambda _id: None)

    with pytest.raises(HTTPException) as exc:
        await main.api_recommendation_detail("missing")

    assert exc.value.status_code == 404
    assert exc.value.detail == "Recommendation not found"


@pytest.mark.asyncio
async def test_api_recommendation_scores_returns_history(monkeypatch):
    calls = {}

    def _history(days, limit):
        calls["days"] = days
        calls["limit"] = limit
        return [{"recommendation_id": "rec-1", "score": 0.8}]

    monkeypatch.setattr("data.storage.get_recommendation_score_history", _history)

    result = await main.api_recommendation_scores(days=45, limit=10)

    assert calls == {"days": 45, "limit": 10}
    assert result == [{"recommendation_id": "rec-1", "score": 0.8}]


@pytest.mark.asyncio
async def test_api_recommendation_calibration_returns_buckets_and_summary(monkeypatch):
    buckets = [{
        "strategy_type": "short_term",
        "side": "long",
        "horizon_days": 5,
        "confidence_bucket": "65-75",
        "total": 4,
        "avg_confidence": 0.7,
        "win_rate": 0.75,
        "outperform_rate": 0.5,
        "avg_side_return_pct": 0.04,
        "avg_excess_return_pct": 0.02,
        "avg_score": 0.8,
    }]
    calls = {}

    def _calibration(days, min_samples):
        calls["days"] = days
        calls["min_samples"] = min_samples
        return buckets

    monkeypatch.setattr("data.storage.get_recommendation_calibration", _calibration)

    result = await main.api_recommendation_calibration(days=120, min_samples=5)

    assert calls == {"days": 120, "min_samples": 5}
    assert result["buckets"] == buckets
    assert "RECOMMENDATION CALIBRATION" in result["summary"]


@pytest.mark.asyncio
async def test_api_recommendation_summary_returns_dashboard_metrics(monkeypatch):
    calls = {}
    summary = {"days": 45, "total": 7, "approved": 5}

    def _summary(days):
        calls["days"] = days
        return summary

    monkeypatch.setattr("data.storage.get_recommendation_dashboard_summary", _summary)

    result = await main.api_recommendation_summary(days=45)

    assert calls == {"days": 45}
    assert result == summary


@pytest.mark.asyncio
async def test_api_recommendation_track_records_returns_grouped_rows(monkeypatch):
    calls = {}
    rows = [{"ticker": "NVDA", "strategy_type": "short_term", "regime": "risk-on"}]

    def _track_records(days, min_samples):
        calls["days"] = days
        calls["min_samples"] = min_samples
        return rows

    monkeypatch.setattr("data.storage.get_recommendation_track_records", _track_records)

    result = await main.api_recommendation_track_records(days=120, min_samples=4)

    assert calls == {"days": 120, "min_samples": 4}
    assert result == rows


@pytest.mark.asyncio
async def test_api_options_dashboard_returns_ranked_rows(monkeypatch):
    calls = {}
    payload = {"snapshots": [], "candidates": [], "summary": {"candidate_count": 0}}

    class _Dashboard:
        def model_dump(self, mode=None):
            calls["mode"] = mode
            return payload

    def _build(**kwargs):
        calls["kwargs"] = kwargs
        return _Dashboard()

    monkeypatch.setattr("options.service.build_options_dashboard", _build)

    result = await main.api_options_dashboard(tickers="nvda, tsla", limit=7)

    assert calls["kwargs"] == {
        "tickers": ["NVDA", "TSLA"],
        "per_ticker_limit": 5,
        "total_limit": 7,
    }
    assert calls["mode"] == "json"
    assert result == payload


@pytest.mark.asyncio
async def test_api_options_refresh_queues_async_job(monkeypatch):
    """Decision 17 — POST /api/options/refresh returns 202 + queued status
    without waiting for the job to finish. The runner task persists its
    own state to option_refresh_jobs; the handler is fire-and-forget.
    """
    import json

    runner_calls: list[dict] = []

    async def _fake_run_chain_refresh(tickers, *, max_expirations, preferred):
        runner_calls.append({
            "tickers": list(tickers),
            "max_expirations": max_expirations,
            "preferred": preferred,
        })

    def _fake_build_preferred_fetcher():
        return object()

    monkeypatch.setattr("options.refresh_job.run_chain_refresh", _fake_run_chain_refresh)
    monkeypatch.setattr(
        "options.refresh_job.build_preferred_fetcher", _fake_build_preferred_fetcher
    )

    response = await main.api_options_refresh(
        main.OptionsRefreshRequest(tickers=["NVDA", "BAD"], max_expirations=2)
    )

    # FastAPI returns the JSONResponse object directly when the handler
    # constructs one (status_code override on the decorator notwithstanding).
    assert response.status_code == 202
    body = json.loads(response.body)
    assert body == {"status": "queued", "tickers": ["NVDA", "BAD"]}

    # Drain the background task so the test isn't leaving asyncio work behind.
    pending = list(main._pending_job_tasks)
    for task in pending:
        await task
    assert runner_calls == [
        {
            "tickers": ["NVDA", "BAD"],
            "max_expirations": 2,
            "preferred": runner_calls[0]["preferred"],
        }
    ]

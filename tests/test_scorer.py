"""Scorer math tests — focuses on the side-correct SELL fix.

Pre-fix bug: actual_return_pct was always (end - entry) / entry (LONG math),
so SELL trades that won (price dropped) recorded NEGATIVE returns and the
downstream agent alignment had to invert the sign — which broke as soon as
anyone changed either side of the contract. This test pins the contract:
positive actual_return_pct == profit on the trade, regardless of side.
"""

from datetime import date, datetime

import pytest

from agents.scorer import (
    _compute_recommendation_score,
    _compute_score,
    _evaluate_agent_alignment,
    _evaluate_agent_alignment_from_reports,
    score_recommendations,
)
from data.models import Decision, FinalSignal, RecommendationSide, RiskVerdict


def _signal(decision, entry_zone, stop_loss, targets, holding_days=5):
    return FinalSignal(
        ticker="NVDA",
        date=date(2026, 4, 25),
        decision=decision,
        confidence=0.7,
        entry_zone=list(entry_zone),
        stop_loss=stop_loss,
        targets=list(targets),
        invalidation="x",
        holding_period_days=holding_days,
        thesis="t",
        bull_case="b",
        bear_case="b",
        risk_verdict=RiskVerdict.APPROVED,
        risk_reasons=[],
        position_size_pct=0.015,
        reward_risk_ratio=2.0,
    )


def _bars(prices):
    """Build a minimal bar list with each price as both H/L/C."""
    return [
        {"date": date(2026, 4, 25 + i), "open": p, "high": p, "low": p, "close": p}
        for i, p in enumerate(prices)
    ]


def _recommendation(side=RecommendationSide.LONG, confidence=0.72):
    decision = {
        RecommendationSide.LONG: Decision.BUY,
        RecommendationSide.SHORT: Decision.SELL,
        RecommendationSide.FLAT: Decision.HOLD,
    }[side]
    return {
        "recommendation_id": "rec-1",
        "ticker": "NVDA",
        "created_at": datetime(2026, 4, 25, 10, 0),
        "horizon_days": 5,
        "decision": decision.value,
        "side": side.value,
        "confidence": confidence,
        "entry_zone": [99, 101],
        "stop_loss": 95 if side != RecommendationSide.SHORT else 110,
        "targets": [110] if side != RecommendationSide.SHORT else [90],
        "benchmark_symbol": "SPY",
        "sector_benchmark_symbol": None,
    }


# ─── Side-correct return math ─────────────────────────

def test_buy_winning_trade_has_positive_return():
    """BUY entered at 100, ends at 110 → +10% profit."""
    sig = _signal(Decision.BUY, (99, 101), stop_loss=95, targets=(110,))
    result = _compute_score(sig, _bars([100, 105, 110]))
    assert result["actual_return_pct"] == pytest.approx(0.10)
    assert result["direction_correct"] is True


def test_buy_losing_trade_has_negative_return():
    """BUY entered at 100, ends at 90 → -10% loss."""
    sig = _signal(Decision.BUY, (99, 101), stop_loss=85, targets=(110,))
    result = _compute_score(sig, _bars([100, 95, 90]))
    assert result["actual_return_pct"] == pytest.approx(-0.10)
    assert result["direction_correct"] is False


def test_sell_winning_trade_has_positive_return():
    """SELL (short) entered at 100, ends at 90 → +10% profit (the fix).

    Before the fix this returned -0.10, which was a real bug — short
    winners were scored as losers in actual_return_pct.
    """
    sig = _signal(Decision.SELL, (99, 101), stop_loss=110, targets=(90,))
    result = _compute_score(sig, _bars([100, 95, 90]))
    assert result["actual_return_pct"] == pytest.approx(0.10)
    assert result["direction_correct"] is True


def test_sell_losing_trade_has_negative_return():
    """SELL entered at 100, ends at 110 → -10% loss (price rose against short)."""
    sig = _signal(Decision.SELL, (99, 101), stop_loss=115, targets=(90,))
    result = _compute_score(sig, _bars([100, 105, 110]))
    assert result["actual_return_pct"] == pytest.approx(-0.10)
    assert result["direction_correct"] is False


def test_hold_uses_long_math_for_diagnostic_return():
    """HOLD's actual_return_pct is reported as long math (used only for the
    flat-price check abs(...) < 0.02 in alignment)."""
    sig = _signal(Decision.HOLD, (99, 101), stop_loss=95, targets=(105,))
    result = _compute_score(sig, _bars([100, 100.5, 101]))
    assert result["actual_return_pct"] == pytest.approx(0.01)


# ─── Agent alignment uses direction_correct symmetrically ──

def test_sell_winning_marks_analyst_aligned():
    """A SELL that won (direction_correct=True) → analysts aligned with outcome."""
    sig = _signal(Decision.SELL, (99, 101), stop_loss=110, targets=(90,))
    alignments = _evaluate_agent_alignment(
        sig, direction_correct=True, actual_return_pct=0.10,
    )
    assert all(alignments.values()), alignments


def test_sell_losing_marks_analyst_not_aligned():
    """A SELL that lost → analysts NOT aligned."""
    sig = _signal(Decision.SELL, (99, 101), stop_loss=115, targets=(90,))
    alignments = _evaluate_agent_alignment(
        sig, direction_correct=False, actual_return_pct=-0.10,
    )
    assert not any(alignments.values()), alignments


def test_buy_winning_marks_analyst_aligned():
    sig = _signal(Decision.BUY, (99, 101), stop_loss=95, targets=(110,))
    alignments = _evaluate_agent_alignment(
        sig, direction_correct=True, actual_return_pct=0.10,
    )
    assert all(alignments.values())


def test_hold_alignment_uses_flat_price_check():
    """HOLD aligned when |actual_return_pct| < 2%."""
    sig = _signal(Decision.HOLD, (99, 101), stop_loss=95, targets=(105,))
    aligned_flat = _evaluate_agent_alignment(sig, False, actual_return_pct=0.005)
    aligned_moved = _evaluate_agent_alignment(sig, False, actual_return_pct=0.05)
    assert all(aligned_flat.values())
    assert not any(aligned_moved.values())


def test_agent_alignment_uses_stored_report_stance_not_final_signal_proxy():
    sig = _signal(Decision.BUY, (99, 101), stop_loss=85, targets=(110,))
    reports = {
        "technical": {"trend": "bearish"},
        "fundamentals": {
            "valuation_assessment": "rich valuation",
            "growth_assessment": "growth slowing",
            "financial_health": "debt risk",
            "sector_comparison": "overvalued",
            "summary": "weak setup",
        },
        "sentiment": {"overall_score": -0.35, "summary": "negative"},
        "news": {"events": [], "macro_context": "risk-off", "summary": "weak negative news"},
    }

    alignments = _evaluate_agent_alignment_from_reports(
        sig,
        reports,
        actual_return_pct=-0.10,
    )

    assert alignments == {
        "technical": True,
        "fundamentals": True,
        "sentiment": True,
        "news": True,
    }


# ─── Recommendation v2 scoring ──────────────────────────

def test_recommendation_score_buy_tracks_benchmark_excess_return():
    rec = _recommendation(RecommendationSide.LONG, confidence=0.72)
    score = _compute_recommendation_score(
        rec,
        bars=[
            {"date": date(2026, 4, 25), "open": 100, "high": 101, "low": 99, "close": 100},
            {"date": date(2026, 4, 26), "open": 104, "high": 112, "low": 103, "close": 110},
        ],
        benchmark_bars=_bars([100, 102]),
        score_date=date(2026, 4, 30),
    )

    assert score.recommendation_id == "rec-1"
    assert score.side_return_pct == pytest.approx(0.10)
    assert score.benchmark_return_pct == pytest.approx(0.02)
    assert score.excess_return_pct == pytest.approx(0.08)
    assert score.target_hit == [True]
    assert score.stop_hit is False
    assert score.confidence_bucket == "65-75"
    assert score.score == pytest.approx(1.0)


def test_recommendation_score_short_profit_and_excursions_are_side_correct():
    rec = _recommendation(RecommendationSide.SHORT, confidence=0.81)
    score = _compute_recommendation_score(
        rec,
        bars=[
            {"date": date(2026, 4, 25), "open": 100, "high": 104, "low": 98, "close": 100},
            {"date": date(2026, 4, 26), "open": 96, "high": 99, "low": 89, "close": 90},
        ],
        benchmark_bars=_bars([100, 101]),
        score_date=date(2026, 4, 30),
    )

    assert score.side_return_pct == pytest.approx(0.10)
    assert score.benchmark_return_pct == pytest.approx(0.01)
    assert score.excess_return_pct == pytest.approx(0.09)
    assert score.mae_pct == pytest.approx(-0.04)
    assert score.mfe_pct == pytest.approx(0.11)
    assert score.target_hit == [True]
    assert score.stop_hit is False
    assert score.confidence_bucket == "75-85"


def test_recommendation_score_records_execution_fill_quality():
    rec = _recommendation(RecommendationSide.LONG, confidence=0.72)
    score = _compute_recommendation_score(
        rec,
        bars=[
            {"date": date(2026, 4, 25), "open": 100, "high": 101, "low": 99, "close": 100},
            {"date": date(2026, 4, 26), "open": 104, "high": 112, "low": 103, "close": 110},
        ],
        paper_trades=[{
            "status": "filled",
            "strategy": "balanced",
            "qty": 10,
            "avg_entry": 99,
        }],
        score_date=date(2026, 4, 30),
    )

    assert score.execution_status == "filled"
    assert score.execution_return_pct == pytest.approx(0.1111)
    assert score.execution_slippage_pct == pytest.approx(0.01)


def test_score_recommendations_waits_for_horizon(monkeypatch):
    saved = []
    monkeypatch.setattr(
        "agents.scorer.get_unscored_recommendations",
        lambda days_back: [{
            **_recommendation(RecommendationSide.LONG),
            "created_at": datetime(2999, 1, 1, 10, 0),
        }],
    )
    monkeypatch.setattr("agents.scorer.save_recommendation_score", saved.append)
    monkeypatch.setattr("agents.scorer.get_paper_trades_by_recommendation", lambda _id: [])

    assert score_recommendations(days_back=10) == []
    assert saved == []


def test_score_recommendations_saves_elapsed_scores(monkeypatch):
    saved = []
    fetches = []

    monkeypatch.setattr(
        "agents.scorer.get_unscored_recommendations",
        lambda days_back: [{
            **_recommendation(RecommendationSide.LONG),
            "created_at": datetime(2020, 1, 1, 10, 0),
        }],
    )

    def fake_fetch(ticker, start, end):
        fetches.append((ticker, start, end))
        if ticker == "SPY":
            return _bars([100, 101])
        return [
            {"date": date(2026, 4, 25), "open": 100, "high": 101, "low": 99, "close": 100},
            {"date": date(2026, 4, 26), "open": 104, "high": 112, "low": 103, "close": 110},
        ]

    monkeypatch.setattr("agents.scorer._fetch_holding_period_data", fake_fetch)
    monkeypatch.setattr("agents.scorer.save_recommendation_score", saved.append)
    monkeypatch.setattr(
        "agents.scorer.get_paper_trades_by_recommendation",
        lambda _id: [{
            "status": "filled",
            "strategy": "balanced",
            "qty": 10,
            "avg_entry": 99,
        }],
    )

    results = score_recommendations(days_back=10)

    assert len(saved) == 1
    assert saved[0].recommendation_id == "rec-1"
    assert saved[0].execution_status == "filled"
    assert results[0]["recommendation_id"] == "rec-1"
    assert results[0]["side_return_pct"] == pytest.approx(0.10)
    assert fetches[0][0] == "NVDA"
    assert fetches[1][0] == "SPY"

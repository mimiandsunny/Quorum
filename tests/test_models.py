from datetime import date

import pytest
from pydantic import ValidationError

from data.models import (
    Decision,
    FinalSignal,
    RiskAssessment,
    RiskVerdict,
    SentimentAnalysis,
    TechnicalAnalysis,
    TechnicalIndicators,
    TraderDecision,
    Trend,
)


def test_technical_indicators_all_fields():
    ti = TechnicalIndicators(
        rsi_14=55.3,
        macd=1.2,
        macd_signal=0.8,
        macd_histogram=0.4,
        ma_50=185.0,
        ma_200=170.0,
        current_price=188.50,
        support_levels=[182.0, 175.0],
        resistance_levels=[192.0, 198.0],
    )
    assert ti.current_price == 188.50
    assert len(ti.support_levels) == 2


def test_technical_indicators_optional_fields():
    ti = TechnicalIndicators(current_price=100.0)
    assert ti.rsi_14 is None
    assert ti.ma_50 is None
    assert ti.support_levels == []


def test_sentiment_score_bounds():
    s = SentimentAnalysis(
        ticker="AAPL", overall_score=0.5, summary="Positive"
    )
    assert s.overall_score == 0.5

    with pytest.raises(ValidationError):
        SentimentAnalysis(ticker="AAPL", overall_score=1.5, summary="Too high")

    with pytest.raises(ValidationError):
        SentimentAnalysis(ticker="AAPL", overall_score=-1.5, summary="Too low")


def test_trader_decision_entry_zone_requires_two():
    with pytest.raises(ValidationError):
        TraderDecision(
            ticker="AAPL",
            date=date(2026, 4, 14),
            decision=Decision.BUY,
            confidence=0.78,
            entry_zone=[185.0],  # needs 2 values
            stop_loss=182.0,
            targets=[192.0],
            invalidation="Close below 180",
            holding_period_days=5,
            thesis="Test",
        )


def test_trader_decision_valid():
    td = TraderDecision(
        ticker="AAPL",
        date=date(2026, 4, 14),
        decision=Decision.BUY,
        confidence=0.78,
        entry_zone=[185.50, 187.00],
        stop_loss=182.0,
        targets=[192.0, 198.0],
        invalidation="Close below 180 on volume",
        holding_period_days=5,
        thesis="Strong momentum with earnings catalyst",
    )
    assert td.decision == Decision.BUY
    assert len(td.targets) == 2


def test_trader_decision_confidence_bounds():
    with pytest.raises(ValidationError):
        TraderDecision(
            ticker="AAPL",
            date=date(2026, 4, 14),
            decision=Decision.BUY,
            confidence=1.5,
            entry_zone=[185.0, 187.0],
            stop_loss=182.0,
            targets=[192.0],
            invalidation="Test",
            holding_period_days=5,
            thesis="Test",
        )


def test_final_signal_roundtrip_json():
    signal = FinalSignal(
        ticker="NVDA",
        date=date(2026, 4, 14),
        decision=Decision.BUY,
        confidence=0.82,
        entry_zone=[850.0, 860.0],
        stop_loss=830.0,
        targets=[900.0, 950.0],
        invalidation="Close below 820",
        holding_period_days=5,
        thesis="AI demand continues",
        bull_case="Data center spending accelerating",
        bear_case="Valuation stretched at 40x",
        risk_verdict=RiskVerdict.APPROVED,
        risk_reasons=[],
        position_size_pct=0.03,
        reward_risk_ratio=2.73,
    )
    json_str = signal.model_dump_json()
    restored = FinalSignal.model_validate_json(json_str)
    assert restored.ticker == "NVDA"
    assert restored.decision == Decision.BUY
    assert restored.confidence == 0.82


def test_technical_analysis_valid():
    ta = TechnicalAnalysis(
        ticker="AAPL",
        trend=Trend.BULLISH,
        key_levels={"support": [182.0], "resistance": [192.0]},
        pattern="Bull flag",
        momentum="Strong upward",
        summary="Price above MA50 and MA200",
    )
    assert ta.trend == Trend.BULLISH


def test_risk_assessment_rejected():
    ra = RiskAssessment(
        ticker="TSLA",
        verdict=RiskVerdict.REJECTED,
        position_size_pct=0.0,
        rejection_reasons=["Confidence 0.55 below minimum 0.65"],
        adjustments=[],
        reward_risk_ratio=1.5,
    )
    assert ra.verdict == RiskVerdict.REJECTED
    assert ra.position_size_pct == 0.0

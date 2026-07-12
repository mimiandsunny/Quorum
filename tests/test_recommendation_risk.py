from datetime import date, datetime

from data.models import (
    AlphaOutput,
    Decision,
    FundamentalsData,
    OHLCVBar,
    RecommendationSide,
    RecommendationStrategyType,
    RiskAssessment,
    RiskVerdict,
    TechnicalIndicators,
    TickerDataPackage,
    TraderDecision,
)
from recommendation.risk import assess_recommendation_risk


def _bars(*, close=100.0, volume=1_000_000, count=60):
    return [
        OHLCVBar(
            date=date(2026, 1, 1),
            open=close,
            high=close + 1,
            low=close - 1,
            close=close,
            volume=volume,
        )
        for _ in range(count)
    ]


def _package(**overrides):
    close = overrides.pop("close", 100.0)
    volume = overrides.pop("volume", 1_000_000)
    count = overrides.pop("count", 60)
    return TickerDataPackage(
        ticker="NVDA",
        fetch_timestamp=datetime(2026, 4, 28, 9, 30),
        price_history=overrides.pop("price_history", _bars(close=close, volume=volume, count=count)),
        technicals=TechnicalIndicators(
            current_price=close,
            rsi_14=55.0,
            macd=1.0,
            macd_signal=0.8,
            macd_histogram=0.2,
            ma_50=95.0,
            ma_200=90.0,
        ),
        fundamentals=FundamentalsData(
            sector=overrides.pop("sector", "Technology"),
            industry=overrides.pop("industry", "Semiconductors"),
        ),
        news=overrides.pop("news", [{"headline": "Test", "source": "Unit"}]),
        stale_sources=overrides.pop("stale_sources", []),
    )


def _decision(decision=Decision.BUY):
    return TraderDecision(
        ticker="NVDA",
        date=date(2026, 4, 28),
        decision=decision,
        confidence=0.78,
        entry_zone=[99.0, 101.0],
        stop_loss=96.0 if decision != Decision.SELL else 104.0,
        targets=[110.0 if decision != Decision.SELL else 90.0],
        invalidation="invalid",
        holding_period_days=5,
        thesis="test",
    )


def _approved(size=0.03):
    return RiskAssessment(
        ticker="NVDA",
        verdict=RiskVerdict.APPROVED,
        position_size_pct=size,
        rejection_reasons=[],
        adjustments=[],
        reward_risk_ratio=2.5,
    )


def test_v2_risk_rejects_severe_data_quality_flags():
    bad_bars = _bars(count=60)
    bad_bars[-1] = bad_bars[-1].model_copy(update={"close": 0.0})

    result = assess_recommendation_risk(
        decision=_decision(),
        data=_package(price_history=bad_bars),
        base_assessment=_approved(),
    )

    assert result.verdict == RiskVerdict.REJECTED
    assert result.position_size_pct == 0.0
    assert any("Severe data quality flags" in reason for reason in result.rejection_reasons)


def test_v2_risk_halves_weak_liquidity_size(monkeypatch):
    monkeypatch.setattr(
        "recommendation.risk.feature_payload",
        lambda _data: {
            "liquidity": {"liquidity_score": 0.20},  # weak but above reject floor
            "risk": {
                "realized_volatility_20d": 0.30,
                "gap_pct": 0.0,
                "max_drawdown_20d": -0.05,
                "atr_14_pct": 0.02,  # 2% ATR — stop=4% comfortably above 1.0x floor
            },
        },
    )

    result = assess_recommendation_risk(
        decision=_decision(),
        data=_package(close=10.0, volume=1_000_000),
        base_assessment=_approved(size=0.04),
    )

    assert result.verdict == RiskVerdict.APPROVED
    assert result.position_size_pct == 0.02
    assert any("weak liquidity" in adjustment for adjustment in result.adjustments)


def test_v2_risk_rejects_strong_alpha_conflict():
    result = assess_recommendation_risk(
        decision=_decision(Decision.BUY),
        data=_package(),
        base_assessment=_approved(),
        alpha_outputs=[
            AlphaOutput(
                strategy_type=RecommendationStrategyType.SHORT_TERM,
                direction=RecommendationSide.SHORT,
                horizon_days=5,
                confidence=0.72,
                evidence=["negative setup"],
            )
        ],
    )

    assert result.verdict == RiskVerdict.REJECTED
    assert any("Deterministic alpha conflict" in reason for reason in result.rejection_reasons)


def test_v2_risk_keeps_soft_data_warnings_as_adjustments():
    result = assess_recommendation_risk(
        decision=_decision(),
        data=_package(news=[], stale_sources=["news"]),
        base_assessment=_approved(),
    )

    assert result.verdict == RiskVerdict.APPROVED
    assert result.position_size_pct == 0.03
    assert any("Data quality warnings" in adjustment for adjustment in result.adjustments)


def test_v2_risk_halves_when_alpha_is_flat_or_weak():
    result = assess_recommendation_risk(
        decision=_decision(Decision.BUY),
        data=_package(),
        base_assessment=_approved(size=0.04),
        alpha_outputs=[
            AlphaOutput(
                strategy_type=RecommendationStrategyType.SHORT_TERM,
                direction=RecommendationSide.FLAT,
                horizon_days=5,
                confidence=0.35,
                evidence=["no edge"],
            )
        ],
    )

    assert result.verdict == RiskVerdict.APPROVED
    assert result.position_size_pct == 0.02
    assert any("flat or weak" in adjustment for adjustment in result.adjustments)


def test_v2_risk_rejects_imminent_event_block():
    result = assess_recommendation_risk(
        decision=_decision(Decision.BUY),
        data=_package(),
        base_assessment=_approved(),
        alpha_outputs=[
            AlphaOutput(
                strategy_type=RecommendationStrategyType.EVENT,
                direction=RecommendationSide.FLAT,
                horizon_days=2,
                confidence=0.68,
                evidence=["Earnings event is 2 day(s) away"],
            )
        ],
    )

    assert result.verdict == RiskVerdict.REJECTED
    assert any("Event risk block" in reason for reason in result.rejection_reasons)


def test_v2_risk_rejects_extreme_gap(monkeypatch):
    monkeypatch.setattr(
        "recommendation.risk.feature_payload",
        lambda _data: {
            "liquidity": {"liquidity_score": 1.0},
            "risk": {
                "realized_volatility_20d": 0.30,
                "gap_pct": 0.09,
                "max_drawdown_20d": -0.05,
            },
        },
    )

    result = assess_recommendation_risk(
        decision=_decision(),
        data=_package(),
        base_assessment=_approved(size=0.04),
    )

    assert result.verdict == RiskVerdict.REJECTED
    assert any("Opening gap" in reason for reason in result.rejection_reasons)


def test_v2_risk_trims_high_volatility_and_deep_drawdown(monkeypatch):
    monkeypatch.setattr(
        "recommendation.risk.feature_payload",
        lambda _data: {
            "liquidity": {"liquidity_score": 1.0},
            "risk": {
                "realized_volatility_20d": 0.85,
                "gap_pct": 0.0,
                "max_drawdown_20d": -0.22,
            },
        },
    )

    result = assess_recommendation_risk(
        decision=_decision(),
        data=_package(),
        base_assessment=_approved(size=0.04),
    )

    assert result.verdict == RiskVerdict.APPROVED
    assert result.position_size_pct == 0.01
    assert any("high realized volatility" in adjustment for adjustment in result.adjustments)
    assert any("max drawdown" in adjustment for adjustment in result.adjustments)


# ─── ATR floor — root-cause fix for systemic tight-stop bug ──────

def _features(atr_14_pct=None, **risk_overrides):
    """Build a feature_payload stub with sane defaults, ATR overridable."""
    risk = {
        "realized_volatility_20d": 0.30,
        "gap_pct": 0.0,
        "max_drawdown_20d": -0.05,
    }
    if atr_14_pct is not None:
        risk["atr_14_pct"] = atr_14_pct
    risk.update(risk_overrides)
    return {"liquidity": {"liquidity_score": 1.0}, "risk": risk}


def test_atr_floor_rejects_stop_inside_one_atr(monkeypatch):
    """The BEAM/AMD class: stop set tighter than 1.0x ATR_14 (default coefficient)."""
    monkeypatch.setattr(
        "recommendation.risk.feature_payload",
        lambda _data: _features(atr_14_pct=0.05),  # 5% ATR, e.g. biotech
    )
    decision = _decision()
    decision = decision.model_copy(update={"stop_loss": 99.0})  # ~0.5% stop on 100 entry

    result = assess_recommendation_risk(
        decision=decision,
        data=_package(),
        base_assessment=_approved(),
    )

    assert result.verdict == RiskVerdict.REJECTED
    assert any("Stop too tight" in r for r in result.rejection_reasons)
    assert any("ATR_14=5.0%" in r for r in result.rejection_reasons)
    assert any("required <= $95.00" in r for r in result.rejection_reasons)


def test_atr_floor_does_not_add_tight_reason_when_stop_is_already_too_wide(monkeypatch):
    """BEAM-style case: the static max-stop rule owns wide stops, so the ATR
    floor should not add the contradictory "too tight" reason too.
    """
    monkeypatch.setattr(
        "recommendation.risk.feature_payload",
        lambda _data: _features(atr_14_pct=0.0705),
    )
    decision = _decision(decision=Decision.SELL).model_copy(
        update={
            "entry_zone": [30.50, 30.60],
            "stop_loss": 32.60,
            "targets": [24.73, 21.90],
        }
    )
    base = RiskAssessment(
        ticker="BEAM",
        verdict=RiskVerdict.REJECTED,
        position_size_pct=0.0,
        rejection_reasons=["Stop loss 6.7% from entry exceeds 5% max"],
        adjustments=[],
        reward_risk_ratio=2.8,
    )

    result = assess_recommendation_risk(
        decision=decision,
        data=_package(),
        base_assessment=base,
    )

    assert result.verdict == RiskVerdict.REJECTED
    assert any("Stop loss 6.7%" in r for r in result.rejection_reasons)
    assert not any("Stop too tight" in r for r in result.rejection_reasons)
    assert any("Target too far" in r for r in result.rejection_reasons)


def test_atr_floor_reports_impossible_stop_window_when_floor_exceeds_max(monkeypatch):
    monkeypatch.setattr(
        "recommendation.risk.feature_payload",
        lambda _data: _features(atr_14_pct=0.0705),
    )
    decision = _decision().model_copy(
        update={
            "entry_zone": [99.0, 101.0],
            "stop_loss": 96.0,
            "targets": [110.0],
        }
    )

    result = assess_recommendation_risk(
        decision=decision,
        data=_package(),
        base_assessment=_approved(),
    )

    assert result.verdict == RiskVerdict.REJECTED
    assert any("Stop window impossible" in r for r in result.rejection_reasons)
    assert not any("Stop too tight" in r for r in result.rejection_reasons)


def test_target_floor_rejects_first_target_inside_minimum_move(monkeypatch):
    """A mathematically acceptable R/R is still a bad swing setup if target1
    is inside the minimum tradable move from entry.
    """
    monkeypatch.setattr(
        "recommendation.risk.feature_payload",
        lambda _data: _features(atr_14_pct=0.005),  # 0.5% ATR; absolute 2% target floor wins
    )
    decision = _decision().model_copy(
        update={
            "entry_zone": [99.0, 101.0],
            "stop_loss": 98.8,   # 1.2% stop passes the 1% stop floor
            "targets": [101.5],  # 1.5% target fails the 2% target floor
        }
    )

    result = assess_recommendation_risk(
        decision=decision,
        data=_package(),
        base_assessment=_approved(),
    )

    assert result.verdict == RiskVerdict.REJECTED
    assert any("Target too close" in r for r in result.rejection_reasons)
    assert any("required >= $102.00" in r for r in result.rejection_reasons)


def test_target_ceiling_rejects_upside_target_too_far_for_swing(monkeypatch):
    monkeypatch.setattr(
        "recommendation.risk.feature_payload",
        lambda _data: _features(atr_14_pct=0.03),  # ceiling max(15%, 9%) = 15%
    )
    decision = _decision().model_copy(
        update={
            "entry_zone": [99.0, 101.0],
            "stop_loss": 96.0,
            "targets": [130.0],
        }
    )

    result = assess_recommendation_risk(
        decision=decision,
        data=_package(),
        base_assessment=_approved(),
    )

    assert result.verdict == RiskVerdict.REJECTED
    assert any("Target too far" in r for r in result.rejection_reasons)
    assert any("limit <= $115.00" in r for r in result.rejection_reasons)


def test_target_ceiling_rejects_downside_target_too_far_for_short(monkeypatch):
    monkeypatch.setattr(
        "recommendation.risk.feature_payload",
        lambda _data: _features(atr_14_pct=0.04),  # ceiling max(15%, 12%) = 15%
    )
    decision = _decision(decision=Decision.SELL).model_copy(
        update={
            "entry_zone": [99.0, 101.0],
            "stop_loss": 104.0,
            "targets": [70.0],
        }
    )

    result = assess_recommendation_risk(
        decision=decision,
        data=_package(),
        base_assessment=_approved(),
    )

    assert result.verdict == RiskVerdict.REJECTED
    assert any("Target too far" in r for r in result.rejection_reasons)
    assert any("limit >= $85.00" in r for r in result.rejection_reasons)


def test_bracket_floor_reasons_are_added_to_base_rejections(monkeypatch):
    """JNJ-style case: low confidence already rejects the trade, but the
    signal should still explain that the short stop is mechanically too tight.
    """
    monkeypatch.setattr(
        "recommendation.risk.feature_payload",
        lambda _data: _features(atr_14_pct=0.018),
    )
    decision = _decision(decision=Decision.SELL).model_copy(
        update={
            "confidence": 0.60,
            "entry_zone": [246.0, 248.0],
            "stop_loss": 248.56,
            "targets": [203.70],
        }
    )
    base = RiskAssessment(
        ticker="NVDA",
        verdict=RiskVerdict.REJECTED,
        position_size_pct=0.0,
        rejection_reasons=["Confidence 0.60 below minimum 0.65"],
        adjustments=[],
        reward_risk_ratio=27.76,
    )

    result = assess_recommendation_risk(
        decision=decision,
        data=_package(),
        base_assessment=base,
    )

    assert result.verdict == RiskVerdict.REJECTED
    assert any("Confidence" in r for r in result.rejection_reasons)
    assert any("Stop too tight" in r for r in result.rejection_reasons)


def test_atr_floor_does_not_flag_stop_above_one_atr_as_tight(monkeypatch):
    """The ATR overlay only owns tight-stop rejections; wide stops belong to
    the base risk manager's max-stop rule.
    """
    monkeypatch.setattr(
        "recommendation.risk.feature_payload",
        lambda _data: _features(atr_14_pct=0.05),
    )
    decision = _decision()
    decision = decision.model_copy(update={"stop_loss": 94.0})  # 6% stop on 100 entry

    result = assess_recommendation_risk(
        decision=decision,
        data=_package(),
        base_assessment=_approved(),
    )

    assert result.verdict == RiskVerdict.APPROVED


def test_atr_floor_falls_back_to_one_percent_when_atr_missing(monkeypatch):
    """SPY-class case: ATR not in payload, but a 0.3% stop is still mechanically
    too tight regardless. Floor falls back to ABSOLUTE_MIN_STOP_PCT (1%)."""
    monkeypatch.setattr(
        "recommendation.risk.feature_payload",
        lambda _data: _features(atr_14_pct=None),
    )
    decision = _decision()
    decision = decision.model_copy(update={"stop_loss": 99.7})  # 0.3% stop

    result = assess_recommendation_risk(
        decision=decision,
        data=_package(),
        base_assessment=_approved(),
    )

    assert result.verdict == RiskVerdict.REJECTED
    assert any("Stop too tight" in r for r in result.rejection_reasons)


def test_atr_floor_disabled_when_coefficient_zero(monkeypatch):
    """Operator escape hatch: paper_atr_floor_coefficient=0 turns the gate off."""
    from config import settings as live
    monkeypatch.setattr(live, "paper_atr_floor_coefficient", 0.0)
    monkeypatch.setattr(
        "recommendation.risk.feature_payload",
        lambda _data: _features(atr_14_pct=0.05),
    )
    decision = _decision()
    decision = decision.model_copy(update={"stop_loss": 99.0})  # would normally be rejected

    result = assess_recommendation_risk(
        decision=decision,
        data=_package(),
        base_assessment=_approved(),
    )

    # Floor disabled → only the absolute 1% min could trip; stop is 1%, so... wait,
    # coefficient=0 disables the gate entirely (early return), so we expect APPROVED.
    assert result.verdict == RiskVerdict.APPROVED


def test_atr_floor_skipped_for_hold_decisions(monkeypatch):
    """HOLD has no execution geometry — the gate must not fire."""
    monkeypatch.setattr(
        "recommendation.risk.feature_payload",
        lambda _data: _features(atr_14_pct=0.10),
    )
    decision = _decision(decision=Decision.HOLD)
    decision = decision.model_copy(update={"stop_loss": 99.0})

    result = assess_recommendation_risk(
        decision=decision,
        data=_package(),
        base_assessment=_approved(),
    )

    assert result.verdict == RiskVerdict.APPROVED

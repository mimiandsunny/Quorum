from __future__ import annotations

from data.models import (
    AlphaOutput,
    RecommendationSide,
    RecommendationStrategyType,
    TickerDataPackage,
)
from recommendation.features import feature_payload
from recommendation.quality import data_quality_flags


def _positive(value: float | None, threshold: float) -> bool:
    return value is not None and value > threshold


def _negative(value: float | None, threshold: float) -> bool:
    return value is not None and value < -threshold


def _confidence_from_score(score: float) -> float:
    return round(min(0.70, 0.44 + abs(score) * 0.06), 2)


def _expected_return_from_score(score: float, volatility: float | None) -> float:
    baseline = 0.018 + min(abs(score), 4.0) * 0.008
    if volatility is not None:
        baseline = max(baseline, min(volatility / 12, 0.08))
    return round(min(baseline, 0.08), 4)


def _expected_drawdown(expected_return: float, volatility: float | None) -> float:
    drawdown = max(expected_return * 0.75, (volatility or 0.0) / 10)
    return round(-min(drawdown, 0.10), 4)


def analyze(pkg: TickerDataPackage) -> AlphaOutput:
    """Systematic 20-day alpha estimate from momentum, trend, risk, and liquidity."""
    flags = data_quality_flags(pkg)
    severe_flags = {"missing_price_history", "nonpositive_price", "negative_volume"}
    if severe_flags.intersection(flags):
        return AlphaOutput(
            strategy_type=RecommendationStrategyType.QUANT,
            direction=RecommendationSide.FLAT,
            horizon_days=20,
            expected_return=0.0,
            expected_drawdown=0.0,
            expected_volatility=None,
            confidence=0.2,
            evidence=[f"Data quality blocks quant alpha: {', '.join(sorted(severe_flags.intersection(flags)))}"],
            invalidation="Restore clean price and volume history.",
        )

    features = feature_payload(pkg)
    returns = features["returns"]
    risk = features["risk"]
    technicals = features["technicals"]
    liquidity = features["liquidity"]

    score = 0.0
    evidence: list[str] = []

    if _positive(returns["20d"], 0.08):
        score += 1.0
        evidence.append(f"20-day momentum is strong at {returns['20d']:.1%}")
    elif _negative(returns["20d"], 0.08):
        score -= 1.0
        evidence.append(f"20-day momentum is weak at {returns['20d']:.1%}")

    if _positive(returns["5d"], 0.025):
        score += 0.6
        evidence.append(f"5-day momentum confirms at {returns['5d']:.1%}")
    elif _negative(returns["5d"], 0.025):
        score -= 0.6
        evidence.append(f"5-day momentum deteriorates at {returns['5d']:.1%}")

    if _positive(technicals["distance_to_ma_50_pct"], 0.02):
        score += 0.45
        evidence.append("Price is meaningfully above the 50-day average")
    elif _negative(technicals["distance_to_ma_50_pct"], 0.02):
        score -= 0.45
        evidence.append("Price is meaningfully below the 50-day average")

    if _positive(technicals["distance_to_ma_200_pct"], 0.05):
        score += 0.35
        evidence.append("Price is above the 200-day trend")
    elif _negative(technicals["distance_to_ma_200_pct"], 0.05):
        score -= 0.35
        evidence.append("Price is below the 200-day trend")

    volatility = risk["realized_volatility_20d"]
    if volatility is not None:
        if volatility <= 0.35:
            score += 0.25
            evidence.append(f"Realized volatility is controlled at {volatility:.1%}")
        elif volatility > 0.80:
            score -= 0.35
            evidence.append(f"Realized volatility is high at {volatility:.1%}")

    liquidity_score = liquidity["liquidity_score"]
    if liquidity_score is not None:
        if liquidity_score >= 0.75:
            score += 0.25
            evidence.append(f"Liquidity score is strong at {liquidity_score:.2f}")
        elif liquidity_score < 0.25:
            score -= 0.35
            evidence.append(f"Liquidity score is weak at {liquidity_score:.2f}")

    if score >= 1.2:
        direction = RecommendationSide.LONG
        invalidation = "Quant view invalidates if 20-day momentum rolls over or price loses the 50-day average."
    elif score <= -1.2:
        direction = RecommendationSide.SHORT
        invalidation = "Quant short view invalidates if momentum recovers above the 50-day average."
    else:
        direction = RecommendationSide.FLAT
        invalidation = "Wait for stronger factor alignment."

    if not evidence:
        evidence.append("Insufficient systematic factor edge")

    expected_return = 0.0
    expected_drawdown = 0.0
    confidence = 0.35
    if direction != RecommendationSide.FLAT:
        confidence = _confidence_from_score(score)
        expected_return = _expected_return_from_score(score, volatility)
        expected_drawdown = _expected_drawdown(expected_return, volatility)

    return AlphaOutput(
        strategy_type=RecommendationStrategyType.QUANT,
        direction=direction,
        horizon_days=20,
        expected_return=expected_return,
        expected_drawdown=expected_drawdown,
        expected_volatility=volatility,
        confidence=confidence,
        evidence=evidence[:6],
        invalidation=invalidation,
    )

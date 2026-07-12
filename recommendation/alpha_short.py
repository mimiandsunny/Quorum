from __future__ import annotations

from data.models import (
    AlphaOutput,
    RecommendationSide,
    RecommendationStrategyType,
    TickerDataPackage,
)
from recommendation.features import feature_payload
from recommendation.quality import data_quality_flags


def _is_positive(value: float | None, threshold: float = 0.0) -> bool:
    return value is not None and value > threshold


def _is_negative(value: float | None, threshold: float = 0.0) -> bool:
    return value is not None and value < -threshold


def _confidence_from_score(score: float) -> float:
    return round(min(0.78, 0.48 + abs(score) * 0.06), 2)


def _expected_return_from_score(score: float, atr_pct: float | None) -> float:
    baseline = 0.018 + min(abs(score), 4.0) * 0.006
    if atr_pct is not None:
        baseline = max(baseline, atr_pct * 1.25)
    return round(min(baseline, 0.07), 4)


def _expected_drawdown(expected_return: float, atr_pct: float | None) -> float:
    drawdown = max(expected_return / 2, (atr_pct or 0.0) * 0.9)
    return round(-min(drawdown, 0.05), 4)


def analyze(pkg: TickerDataPackage) -> AlphaOutput:
    """Conservative deterministic alpha estimate for 1-10 day swing trades."""
    flags = data_quality_flags(pkg)
    severe_flags = {"missing_price_history", "nonpositive_price", "negative_volume"}
    if severe_flags.intersection(flags):
        return AlphaOutput(
            strategy_type=RecommendationStrategyType.SHORT_TERM,
            direction=RecommendationSide.FLAT,
            horizon_days=5,
            expected_return=0.0,
            expected_drawdown=0.0,
            confidence=0.2,
            evidence=[f"Data quality blocks short-term alpha: {', '.join(sorted(severe_flags.intersection(flags)))}"],
            invalidation="Restore clean price and volume history.",
        )

    features = feature_payload(pkg)
    returns = features["returns"]
    risk = features["risk"]
    technicals = features["technicals"]
    liquidity = features["liquidity"]

    score = 0.0
    evidence: list[str] = []

    if _is_positive(returns["20d"], 0.05):
        score += 1.0
        evidence.append(f"20-day return is positive at {returns['20d']:.1%}")
    elif _is_negative(returns["20d"], 0.05):
        score -= 1.0
        evidence.append(f"20-day return is negative at {returns['20d']:.1%}")

    if _is_positive(returns["5d"], 0.02):
        score += 0.75
        evidence.append(f"5-day return is positive at {returns['5d']:.1%}")
    elif _is_negative(returns["5d"], 0.02):
        score -= 0.75
        evidence.append(f"5-day return is negative at {returns['5d']:.1%}")

    if _is_positive(technicals["macd_histogram"], 0.0):
        score += 0.6
        evidence.append("MACD histogram is positive")
    elif _is_negative(technicals["macd_histogram"], 0.0):
        score -= 0.6
        evidence.append("MACD histogram is negative")

    if _is_positive(technicals["distance_to_ma_50_pct"], 0.0):
        score += 0.75
        evidence.append("Price is above the 50-day moving average")
    elif _is_negative(technicals["distance_to_ma_50_pct"], 0.0):
        score -= 0.75
        evidence.append("Price is below the 50-day moving average")

    if _is_positive(technicals["distance_to_ma_200_pct"], 0.0):
        score += 0.5
        evidence.append("Price is above the 200-day moving average")
    elif _is_negative(technicals["distance_to_ma_200_pct"], 0.0):
        score -= 0.5
        evidence.append("Price is below the 200-day moving average")

    rsi = technicals["rsi_14"]
    if rsi is not None:
        if 45 <= rsi <= 70:
            score += 0.35
            evidence.append(f"RSI is constructive but not extreme at {rsi:.1f}")
        elif rsi > 78:
            score -= 0.25
            evidence.append(f"RSI is extended at {rsi:.1f}")
        elif rsi < 35:
            score += 0.15
            evidence.append(f"RSI is oversold at {rsi:.1f}")

    volume_spike = liquidity["volume_spike_20d"]
    if volume_spike is not None and volume_spike >= 1.5:
        score += 0.25 if score >= 0 else -0.25
        evidence.append(f"Volume is elevated at {volume_spike:.1f}x the 20-day average")

    liquidity_score = liquidity["liquidity_score"]
    if liquidity_score is not None and liquidity_score < 0.25:
        score *= 0.7
        evidence.append(f"Liquidity score is weak at {liquidity_score:.2f}")

    if score >= 1.4:
        direction = RecommendationSide.LONG
        invalidation = "Close below the 50-day moving average or nearest support."
    elif score <= -1.4:
        direction = RecommendationSide.SHORT
        invalidation = "Close above the 50-day moving average or nearest resistance."
    else:
        direction = RecommendationSide.FLAT
        invalidation = "Wait for clearer trend, momentum, or relative-strength confirmation."

    if not evidence:
        evidence.append("Insufficient deterministic edge from short-term features")

    expected_return = 0.0
    expected_drawdown = 0.0
    confidence = 0.35
    if direction != RecommendationSide.FLAT:
        confidence = _confidence_from_score(score)
        expected_return = _expected_return_from_score(score, risk["atr_14_pct"])
        expected_drawdown = _expected_drawdown(expected_return, risk["atr_14_pct"])

    return AlphaOutput(
        strategy_type=RecommendationStrategyType.SHORT_TERM,
        direction=direction,
        horizon_days=5,
        expected_return=expected_return,
        expected_drawdown=expected_drawdown,
        expected_volatility=risk["realized_volatility_20d"],
        confidence=confidence,
        evidence=evidence[:6],
        invalidation=invalidation,
    )

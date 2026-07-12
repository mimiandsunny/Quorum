from __future__ import annotations

from data.models import (
    AlphaOutput,
    RecommendationSide,
    RecommendationStrategyType,
    TickerDataPackage,
)
from recommendation.features import feature_payload
from recommendation.quality import data_quality_flags


def _has_value(value: float | None) -> bool:
    return value is not None


def _confidence_from_score(score: float) -> float:
    return round(min(0.72, 0.46 + abs(score) * 0.05), 2)


def _expected_return_from_score(score: float) -> float:
    return round(min(0.24, 0.08 + min(abs(score), 4.0) * 0.025), 4)


def _expected_drawdown_from_score(score: float) -> float:
    return round(-min(0.16, 0.07 + min(abs(score), 4.0) * 0.018), 4)


def analyze(pkg: TickerDataPackage) -> AlphaOutput:
    """Conservative deterministic alpha estimate for 3-12 month ideas."""
    flags = data_quality_flags(pkg)
    severe_flags = {"missing_price_history", "nonpositive_price", "negative_volume"}
    if severe_flags.intersection(flags):
        return AlphaOutput(
            strategy_type=RecommendationStrategyType.LONG_TERM,
            direction=RecommendationSide.FLAT,
            horizon_days=180,
            expected_return=0.0,
            expected_drawdown=0.0,
            confidence=0.2,
            evidence=[f"Data quality blocks long-term alpha: {', '.join(sorted(severe_flags.intersection(flags)))}"],
            invalidation="Restore clean price and volume history.",
        )

    fundamentals = pkg.fundamentals
    features = feature_payload(pkg)
    technicals = features["technicals"]
    liquidity = features["liquidity"]

    score = 0.0
    evidence: list[str] = []

    if _has_value(fundamentals.revenue_growth):
        if fundamentals.revenue_growth >= 0.20:
            score += 1.0
            evidence.append(f"Revenue growth is strong at {fundamentals.revenue_growth:.1%}")
        elif fundamentals.revenue_growth >= 0.08:
            score += 0.45
            evidence.append(f"Revenue growth is positive at {fundamentals.revenue_growth:.1%}")
        elif fundamentals.revenue_growth < 0:
            score -= 0.9
            evidence.append(f"Revenue growth is negative at {fundamentals.revenue_growth:.1%}")

    pe = fundamentals.forward_pe or fundamentals.pe_ratio
    if _has_value(pe):
        if pe <= 22:
            score += 0.55
            evidence.append(f"Valuation is reasonable with P/E {pe:.1f}")
        elif pe <= 35:
            score += 0.15
            evidence.append(f"Valuation is acceptable with P/E {pe:.1f}")
        elif pe > 60:
            score -= 0.65
            evidence.append(f"Valuation is demanding with P/E {pe:.1f}")

    if _has_value(fundamentals.debt_to_equity):
        if fundamentals.debt_to_equity <= 0.8:
            score += 0.35
            evidence.append(f"Balance sheet leverage is manageable at {fundamentals.debt_to_equity:.2f} debt/equity")
        elif fundamentals.debt_to_equity > 2.0:
            score -= 0.55
            evidence.append(f"Balance sheet leverage is elevated at {fundamentals.debt_to_equity:.2f} debt/equity")

    if _has_value(fundamentals.market_cap):
        if fundamentals.market_cap >= 10_000_000_000:
            score += 0.25
            evidence.append("Market cap supports institutional liquidity")
        elif fundamentals.market_cap < 1_000_000_000:
            score -= 0.25
            evidence.append("Small market cap raises durability risk")

    if _has_value(technicals["distance_to_ma_200_pct"]):
        if technicals["distance_to_ma_200_pct"] > 0:
            score += 0.3
            evidence.append("Price is above the 200-day moving average")
        elif technicals["distance_to_ma_200_pct"] < -0.15:
            score -= 0.3
            evidence.append("Price is materially below the 200-day moving average")

    liquidity_score = liquidity["liquidity_score"]
    if liquidity_score is not None and liquidity_score < 0.25:
        score -= 0.25
        evidence.append(f"Liquidity score is weak at {liquidity_score:.2f}")

    if score >= 1.5:
        direction = RecommendationSide.LONG
        invalidation = "Long-term thesis weakens if growth decelerates or price loses the 200-day trend."
    elif score <= -1.4:
        direction = RecommendationSide.SHORT
        invalidation = "Bearish long-term view weakens if growth reaccelerates or valuation resets lower."
    else:
        direction = RecommendationSide.FLAT
        invalidation = "Wait for stronger quality, valuation, or durability evidence."

    if not evidence:
        evidence.append("Insufficient long-term fundamentals for a durable edge")

    expected_return = 0.0
    expected_drawdown = 0.0
    confidence = 0.35
    if direction != RecommendationSide.FLAT:
        confidence = _confidence_from_score(score)
        expected_return = _expected_return_from_score(score)
        expected_drawdown = _expected_drawdown_from_score(score)

    return AlphaOutput(
        strategy_type=RecommendationStrategyType.LONG_TERM,
        direction=direction,
        horizon_days=180,
        expected_return=expected_return,
        expected_drawdown=expected_drawdown,
        expected_volatility=features["risk"]["realized_volatility_20d"],
        confidence=confidence,
        evidence=evidence[:6],
        invalidation=invalidation,
    )

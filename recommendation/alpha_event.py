from __future__ import annotations

from datetime import date

from data.models import (
    AlphaOutput,
    RecommendationSide,
    RecommendationStrategyType,
    TickerDataPackage,
)
from recommendation.features import feature_payload
from recommendation.quality import data_quality_flags

IMMINENT_EVENT_DAYS = 7
WATCH_EVENT_DAYS = 30


def _days_to_event(pkg: TickerDataPackage) -> int | None:
    event_date = pkg.fundamentals.earnings_date
    if event_date is None:
        return None
    captured = pkg.fetch_timestamp.date()
    return (event_date - captured).days


def _event_volatility(pkg: TickerDataPackage) -> float | None:
    volatility = feature_payload(pkg)["risk"]["realized_volatility_20d"]
    if volatility is None:
        return None
    return round(min(max(volatility * 1.5, 0.20), 1.20), 4)


def analyze(pkg: TickerDataPackage) -> AlphaOutput:
    """Event-risk alpha engine.

    Event trades need a dated catalyst and separate risk handling. This first
    slice treats imminent earnings as a high-confidence no-trade event unless
    a future event-specific strategy explicitly overrides it.
    """
    flags = data_quality_flags(pkg)
    severe_flags = {"missing_price_history", "nonpositive_price", "negative_volume"}
    if severe_flags.intersection(flags):
        return AlphaOutput(
            strategy_type=RecommendationStrategyType.EVENT,
            direction=RecommendationSide.FLAT,
            horizon_days=1,
            expected_return=0.0,
            expected_drawdown=0.0,
            expected_volatility=None,
            confidence=0.2,
            evidence=[f"Data quality blocks event alpha: {', '.join(sorted(severe_flags.intersection(flags)))}"],
            invalidation="Restore clean price and volume history.",
        )

    days_to_event = _days_to_event(pkg)
    expected_volatility = _event_volatility(pkg)
    if days_to_event is None:
        return AlphaOutput(
            strategy_type=RecommendationStrategyType.EVENT,
            direction=RecommendationSide.FLAT,
            horizon_days=1,
            expected_return=0.0,
            expected_drawdown=0.0,
            expected_volatility=expected_volatility,
            confidence=0.30,
            evidence=["No explicit dated event available"],
            invalidation="Provide a dated catalyst and event-specific volatility range.",
        )

    if -1 <= days_to_event <= IMMINENT_EVENT_DAYS:
        return AlphaOutput(
            strategy_type=RecommendationStrategyType.EVENT,
            direction=RecommendationSide.FLAT,
            horizon_days=max(days_to_event, 1),
            expected_return=0.0,
            expected_drawdown=0.0,
            expected_volatility=expected_volatility,
            confidence=0.68,
            evidence=[
                f"Earnings event is {days_to_event} day(s) away",
                "Ordinary setup should not be treated as an event trade",
            ],
            invalidation="Wait until earnings risk clears or define an explicit event strategy.",
        )

    if 0 < days_to_event <= WATCH_EVENT_DAYS:
        return AlphaOutput(
            strategy_type=RecommendationStrategyType.EVENT,
            direction=RecommendationSide.FLAT,
            horizon_days=min(days_to_event, WATCH_EVENT_DAYS),
            expected_return=0.0,
            expected_drawdown=0.0,
            expected_volatility=expected_volatility,
            confidence=0.45,
            evidence=[f"Earnings event is {days_to_event} day(s) away"],
            invalidation="Re-evaluate as the event window approaches.",
        )

    return AlphaOutput(
        strategy_type=RecommendationStrategyType.EVENT,
        direction=RecommendationSide.FLAT,
        horizon_days=1,
        expected_return=0.0,
        expected_drawdown=0.0,
        expected_volatility=expected_volatility,
        confidence=0.30,
        evidence=[f"Next earnings event is outside the active window ({days_to_event} days away)"],
        invalidation="Re-check if a nearer dated catalyst appears.",
    )

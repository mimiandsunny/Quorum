from __future__ import annotations

from math import inf
from typing import Any

from config import settings
from data.models import (
    AlphaOutput,
    PortfolioAllocation,
    PortfolioExposure,
    RecommendationSide,
    RiskAssessment,
    RiskVerdict,
)

MAX_SINGLE_NAME_WEIGHT = 0.05
MAX_SHORT_SINGLE_NAME_WEIGHT = 0.03
MAX_RECOMMENDATION_LOSS_BUDGET = 0.005
MAX_SECTOR_WEIGHT = 0.15
MAX_GROSS_EXPOSURE = 0.30
MAX_SHORT_EXPOSURE = 0.10
HIGH_VOLATILITY_TRIM_THRESHOLD = 0.80
EXTREME_VOLATILITY_TRIM_THRESHOLD = 1.20


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _side_from_position_side(side: str | None) -> RecommendationSide | None:
    if side in ("buy", "long"):
        return RecommendationSide.LONG
    if side in ("sell_short", "short", "sell"):
        return RecommendationSide.SHORT
    return None


def portfolio_exposures_from_open_positions(
    rows: list[dict],
    account_value: float | None = None,
) -> list[PortfolioExposure]:
    """Convert open paper-position rows into coarse portfolio exposure weights."""
    account = account_value if account_value is not None else settings.paper_account_starting_value
    if account <= 0:
        return []

    exposures: list[PortfolioExposure] = []
    for row in rows:
        side = _side_from_position_side(row.get("side"))
        if side is None:
            continue

        qty = _to_float(row.get("qty"))
        price = _to_float(row.get("current_price")) or _to_float(row.get("avg_entry"))
        if qty is None or price is None or qty <= 0 or price <= 0:
            continue

        weight = round(abs(qty) * price / account, 4)
        if weight <= 0:
            continue

        exposures.append(
            PortfolioExposure(
                ticker=row["ticker"],
                side=side,
                target_weight=weight,
                sector=row.get("sector"),
            )
        )

    return exposures


def _alpha_multiplier(
    *,
    side: RecommendationSide,
    alpha_outputs: list[AlphaOutput] | None,
) -> tuple[float, str]:
    """Return a conservative sizing multiplier from deterministic alpha outputs."""
    if not alpha_outputs:
        return 1.0, "no_alpha_adjustment"

    aligned = [alpha for alpha in alpha_outputs if alpha.direction == side]
    conflicting = [
        alpha for alpha in alpha_outputs
        if alpha.direction not in (side, RecommendationSide.FLAT)
    ]

    if aligned:
        best = max(aligned, key=lambda alpha: alpha.confidence)
        if best.confidence >= 0.65:
            return 1.0, "alpha_aligned"
        if best.confidence >= 0.50:
            return 1.0, "alpha_aligned_moderate"
        return 0.50, "alpha_aligned_but_weak"

    strongest_conflict = max((alpha.confidence for alpha in conflicting), default=0.0)
    if strongest_conflict >= 0.50:
        return 0.50, "alpha_conflict_trim"

    return 0.50, "alpha_flat_or_weak_trim"


def _reward_risk_multiplier(
    *,
    expected_return: float | None,
    expected_drawdown: float | None,
) -> tuple[float, str, float]:
    """Return sizing multiplier, reason token, and implied reward/risk."""
    if expected_return is None or expected_return <= 0:
        return 0.0, "nonpositive_expected_return", 0.0

    drawdown_abs = abs(expected_drawdown or 0.0)
    reward_risk = expected_return / drawdown_abs if drawdown_abs > 0 else inf
    if reward_risk < 1.0:
        return 0.0, "reward_risk_below_1", reward_risk
    if reward_risk < settings.min_reward_risk:
        return 0.50, "reward_risk_trim", reward_risk
    return 1.0, "reward_risk_ok", reward_risk


def _volatility_multiplier(expected_volatility: float | None) -> tuple[float, str | None]:
    if expected_volatility is None:
        return 1.0, None
    if expected_volatility >= EXTREME_VOLATILITY_TRIM_THRESHOLD:
        return 0.25, "extreme_volatility_trim"
    if expected_volatility >= HIGH_VOLATILITY_TRIM_THRESHOLD:
        return 0.50, "high_volatility_trim"
    return 1.0, None


def _sum_exposure(
    exposures: list[PortfolioExposure],
    predicate,
) -> float:
    return sum(
        exposure.target_weight
        for exposure in exposures
        if predicate(exposure)
    )


def _apply_cap(
    *,
    target_weight: float,
    current_weight: float,
    cap: float,
    reason: str,
    reason_tokens: list[str],
) -> float:
    remaining = max(cap - current_weight, 0.0)
    if target_weight > remaining:
        reason_tokens.append(reason)
        return remaining
    return target_weight


def _apply_exposure_caps(
    *,
    target_weight: float,
    ticker: str,
    side: RecommendationSide,
    sector: str | None,
    current_exposures: list[PortfolioExposure] | None,
    reason_tokens: list[str],
) -> float:
    """Trim a target weight against existing portfolio exposure."""
    if target_weight <= 0 or not current_exposures:
        return target_weight

    target_weight = _apply_cap(
        target_weight=target_weight,
        current_weight=_sum_exposure(
            current_exposures,
            lambda exposure: exposure.ticker == ticker,
        ),
        cap=MAX_SINGLE_NAME_WEIGHT,
        reason="existing_single_name_cap",
        reason_tokens=reason_tokens,
    )

    if sector:
        target_weight = _apply_cap(
            target_weight=target_weight,
            current_weight=_sum_exposure(
                current_exposures,
                lambda exposure: exposure.sector == sector,
            ),
            cap=MAX_SECTOR_WEIGHT,
            reason="sector_exposure_cap",
            reason_tokens=reason_tokens,
        )

    target_weight = _apply_cap(
        target_weight=target_weight,
        current_weight=_sum_exposure(current_exposures, lambda exposure: True),
        cap=MAX_GROSS_EXPOSURE,
        reason="gross_exposure_cap",
        reason_tokens=reason_tokens,
    )

    if side == RecommendationSide.SHORT:
        target_weight = _apply_cap(
            target_weight=target_weight,
            current_weight=_sum_exposure(
                current_exposures,
                lambda exposure: exposure.side == RecommendationSide.SHORT,
            ),
            cap=MAX_SHORT_EXPOSURE,
            reason="short_exposure_cap",
            reason_tokens=reason_tokens,
        )

    return target_weight


def build_portfolio_allocation(
    *,
    recommendation_id: str,
    ticker: str,
    side: RecommendationSide,
    confidence: float,
    risk_assessment: RiskAssessment,
    expected_return: float | None,
    expected_drawdown: float | None,
    expected_volatility: float | None = None,
    alpha_outputs: list[AlphaOutput] | None = None,
    sector: str | None = None,
    current_exposures: list[PortfolioExposure] | None = None,
) -> PortfolioAllocation:
    """Build a deterministic single-name allocation for a recommendation.

    This is the first portfolio-construction slice: it converts approved trade
    sizing into a portfolio target weight with alpha, reward/risk, single-name,
    short-side, stop-loss-budget, and coarse portfolio-exposure caps.
    """
    reason_tokens: list[str] = []

    if side == RecommendationSide.FLAT:
        return PortfolioAllocation(
            recommendation_id=recommendation_id,
            ticker=ticker,
            target_weight=0.0,
            max_loss_budget=0.0,
            risk_budget_reason="flat_side_cash_allocation",
        )

    if risk_assessment.verdict != RiskVerdict.APPROVED:
        return PortfolioAllocation(
            recommendation_id=recommendation_id,
            ticker=ticker,
            target_weight=0.0,
            max_loss_budget=0.0,
            risk_budget_reason="risk_rejected_cash_allocation",
        )

    rr_multiplier, rr_reason, reward_risk = _reward_risk_multiplier(
        expected_return=expected_return,
        expected_drawdown=expected_drawdown,
    )
    reason_tokens.append(rr_reason)
    if rr_multiplier == 0.0:
        return PortfolioAllocation(
            recommendation_id=recommendation_id,
            ticker=ticker,
            target_weight=0.0,
            max_loss_budget=0.0,
            risk_budget_reason=";".join(reason_tokens),
        )

    alpha_multiplier, alpha_reason = _alpha_multiplier(
        side=side,
        alpha_outputs=alpha_outputs,
    )
    reason_tokens.append(alpha_reason)

    target_weight = risk_assessment.position_size_pct * rr_multiplier * alpha_multiplier
    volatility_multiplier, volatility_reason = _volatility_multiplier(expected_volatility)
    target_weight *= volatility_multiplier
    if volatility_reason:
        reason_tokens.append(volatility_reason)

    if confidence < settings.min_confidence:
        target_weight = 0.0
        reason_tokens.append("confidence_below_minimum")

    if target_weight > MAX_SINGLE_NAME_WEIGHT:
        target_weight = MAX_SINGLE_NAME_WEIGHT
        reason_tokens.append("single_name_cap")

    if side == RecommendationSide.SHORT and target_weight > MAX_SHORT_SINGLE_NAME_WEIGHT:
        target_weight = MAX_SHORT_SINGLE_NAME_WEIGHT
        reason_tokens.append("short_single_name_cap")

    drawdown_abs = abs(expected_drawdown or 0.0)
    if drawdown_abs > 0:
        budget_weight_cap = MAX_RECOMMENDATION_LOSS_BUDGET / drawdown_abs
        if target_weight > budget_weight_cap:
            target_weight = budget_weight_cap
            reason_tokens.append("loss_budget_cap")

    target_weight = _apply_exposure_caps(
        target_weight=target_weight,
        ticker=ticker,
        side=side,
        sector=sector,
        current_exposures=current_exposures,
        reason_tokens=reason_tokens,
    )

    target_weight = round(max(target_weight, 0.0), 4)
    implied_loss_budget = round(target_weight * drawdown_abs, 4)
    reason_tokens.append(f"implied_reward_risk={reward_risk:.2f}")

    return PortfolioAllocation(
        recommendation_id=recommendation_id,
        ticker=ticker,
        target_weight=target_weight,
        max_loss_budget=implied_loss_budget,
        risk_budget_reason=";".join(reason_tokens),
    )

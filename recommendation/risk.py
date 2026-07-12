from __future__ import annotations

from config import settings
from data.models import (
    AlphaOutput,
    Decision,
    RecommendationSide,
    RecommendationStrategyType,
    RiskAssessment,
    RiskVerdict,
    TickerDataPackage,
    TraderDecision,
)
from recommendation.features import feature_payload
from recommendation.quality import data_quality_flags

SEVERE_DATA_FLAGS = {"missing_price_history", "nonpositive_price", "negative_volume"}
LOW_LIQUIDITY_REJECT_THRESHOLD = 0.10
LOW_LIQUIDITY_TRIM_THRESHOLD = 0.25
STRONG_ALPHA_CONFLICT_THRESHOLD = 0.65
EVENT_RISK_BLOCK_THRESHOLD = 0.65
HIGH_VOLATILITY_TRIM_THRESHOLD = 0.80
EXTREME_VOLATILITY_REJECT_THRESHOLD = 1.50
OVERNIGHT_GAP_TRIM_THRESHOLD = 0.03
OVERNIGHT_GAP_REJECT_THRESHOLD = 0.08
DEEP_DRAWDOWN_TRIM_THRESHOLD = -0.20
EXTREME_DRAWDOWN_REJECT_THRESHOLD = -0.35
# Stop must be at least this fraction of the entry price even when ATR is
# unavailable — guards against the SPY-style 0.26% stop on tame names.
ABSOLUTE_MIN_STOP_PCT = 0.01
# First target must also be meaningfully away from entry. A target inside a
# normal move creates low-quality churn even when the stop is technically valid.
ABSOLUTE_MIN_TARGET_PCT = 0.02
# Targets should still be plausible for a short swing horizon. This caps
# target distance at max(15%, 3x daily ATR_14) so stale/fantasy targets do not
# make the setup look better than the actual tape supports.
ABSOLUTE_MAX_TARGET_PCT = 0.15
TARGET_ATR_CEILING_MULTIPLE = 3.0


def _side_for_decision(decision: Decision) -> RecommendationSide:
    if decision == Decision.BUY:
        return RecommendationSide.LONG
    if decision == Decision.SELL:
        return RecommendationSide.SHORT
    return RecommendationSide.FLAT


def _reject_from(
    base: RiskAssessment,
    reasons: list[str],
    adjustments: list[str] | None = None,
) -> RiskAssessment:
    combined_reasons = []
    seen_reasons = set()
    for reason in [*base.rejection_reasons, *reasons]:
        if reason not in seen_reasons:
            combined_reasons.append(reason)
            seen_reasons.add(reason)

    return RiskAssessment(
        ticker=base.ticker,
        verdict=RiskVerdict.REJECTED,
        position_size_pct=0.0,
        rejection_reasons=combined_reasons,
        adjustments=[*base.adjustments, *(adjustments or [])],
        reward_risk_ratio=base.reward_risk_ratio,
    )


def _with_size_adjustment(
    base: RiskAssessment,
    *,
    multiplier: float,
    adjustment: str,
) -> RiskAssessment:
    return RiskAssessment(
        ticker=base.ticker,
        verdict=base.verdict,
        position_size_pct=round(base.position_size_pct * multiplier, 4),
        rejection_reasons=base.rejection_reasons,
        adjustments=[*base.adjustments, adjustment],
        reward_risk_ratio=base.reward_risk_ratio,
    )


def _alpha_conflict_reason(
    decision: Decision,
    alpha_outputs: list[AlphaOutput] | None,
) -> str | None:
    side = _side_for_decision(decision)
    if side == RecommendationSide.FLAT or not alpha_outputs:
        return None

    conflicting = [
        alpha for alpha in alpha_outputs
        if alpha.direction not in (side, RecommendationSide.FLAT)
           and alpha.confidence >= STRONG_ALPHA_CONFLICT_THRESHOLD
    ]
    if not conflicting:
        return None

    strongest = max(conflicting, key=lambda alpha: alpha.confidence)
    return (
        "Deterministic alpha conflict: "
        f"{strongest.strategy_type.value} is {strongest.direction.value} "
        f"at {strongest.confidence:.0%} confidence"
    )


def _alpha_flat_or_weak(
    decision: Decision,
    alpha_outputs: list[AlphaOutput] | None,
) -> bool:
    side = _side_for_decision(decision)
    if side == RecommendationSide.FLAT or not alpha_outputs:
        return False
    return all(
        alpha.direction == RecommendationSide.FLAT or alpha.confidence < 0.50
        for alpha in alpha_outputs
    )


def _atr_floor_rejection_reason(
    decision: TraderDecision,
    risk_features: dict,
) -> str | None:
    """Reject trades whose stop is tighter than max(coefficient*ATR, 1% of price).

    Why: data shows 67% of recent trader stops sit inside 1.0x ATR — they get
    wicked through within hours of normal trading and inflate R/R via tight
    geometry rather than real edge. Reject-only (no auto-widen) for v1; the
    trader picks the new stop on next rerun.
    """
    coefficient = settings.paper_atr_floor_coefficient
    if coefficient <= 0 or decision.decision == Decision.HOLD:
        return None
    if not decision.entry_zone:
        return None

    entry_mid = (decision.entry_zone[0] + decision.entry_zone[1]) / 2
    if entry_mid <= 0:
        return None

    stop_distance_pct = abs(entry_mid - decision.stop_loss) / entry_mid
    floor_pct = ABSOLUTE_MIN_STOP_PCT
    atr_pct = risk_features.get("atr_14_pct")
    if atr_pct is not None and atr_pct > 0:
        floor_pct = max(floor_pct, coefficient * atr_pct)

    max_stop_pct = settings.max_stop_pct
    if max_stop_pct > 0 and stop_distance_pct > max_stop_pct:
        return None

    if max_stop_pct > 0 and floor_pct > max_stop_pct:
        atr_str = f"{atr_pct:.1%}" if atr_pct is not None else "n/a"
        return (
            f"Stop window impossible: ATR floor {floor_pct:.2%} exceeds "
            f"max stop {max_stop_pct:.2%} "
            f"({coefficient:g}x ATR_14={atr_str}); skip until volatility cools"
        )

    if stop_distance_pct < floor_pct:
        atr_str = f"{atr_pct:.1%}" if atr_pct is not None else "n/a"
        side_text = "above entry" if decision.decision == Decision.SELL else "below entry"
        required_price = (
            entry_mid * (1 + floor_pct)
            if decision.decision == Decision.SELL
            else entry_mid * (1 - floor_pct)
        )
        required_op = ">=" if decision.decision == Decision.SELL else "<="
        return (
            f"Stop too tight: ${decision.stop_loss:.2f} is "
            f"{stop_distance_pct:.2%} {side_text} "
            f"(required {required_op} ${required_price:.2f}; "
            f"floor {floor_pct:.2%} = max(1%, {coefficient:g}x ATR_14={atr_str}))"
        )
    return None


def _target_floor_rejection_reason(
    decision: TraderDecision,
    risk_features: dict,
) -> str | None:
    """Reject trades whose first target sits inside a routine price move."""
    coefficient = settings.paper_atr_floor_coefficient
    if coefficient <= 0 or decision.decision == Decision.HOLD:
        return None
    if not decision.entry_zone or not decision.targets:
        return None

    entry_mid = (decision.entry_zone[0] + decision.entry_zone[1]) / 2
    if entry_mid <= 0:
        return None

    target = decision.targets[0]
    if decision.decision == Decision.SELL:
        target_distance = entry_mid - target
    else:
        target_distance = target - entry_mid
    if target_distance <= 0:
        return None

    target_distance_pct = target_distance / entry_mid
    floor_pct = ABSOLUTE_MIN_TARGET_PCT
    atr_pct = risk_features.get("atr_14_pct")
    if atr_pct is not None and atr_pct > 0:
        floor_pct = max(floor_pct, coefficient * atr_pct)

    if target_distance_pct < floor_pct:
        atr_str = f"{atr_pct:.1%}" if atr_pct is not None else "n/a"
        side_text = "below entry" if decision.decision == Decision.SELL else "above entry"
        required_price = (
            entry_mid * (1 - floor_pct)
            if decision.decision == Decision.SELL
            else entry_mid * (1 + floor_pct)
        )
        required_op = "<=" if decision.decision == Decision.SELL else ">="
        return (
            f"Target too close: ${target:.2f} is "
            f"{target_distance_pct:.2%} {side_text} "
            f"(required {required_op} ${required_price:.2f}; "
            f"floor {floor_pct:.2%} = max(2%, {coefficient:g}x ATR_14={atr_str}))"
        )
    return None


def _target_ceiling_rejection_reasons(
    decision: TraderDecision,
    risk_features: dict,
) -> list[str]:
    if decision.decision == Decision.HOLD:
        return []
    if not decision.entry_zone or not decision.targets:
        return []

    entry_mid = (decision.entry_zone[0] + decision.entry_zone[1]) / 2
    if entry_mid <= 0:
        return []

    ceiling_pct = ABSOLUTE_MAX_TARGET_PCT
    atr_pct = risk_features.get("atr_14_pct")
    if atr_pct is not None and atr_pct > 0:
        ceiling_pct = max(ceiling_pct, TARGET_ATR_CEILING_MULTIPLE * atr_pct)

    reasons = []
    for target in decision.targets:
        if decision.decision == Decision.SELL:
            target_distance = entry_mid - target
        else:
            target_distance = target - entry_mid
        if target_distance <= 0:
            continue

        target_distance_pct = target_distance / entry_mid
        if target_distance_pct > ceiling_pct:
            atr_str = f"{atr_pct:.1%}" if atr_pct is not None else "n/a"
            side_text = "below entry" if decision.decision == Decision.SELL else "above entry"
            limit_price = (
                entry_mid * (1 - ceiling_pct)
                if decision.decision == Decision.SELL
                else entry_mid * (1 + ceiling_pct)
            )
            limit_op = ">=" if decision.decision == Decision.SELL else "<="
            reasons.append(
                f"Target too far: ${target:.2f} is "
                f"{target_distance_pct:.2%} {side_text} "
                f"(limit {limit_op} ${limit_price:.2f}; "
                f"ceiling {ceiling_pct:.2%} = max(15%, "
                f"{TARGET_ATR_CEILING_MULTIPLE:g}x ATR_14={atr_str}))"
            )
    return reasons


def _bracket_floor_rejection_reasons(
    decision: TraderDecision,
    risk_features: dict,
) -> list[str]:
    reasons = []
    stop_reason = _atr_floor_rejection_reason(decision, risk_features)
    if stop_reason:
        reasons.append(stop_reason)
    target_reason = _target_floor_rejection_reason(decision, risk_features)
    if target_reason:
        reasons.append(target_reason)
    reasons.extend(_target_ceiling_rejection_reasons(decision, risk_features))
    return reasons


def _event_risk_block_reason(alpha_outputs: list[AlphaOutput] | None) -> str | None:
    if not alpha_outputs:
        return None
    blocking = [
        alpha for alpha in alpha_outputs
        if alpha.strategy_type == RecommendationStrategyType.EVENT
           and alpha.direction == RecommendationSide.FLAT
           and alpha.confidence >= EVENT_RISK_BLOCK_THRESHOLD
    ]
    if not blocking:
        return None
    strongest = max(blocking, key=lambda alpha: alpha.confidence)
    evidence = "; ".join(strongest.evidence[:2])
    return f"Event risk block: {evidence}"


def assess_recommendation_risk(
    *,
    decision: TraderDecision,
    data: TickerDataPackage,
    base_assessment: RiskAssessment,
    alpha_outputs: list[AlphaOutput] | None = None,
) -> RiskAssessment:
    """Apply v2 data, liquidity, and deterministic-alpha risk gates.

    `agents.risk_manager.assess_risk` remains the v1 compatibility layer for
    trade geometry and confidence. This overlay adds gates that need the market
    data package or deterministic alpha outputs.
    """
    if decision.decision == Decision.HOLD:
        return base_assessment

    flags = data_quality_flags(data)
    severe_flags = sorted(SEVERE_DATA_FLAGS.intersection(flags))
    if severe_flags:
        return _reject_from(
            base_assessment,
            [f"Severe data quality flags: {', '.join(severe_flags)}"],
        )

    hard_reasons = []

    conflict_reason = _alpha_conflict_reason(decision.decision, alpha_outputs)
    if conflict_reason:
        hard_reasons.append(conflict_reason)

    event_risk_reason = _event_risk_block_reason(alpha_outputs)
    if event_risk_reason:
        hard_reasons.append(event_risk_reason)

    assessment = base_assessment
    features = feature_payload(data)
    liquidity_score = features["liquidity"]["liquidity_score"]
    risk_features = features["risk"]

    hard_reasons.extend(_bracket_floor_rejection_reasons(decision, risk_features))
    if liquidity_score is not None and liquidity_score < LOW_LIQUIDITY_REJECT_THRESHOLD:
        hard_reasons.append(
            f"Liquidity score {liquidity_score:.2f} below tradable minimum"
        )

    realized_volatility = risk_features.get("realized_volatility_20d")
    if (
        realized_volatility is not None
        and realized_volatility >= EXTREME_VOLATILITY_REJECT_THRESHOLD
    ):
        hard_reasons.append(
            f"Realized volatility {realized_volatility:.1%} above tradable maximum"
        )

    gap_pct = risk_features.get("gap_pct")
    if gap_pct is not None and abs(gap_pct) >= OVERNIGHT_GAP_REJECT_THRESHOLD:
        hard_reasons.append(f"Opening gap {gap_pct:.1%} above risk maximum")

    max_drawdown = risk_features.get("max_drawdown_20d")
    if (
        max_drawdown is not None
        and max_drawdown <= EXTREME_DRAWDOWN_REJECT_THRESHOLD
    ):
        hard_reasons.append(f"20-day max drawdown {max_drawdown:.1%} below risk maximum")

    if hard_reasons:
        return _reject_from(assessment, hard_reasons)

    if base_assessment.verdict != RiskVerdict.APPROVED:
        return base_assessment

    if liquidity_score is not None and liquidity_score < LOW_LIQUIDITY_TRIM_THRESHOLD:
        assessment = _with_size_adjustment(
            assessment,
            multiplier=0.5,
            adjustment=f"Size halved for weak liquidity score {liquidity_score:.2f}",
        )

    if (
        realized_volatility is not None
        and realized_volatility >= HIGH_VOLATILITY_TRIM_THRESHOLD
    ):
        assessment = _with_size_adjustment(
            assessment,
            multiplier=0.5,
            adjustment=f"Size halved for high realized volatility {realized_volatility:.1%}",
        )

    if gap_pct is not None and abs(gap_pct) >= OVERNIGHT_GAP_TRIM_THRESHOLD:
        assessment = _with_size_adjustment(
            assessment,
            multiplier=0.5,
            adjustment=f"Size halved for opening gap {gap_pct:.1%}",
        )

    if max_drawdown is not None and max_drawdown <= DEEP_DRAWDOWN_TRIM_THRESHOLD:
        assessment = _with_size_adjustment(
            assessment,
            multiplier=0.5,
            adjustment=f"Size halved for 20-day max drawdown {max_drawdown:.1%}",
        )

    warnings = sorted(set(flags) - SEVERE_DATA_FLAGS)
    if warnings:
        assessment = RiskAssessment(
            ticker=assessment.ticker,
            verdict=assessment.verdict,
            position_size_pct=assessment.position_size_pct,
            rejection_reasons=assessment.rejection_reasons,
            adjustments=[
                *assessment.adjustments,
                f"Data quality warnings: {', '.join(warnings)}",
            ],
            reward_risk_ratio=assessment.reward_risk_ratio,
        )

    if _alpha_flat_or_weak(decision.decision, alpha_outputs):
        assessment = _with_size_adjustment(
            assessment,
            multiplier=0.5,
            adjustment="Size halved because deterministic alpha is flat or weak",
        )

    return assessment

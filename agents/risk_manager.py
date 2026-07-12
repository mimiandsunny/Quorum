"""Deterministic risk manager. Pure Python, no LLM.

Rules:
  REJECT if:
    - confidence < 0.65
    - stop loss > 5% from entry midpoint
    - holding period > 10 days
    - entry zone width > 3%
    - trade geometry is invalid for BUY/SELL

  POSITION SIZE:
    - HOLD → 0%
    - 0.65–0.75 confidence → 2%
    - 0.75–0.85 confidence → 3%
    - 0.85+ confidence → 5%
    - If reward/risk ratio < 2.0, halve the size
"""

from config import settings
from data.models import (
    Decision,
    RiskAssessment,
    RiskVerdict,
    TraderDecision,
)


def assess_risk(decision: TraderDecision) -> RiskAssessment:
    """Evaluate a trader decision against deterministic risk rules."""
    reasons: list[str] = []
    adjustments: list[str] = []

    if decision.decision == Decision.HOLD:
        return RiskAssessment(
            ticker=decision.ticker,
            verdict=RiskVerdict.APPROVED,
            position_size_pct=0.0,
            rejection_reasons=[],
            adjustments=["No position sized for HOLD decision"],
            reward_risk_ratio=0.0,
        )

    entry_mid = (decision.entry_zone[0] + decision.entry_zone[1]) / 2
    first_target = decision.targets[0]

    # ── Reward/Risk Ratio ─────────────────────────────
    if decision.decision == Decision.SELL:
        # For shorts: reward = entry - target, risk = stop - entry
        reward = entry_mid - first_target
        risk = decision.stop_loss - entry_mid
    else:
        # For longs: reward = target - entry, risk = entry - stop
        reward = first_target - entry_mid
        risk = entry_mid - decision.stop_loss

    rr_ratio = reward / risk if risk > 0 else 0.0

    # ── Rejection Checks ──────────────────────────────
    if decision.confidence < settings.min_confidence:
        reasons.append(
            f"Confidence {decision.confidence:.2f} below minimum {settings.min_confidence}"
        )

    stop_distance_pct = abs(decision.stop_loss - entry_mid) / entry_mid
    if stop_distance_pct > settings.max_stop_pct:
        reasons.append(
            f"Stop loss {stop_distance_pct:.1%} from entry exceeds {settings.max_stop_pct:.0%} max"
        )

    if decision.holding_period_days > settings.max_hold_days:
        reasons.append(
            f"Holding period {decision.holding_period_days}d exceeds {settings.max_hold_days}d max"
        )

    if decision.decision == Decision.BUY and not (
        decision.stop_loss < entry_mid < first_target
    ):
        reasons.append(
            "Invalid BUY bracket: stop must be below entry and first target above entry"
        )
    elif decision.decision == Decision.SELL and not (
        first_target < entry_mid < decision.stop_loss
    ):
        reasons.append(
            "Invalid SELL bracket: stop must be above entry and first target below entry"
        )

    entry_width_pct = abs(decision.entry_zone[1] - decision.entry_zone[0]) / entry_mid
    if entry_width_pct > settings.max_entry_zone_width_pct:
        reasons.append(
            f"Entry zone width {entry_width_pct:.1%} exceeds {settings.max_entry_zone_width_pct:.0%} max"
        )

    if rr_ratio < 1.0 and not reasons:
        reasons.append(f"Reward/risk ratio {rr_ratio:.2f} below 1.0")

    # ── Verdict ───────────────────────────────────────
    if reasons:
        return RiskAssessment(
            ticker=decision.ticker,
            verdict=RiskVerdict.REJECTED,
            position_size_pct=0.0,
            rejection_reasons=reasons,
            adjustments=[],
            reward_risk_ratio=round(rr_ratio, 2),
        )

    # ── Position Sizing ───────────────────────────────
    if decision.confidence >= 0.85:
        size = settings.size_tier_high
    elif decision.confidence >= 0.75:
        size = settings.size_tier_mid
    else:
        size = settings.size_tier_low

    if rr_ratio < settings.min_reward_risk:
        size /= 2
        adjustments.append(
            f"Size halved (R/R {rr_ratio:.2f} < {settings.min_reward_risk})"
        )

    return RiskAssessment(
        ticker=decision.ticker,
        verdict=RiskVerdict.APPROVED,
        position_size_pct=round(size, 4),
        rejection_reasons=[],
        adjustments=adjustments,
        reward_risk_ratio=round(rr_ratio, 2),
    )

"""Sanity checks for LLM debate price targets."""

from __future__ import annotations


LOWER_TARGET_MULTIPLE = 0.5
UPPER_TARGET_MULTIPLE = 2.0


def is_implausible_target(target: float | None, reference_price: float | None) -> bool:
    if target is None or reference_price is None or reference_price <= 0:
        return False
    if target <= 0:
        return True
    return (
        target < reference_price * LOWER_TARGET_MULTIPLE
        or target > reference_price * UPPER_TARGET_MULTIPLE
    )


def choose_replacement_target(
    stance: str,
    reference_price: float,
    support_levels: list[float] | None = None,
    resistance_levels: list[float] | None = None,
    fallback_targets: list[float] | None = None,
) -> float:
    """Pick a nearby technical/final target when an LLM emits an outlier."""
    supports = support_levels or []
    resistances = resistance_levels or []
    fallbacks = fallback_targets or []

    if stance.lower() == "bear":
        candidates = [
            level
            for level in [*supports, *fallbacks]
            if reference_price * LOWER_TARGET_MULTIPLE <= level < reference_price
        ]
        return round(max(candidates) if candidates else reference_price * 0.94, 2)

    candidates = [
        level
        for level in [*resistances, *fallbacks]
        if reference_price < level <= reference_price * UPPER_TARGET_MULTIPLE
    ]
    return round(min(candidates) if candidates else reference_price * 1.06, 2)


def target_sanity_note(original: float, replacement: float, reference_price: float) -> str:
    return (
        f"Original debate target ${original:.2f} was outside the "
        f"{LOWER_TARGET_MULTIPLE:.1f}x-{UPPER_TARGET_MULTIPLE:.1f}x sanity band "
        f"around reference price ${reference_price:.2f}; using ${replacement:.2f}."
    )


def sanitize_research_case(
    case,
    reference_price: float | None,
    support_levels: list[float] | None = None,
    resistance_levels: list[float] | None = None,
) -> bool:
    """Mutate a ResearchCase-like object if its price target is implausible."""
    target = getattr(case, "price_target", None)
    if not is_implausible_target(target, reference_price):
        return False

    replacement = choose_replacement_target(
        getattr(case, "stance", "bull"),
        reference_price,
        support_levels=support_levels,
        resistance_levels=resistance_levels,
    )
    note = target_sanity_note(float(target), replacement, reference_price)
    case.price_target = replacement
    case.thesis = f"{case.thesis}\n\nTarget sanity note: {note}"
    case.risks = [*case.risks, note]
    return True


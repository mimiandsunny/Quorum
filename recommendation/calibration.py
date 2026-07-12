from __future__ import annotations

from decimal import Decimal
from typing import Any


def _float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pct(value: Any) -> str:
    number = _float(value)
    if number is None:
        return "n/a"
    return f"{number:.0%}"


def _signed_pct(value: Any) -> str:
    number = _float(value)
    if number is None:
        return "n/a"
    return f"{number:+.1%}"


def _score(value: Any) -> str:
    number = _float(value)
    if number is None:
        return "n/a"
    return f"{number:.2f}"


def build_calibration_summary(
    rows: list[dict],
    max_lines: int = 8,
) -> str:
    """Format recommendation calibration rows for compact prompt context."""
    if not rows:
        return ""

    lines = ["RECOMMENDATION CALIBRATION (recent scored recommendations):"]
    for row in rows[:max_lines]:
        strategy = row.get("strategy_type", "unknown")
        side = row.get("side", "unknown")
        horizon = row.get("horizon_days", "?")
        bucket = row.get("confidence_bucket", "unknown")
        total = row.get("total", 0)
        lines.append(
            "- "
            f"{strategy}/{side}/h{horizon}/{bucket}: "
            f"n={total}, "
            f"avg_conf={_pct(row.get('avg_confidence'))}, "
            f"win={_pct(row.get('win_rate'))}, "
            f"outperform={_pct(row.get('outperform_rate'))}, "
            f"avg_return={_signed_pct(row.get('avg_side_return_pct'))}, "
            f"avg_excess={_signed_pct(row.get('avg_excess_return_pct'))}, "
            f"score={_score(row.get('avg_score'))}"
        )

    return "\n".join(lines)

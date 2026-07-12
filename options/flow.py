from __future__ import annotations

from options.features import rank_options_snapshot
from options.models import OptionChainSnapshot, OptionRank


def scan_unusual_flow(
    snapshot: OptionChainSnapshot,
    *,
    limit: int = 20,
    min_volume: int = 100,
    min_volume_oi_ratio: float = 1.0,
) -> list[OptionRank]:
    """Return option contracts with unusual same-day activity.

    Scans the FULL snapshot before filtering (K3): high-flow contracts
    sitting on illiquid strikes were previously cut by `rank_options_snapshot`'s
    truncation before the unusual-flow filter ever saw them. Pass ``limit=None``
    to rank everything, then sort filtered results by flow score.

    This is scanner logic only. It does not infer whether the trade was bought
    or sold because retail chain snapshots usually do not include aggressor
    side or trade prints.
    """
    candidates = [
        rank for rank in rank_options_snapshot(snapshot, limit=None)
        if rank.volume >= min_volume
        and (
            rank.flow_score >= 0.60
            or (rank.volume_oi_ratio is not None and rank.volume_oi_ratio >= min_volume_oi_ratio)
        )
    ]
    return sorted(candidates, key=lambda row: row.flow_score, reverse=True)[:limit]


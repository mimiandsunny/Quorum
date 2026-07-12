from __future__ import annotations

from datetime import datetime
from typing import TypedDict

from options.features import rank_options_snapshot, summarize_options_snapshot
from options.models import OptionChainSnapshot, OptionsDashboard, OptionsSnapshotSummary


class OptionChainSnapshotRow(TypedDict):
    """Shape of a row returned by `data.storage.get_latest_option_chain_snapshots`.

    Names the implicit dict shape that flowed through `_snapshot_from_row` (K15).
    Mirrors the `option_chain_snapshots` table schema after JSON columns are
    decoded by `_normalize_option_snapshot_row`.
    """

    snapshot_id: str
    ticker: str
    captured_at: datetime
    source: str
    underlying_price: float
    expirations: list
    contracts: list
    metadata: dict


def _snapshot_from_row(row: OptionChainSnapshotRow) -> OptionChainSnapshot:
    return OptionChainSnapshot.model_validate(row)


def build_options_dashboard(
    *,
    tickers: list[str] | None = None,
    per_ticker_limit: int = 5,
    total_limit: int = 20,
) -> OptionsDashboard:
    from data.storage import get_latest_option_chain_snapshots

    rows = get_latest_option_chain_snapshots(tickers=tickers, limit=50)
    snapshot_summaries: list[OptionsSnapshotSummary] = []
    candidates = []
    for row in rows:
        snapshot = _snapshot_from_row(row)
        ranks = rank_options_snapshot(snapshot, limit=per_ticker_limit)
        snapshot_summaries.append(
            OptionsSnapshotSummary.model_validate(summarize_options_snapshot(snapshot, ranks))
        )
        candidates.extend(ranks)

    candidates = sorted(candidates, key=lambda row: row.rank_score, reverse=True)[:total_limit]
    summary = {
        "snapshot_count": len(snapshot_summaries),
        "candidate_count": len(candidates),
        "unusual_flow_count": sum(1 for row in candidates if "unusual_flow" in row.tags),
        "cheap_vol_count": sum(1 for row in candidates if row.iv_label == "cheap"),
        "rich_vol_count": sum(1 for row in candidates if row.iv_label == "rich"),
    }
    return OptionsDashboard(
        snapshots=snapshot_summaries,
        candidates=candidates,
        summary=summary,
    )


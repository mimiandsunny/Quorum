"""D3 — IV-rank screener (Wave 2 plan rev 4).

Returns two buckets per DR8:
  1. `ranked`: tickers with ≥60 days of `option_iv_history` (A5 cold-start
     gate passed). Sorted by `iv_rank_30d` descending — richest vol first
     so the user sees "what's expensive to buy today" at the top.
  2. `cold_start`: tickers in the universe whose latest row is
     `iv_label='insufficient'`, in alphabetical order per the design
     wireframe. Surface them — never silent-drop — so the operator can
     see "5 of 50 tickers are still warming up."

The two buckets render under one section header in the cockpit (DR1/DR8
inline subsection), which is why both ship from one read.
"""

from __future__ import annotations

from dataclasses import dataclass

from data.models import OptionIVHistory, OptionIVLabel


@dataclass(frozen=True)
class IVScreenerResult:
    """Two-bucket screener payload. The dashboard uses `ranked` for the
    main table and `cold_start` for DR8's inline "building history" row.
    """

    ranked: list[OptionIVHistory]
    cold_start: list[str]

    @property
    def total(self) -> int:
        return len(self.ranked) + len(self.cold_start)


def build_iv_screener(
    *,
    labels: list[OptionIVLabel] | None = None,
    tickers: list[str] | None = None,
    limit: int = 100,
) -> IVScreenerResult:
    """Pulls latest IV row per ticker, splits cold-start from ranked.

    Sort order on `ranked`: iv_rank_30d DESC, with None-rank rows pushed to
    the bottom (defensive — the A5 gate should keep them out, but if a row
    slips through with iv_label != 'insufficient' and rank None we still
    want it visible without breaking the comparator).
    """
    from data.storage import get_iv_cold_start_tickers, get_iv_screener_rows

    ranked = get_iv_screener_rows(labels=labels, tickers=tickers, limit=limit)
    ranked.sort(
        key=lambda row: (
            -(row.iv_rank_30d if row.iv_rank_30d is not None else -1.0),
            row.ticker,
        )
    )
    cold_start = sorted(get_iv_cold_start_tickers())
    if tickers is not None:
        wanted = {t.upper() for t in tickers}
        cold_start = [t for t in cold_start if t in wanted]
    return IVScreenerResult(ranked=ranked, cold_start=cold_start)

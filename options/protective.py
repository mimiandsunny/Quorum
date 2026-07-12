"""D2 — protective-put cost compute (Wave 2 plan rev 4).

Reads open paper positions via the frozen public API
`data/storage.py::get_open_paper_positions()` (A1 freeze rule — no edits to
that function or the `paper_positions` table during the wave-1.5 evaluation
window), picks the put closest to a target delta within a sensible DTE
window, and writes the result to `option_protective_costs` (D2 schema).

Single-leg only. Collars (multileg) defer to Wave 2.5 or absorb leg
primitives into D4 (decision 7 of rev 2; OV5 of rev 2 cross-model tensions).

`cost_pct_of_position` is the actionable number — premium relative to
notional, not raw dollars — because the user thinks in "% of position
budget gets spent on insurance," not "$1.23 per share." Returns None when:
  - the position has no usable chain snapshot in the DB,
  - no put within the target-delta tolerance exists, or
  - the underlying notional is zero (defensive: avg_entry can be 0 for
    edge-case rows; we surface that as "no cost" rather than NaN).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime

from data.models import OptionGreeksSource, OptionProtectiveCost
from options.features import option_mid
from options.models import (
    OptionChainSnapshot,
    OptionContractSnapshot,
    OptionType,
)

logger = logging.getLogger(__name__)


# Plan rev 4 / OV4: target a moderately-OTM hedge. -0.25 to -0.30 delta puts
# are the "1-in-4-chance-of-being-in-the-money" band — far enough OTM to be
# affordable, close enough to actually protect against a real drawdown.
_TARGET_DELTA: float = -0.25
_DELTA_TOLERANCE: float = 0.15  # accept anything in [-0.40, -0.10]
# 21-60 DTE window: 21d to skip near-expiry theta cliff; 60d to keep the
# rolling-protection cadence quarterly (4× per year × ~60d ≈ year-round).
_MIN_DTE: int = 14
_MAX_DTE: int = 75


@dataclass(frozen=True)
class _PutCandidate:
    contract: OptionContractSnapshot
    mid: float
    delta: float | None
    delta_distance: float  # |actual_delta - target_delta|; lower is better
    dte: int


def _dte(expiration: date, as_of: date) -> int:
    return (expiration - as_of).days


def _select_protective_put(
    snapshot: OptionChainSnapshot,
    *,
    target_delta: float = _TARGET_DELTA,
    delta_tolerance: float = _DELTA_TOLERANCE,
    as_of: date | None = None,
) -> _PutCandidate | None:
    """Pick the put closest to `target_delta` inside the DTE window.

    Strategy: rank by delta distance first, then by liquidity proxy (volume),
    so a tied-delta put with real volume wins over an illiquid neighbor.
    """
    as_of = as_of or (
        snapshot.captured_at.date()
        if isinstance(snapshot.captured_at, datetime)
        else snapshot.captured_at
    )
    candidates: list[_PutCandidate] = []
    for c in snapshot.contracts:
        if c.option_type != OptionType.PUT:
            continue
        dte = _dte(c.expiration, as_of)
        if dte < _MIN_DTE or dte > _MAX_DTE:
            continue
        mid = option_mid(c)
        if mid is None or mid <= 0:
            continue
        delta = c.delta
        # No Greeks (yfinance path) → estimate via moneyness proxy: treat
        # the strike at ~5% OTM as a -0.25 delta stand-in. This keeps the
        # protective-cost field populated during the IBKR-waiting window
        # without inventing fake deltas; downstream tags it as `greeks=none`.
        if delta is None:
            moneyness = (snapshot.underlying_price - c.strike) / snapshot.underlying_price
            # Put is OTM when strike < underlying; want strikes 3-10% OTM.
            if not 0.03 <= moneyness <= 0.10:
                continue
            distance = abs(target_delta - (-0.25))  # midpoint proxy; ties broken on volume
        else:
            if abs(delta - target_delta) > delta_tolerance:
                continue
            distance = abs(delta - target_delta)
        candidates.append(
            _PutCandidate(contract=c, mid=mid, delta=delta, delta_distance=distance, dte=dte)
        )

    if not candidates:
        return None
    # Sort: smallest delta-distance first; tie-break on higher volume (proxy
    # for tradability), then on smaller DTE (cheaper premium for same delta).
    candidates.sort(
        key=lambda x: (
            x.delta_distance,
            -(x.contract.volume or 0),
            x.dte,
        )
    )
    return candidates[0]


def _greeks_source_for(snapshot: OptionChainSnapshot, candidate: _PutCandidate) -> OptionGreeksSource:
    """Mirror the per-contract greeks_source heuristic from IBKRFetcher.

    Snapshots written by `IBKRFetcher` carry Greeks when
    `options_greeks_strategy='ibkr_provider'`, and the local-BS path
    populates Greeks too. The yfinance fetcher leaves Greeks None — that's
    the `NONE` case here.
    """
    if candidate.contract.gamma is not None or candidate.contract.delta is not None:
        # Distinguish provider-NaN: a row that has SOME Greeks but the
        # specific delta/gamma we need is None falls through to PROVIDER_NAN.
        if candidate.delta is None:
            return OptionGreeksSource.PROVIDER_NAN
        # Source string lives on the snapshot. ibkr → provider; yfinance
        # rows with non-None Greeks came from the local_bs derivation path.
        if snapshot.source.value == "ibkr":
            return OptionGreeksSource.PROVIDER
        return OptionGreeksSource.LOCAL_BS
    return OptionGreeksSource.NONE


def compute_protective_cost(
    position: dict,
    snapshot: OptionChainSnapshot,
    *,
    target_delta: float = _TARGET_DELTA,
    as_of: date | None = None,
) -> OptionProtectiveCost | None:
    """Compute the protective-put cost for one open position.

    `position` is the dict shape returned by `get_open_paper_positions()`:
    `{paper_trade_id, ticker, qty, avg_entry, ...}`. Long positions only —
    short legs in wave-1.5 paper paths are skipped (a covered call hedges a
    short, but that's a different conversation and we're not building it
    here per OV5 scope cut).

    Returns None when no eligible put exists. The position is then surfaced
    in the dashboard with `protective_cost=None` so DR3-default-empty kicks
    in ("No protective option available") rather than silently omitting it.
    """
    qty = float(position.get("qty") or 0)
    avg_entry = float(position.get("avg_entry") or 0)
    if qty <= 0 or avg_entry <= 0:
        # Short / closed / malformed row → no protective compute.
        return None

    candidate = _select_protective_put(
        snapshot, target_delta=target_delta, as_of=as_of
    )
    if candidate is None:
        return None

    notional = qty * avg_entry
    if notional <= 0:
        return None
    # 100x multiplier: one option contract covers 100 shares. cost_per_share
    # is the premium, NOT premium-times-100 — the per-share frame matches the
    # `avg_entry` denominator the dashboard uses.
    cost_pct = candidate.mid / avg_entry

    return OptionProtectiveCost(
        position_id=int(position["paper_trade_id"]),
        contract_symbol=candidate.contract.contract_symbol,
        cost_per_share=round(candidate.mid, 4),
        cost_pct_of_position=round(cost_pct, 4),
        delta=candidate.delta,
        greeks_source=_greeks_source_for(snapshot, candidate),
        computed_at=None,  # storage layer fills NOW()
    )


def refresh_protective_costs(
    *,
    target_delta: float = _TARGET_DELTA,
    as_of: date | None = None,
) -> dict[str, int]:
    """Iterate open positions, compute protective-put cost, persist.

    Returns a small summary dict: `{computed, skipped_no_chain, skipped_no_put}`.
    Per A1 freeze, this function ONLY reads from `paper_positions` (via
    `get_open_paper_positions`) — never writes. All writes go to
    `option_protective_costs` (a separate table the freeze doesn't cover).
    """
    from data.storage import (
        get_latest_option_chain_snapshot,
        get_open_paper_positions,
        upsert_option_protective_cost,
    )
    from options.service import _snapshot_from_row

    positions = get_open_paper_positions()
    computed = 0
    skipped_no_chain = 0
    skipped_no_put = 0

    # Cache: many positions share a ticker (3 strategies per signal) so we
    # don't want a chain-fetch per row. Snapshots are immutable in-memory.
    snapshot_cache: dict[str, OptionChainSnapshot | None] = {}

    for position in positions:
        ticker = (position.get("ticker") or "").upper()
        if not ticker:
            continue
        if ticker not in snapshot_cache:
            row = get_latest_option_chain_snapshot(ticker)
            snapshot_cache[ticker] = _snapshot_from_row(row) if row else None
        snapshot = snapshot_cache[ticker]
        if snapshot is None:
            skipped_no_chain += 1
            continue
        cost = compute_protective_cost(
            position, snapshot, target_delta=target_delta, as_of=as_of
        )
        if cost is None:
            skipped_no_put += 1
            continue
        try:
            upsert_option_protective_cost(cost)
            computed += 1
        except Exception as exc:
            logger.warning(
                f"[{ticker}] upsert_option_protective_cost failed "
                f"({type(exc).__name__}: {exc}); skipping"
            )

    return {
        "computed": computed,
        "skipped_no_chain": skipped_no_chain,
        "skipped_no_put": skipped_no_put,
    }

"""ATM IV summary + per-ticker chain-write orchestration (A6).

A6 contract: chain ingest is the source of truth, IV summary is derived.
The chain-write MUST succeed even when IV summary cannot be computed
(e.g., all IVs zero, no contract near ATM, only one expiration available).
On summary failure we record a `data_provider_events('iv_summary_skipped')`
audit row so the operator can see why a ticker is missing from the
historical IV-rank screener.

ATM IV definition: nearest expiration to the target DTE, strike closest
to the underlying price, average of call+put IV when both exist (else
whichever is present). Returns None when no eligible contract is found.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime

from options.models import OptionChainSnapshot, OptionContractSnapshot, OptionType

logger = logging.getLogger(__name__)


# Target DTE windows for the three ATM-IV measurements. Nearest expiration
# within ±_ATM_DTE_TOLERANCE days qualifies; otherwise the slot stays None.
_ATM_TARGET_DTES: tuple[int, ...] = (30, 60, 90)
_ATM_DTE_TOLERANCE = 21  # ±3 weeks of slack — typical chain has 7d/14d/21d/30d/60d ladders

# A6: ATM strike must be within this fraction of the underlying or the slot
# is treated as "no eligible ATM strike" — guards against thin chains where
# the closest available strike is 20% away and would mislead the IV reading.
_ATM_STRIKE_TOLERANCE_PCT = 0.05


@dataclass(frozen=True)
class IVSummary:
    underlying_price: float
    atm_iv_30d: float | None
    atm_iv_60d: float | None
    atm_iv_90d: float | None
    skip_reason: str | None = None

    @property
    def term_structure_30_60(self) -> float | None:
        if self.atm_iv_30d is None or self.atm_iv_60d is None:
            return None
        return round(self.atm_iv_60d - self.atm_iv_30d, 4)

    @property
    def term_structure_60_90(self) -> float | None:
        if self.atm_iv_60d is None or self.atm_iv_90d is None:
            return None
        return round(self.atm_iv_90d - self.atm_iv_60d, 4)

    @property
    def is_eligible(self) -> bool:
        """A6 minimum bar: the 30d slot is the IV-rank screener anchor.
        If 30d is missing the row gets audit-skipped — partial 60/90-only
        rows have no D3 read path.
        """
        return self.atm_iv_30d is not None


def _dte(expiration: date, as_of: date) -> int:
    return (expiration - as_of).days


def _nearest_expiration(
    expirations: list[date],
    target_dte: int,
    *,
    as_of: date,
    tolerance: int = _ATM_DTE_TOLERANCE,
) -> date | None:
    eligible = [exp for exp in expirations if _dte(exp, as_of) >= 0]
    if not eligible:
        return None
    nearest = min(eligible, key=lambda exp: abs(_dte(exp, as_of) - target_dte))
    if abs(_dte(nearest, as_of) - target_dte) > tolerance:
        return None
    return nearest


def _atm_iv_for_expiration(
    contracts: list[OptionContractSnapshot],
    expiration: date,
    underlying_price: float,
) -> float | None:
    """Average call+put IV at the strike closest to underlying for the given
    expiration. Returns None when:
      - no contract for that expiration carries a usable IV (all-NaN), OR
      - the closest strike is more than `_ATM_STRIKE_TOLERANCE_PCT` away
        from the underlying (thin chain — no real ATM contract).
    """
    if underlying_price <= 0:
        return None
    by_strike: dict[float, dict[OptionType, float]] = {}
    for c in contracts:
        if c.expiration != expiration:
            continue
        if c.implied_volatility is None or c.implied_volatility <= 0:
            continue
        by_strike.setdefault(c.strike, {})[c.option_type] = c.implied_volatility
    if not by_strike:
        return None
    atm_strike = min(by_strike.keys(), key=lambda s: abs(s - underlying_price))
    if abs(atm_strike - underlying_price) / underlying_price > _ATM_STRIKE_TOLERANCE_PCT:
        return None
    legs = by_strike[atm_strike]
    if not legs:
        return None
    return round(sum(legs.values()) / len(legs), 4)


def compute_iv_summary(snapshot: OptionChainSnapshot) -> IVSummary:
    """Computes the 30/60/90d ATM IV summary for one chain snapshot.

    Per plan A6: returns an `IVSummary` carrying a `skip_reason` when the
    30d anchor slot can't be computed. Callers persist when `is_eligible`,
    else audit-skip with the reason for `data_provider_events`.

    Skip reasons (mirror plan A6 wording):
      - 'no_30d_expiration'      : no expiry within ±21d of 30 DTE
      - 'no_eligible_atm_iv'     : 30d expiry exists, but no strike within
                                   5% of underlying carries a usable IV
                                   (covers all-NaN and thin-chain cases)
    """
    as_of = (
        snapshot.captured_at.date()
        if isinstance(snapshot.captured_at, datetime)
        else snapshot.captured_at
    )
    slots: dict[int, float | None] = {dte: None for dte in _ATM_TARGET_DTES}
    skip_reason: str | None = None

    expiration_30 = _nearest_expiration(snapshot.expirations, 30, as_of=as_of)
    if expiration_30 is None:
        skip_reason = "no_30d_expiration"
    else:
        slots[30] = _atm_iv_for_expiration(
            snapshot.contracts, expiration_30, snapshot.underlying_price
        )
        if slots[30] is None:
            skip_reason = "no_eligible_atm_iv"

    # 60d/90d are best-effort even when 30d succeeded — they only feed
    # term-structure displays, not the screener anchor.
    if slots[30] is not None:
        for target_dte in (60, 90):
            expiration = _nearest_expiration(snapshot.expirations, target_dte, as_of=as_of)
            if expiration is None:
                continue
            slots[target_dte] = _atm_iv_for_expiration(
                snapshot.contracts, expiration, snapshot.underlying_price
            )

    return IVSummary(
        underlying_price=snapshot.underlying_price,
        atm_iv_30d=slots[30],
        atm_iv_60d=slots[60],
        atm_iv_90d=slots[90],
        skip_reason=skip_reason,
    )


def persist_chain_with_iv_summary(snapshot: OptionChainSnapshot) -> tuple[str, bool]:
    """A6 per-ticker write: chain always persists, IV summary is best-effort.

    Returns `(snapshot_id, iv_summary_written)`. When `iv_summary_written`
    is False, a `data_provider_events('iv_summary_skipped')` audit row was
    recorded with the skip reason in `payload`.
    """
    from data.models import DataProviderEvent, DataProviderEventType, OptionIVHistory
    from data.storage import (
        insert_option_iv_history,
        record_data_provider_event,
        save_option_chain_snapshot,
    )

    snapshot_id = save_option_chain_snapshot(snapshot)

    summary = compute_iv_summary(snapshot)
    if not summary.is_eligible:
        skip_reason = summary.skip_reason or "no_eligible_atm_iv"
        record_data_provider_event(
            DataProviderEvent(
                event_type=DataProviderEventType.IV_SUMMARY_SKIPPED,
                ticker=snapshot.ticker,
                reason=skip_reason,
                payload={
                    "snapshot_id": snapshot_id,
                    "expirations": [exp.isoformat() for exp in snapshot.expirations],
                    "contract_count": len(snapshot.contracts),
                    "source": snapshot.source.value if hasattr(snapshot.source, "value") else str(snapshot.source),
                },
            )
        )
        logger.info(
            f"[{snapshot.ticker}] IV summary skipped ({skip_reason}); chain still persisted as {snapshot_id}"
        )
        return snapshot_id, False

    history = OptionIVHistory(
        ticker=snapshot.ticker,
        captured_at=snapshot.captured_at,
        underlying_price=summary.underlying_price,
        atm_iv_30d=summary.atm_iv_30d,
        atm_iv_60d=summary.atm_iv_60d,
        atm_iv_90d=summary.atm_iv_90d,
        term_structure_30_60=summary.term_structure_30_60,
        term_structure_60_90=summary.term_structure_60_90,
    )
    insert_option_iv_history(history)
    return snapshot_id, True

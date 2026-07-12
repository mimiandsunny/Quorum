from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from options.models import OptionChainSnapshot, OptionContractSnapshot, OptionRank, OptionType


@dataclass(frozen=True)
class LiquidityThresholds:
    """Saturation points for the liquidity score components (D9).

    Each field is the value at which the corresponding dimension scores 1.0.
    Below the target, the score scales linearly to 0.0.
    """

    volume_target: int = 500
    oi_target: int = 1000
    spread_target: float = 0.25


def _default_thresholds() -> LiquidityThresholds:
    try:
        from config import settings

        return LiquidityThresholds(
            volume_target=settings.option_liquidity_volume_target,
            oi_target=settings.option_liquidity_oi_target,
            spread_target=settings.option_liquidity_spread_target,
        )
    except Exception:
        return LiquidityThresholds()


def _round(value: float | None, digits: int = 4) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, digits)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return min(max(value, low), high)


def option_mid(contract: OptionContractSnapshot) -> float | None:
    """Mid-market price. Accepts bid >= 0 (OTM contracts often quote bid=0
    but ask > 0; that's a real market, not an absent quote)."""
    bid = contract.bid
    ask = contract.ask
    if bid is not None and ask is not None and bid >= 0 and ask > 0 and ask >= bid:
        return _round((bid + ask) / 2, 4)
    if contract.last_price is not None and contract.last_price > 0:
        return _round(contract.last_price, 4)
    return None


def option_spread_pct(contract: OptionContractSnapshot) -> float | None:
    """Bid/ask spread as fraction of mid. Accepts bid >= 0 (mirrors option_mid)."""
    bid = contract.bid
    ask = contract.ask
    if bid is None or ask is None or bid < 0 or ask <= 0 or ask < bid:
        return None
    mid = option_mid(contract)
    if not mid or mid <= 0:
        return None
    return _round((ask - bid) / mid)


def moneyness_pct(contract: OptionContractSnapshot, underlying_price: float) -> float | None:
    """Moneyness as fraction of underlying. Returns None when underlying is
    unusable (zero/negative) — callers must handle missing values explicitly
    rather than treating 0.0 as 'at the money' (the silent-bug shape)."""
    if underlying_price <= 0:
        return None
    if contract.option_type == OptionType.CALL:
        return _round((underlying_price - contract.strike) / underlying_price)
    return _round((contract.strike - underlying_price) / underlying_price)


def _dte(expiration: date, as_of: date) -> int:
    return max((expiration - as_of).days, 0)


def _iv_percentiles(contracts: list[OptionContractSnapshot]) -> dict[str, float]:
    """Cross-sectional IV percentile within a snapshot, using average-rank
    for ties (the statistically defensible choice; upper-rank biases toward
    extremes when many contracts share an IV)."""
    eligible = [
        c for c in contracts if c.implied_volatility is not None and c.implied_volatility > 0
    ]
    if len(eligible) < 2:
        return {}
    sorted_ivs = sorted(c.implied_volatility for c in eligible)
    n = len(sorted_ivs)
    rank_buckets: dict[float, list[int]] = defaultdict(list)
    for index, value in enumerate(sorted_ivs):
        rank_buckets[value].append(index + 1)
    average_rank = {value: sum(positions) / len(positions) for value, positions in rank_buckets.items()}
    percentiles: dict[str, float] = {}
    for contract in eligible:
        iv = contract.implied_volatility
        rank = average_rank[iv]
        pct = (rank - 1) / (n - 1)
        percentiles[contract.contract_symbol] = round(pct, 4)
    return percentiles


def _iv_label(iv_percentile: float | None) -> str:
    if iv_percentile is None:
        return "unknown"
    if iv_percentile <= 0.30:
        return "cheap"
    if iv_percentile >= 0.70:
        return "rich"
    return "fair"


def _liquidity_score(
    contract: OptionContractSnapshot,
    spread_pct: float | None,
    *,
    thresholds: LiquidityThresholds,
) -> float:
    volume = contract.volume or 0
    open_interest = contract.open_interest or 0
    volume_score = _clamp(volume / max(thresholds.volume_target, 1))
    oi_score = _clamp(open_interest / max(thresholds.oi_target, 1))
    spread_score = (
        0.15 if spread_pct is None else 1.0 - _clamp(spread_pct / max(thresholds.spread_target, 1e-9))
    )
    return round((volume_score * 0.35) + (oi_score * 0.35) + (spread_score * 0.30), 4)


def _flow_score(
    contract: OptionContractSnapshot,
    mid: float | None,
) -> tuple[float, float | None, float | None]:
    """Flow score uses the SAME volume/OI ratio that's reported to the UI.
    Previously the score used `volume / max(open_interest, 1)` (always
    defined) while the displayed ratio was `volume / open_interest` (None when
    OI=0). That divergence meant the rank score reflected a denominator the
    user couldn't see in the table (K13)."""
    volume = contract.volume or 0
    open_interest = contract.open_interest or 0
    volume_oi_ratio = volume / open_interest if open_interest > 0 else None
    premium_dollars = mid * volume * 100 if mid is not None and volume > 0 else None
    ratio_for_score = volume_oi_ratio if volume_oi_ratio is not None else 0.0
    score = (
        _clamp(ratio_for_score / 3) * 0.40
        + _clamp(volume / 1000) * 0.20
        + _clamp((premium_dollars or 0.0) / 1_000_000) * 0.40
    )
    return round(score, 4), _round(volume_oi_ratio), _round(premium_dollars, 2)


def _dte_score(dte: int) -> float:
    if 14 <= dte <= 60:
        return 1.0
    if 7 <= dte < 14 or 60 < dte <= 90:
        return 0.65
    if dte < 7:
        return 0.25
    return 0.35


def position_gamma_dollars(
    contract: OptionContractSnapshot, underlying_price: float
) -> float | None:
    """Position gamma exposure in dollars per 1% underlying move, assuming a
    long position in the contract. NOT dealer net GEX — that needs a dealer-
    positioning model (see TODOS.md TD-1). Renamed from the misleading
    `_gamma_exposure` (K1)."""
    if contract.gamma is None or contract.open_interest is None or underlying_price <= 0:
        return None
    sign = 1 if contract.option_type == OptionType.CALL else -1
    exposure = (
        sign
        * contract.gamma
        * contract.open_interest
        * 100
        * underlying_price
        * underlying_price
        * 0.01
    )
    return _round(exposure, 2)


def compute_tags(
    *,
    contract: OptionContractSnapshot,
    iv_label: str,
    spread_pct: float | None,
    flow_score: float,
    volume_oi_ratio: float | None,
    dte: int,
    moneyness: float | None,
) -> list[str]:
    """Tag computation extracted from rank composition (D10).

    Splitting tags from `rank_options_snapshot` means tweaking
    `unusual_flow` thresholds doesn't silently change scanner behavior;
    callers can re-tag without recomputing ranks.
    """
    tags = []
    if iv_label in {"cheap", "rich"}:
        tags.append(f"{iv_label}_vol")
    if flow_score >= 0.60 or ((contract.volume or 0) >= 100 and (volume_oi_ratio or 0) >= 1.0):
        tags.append("unusual_flow")
    if spread_pct is not None and spread_pct <= 0.08:
        tags.append("tight_spread")
    if dte <= 7:
        tags.append("near_expiry")
    if moneyness is not None and abs(moneyness) <= 0.03:
        tags.append("near_money")
    return tags


def rank_options_snapshot(
    snapshot: OptionChainSnapshot,
    *,
    as_of: date | None = None,
    limit: int | None = 20,
    thresholds: LiquidityThresholds | None = None,
) -> list[OptionRank]:
    """Rank an options snapshot. Pass ``limit=None`` to rank every contract
    (used by the flow scanner to avoid pre-truncation)."""
    as_of = as_of or snapshot.captured_at.date()
    thresholds = thresholds or _default_thresholds()
    iv_percentiles = _iv_percentiles(snapshot.contracts)
    ranks = []
    for contract in snapshot.contracts:
        dte = _dte(contract.expiration, as_of)
        mid = option_mid(contract)
        spread_pct = option_spread_pct(contract)
        liquidity_score = _liquidity_score(contract, spread_pct, thresholds=thresholds)
        flow_score, volume_oi_ratio, premium_dollars = _flow_score(contract, mid)
        iv_percentile = iv_percentiles.get(contract.contract_symbol)
        iv_label = _iv_label(iv_percentile)
        moneyness = moneyness_pct(contract, snapshot.underlying_price)
        iv_edge_score = abs((iv_percentile if iv_percentile is not None else 0.5) - 0.5) * 2
        rank_score = (
            liquidity_score * 0.35
            + flow_score * 0.25
            + iv_edge_score * 0.25
            + _dte_score(dte) * 0.15
        )
        rank = OptionRank(
            ticker=snapshot.ticker,
            contract_symbol=contract.contract_symbol,
            expiration=contract.expiration,
            option_type=contract.option_type,
            strike=contract.strike,
            dte=dte,
            bid=contract.bid,
            ask=contract.ask,
            mid=mid,
            spread_pct=spread_pct,
            implied_volatility=contract.implied_volatility,
            iv_percentile=iv_percentile,
            iv_label=iv_label,
            open_interest=contract.open_interest or 0,
            volume=contract.volume or 0,
            volume_oi_ratio=volume_oi_ratio,
            premium_dollars=premium_dollars,
            liquidity_score=liquidity_score,
            flow_score=flow_score,
            rank_score=round(rank_score, 4),
            moneyness_pct=moneyness,
            position_gamma_dollars=position_gamma_dollars(contract, snapshot.underlying_price),
            tags=compute_tags(
                contract=contract,
                iv_label=iv_label,
                spread_pct=spread_pct,
                flow_score=flow_score,
                volume_oi_ratio=volume_oi_ratio,
                dte=dte,
                moneyness=moneyness,
            ),
        )
        ranks.append(rank)
    ranked = sorted(ranks, key=lambda row: row.rank_score, reverse=True)
    return ranked if limit is None else ranked[:limit]


def summarize_options_snapshot(snapshot: OptionChainSnapshot, ranks: list[OptionRank]) -> dict:
    ivs = [
        contract.implied_volatility
        for contract in snapshot.contracts
        if contract.implied_volatility is not None and contract.implied_volatility > 0
    ]
    gamma_values = [
        rank.position_gamma_dollars for rank in ranks if rank.position_gamma_dollars is not None
    ]
    return {
        "ticker": snapshot.ticker,
        "snapshot_id": snapshot.snapshot_id,
        "captured_at": snapshot.captured_at,
        "underlying_price": snapshot.underlying_price,
        "expirations": len(snapshot.expirations),
        "contracts": len(snapshot.contracts),
        "avg_iv": _round(sum(ivs) / len(ivs)) if ivs else None,
        "cheap_vol": sum(1 for rank in ranks if rank.iv_label == "cheap"),
        "rich_vol": sum(1 for rank in ranks if rank.iv_label == "rich"),
        "unusual_flow": sum(1 for rank in ranks if "unusual_flow" in rank.tags),
        "net_position_gamma_dollars": _round(sum(gamma_values), 2) if gamma_values else None,
    }

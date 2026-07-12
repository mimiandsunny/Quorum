"""D4 — multileg thesis explainer (Wave 2 plan rev 4, decision 7 + C2).

Three strategies, no registry day 1 (decision 3 of CEO rev 2 — extract a
`OptionThesisStrategy(Protocol)` when the fourth thesis arrives):

  - `bullish_debit_spread` — buy ATM call, sell OTM call. Defined max loss
     and max profit; capital-efficient leverage for a bullish thesis with
     a price target.
  - `bearish_protective_put` — buy an OTM put. Single-leg hedge that pays
     when the underlying drops. Acts as both a thesis ("I expect down") and
     as the engine D2 uses on top of existing positions.
  - `neutral_iron_condor` — sell OTM call spread + sell OTM put spread.
     Profits if the underlying stays inside a range; defined max loss on
     each wing. Best when IV is rich (cheap_vol → expand the wings).

Coupling rule (rev 2 decision 2, refined by C2): options reads
recommendation, recommendation never reads options. This module lives in
`options/` precisely so the import direction stays one-way.

Decision 16 prompt-injection hardening: the LLM call sees STRUCTURED
fields only — never raw `contract_symbol` strings concatenated into the
prompt. The contract symbol is a deterministic ticker+date+strike+right
concat, so concatenating it would re-introduce the issue we wrapped around.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable

from data.models import (
    OptionThesisAttempt,
    OptionThesisLLMFailureReason,
    OptionThesisStatus,
)
from options.features import option_mid
from options.models import (
    OptionChainSnapshot,
    OptionContractSnapshot,
    OptionType,
)

logger = logging.getLogger(__name__)


# Strategy names (constants so the dispatcher doesn't typo a string and
# silently fall through to FAIL). Match the OptionThesisAttempt.strategy
# label set per plan A7.
BULLISH_DEBIT_SPREAD = "bullish_debit_spread"
BEARISH_PROTECTIVE_PUT = "bearish_protective_put"
NEUTRAL_IRON_CONDOR = "neutral_iron_condor"


# Selection windows: 21-60 DTE keeps theta decay manageable while giving
# the thesis time to play out. Tighter than D2's protective-put window
# because thesis trades are directional and we want them to expire while
# still useful, not slip into LEAPS territory.
_MIN_DTE: int = 21
_MAX_DTE: int = 60


@dataclass(frozen=True)
class ThesisLeg:
    """One leg of a multileg structure. `qty_sign` is +1 (long) / -1 (short).

    Mid is the snapshot mid-price; for a long leg we pay the mid (cost > 0),
    for a short leg we collect it (credit). The structured payload sums these
    signed per-share costs to derive the net debit/credit of the structure.
    """

    contract_symbol: str
    option_type: str  # 'call' | 'put'
    strike: float
    expiration: str  # ISO date
    qty_sign: int    # +1 long, -1 short
    mid: float
    delta: float | None = None


@dataclass(frozen=True)
class ThesisStructure:
    """Structured output of one strategy build. `narrative` stays None
    until the LLM call fills it (or the LLM call fails and we ship
    STRUCTURED_ONLY per decision 10).
    """

    ticker: str
    strategy: str
    legs: list[ThesisLeg]
    net_debit_per_share: float          # >0 = debit (you pay), <0 = credit
    max_profit_per_share: float | None  # None when unbounded (rare here)
    max_loss_per_share: float | None    # None when unbounded
    breakeven: list[float] = field(default_factory=list)
    rationale_inputs: dict = field(default_factory=dict)  # for the LLM call
    narrative: str | None = None

    def to_payload(self) -> dict:
        return {
            "ticker": self.ticker,
            "strategy": self.strategy,
            "legs": [
                {
                    "contract_symbol": leg.contract_symbol,
                    "option_type": leg.option_type,
                    "strike": leg.strike,
                    "expiration": leg.expiration,
                    "qty_sign": leg.qty_sign,
                    "mid": leg.mid,
                    "delta": leg.delta,
                }
                for leg in self.legs
            ],
            "net_debit_per_share": self.net_debit_per_share,
            "max_profit_per_share": self.max_profit_per_share,
            "max_loss_per_share": self.max_loss_per_share,
            "breakeven": self.breakeven,
            "rationale_inputs": self.rationale_inputs,
            "narrative": self.narrative,
        }


# ─── Strike-selection helpers ─────────────────────────────


def _dte(expiration: date, as_of: date) -> int:
    return (expiration - as_of).days


def _eligible_expirations(snapshot: OptionChainSnapshot, as_of: date) -> list[date]:
    return [exp for exp in snapshot.expirations if _MIN_DTE <= _dte(exp, as_of) <= _MAX_DTE]


def _pick_target_expiration(snapshot: OptionChainSnapshot, as_of: date) -> date | None:
    eligible = _eligible_expirations(snapshot, as_of)
    if not eligible:
        return None
    # Closest to 35 DTE — sweet spot for short-term directional theses.
    return min(eligible, key=lambda exp: abs(_dte(exp, as_of) - 35))


def _strike_table(
    snapshot: OptionChainSnapshot,
    expiration: date,
    option_type: OptionType,
) -> dict[float, OptionContractSnapshot]:
    """Map strike → contract for the given expiration + type.

    The chain may have multiple contracts at the same strike across
    different snapshots, but within ONE snapshot a (strike, type, exp)
    triple is unique. Defensive dedup just in case.
    """
    table: dict[float, OptionContractSnapshot] = {}
    for c in snapshot.contracts:
        if c.expiration != expiration or c.option_type != option_type:
            continue
        table.setdefault(c.strike, c)
    return table


def _nearest_strike(strikes: list[float], target: float) -> float | None:
    if not strikes:
        return None
    return min(strikes, key=lambda s: abs(s - target))


def _signed_premium(contract: OptionContractSnapshot, qty_sign: int) -> float | None:
    """Per-share premium with sign. Long = pay (positive cost), short = receive (negative)."""
    mid = option_mid(contract)
    if mid is None:
        return None
    return mid * (1 if qty_sign > 0 else -1)


def _leg(
    contract: OptionContractSnapshot,
    qty_sign: int,
    mid: float,
) -> ThesisLeg:
    return ThesisLeg(
        contract_symbol=contract.contract_symbol,
        option_type=contract.option_type.value,
        strike=contract.strike,
        expiration=contract.expiration.isoformat(),
        qty_sign=qty_sign,
        mid=round(mid, 4),
        delta=contract.delta,
    )


# ─── Strategy 1: bullish_debit_spread ─────────────────────


def bullish_debit_spread(
    snapshot: OptionChainSnapshot,
    *,
    as_of: date | None = None,
) -> ThesisStructure | None:
    """Buy a call near ATM, sell a call ~5% above. Net debit; defined max
    loss = debit paid; max profit = strike width − debit.
    """
    as_of = as_of or (
        snapshot.captured_at.date()
        if isinstance(snapshot.captured_at, datetime)
        else snapshot.captured_at
    )
    expiration = _pick_target_expiration(snapshot, as_of)
    if expiration is None:
        return None
    calls = _strike_table(snapshot, expiration, OptionType.CALL)
    if len(calls) < 2:
        return None
    spot = snapshot.underlying_price
    long_strike = _nearest_strike(list(calls.keys()), spot)
    short_target = spot * 1.05
    short_strike = _nearest_strike(
        [s for s in calls.keys() if s > long_strike], short_target
    )
    if long_strike is None or short_strike is None or short_strike <= long_strike:
        return None
    long_contract = calls[long_strike]
    short_contract = calls[short_strike]
    long_mid = option_mid(long_contract)
    short_mid = option_mid(short_contract)
    if long_mid is None or short_mid is None:
        return None
    net_debit = round(long_mid - short_mid, 4)
    if net_debit <= 0:
        # Defensive: a non-positive debit means short premium exceeds long
        # premium, which inverts the strategy. Bail rather than ship a
        # "spread" that's actually a credit structure mislabeled.
        return None
    width = short_strike - long_strike
    max_profit = round(width - net_debit, 4)
    max_loss = net_debit
    breakeven = round(long_strike + net_debit, 4)
    return ThesisStructure(
        ticker=snapshot.ticker,
        strategy=BULLISH_DEBIT_SPREAD,
        legs=[
            _leg(long_contract, +1, long_mid),
            _leg(short_contract, -1, short_mid),
        ],
        net_debit_per_share=net_debit,
        max_profit_per_share=max_profit,
        max_loss_per_share=max_loss,
        breakeven=[breakeven],
        rationale_inputs={
            "spot": round(spot, 4),
            "long_strike": long_strike,
            "short_strike": short_strike,
            "width": round(width, 4),
            "expiration": expiration.isoformat(),
        },
    )


# ─── Strategy 2: bearish_protective_put ───────────────────


def bearish_protective_put(
    snapshot: OptionChainSnapshot,
    *,
    as_of: date | None = None,
) -> ThesisStructure | None:
    """Buy an OTM put ~5% below spot. Loss capped at premium paid; max
    profit unbounded down to zero (strike − premium per share if assigned).
    """
    as_of = as_of or (
        snapshot.captured_at.date()
        if isinstance(snapshot.captured_at, datetime)
        else snapshot.captured_at
    )
    expiration = _pick_target_expiration(snapshot, as_of)
    if expiration is None:
        return None
    puts = _strike_table(snapshot, expiration, OptionType.PUT)
    if not puts:
        return None
    spot = snapshot.underlying_price
    target = spot * 0.95
    strike = _nearest_strike(list(puts.keys()), target)
    if strike is None:
        return None
    contract = puts[strike]
    mid = option_mid(contract)
    if mid is None or mid <= 0:
        return None
    net_debit = round(mid, 4)
    max_profit = round(strike - mid, 4)  # cap at strike (underlying → 0)
    breakeven = round(strike - mid, 4)
    return ThesisStructure(
        ticker=snapshot.ticker,
        strategy=BEARISH_PROTECTIVE_PUT,
        legs=[_leg(contract, +1, mid)],
        net_debit_per_share=net_debit,
        max_profit_per_share=max_profit,
        max_loss_per_share=net_debit,
        breakeven=[breakeven],
        rationale_inputs={
            "spot": round(spot, 4),
            "strike": strike,
            "expiration": expiration.isoformat(),
        },
    )


# ─── Strategy 3: neutral_iron_condor ──────────────────────


def neutral_iron_condor(
    snapshot: OptionChainSnapshot,
    *,
    as_of: date | None = None,
) -> ThesisStructure | None:
    """Sell an OTM call spread + sell an OTM put spread.

    Wings 5% OTM, width 5% on each side (so the strikes are ~5%/10% OTM
    for short/long on each wing). Net credit; defined max loss on each
    side = wing width − credit.
    """
    as_of = as_of or (
        snapshot.captured_at.date()
        if isinstance(snapshot.captured_at, datetime)
        else snapshot.captured_at
    )
    expiration = _pick_target_expiration(snapshot, as_of)
    if expiration is None:
        return None
    calls = _strike_table(snapshot, expiration, OptionType.CALL)
    puts = _strike_table(snapshot, expiration, OptionType.PUT)
    if len(calls) < 2 or len(puts) < 2:
        return None
    spot = snapshot.underlying_price
    # Call wing: short 5% OTM, long 10% OTM.
    short_call_strike = _nearest_strike(
        [s for s in calls.keys() if s > spot], spot * 1.05
    )
    long_call_strike = _nearest_strike(
        [s for s in calls.keys() if s > (short_call_strike or 0)], spot * 1.10
    )
    # Put wing: short 5% OTM, long 10% OTM.
    short_put_strike = _nearest_strike(
        [s for s in puts.keys() if s < spot], spot * 0.95
    )
    long_put_strike = _nearest_strike(
        [s for s in puts.keys() if s < (short_put_strike or float("inf"))], spot * 0.90
    )
    if None in (short_call_strike, long_call_strike, short_put_strike, long_put_strike):
        return None
    if long_call_strike <= short_call_strike or long_put_strike >= short_put_strike:
        return None
    legs_contracts: list[tuple[OptionContractSnapshot, int]] = [
        (calls[short_call_strike], -1),
        (calls[long_call_strike], +1),
        (puts[short_put_strike], -1),
        (puts[long_put_strike], +1),
    ]
    legs: list[ThesisLeg] = []
    total = 0.0
    for contract, sign in legs_contracts:
        mid = option_mid(contract)
        if mid is None:
            return None
        legs.append(_leg(contract, sign, mid))
        total += mid * sign
    net_debit = round(total, 4)
    if net_debit >= 0:
        # Iron condor MUST be a credit. If shorts collect less than longs
        # cost, the structure is mispriced or the chain is too thin — bail.
        return None
    credit = -net_debit
    call_wing_width = long_call_strike - short_call_strike
    put_wing_width = short_put_strike - long_put_strike
    max_loss_call = round(call_wing_width - credit, 4)
    max_loss_put = round(put_wing_width - credit, 4)
    max_loss = round(max(max_loss_call, max_loss_put), 4)
    upper_breakeven = round(short_call_strike + credit, 4)
    lower_breakeven = round(short_put_strike - credit, 4)
    return ThesisStructure(
        ticker=snapshot.ticker,
        strategy=NEUTRAL_IRON_CONDOR,
        legs=legs,
        net_debit_per_share=net_debit,
        max_profit_per_share=round(credit, 4),
        max_loss_per_share=max_loss,
        breakeven=[lower_breakeven, upper_breakeven],
        rationale_inputs={
            "spot": round(spot, 4),
            "short_call": short_call_strike,
            "long_call": long_call_strike,
            "short_put": short_put_strike,
            "long_put": long_put_strike,
            "expiration": expiration.isoformat(),
        },
    )


# ─── Dispatcher + LLM narrative ──────────────────────────


_STRATEGIES: dict[str, Callable[[OptionChainSnapshot], ThesisStructure | None]] = {
    BULLISH_DEBIT_SPREAD: bullish_debit_spread,
    BEARISH_PROTECTIVE_PUT: bearish_protective_put,
    NEUTRAL_IRON_CONDOR: neutral_iron_condor,
}


@dataclass(frozen=True)
class ThesisResult:
    """Return shape of `build_option_thesis`. Carries the structured payload
    plus the A7 status so the caller can route the UI (success → full panel,
    structured_only → "(rationale unavailable)" in the right column, fail →
    no thesis card at all).
    """

    status: OptionThesisStatus
    structure: ThesisStructure | None
    llm_failure_reason: OptionThesisLLMFailureReason | None = None
    elapsed_seconds: float = 0.0
    from_cache: bool = False


def _classify_llm_exception(exc: Exception) -> OptionThesisLLMFailureReason:
    """Bucket the exception into one of the A7 failure-reason labels.

    Heuristics on exception class names + messages — we don't take a hard
    dependency on a specific LLM SDK's exception hierarchy because the
    backends (cloud / local / deterministic) vary.
    """
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    if "timeout" in name or "timeout" in text:
        return OptionThesisLLMFailureReason.TIMEOUT
    if "refus" in text or "policy" in text:
        return OptionThesisLLMFailureReason.REFUSAL
    if any(token in text for token in ("invalid json", "malformed", "parse")):
        return OptionThesisLLMFailureReason.MALFORMED
    if any(token in text for token in ("empty", "no content", "blank")):
        return OptionThesisLLMFailureReason.EMPTY
    return OptionThesisLLMFailureReason.SERVER_ERROR


def _default_narrate(structure: ThesisStructure) -> str:
    """LLM-free narrative fallback (deterministic mode or LLM-disabled envs).

    Composes a one-paragraph rationale from the structured fields. Decision
    16 hardening preserved: no `contract_symbol` interpolation. Strike
    numbers and DTE are safe — they're numeric, not free text.
    """
    ri = structure.rationale_inputs
    spot = ri.get("spot")
    exp = ri.get("expiration")
    if structure.strategy == BULLISH_DEBIT_SPREAD:
        return (
            f"Buy the ${ri['long_strike']:.2f} call and sell the "
            f"${ri['short_strike']:.2f} call expiring {exp}. Net debit "
            f"${structure.net_debit_per_share:.2f} per share defines the "
            f"max loss; max profit ${structure.max_profit_per_share:.2f} "
            f"per share if the underlying closes at or above "
            f"${ri['short_strike']:.2f}. Breakeven "
            f"${structure.breakeven[0]:.2f}."
        )
    if structure.strategy == BEARISH_PROTECTIVE_PUT:
        return (
            f"Buy the ${ri['strike']:.2f} put expiring {exp}. Costs "
            f"${structure.net_debit_per_share:.2f} per share; pays when "
            f"the underlying drops below ${structure.breakeven[0]:.2f}, "
            f"max payout at strike."
        )
    if structure.strategy == NEUTRAL_IRON_CONDOR:
        return (
            f"Sell the ${ri['short_call']:.2f}/${ri['long_call']:.2f} "
            f"call spread and the ${ri['short_put']:.2f}/${ri['long_put']:.2f} "
            f"put spread expiring {exp}. Collects "
            f"${structure.max_profit_per_share:.2f} per share if the "
            f"underlying stays between ${structure.breakeven[0]:.2f} and "
            f"${structure.breakeven[1]:.2f}. Max loss "
            f"${structure.max_loss_per_share:.2f} per share on either wing."
        )
    return ""


def build_option_thesis(
    *,
    snapshot: OptionChainSnapshot,
    strategy: str,
    recommendation_id: str | None = None,
    as_of: date | None = None,
    narrate: Callable[[ThesisStructure], str] | None = None,
    record_attempt: bool = True,
    use_cache: bool = True,
) -> ThesisResult:
    """End-to-end thesis build with P1 cache + A7 audit.

    `narrate` is the optional LLM call. Default = deterministic templating
    (`_default_narrate`), so tests and environments without an LLM backend
    still ship a narrative. Pass a callable to swap in a real model call.

    Cache rule: if `use_cache` is True and a fresh cache row exists
    (chain_captured_at matches latest snapshot for this ticker), return
    it without recomputing. Otherwise compute, persist, return.
    """
    # P1 cache read. The dispatcher decides freshness via storage — see
    # `get_fresh_option_thesis_cache`'s docstring for the rule. Cache
    # short-circuits LLM cost on re-clicks.
    if use_cache:
        try:
            cached = _read_cache(
                ticker=snapshot.ticker,
                strategy=strategy,
                recommendation_id=recommendation_id,
            )
        except Exception as exc:  # storage offline / schema drift → recompute
            logger.warning(f"thesis cache read failed ({type(exc).__name__}): {exc}")
            cached = None
        if cached is not None:
            structure = _rehydrate_from_cache(cached)
            return ThesisResult(
                status=OptionThesisStatus(cached["llm_status"]),
                structure=structure,
                elapsed_seconds=0.0,
                from_cache=True,
            )

    started = time.monotonic()
    builder = _STRATEGIES.get(strategy)
    if builder is None:
        # Unknown strategy = FAIL. Record-and-return so the metric reflects it.
        elapsed = round(time.monotonic() - started, 3)
        if record_attempt:
            _record_attempt(
                ticker=snapshot.ticker,
                strategy=strategy,
                status=OptionThesisStatus.FAIL,
                reason=None,
                elapsed=elapsed,
            )
        return ThesisResult(status=OptionThesisStatus.FAIL, structure=None, elapsed_seconds=elapsed)

    structure = builder(snapshot, as_of=as_of)
    if structure is None:
        elapsed = round(time.monotonic() - started, 3)
        if record_attempt:
            _record_attempt(
                ticker=snapshot.ticker,
                strategy=strategy,
                status=OptionThesisStatus.FAIL,
                reason=None,
                elapsed=elapsed,
            )
        return ThesisResult(status=OptionThesisStatus.FAIL, structure=None, elapsed_seconds=elapsed)

    narrate_fn = narrate or _default_narrate
    narrative: str | None = None
    failure_reason: OptionThesisLLMFailureReason | None = None
    try:
        narrative = narrate_fn(structure)
        if not narrative or not narrative.strip():
            narrative = None
            failure_reason = OptionThesisLLMFailureReason.EMPTY
    except Exception as exc:
        logger.warning(
            f"[{snapshot.ticker}/{strategy}] thesis narrative call failed "
            f"({type(exc).__name__}: {exc}); shipping STRUCTURED_ONLY"
        )
        failure_reason = _classify_llm_exception(exc)

    status = (
        OptionThesisStatus.SUCCESS
        if narrative is not None
        else OptionThesisStatus.STRUCTURED_ONLY
    )
    enriched = ThesisStructure(
        ticker=structure.ticker,
        strategy=structure.strategy,
        legs=structure.legs,
        net_debit_per_share=structure.net_debit_per_share,
        max_profit_per_share=structure.max_profit_per_share,
        max_loss_per_share=structure.max_loss_per_share,
        breakeven=structure.breakeven,
        rationale_inputs=structure.rationale_inputs,
        narrative=narrative,
    )
    elapsed = round(time.monotonic() - started, 3)

    # P1 cache write — fail-open so a storage outage never breaks the UI.
    try:
        _write_cache(
            ticker=snapshot.ticker,
            strategy=strategy,
            recommendation_id=recommendation_id,
            chain_captured_at=snapshot.captured_at,
            structured_json=enriched.to_payload(),
            narrative_text=enriched.narrative,
            llm_status=status.value,
        )
    except Exception as exc:
        logger.warning(f"thesis cache write failed ({type(exc).__name__}): {exc}")

    if record_attempt:
        _record_attempt(
            ticker=snapshot.ticker,
            strategy=strategy,
            status=status,
            reason=failure_reason,
            elapsed=elapsed,
        )

    return ThesisResult(
        status=status,
        structure=enriched,
        llm_failure_reason=failure_reason,
        elapsed_seconds=elapsed,
    )


# ─── Storage seams (mockable in tests) ────────────────────


def _record_attempt(
    *,
    ticker: str,
    strategy: str,
    status: OptionThesisStatus,
    reason: OptionThesisLLMFailureReason | None,
    elapsed: float,
) -> None:
    from data.storage import record_option_thesis_attempt

    record_option_thesis_attempt(
        OptionThesisAttempt(
            ticker=ticker,
            strategy=strategy,
            status=status,
            llm_failure_reason=reason,
            elapsed_seconds=elapsed,
        )
    )


def _read_cache(
    *, ticker: str, strategy: str, recommendation_id: str | None
) -> dict | None:
    from data.storage import get_fresh_option_thesis_cache

    return get_fresh_option_thesis_cache(
        ticker=ticker, strategy=strategy, recommendation_id=recommendation_id
    )


def _write_cache(
    *,
    ticker: str,
    strategy: str,
    recommendation_id: str | None,
    chain_captured_at: datetime,
    structured_json: dict,
    narrative_text: str | None,
    llm_status: str,
) -> None:
    from data.storage import upsert_option_thesis_cache

    upsert_option_thesis_cache(
        ticker=ticker,
        strategy=strategy,
        recommendation_id=recommendation_id,
        chain_captured_at=chain_captured_at,
        structured_json=structured_json,
        narrative_text=narrative_text,
        llm_status=llm_status,
    )


def _rehydrate_from_cache(cached: dict) -> ThesisStructure:
    """Build a ThesisStructure from the cached JSON payload."""
    payload = cached["structured"]
    legs = [
        ThesisLeg(
            contract_symbol=leg["contract_symbol"],
            option_type=leg["option_type"],
            strike=leg["strike"],
            expiration=leg["expiration"],
            qty_sign=leg["qty_sign"],
            mid=leg["mid"],
            delta=leg.get("delta"),
        )
        for leg in payload.get("legs", [])
    ]
    return ThesisStructure(
        ticker=payload["ticker"],
        strategy=payload["strategy"],
        legs=legs,
        net_debit_per_share=payload["net_debit_per_share"],
        max_profit_per_share=payload.get("max_profit_per_share"),
        max_loss_per_share=payload.get("max_loss_per_share"),
        breakeven=payload.get("breakeven", []),
        rationale_inputs=payload.get("rationale_inputs", {}),
        narrative=payload.get("narrative") or cached.get("narrative"),
    )

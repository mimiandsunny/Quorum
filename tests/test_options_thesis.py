"""Tests for D4 — multileg thesis explainer (Wave 2 plan rev 4)."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from data.models import (
    OptionThesisAttempt,
    OptionThesisLLMFailureReason,
    OptionThesisStatus,
)
from options import thesis as thesis_mod
from options.models import (
    OptionChainSnapshot,
    OptionChainSource,
    OptionContractSnapshot,
    OptionType,
)
from options.thesis import (
    BEARISH_PROTECTIVE_PUT,
    BULLISH_DEBIT_SPREAD,
    NEUTRAL_IRON_CONDOR,
    ThesisResult,
    ThesisStructure,
    bearish_protective_put,
    build_option_thesis,
    bullish_debit_spread,
    neutral_iron_condor,
)


def _contract(
    *,
    option_type: OptionType,
    strike: float,
    bid: float,
    ask: float,
    delta: float | None = None,
    expiration: date = date(2026, 6, 20),
    ticker: str = "AAPL",
) -> OptionContractSnapshot:
    right = "C" if option_type == OptionType.CALL else "P"
    sym = f"{ticker}{expiration.strftime('%y%m%d')}{right}{int(strike * 1000):08d}"
    return OptionContractSnapshot(
        contract_symbol=sym,
        ticker=ticker,
        expiration=expiration,
        option_type=option_type,
        strike=strike,
        bid=bid,
        ask=ask,
        delta=delta,
        volume=100,
        open_interest=500,
    )


def _snapshot(
    *,
    contracts: list[OptionContractSnapshot] | None = None,
    underlying: float = 100.0,
    expiration: date = date(2026, 6, 20),
    captured_at: datetime = datetime(2026, 5, 16, 14, 0),
    ticker: str = "AAPL",
) -> OptionChainSnapshot:
    return OptionChainSnapshot(
        snapshot_id="snap-d4",
        ticker=ticker,
        captured_at=captured_at,
        source=OptionChainSource.IBKR,
        underlying_price=underlying,
        expirations=[expiration],
        contracts=contracts or [],
    )


def _full_chain_at(strikes_calls: list[tuple[float, float, float]],
                   strikes_puts: list[tuple[float, float, float]],
                   *, expiration: date = date(2026, 6, 20)) -> list[OptionContractSnapshot]:
    """Helper: build contracts from (strike, bid, ask) tuples."""
    return [
        _contract(option_type=OptionType.CALL, strike=s, bid=b, ask=a, expiration=expiration)
        for s, b, a in strikes_calls
    ] + [
        _contract(option_type=OptionType.PUT, strike=s, bid=b, ask=a, expiration=expiration)
        for s, b, a in strikes_puts
    ]


# ─── bullish_debit_spread ─────────────────────────────────


def test_bullish_debit_spread_picks_atm_long_and_otm_short():
    contracts = _full_chain_at(
        strikes_calls=[
            (95, 6.0, 6.2),
            (100, 3.0, 3.2),   # ATM long
            (105, 1.0, 1.2),   # ~5% OTM short
            (110, 0.4, 0.5),
        ],
        strikes_puts=[(95, 0.5, 0.6)],  # one put so calls dominate
    )
    result = bullish_debit_spread(_snapshot(contracts=contracts))
    assert result is not None
    assert result.strategy == BULLISH_DEBIT_SPREAD
    assert [l.qty_sign for l in result.legs] == [+1, -1]
    assert result.legs[0].strike == 100.0
    assert result.legs[1].strike == 105.0
    # Net debit = 3.1 (mid of 3.0/3.2) − 1.1 (mid of 1.0/1.2) = 2.0
    assert result.net_debit_per_share == 2.0
    # Width 5, debit 2 → max profit 3, max loss 2.
    assert result.max_profit_per_share == 3.0
    assert result.max_loss_per_share == 2.0
    assert result.breakeven == [102.0]


def test_bullish_debit_spread_returns_none_when_only_one_call_strike():
    contracts = [
        _contract(option_type=OptionType.CALL, strike=100, bid=3.0, ask=3.2),
    ]
    assert bullish_debit_spread(_snapshot(contracts=contracts)) is None


def test_bullish_debit_spread_returns_none_when_no_eligible_expiration():
    """A chain whose only expiration is 5 days out is below _MIN_DTE."""
    too_near = date(2026, 5, 21)
    contracts = _full_chain_at(
        strikes_calls=[(100, 3.0, 3.2), (105, 1.0, 1.2)],
        strikes_puts=[],
        expiration=too_near,
    )
    assert bullish_debit_spread(
        _snapshot(contracts=contracts, expiration=too_near), as_of=date(2026, 5, 16)
    ) is None


def test_bullish_debit_spread_rejects_non_positive_debit():
    # Long premium == short premium → net debit 0. Should bail.
    contracts = _full_chain_at(
        strikes_calls=[
            (100, 1.0, 1.2),
            (105, 1.0, 1.2),
        ],
        strikes_puts=[],
    )
    assert bullish_debit_spread(_snapshot(contracts=contracts)) is None


# ─── bearish_protective_put ───────────────────────────────


def test_bearish_protective_put_picks_5pct_otm_strike():
    contracts = _full_chain_at(
        strikes_calls=[(100, 3.0, 3.2)],
        strikes_puts=[
            (90, 0.5, 0.6),
            (95, 1.0, 1.2),  # ~5% OTM — winner
            (100, 3.0, 3.2),
        ],
    )
    result = bearish_protective_put(_snapshot(contracts=contracts))
    assert result is not None
    assert result.strategy == BEARISH_PROTECTIVE_PUT
    assert len(result.legs) == 1
    assert result.legs[0].strike == 95.0
    assert result.legs[0].qty_sign == +1
    assert result.net_debit_per_share == 1.1
    # Max profit = strike − premium = 95 − 1.1 = 93.9 (cap at strike collapse).
    assert result.max_profit_per_share == 93.9
    assert result.breakeven == [93.9]


def test_bearish_protective_put_returns_none_when_no_puts():
    contracts = _full_chain_at(
        strikes_calls=[(100, 3.0, 3.2)],
        strikes_puts=[],
    )
    assert bearish_protective_put(_snapshot(contracts=contracts)) is None


# ─── neutral_iron_condor ──────────────────────────────────


def test_neutral_iron_condor_constructs_four_legs():
    contracts = _full_chain_at(
        strikes_calls=[
            (105, 1.5, 1.7),   # short call (5% OTM)
            (110, 0.5, 0.7),   # long call (10% OTM)
        ],
        strikes_puts=[
            (95, 1.4, 1.6),    # short put (5% OTM)
            (90, 0.4, 0.6),    # long put (10% OTM)
        ],
    )
    result = neutral_iron_condor(_snapshot(contracts=contracts))
    assert result is not None
    assert result.strategy == NEUTRAL_IRON_CONDOR
    assert len(result.legs) == 4
    assert [l.qty_sign for l in result.legs] == [-1, +1, -1, +1]
    # Credit: shorts collect 1.6+1.5=3.1, longs cost 0.6+0.5=1.1 → credit 2.0.
    assert result.net_debit_per_share == -2.0
    assert result.max_profit_per_share == 2.0
    # Wing width 5, credit 2 → max loss 3 on either side.
    assert result.max_loss_per_share == 3.0
    # Breakevens: 95 − 2 = 93, 105 + 2 = 107.
    assert result.breakeven == [93.0, 107.0]


def test_neutral_iron_condor_returns_none_when_structure_is_a_debit():
    """Shorts collecting less than longs cost = not an iron condor."""
    contracts = _full_chain_at(
        strikes_calls=[(105, 0.1, 0.2), (110, 1.0, 1.2)],  # short cheap, long expensive
        strikes_puts=[(95, 0.1, 0.2), (90, 1.0, 1.2)],
    )
    assert neutral_iron_condor(_snapshot(contracts=contracts)) is None


# ─── build_option_thesis dispatcher ───────────────────────


@pytest.fixture(autouse=True)
def _stub_storage_seams(monkeypatch):
    """Default to no-op storage seams so tests don't hit Postgres.
    Individual tests override these as needed.
    """
    monkeypatch.setattr(thesis_mod, "_record_attempt", lambda **kw: None)
    monkeypatch.setattr(thesis_mod, "_read_cache", lambda **kw: None)
    monkeypatch.setattr(thesis_mod, "_write_cache", lambda **kw: None)


def test_build_option_thesis_success_with_default_narrate():
    contracts = _full_chain_at(
        strikes_calls=[
            (100, 3.0, 3.2),
            (105, 1.0, 1.2),
        ],
        strikes_puts=[(95, 0.5, 0.6)],
    )
    result = build_option_thesis(
        snapshot=_snapshot(contracts=contracts),
        strategy=BULLISH_DEBIT_SPREAD,
    )
    assert result.status == OptionThesisStatus.SUCCESS
    assert result.structure is not None
    assert result.structure.narrative is not None
    assert "Buy" in result.structure.narrative
    assert result.from_cache is False


def test_build_option_thesis_records_attempt_on_success(monkeypatch):
    recorded: list[OptionThesisAttempt] = []
    monkeypatch.setattr(
        thesis_mod,
        "_record_attempt",
        lambda **kw: recorded.append(kw),
    )
    contracts = _full_chain_at(
        strikes_calls=[(100, 3.0, 3.2), (105, 1.0, 1.2)],
        strikes_puts=[(95, 0.5, 0.6)],
    )
    build_option_thesis(
        snapshot=_snapshot(contracts=contracts),
        strategy=BULLISH_DEBIT_SPREAD,
    )
    assert len(recorded) == 1
    assert recorded[0]["status"] == OptionThesisStatus.SUCCESS
    assert recorded[0]["strategy"] == BULLISH_DEBIT_SPREAD


def test_build_option_thesis_structured_only_on_llm_failure(monkeypatch):
    recorded: list[dict] = []
    monkeypatch.setattr(thesis_mod, "_record_attempt", lambda **kw: recorded.append(kw))
    contracts = _full_chain_at(
        strikes_calls=[(100, 3.0, 3.2), (105, 1.0, 1.2)],
        strikes_puts=[(95, 0.5, 0.6)],
    )

    def _boom(structure):
        raise TimeoutError("model server timed out")

    result = build_option_thesis(
        snapshot=_snapshot(contracts=contracts),
        strategy=BULLISH_DEBIT_SPREAD,
        narrate=_boom,
    )
    assert result.status == OptionThesisStatus.STRUCTURED_ONLY
    assert result.structure is not None
    assert result.structure.narrative is None
    assert result.llm_failure_reason == OptionThesisLLMFailureReason.TIMEOUT
    assert recorded[0]["status"] == OptionThesisStatus.STRUCTURED_ONLY
    assert recorded[0]["reason"] == OptionThesisLLMFailureReason.TIMEOUT


def test_build_option_thesis_empty_narrative_classified_as_empty(monkeypatch):
    contracts = _full_chain_at(
        strikes_calls=[(100, 3.0, 3.2), (105, 1.0, 1.2)],
        strikes_puts=[(95, 0.5, 0.6)],
    )
    result = build_option_thesis(
        snapshot=_snapshot(contracts=contracts),
        strategy=BULLISH_DEBIT_SPREAD,
        narrate=lambda s: "",
    )
    assert result.status == OptionThesisStatus.STRUCTURED_ONLY
    assert result.llm_failure_reason == OptionThesisLLMFailureReason.EMPTY


def test_build_option_thesis_fail_when_strategy_unknown():
    result = build_option_thesis(snapshot=_snapshot(), strategy="not_a_strategy")
    assert result.status == OptionThesisStatus.FAIL
    assert result.structure is None


def test_build_option_thesis_fail_when_chain_too_thin():
    result = build_option_thesis(
        snapshot=_snapshot(),  # zero contracts
        strategy=BULLISH_DEBIT_SPREAD,
    )
    assert result.status == OptionThesisStatus.FAIL


def test_build_option_thesis_returns_cache_when_fresh(monkeypatch):
    """Fresh cache short-circuits computation — the builder shouldn't run."""
    cached_payload = {
        "ticker": "AAPL",
        "strategy": BULLISH_DEBIT_SPREAD,
        "legs": [
            {
                "contract_symbol": "AAPL260620C00100000",
                "option_type": "call",
                "strike": 100.0,
                "expiration": "2026-06-20",
                "qty_sign": +1,
                "mid": 3.1,
                "delta": 0.55,
            },
        ],
        "net_debit_per_share": 2.0,
        "max_profit_per_share": 3.0,
        "max_loss_per_share": 2.0,
        "breakeven": [102.0],
        "rationale_inputs": {},
        "narrative": "cached narrative",
    }
    monkeypatch.setattr(
        thesis_mod,
        "_read_cache",
        lambda **kw: {
            "structured": cached_payload,
            "narrative": "cached narrative",
            "llm_status": "success",
            "chain_captured_at": datetime(2026, 5, 16, 14, 0),
            "computed_at": datetime(2026, 5, 16, 14, 1),
        },
    )

    def _should_not_run(snapshot, **kw):
        raise AssertionError("strategy builder should not run on cache hit")

    monkeypatch.setattr(thesis_mod, "bullish_debit_spread", _should_not_run)
    result = build_option_thesis(
        snapshot=_snapshot(),
        strategy=BULLISH_DEBIT_SPREAD,
    )
    assert result.from_cache is True
    assert result.status == OptionThesisStatus.SUCCESS
    assert result.structure.narrative == "cached narrative"


def test_build_option_thesis_classify_llm_failure_reasons():
    cases = [
        (TimeoutError("timed out"), OptionThesisLLMFailureReason.TIMEOUT),
        (ValueError("invalid json"), OptionThesisLLMFailureReason.MALFORMED),
        (RuntimeError("policy violation"), OptionThesisLLMFailureReason.REFUSAL),
        (RuntimeError("empty response"), OptionThesisLLMFailureReason.EMPTY),
        (ConnectionError("upstream 503"), OptionThesisLLMFailureReason.SERVER_ERROR),
    ]
    for exc, expected in cases:
        assert thesis_mod._classify_llm_exception(exc) == expected

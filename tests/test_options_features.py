from datetime import date, datetime

from options.features import rank_options_snapshot, summarize_options_snapshot
from options.flow import scan_unusual_flow
from options.models import OptionChainSnapshot, OptionContractSnapshot, OptionType


def _contract(
    symbol: str,
    option_type: OptionType,
    strike: float,
    *,
    bid=1.0,
    ask=1.1,
    iv=0.30,
    volume=100,
    oi=200,
    expiration=date(2026, 5, 15),
):
    return OptionContractSnapshot(
        contract_symbol=symbol,
        ticker="NVDA",
        expiration=expiration,
        option_type=option_type,
        strike=strike,
        bid=bid,
        ask=ask,
        implied_volatility=iv,
        volume=volume,
        open_interest=oi,
        gamma=0.02,
    )


def _snapshot():
    return OptionChainSnapshot(
        snapshot_id="opt-snap-1",
        ticker="NVDA",
        captured_at=datetime(2026, 4, 29, 10, 0),
        source="test",
        underlying_price=100.0,
        expirations=[date(2026, 5, 15)],
        contracts=[
            _contract("NVDA260515C00100000", OptionType.CALL, 100, iv=0.20, volume=80, oi=1000),
            _contract("NVDA260515C00105000", OptionType.CALL, 105, iv=0.55, volume=1500, oi=200, bid=2.0, ask=2.1),
            _contract("NVDA260515P00095000", OptionType.PUT, 95, iv=0.35, volume=20, oi=400),
        ],
    )


def test_rank_options_snapshot_scores_iv_liquidity_and_flow():
    ranks = rank_options_snapshot(_snapshot(), as_of=date(2026, 4, 29), limit=3)

    assert ranks[0].contract_symbol == "NVDA260515C00105000"
    assert ranks[0].iv_label == "rich"
    assert "unusual_flow" in ranks[0].tags
    assert ranks[0].volume_oi_ratio == 7.5
    assert ranks[0].premium_dollars == 307500.0


def test_scan_unusual_flow_filters_by_volume_and_ratio():
    flow = scan_unusual_flow(_snapshot(), limit=5)

    assert [row.contract_symbol for row in flow] == ["NVDA260515C00105000"]


def test_summarize_options_snapshot_counts_iv_and_flow_tags():
    snapshot = _snapshot()
    ranks = rank_options_snapshot(snapshot, as_of=date(2026, 4, 29), limit=3)

    summary = summarize_options_snapshot(snapshot, ranks)

    assert summary["ticker"] == "NVDA"
    assert summary["contracts"] == 3
    assert summary["avg_iv"] == 0.3667
    assert summary["rich_vol"] >= 1
    assert summary["unusual_flow"] == 1


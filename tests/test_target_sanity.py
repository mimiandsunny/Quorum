from datetime import date

import main
from data.models import Decision, EvidenceItem, FinalSignal, ResearchCase, RiskVerdict
from recommendation.target_sanity import (
    choose_replacement_target,
    is_implausible_target,
    sanitize_research_case,
)


def test_implausible_target_detects_split_or_stale_outlier():
    assert is_implausible_target(650.0, 176.0) is True
    assert is_implausible_target(187.0, 176.0) is False


def test_replacement_target_prefers_nearby_trade_target():
    assert choose_replacement_target(
        "bull",
        176.0,
        fallback_targets=[187.0],
    ) == 187.0


def test_sanitize_research_case_replaces_outlier_and_records_note():
    case = ResearchCase(
        ticker="NVDA",
        stance="bull",
        thesis="Upside case cites a stale target.",
        evidence=[EvidenceItem(claim="momentum", data_citation="technical.summary", weight=0.7)],
        price_target=650.0,
        catalysts=["AI demand"],
        risks=[],
    )

    assert sanitize_research_case(case, 176.0, resistance_levels=[187.0]) is True

    assert case.price_target == 187.0
    assert "Original debate target $650.00" in case.thesis
    assert case.risks


def test_dashboard_sanitizes_stored_debate_outlier_without_showing_bad_thesis():
    signal = FinalSignal(
        ticker="NVDA",
        date=date(2026, 4, 29),
        decision=Decision.BUY,
        confidence=0.68,
        entry_zone=[176.0, 178.5],
        stop_loss=171.87,
        targets=[187.0],
        invalidation="Daily close below stop.",
        holding_period_days=5,
        thesis="Trade thesis.",
        bull_case="Bull",
        bear_case="Bear",
        risk_verdict=RiskVerdict.APPROVED,
        risk_reasons=[],
        position_size_pct=0.005,
        reward_risk_ratio=1.8,
    )
    debate = {
        "rounds": [{
            "round": 1,
            "bull": {
                "stance": "bull",
                "price_target": 650.0,
                "thesis": "This text repeats the stale $650 target.",
            },
            "bear": {
                "stance": "bear",
                "price_target": 165.0,
                "thesis": "Downside case.",
            },
        }]
    }

    sanitized = main._sanitize_debate_for_signal(debate, signal)
    bull = sanitized["rounds"][0]["bull"]

    assert bull["price_target"] == 187.0
    assert bull["target_sanity"]["original"] == 650.0

    html = main.templates.get_template("dashboard.html").render(
        signals=[signal],
        recommendations=[],
        report_date="2026-04-29",
        filter="all",
        score_map={},
        leaderboard=[],
        changelog=[],
        concentration_warnings=[],
        run_status=None,
        exec_stats={},
        recommendation_summary={},
        paper_trades_by_ticker={},
        strategy_summary={},
        strategies_meta=[],
        debate_by_ticker={"NVDA": sanitized},
        options_dashboard={"snapshots": [], "candidates": [], "summary": {}},
        option_refresh_tickers="NVDA",
        has_digest=False,
        digest_date=None,
    )

    assert "Bull · target $187.00 adjusted" in html
    assert "Outlier target $650.00 hidden" in html
    assert "This text repeats the stale $650 target." not in html

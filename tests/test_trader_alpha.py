from datetime import date, datetime

from agents import trader
from data.models import (
    AlphaOutput,
    AnalystReports,
    Decision,
    DebateTranscript,
    FundamentalsData,
    FundamentalsAnalysis,
    NewsAnalysis,
    OHLCVBar,
    RecommendationSide,
    RecommendationStrategyType,
    ResearchCase,
    SentimentAnalysis,
    TechnicalIndicators,
    TechnicalAnalysis,
    TickerDataPackage,
    TraderDecision,
    Trend,
)


def _reports():
    return AnalystReports(
        ticker="NVDA",
        technical=TechnicalAnalysis(
            ticker="NVDA",
            trend=Trend.NEUTRAL,
            key_levels={"support": [100.0], "resistance": [115.0]},
            pattern="range",
            momentum="mixed",
            summary="rangebound",
        ),
        fundamentals=FundamentalsAnalysis(
            ticker="NVDA",
            valuation_assessment="premium",
            growth_assessment="solid",
            financial_health="strong",
            sector_comparison="above average",
            summary="solid but expensive",
        ),
        sentiment=SentimentAnalysis(ticker="NVDA", overall_score=0.1, summary="mixed"),
        news=NewsAnalysis(ticker="NVDA", events=[], macro_context="neutral", summary="quiet"),
    )


def _debate():
    case = ResearchCase(
        ticker="NVDA",
        stance="bull",
        thesis="balanced evidence",
        evidence=[],
        price_target=115.0,
        catalysts=[],
        risks=[],
    )
    return DebateTranscript(ticker="NVDA", bull_case=case, bear_case=case)


def _data_package():
    return TickerDataPackage(
        ticker="NVDA",
        fetch_timestamp=datetime(2026, 4, 28, 9, 30),
        price_history=[
            OHLCVBar(
                date=date(2026, 4, 1),
                open=100.0,
                high=102.0,
                low=98.0,
                close=100.0,
                volume=1_000_000,
            )
        ],
        technicals=TechnicalIndicators(
            current_price=100.0,
            support_levels=[95.0, 98.0],
            resistance_levels=[105.0, 110.0],
        ),
        fundamentals=FundamentalsData(sector="Technology"),
    )


def test_trader_prompt_includes_alpha_outputs(monkeypatch):
    captured = {}

    def _stub(prompt, output_model, system=None, **kwargs):
        captured["prompt"] = prompt
        captured["system"] = system
        return TraderDecision(
            ticker="NVDA",
            date=date(2026, 4, 28),
            decision=Decision.HOLD,
            confidence=0.45,
            entry_zone=[100.0, 101.0],
            stop_loss=99.0,
            targets=[103.0],
            invalidation="wait",
            holding_period_days=5,
            thesis="weak deterministic setup",
        )

    monkeypatch.setattr(trader, "call_cloud", _stub)

    alpha = AlphaOutput(
        strategy_type=RecommendationStrategyType.SHORT_TERM,
        direction=RecommendationSide.FLAT,
        horizon_days=5,
        expected_return=0.0,
        expected_drawdown=0.0,
        confidence=0.35,
        evidence=["Insufficient deterministic edge"],
        invalidation="Wait for confirmation.",
    )

    decision = trader.decide(_reports(), _debate(), today=date(2026, 4, 28), alpha_outputs=[alpha])

    assert decision.decision == Decision.HOLD
    assert "DETERMINISTIC ALPHA ENGINE OUTPUTS" in captured["prompt"]
    assert '"direction": "flat"' in captured["prompt"]
    assert "default to HOLD" in captured["prompt"]
    assert "Deterministic alpha engines are guardrails" in captured["system"]


def test_trader_prompt_includes_atr_stop_and_target_guardrails(monkeypatch):
    captured = {}

    def _stub(prompt, output_model, system=None, **kwargs):
        captured["prompt"] = prompt
        captured["system"] = system
        return TraderDecision(
            ticker="NVDA",
            date=date(2026, 4, 28),
            decision=Decision.HOLD,
            confidence=0.45,
            entry_zone=[100.0, 101.0],
            stop_loss=95.0,
            targets=[107.0],
            invalidation="wait",
            holding_period_days=5,
            thesis="risk geometry is not attractive",
        )

    monkeypatch.setattr(trader, "call_cloud", _stub)
    monkeypatch.setattr(
        trader,
        "feature_payload",
        lambda _data: {"risk": {"atr_14_pct": 0.04}},
    )

    trader.decide(_reports(), _debate(), today=date(2026, 4, 28), data=_data_package())

    assert "TRADE GEOMETRY GUARDRAILS" in captured["prompt"]
    assert "Stop distance from entry midpoint must be >= 4.00% and <= 5.00%" in captured["prompt"]
    assert "First target distance from entry midpoint must be >= 4.00%" in captured["prompt"]
    assert "tiny stop" in captured["system"]


def test_trader_prompt_tells_model_to_hold_when_stop_window_impossible(monkeypatch):
    captured = {}

    def _stub(prompt, output_model, system=None, **kwargs):
        captured["prompt"] = prompt
        return TraderDecision(
            ticker="NVDA",
            date=date(2026, 4, 28),
            decision=Decision.HOLD,
            confidence=0.40,
            entry_zone=[100.0, 101.0],
            stop_loss=93.0,
            targets=[110.0],
            invalidation="wait",
            holding_period_days=5,
            thesis="volatility makes the stop window impossible",
        )

    monkeypatch.setattr(trader, "call_cloud", _stub)
    monkeypatch.setattr(
        trader,
        "feature_payload",
        lambda _data: {"risk": {"atr_14_pct": 0.0705}},
    )

    trader.decide(_reports(), _debate(), today=date(2026, 4, 28), data=_data_package())

    assert "Stop distance from entry midpoint must be >= 7.05% and <= 5.00%" in captured["prompt"]
    assert "choose HOLD; do not force a trade" in captured["prompt"]


def test_trader_prompt_includes_calibration_summary(monkeypatch):
    captured = {}

    def _stub(prompt, output_model, system=None, **kwargs):
        captured["prompt"] = prompt
        captured["system"] = system
        return TraderDecision(
            ticker="NVDA",
            date=date(2026, 4, 28),
            decision=Decision.HOLD,
            confidence=0.45,
            entry_zone=[100.0, 101.0],
            stop_loss=99.0,
            targets=[103.0],
            invalidation="wait",
            holding_period_days=5,
            thesis="calibration says be careful",
        )

    monkeypatch.setattr(trader, "call_cloud", _stub)

    decision = trader.decide(
        _reports(),
        _debate(),
        today=date(2026, 4, 28),
        calibration_summary=(
            "RECOMMENDATION CALIBRATION (recent scored recommendations):\n"
            "- short_term/long/h5/65-75: n=6, win=33%"
        ),
    )

    assert decision.decision == Decision.HOLD
    assert "RECOMMENDATION CALIBRATION" in captured["prompt"]
    assert "short_term/long/h5/65-75" in captured["prompt"]
    assert "calibration is a humility prior" in captured["system"]

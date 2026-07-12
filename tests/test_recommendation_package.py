from datetime import date, datetime

from data.models import (
    AlphaOutput,
    AnalystReports,
    Decision,
    DebateTranscript,
    FundamentalsAnalysis,
    FundamentalsData,
    MarketRegime,
    NewsAnalysis,
    NewsItem,
    OHLCVBar,
    PortfolioExposure,
    RegimeClassification,
    RecommendationSide,
    RecommendationStrategyType,
    ResearchCase,
    RiskAssessment,
    RiskVerdict,
    SentimentAnalysis,
    TechnicalAnalysis,
    TechnicalIndicators,
    TickerDataPackage,
    TraderDecision,
    Trend,
)
from recommendation.alpha_event import analyze as analyze_event_alpha
from recommendation.alpha_long import analyze as analyze_long_term_alpha
from recommendation.alpha_quant import analyze as analyze_quant_alpha
from recommendation.alpha_short import analyze as analyze_short_term_alpha
from recommendation.features import feature_payload
from recommendation.ledger import build_recommendation, strategy_type_from_alpha_outputs
from recommendation.portfolio import (
    build_portfolio_allocation,
    portfolio_exposures_from_open_positions,
)
from recommendation.quality import data_quality_flags
from recommendation.snapshots import build_data_snapshot


def _bars(count=25):
    rows = []
    for i in range(count):
        close = 100.0 + i
        rows.append(OHLCVBar(
            date=date(2026, 4, 1),
            open=close - 0.5,
            high=close + 1.0,
            low=close - 1.0,
            close=close,
            volume=1_000_000 + i,
        ))
    return rows


def _package(**fundamental_overrides):
    fundamental_values = {
        "sector": "Technology",
        "industry": "Semiconductors",
        "market_cap": 1_000_000_000,
        "revenue_growth": 0.25,
        "forward_pe": 24.0,
        "debt_to_equity": 0.4,
        "dividend_yield": 0.01,
    }
    fundamental_values.update(fundamental_overrides)
    fundamentals = FundamentalsData(**fundamental_values)
    return TickerDataPackage(
        ticker="NVDA",
        fetch_timestamp=datetime(2026, 4, 28, 9, 30),
        price_history=_bars(),
        technicals=TechnicalIndicators(
            current_price=124.0,
            rsi_14=61.0,
            macd=1.2,
            macd_signal=0.9,
            macd_histogram=0.3,
            ma_50=115.0,
            ma_200=100.0,
            support_levels=[118.0],
            resistance_levels=[130.0],
        ),
        fundamentals=fundamentals,
        news=[NewsItem(headline="AI demand remains strong", source="Test")],
    )


def _benchmark_bars(multiplier=0.5):
    rows = []
    for i, bar in enumerate(_bars()):
        close = 100.0 + i * multiplier
        rows.append(bar.model_copy(update={
            "open": close - 0.25,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
        }))
    return rows


def test_feature_payload_computes_returns_and_liquidity():
    features = feature_payload(_package())

    assert features["price_history_rows"] == 25
    assert features["returns"]["1d"] == 0.0081
    assert features["returns"]["5d"] == 0.042
    assert features["returns"]["20d"] == 0.1923
    assert features["technicals"]["distance_to_ma_50_pct"] == 0.0726
    assert features["liquidity"]["avg_dollar_volume_20d"] > 100_000_000
    assert features["liquidity"]["liquidity_score"] == 1.0
    assert features["risk"]["max_drawdown_20d"] == 0.0
    assert features["risk"]["beta_20d"] is None
    assert features["relative_strength"]["self_20d"] == 0.1923
    assert features["relative_strength"]["sector_benchmark"] == "XLK"
    assert features["technicals"]["nearest_support_distance_pct"] == 0.0484
    assert features["technicals"]["nearest_resistance_distance_pct"] == 0.0484
    assert features["fundamentals"]["debt_to_equity"] == 0.4


def test_feature_payload_adds_factor_ranks():
    features = feature_payload(_package())
    factors = features["factors"]

    assert factors["momentum_rank"] > 0.80
    assert factors["value_rank"] == 0.72
    assert factors["quality_rank"] == 0.81
    assert factors["low_vol_rank"] is not None
    assert factors["liquidity_rank"] == 1.0
    assert 0.0 <= factors["composite_rank"] <= 1.0


def test_feature_payload_computes_benchmark_relative_features_when_available():
    pkg = _package()
    pkg.benchmark_price_history = {
        "SPY": _benchmark_bars(multiplier=0.5),
        "QQQ": _benchmark_bars(multiplier=0.75),
        "XLK": _benchmark_bars(multiplier=0.6),
    }

    features = feature_payload(pkg)

    assert features["risk"]["beta_20d"] is not None
    assert features["risk"]["benchmark_sensitivity"] == features["risk"]["beta_20d"]
    assert features["relative_strength"]["vs_spy_20d"] > 0
    assert features["relative_strength"]["vs_qqq_20d"] > 0
    assert features["relative_strength"]["vs_sector_20d"] > 0


def test_data_quality_flags_catch_common_anomalies():
    pkg = _package(sector=None, industry=None, dividend_yield=0.87)
    pkg.news = []

    assert data_quality_flags(pkg) == [
        "dividend_yield_anomaly",
        "missing_industry",
        "missing_news",
        "missing_sector",
        "short_price_history",
    ]


def test_build_data_snapshot_includes_features_and_macro():
    regime = RegimeClassification(
        regime=MarketRegime.RISK_ON,
        confidence=0.7,
        key_factors=["AI breadth"],
        summary="Risk appetite is constructive.",
    )

    snapshot = build_data_snapshot(
        _package(),
        run_id="run-1",
        external_digest="daily digest",
        regime=regime,
    )

    assert snapshot.run_id == "run-1"
    assert snapshot.ticker == "NVDA"
    assert snapshot.feature_payload["returns"]["20d"] == 0.1923
    assert snapshot.macro_payload["external_digest"] == "daily digest"
    assert snapshot.macro_payload["regime"]["regime"] == "risk-on"
    assert snapshot.data_quality_flags == ["short_price_history"]


def test_short_term_alpha_detects_bullish_setup():
    alpha = analyze_short_term_alpha(_package())

    assert alpha.strategy_type.value == "short_term"
    assert alpha.direction == RecommendationSide.LONG
    assert alpha.confidence == 0.72
    assert alpha.expected_return == 0.0417
    assert alpha.expected_drawdown == -0.0209
    assert any("20-day return is positive" in item for item in alpha.evidence)


def test_long_term_alpha_detects_quality_growth_setup():
    alpha = analyze_long_term_alpha(_package(market_cap=25_000_000_000))

    assert alpha.strategy_type == RecommendationStrategyType.LONG_TERM
    assert alpha.direction == RecommendationSide.LONG
    assert alpha.horizon_days == 180
    assert alpha.confidence == 0.56
    assert alpha.expected_return == 0.1313
    assert alpha.expected_drawdown == -0.1069
    assert any("Revenue growth is strong" in item for item in alpha.evidence)
    assert any("institutional liquidity" in item for item in alpha.evidence)


def test_long_term_alpha_detects_poor_quality_short_setup():
    alpha = analyze_long_term_alpha(_package(
        market_cap=500_000_000,
        revenue_growth=-0.05,
        forward_pe=75.0,
        debt_to_equity=2.5,
    ))

    assert alpha.strategy_type == RecommendationStrategyType.LONG_TERM
    assert alpha.direction == RecommendationSide.SHORT
    assert alpha.confidence == 0.56
    assert alpha.expected_return == 0.1313
    assert alpha.expected_drawdown == -0.1069
    assert any("negative" in item for item in alpha.evidence)


def test_quant_alpha_detects_systematic_momentum_setup():
    alpha = analyze_quant_alpha(_package())

    assert alpha.strategy_type == RecommendationStrategyType.QUANT
    assert alpha.direction == RecommendationSide.LONG
    assert alpha.horizon_days == 20
    assert alpha.confidence == 0.61
    assert alpha.expected_return == 0.0412
    assert alpha.expected_drawdown == -0.0309
    assert any("20-day momentum is strong" in item for item in alpha.evidence)
    assert any("Liquidity score is strong" in item for item in alpha.evidence)


def test_quant_alpha_goes_short_on_broad_factor_weakness():
    weak_pkg = _package()
    weak_pkg.price_history = [
        bar.model_copy(update={"close": 124 - i, "open": 124 - i, "high": 125 - i, "low": 123 - i})
        for i, bar in enumerate(_bars())
    ]
    weak_pkg.technicals.current_price = 100.0
    weak_pkg.technicals.ma_50 = 112.0
    weak_pkg.technicals.ma_200 = 118.0
    weak_pkg.technicals.macd_histogram = -0.4

    alpha = analyze_quant_alpha(weak_pkg)

    assert alpha.strategy_type == RecommendationStrategyType.QUANT
    assert alpha.direction == RecommendationSide.SHORT
    assert alpha.confidence == 0.55
    assert alpha.expected_return == 0.0332
    assert alpha.expected_drawdown == -0.0249
    assert any("20-day momentum is weak" in item for item in alpha.evidence)


def test_event_alpha_blocks_imminent_earnings_as_flat_event():
    alpha = analyze_event_alpha(_package(earnings_date=date(2026, 4, 30)))

    assert alpha.strategy_type == RecommendationStrategyType.EVENT
    assert alpha.direction == RecommendationSide.FLAT
    assert alpha.horizon_days == 2
    assert alpha.confidence == 0.68
    assert alpha.expected_return == 0.0
    assert alpha.expected_drawdown == 0.0
    assert any("Earnings event is 2 day" in item for item in alpha.evidence)


def test_event_alpha_is_low_confidence_without_dated_event():
    alpha = analyze_event_alpha(_package())

    assert alpha.strategy_type == RecommendationStrategyType.EVENT
    assert alpha.direction == RecommendationSide.FLAT
    assert alpha.confidence == 0.30
    assert alpha.evidence == ["No explicit dated event available"]


def test_ledger_strategy_type_uses_strongest_aligned_alpha():
    strategy_type = strategy_type_from_alpha_outputs(
        side=RecommendationSide.LONG,
        fallback_horizon_days=5,
        alpha_outputs=[
            AlphaOutput(
                strategy_type=RecommendationStrategyType.SHORT_TERM,
                direction=RecommendationSide.LONG,
                horizon_days=5,
                confidence=0.52,
            ),
            AlphaOutput(
                strategy_type=RecommendationStrategyType.LONG_TERM,
                direction=RecommendationSide.LONG,
                horizon_days=180,
                confidence=0.68,
            ),
        ],
    )

    assert strategy_type == RecommendationStrategyType.LONG_TERM


def test_build_recommendation_maps_short_geometry():
    reports = AnalystReports(
        ticker="NVDA",
        technical=TechnicalAnalysis(
            ticker="NVDA",
            trend=Trend.BEARISH,
            key_levels={"support": [90.0], "resistance": [110.0]},
            pattern="lower highs",
            momentum="weak",
            summary="below key levels",
        ),
        fundamentals=FundamentalsAnalysis(
            ticker="NVDA",
            valuation_assessment="expensive",
            growth_assessment="slowing",
            financial_health="solid",
            sector_comparison="premium",
            summary="rich valuation",
        ),
        sentiment=SentimentAnalysis(ticker="NVDA", overall_score=-0.2, summary="mixed"),
        news=NewsAnalysis(ticker="NVDA", events=[], macro_context="risk-off", summary="weak"),
    )
    bear_case = ResearchCase(
        ticker="NVDA",
        stance="bear",
        thesis="downside",
        evidence=[],
        price_target=90.0,
        catalysts=[],
        risks=[],
    )
    debate = DebateTranscript(ticker="NVDA", bull_case=bear_case, bear_case=bear_case)
    trader_decision = TraderDecision(
        ticker="NVDA",
        date=date(2026, 4, 28),
        decision=Decision.SELL,
        confidence=0.7,
        entry_zone=[100.0, 102.0],
        stop_loss=106.0,
        targets=[91.0],
        invalidation="breakout above resistance",
        holding_period_days=5,
        thesis="short setup",
    )
    risk = RiskAssessment(
        ticker="NVDA",
        verdict=RiskVerdict.APPROVED,
        position_size_pct=0.02,
        reward_risk_ratio=2.0,
    )
    signal = trader_decision.model_dump()
    from data.models import FinalSignal

    recommendation = build_recommendation(
        signal=FinalSignal(
            ticker=signal["ticker"],
            date=signal["date"],
            decision=signal["decision"],
            confidence=signal["confidence"],
            entry_zone=signal["entry_zone"],
            stop_loss=signal["stop_loss"],
            targets=signal["targets"],
            invalidation=signal["invalidation"],
            holding_period_days=signal["holding_period_days"],
            thesis=signal["thesis"],
            bull_case="bull",
            bear_case="bear",
            risk_verdict=risk.verdict,
            risk_reasons=[],
            position_size_pct=risk.position_size_pct,
            reward_risk_ratio=risk.reward_risk_ratio,
            sector="Technology",
        ),
        reports=reports,
        debate=debate,
        trader_decision=trader_decision,
        risk_assessment=risk,
        alpha_outputs=[
            AlphaOutput(
                strategy_type=RecommendationStrategyType.SHORT_TERM,
                direction=RecommendationSide.SHORT,
                horizon_days=5,
                confidence=0.62,
                evidence=["negative momentum"],
            )
        ],
        run_id="run-1",
        snapshot_id="snap-1",
    )

    assert recommendation.side.value == "short"
    assert recommendation.expected_return == 0.099
    assert recommendation.expected_drawdown == -0.0495
    assert recommendation.sector_benchmark_symbol == "XLK"
    assert recommendation.portfolio_target_weight == 0.02
    assert recommendation.alpha_outputs[0].direction == RecommendationSide.SHORT
    allocation = recommendation.committee_outputs["portfolio_allocation"]
    assert allocation["target_weight"] == 0.02
    assert allocation["max_loss_budget"] == 0.001
    assert "alpha_aligned_moderate" in allocation["risk_budget_reason"]


def test_portfolio_allocation_trims_conflicting_alpha():
    risk = RiskAssessment(
        ticker="NVDA",
        verdict=RiskVerdict.APPROVED,
        position_size_pct=0.04,
        reward_risk_ratio=2.0,
    )

    allocation = build_portfolio_allocation(
        recommendation_id="rec-1",
        ticker="NVDA",
        side=RecommendationSide.LONG,
        confidence=0.72,
        risk_assessment=risk,
        expected_return=0.08,
        expected_drawdown=-0.04,
        alpha_outputs=[
            AlphaOutput(
                strategy_type=RecommendationStrategyType.SHORT_TERM,
                direction=RecommendationSide.SHORT,
                horizon_days=5,
                confidence=0.7,
                evidence=["negative momentum"],
            )
        ],
    )

    assert allocation.target_weight == 0.02
    assert allocation.max_loss_budget == 0.0008
    assert "alpha_conflict_trim" in allocation.risk_budget_reason


def test_portfolio_allocation_caps_loss_budget():
    risk = RiskAssessment(
        ticker="NVDA",
        verdict=RiskVerdict.APPROVED,
        position_size_pct=0.1,
        reward_risk_ratio=2.0,
    )

    allocation = build_portfolio_allocation(
        recommendation_id="rec-1",
        ticker="NVDA",
        side=RecommendationSide.LONG,
        confidence=0.9,
        risk_assessment=risk,
        expected_return=0.4,
        expected_drawdown=-0.2,
        alpha_outputs=[],
    )

    assert allocation.target_weight == 0.025
    assert allocation.max_loss_budget == 0.005
    assert "single_name_cap" in allocation.risk_budget_reason
    assert "loss_budget_cap" in allocation.risk_budget_reason


def test_portfolio_allocation_trims_high_expected_volatility():
    risk = RiskAssessment(
        ticker="NVDA",
        verdict=RiskVerdict.APPROVED,
        position_size_pct=0.04,
        reward_risk_ratio=2.0,
    )

    allocation = build_portfolio_allocation(
        recommendation_id="rec-1",
        ticker="NVDA",
        side=RecommendationSide.LONG,
        confidence=0.8,
        risk_assessment=risk,
        expected_return=0.08,
        expected_drawdown=-0.02,
        expected_volatility=0.90,
        alpha_outputs=[],
    )

    assert allocation.target_weight == 0.02
    assert "high_volatility_trim" in allocation.risk_budget_reason


def test_portfolio_allocation_caps_existing_sector_exposure():
    risk = RiskAssessment(
        ticker="NVDA",
        verdict=RiskVerdict.APPROVED,
        position_size_pct=0.04,
        reward_risk_ratio=4.0,
    )

    allocation = build_portfolio_allocation(
        recommendation_id="rec-1",
        ticker="NVDA",
        side=RecommendationSide.LONG,
        confidence=0.8,
        risk_assessment=risk,
        expected_return=0.08,
        expected_drawdown=-0.02,
        sector="Technology",
        current_exposures=[
            PortfolioExposure(
                ticker="AMD",
                side=RecommendationSide.LONG,
                sector="Technology",
                target_weight=0.145,
            )
        ],
    )

    assert allocation.target_weight == 0.005
    assert allocation.max_loss_budget == 0.0001
    assert "sector_exposure_cap" in allocation.risk_budget_reason


def test_portfolio_exposures_from_open_positions_normalizes_rows():
    exposures = portfolio_exposures_from_open_positions(
        [
            {
                "ticker": "NVDA",
                "side": "buy",
                "qty": 10,
                "avg_entry": 100,
                "current_price": 120,
                "sector": "Technology",
            },
            {
                "ticker": "TSLA",
                "side": "sell_short",
                "qty": 5,
                "avg_entry": 200,
                "current_price": None,
                "sector": "Consumer Cyclical",
            },
            {
                "ticker": "CASH",
                "side": "hold",
                "qty": 100,
                "avg_entry": 1,
                "current_price": 1,
            },
        ],
        account_value=100_000,
    )

    assert len(exposures) == 2
    assert exposures[0].ticker == "NVDA"
    assert exposures[0].side == RecommendationSide.LONG
    assert exposures[0].target_weight == 0.012
    assert exposures[0].sector == "Technology"
    assert exposures[1].ticker == "TSLA"
    assert exposures[1].side == RecommendationSide.SHORT
    assert exposures[1].target_weight == 0.01


def test_portfolio_allocation_caps_gross_and_short_exposure():
    risk = RiskAssessment(
        ticker="NVDA",
        verdict=RiskVerdict.APPROVED,
        position_size_pct=0.04,
        reward_risk_ratio=4.0,
    )

    gross_capped = build_portfolio_allocation(
        recommendation_id="rec-1",
        ticker="NVDA",
        side=RecommendationSide.LONG,
        confidence=0.8,
        risk_assessment=risk,
        expected_return=0.08,
        expected_drawdown=-0.02,
        current_exposures=[
            PortfolioExposure(
                ticker="AAPL",
                side=RecommendationSide.LONG,
                target_weight=0.29,
            )
        ],
    )
    short_capped = build_portfolio_allocation(
        recommendation_id="rec-2",
        ticker="NVDA",
        side=RecommendationSide.SHORT,
        confidence=0.8,
        risk_assessment=risk,
        expected_return=0.08,
        expected_drawdown=-0.02,
        current_exposures=[
            PortfolioExposure(
                ticker="TSLA",
                side=RecommendationSide.SHORT,
                target_weight=0.095,
            )
        ],
    )

    assert gross_capped.target_weight == 0.01
    assert "gross_exposure_cap" in gross_capped.risk_budget_reason
    assert short_capped.target_weight == 0.005
    assert "short_exposure_cap" in short_capped.risk_budget_reason


def test_portfolio_allocation_uses_cash_for_flat_or_rejected():
    risk = RiskAssessment(
        ticker="NVDA",
        verdict=RiskVerdict.REJECTED,
        position_size_pct=0.0,
        reward_risk_ratio=0.0,
        rejection_reasons=["low confidence"],
    )

    rejected = build_portfolio_allocation(
        recommendation_id="rec-1",
        ticker="NVDA",
        side=RecommendationSide.LONG,
        confidence=0.4,
        risk_assessment=risk,
        expected_return=0.05,
        expected_drawdown=-0.02,
    )
    flat = build_portfolio_allocation(
        recommendation_id="rec-2",
        ticker="NVDA",
        side=RecommendationSide.FLAT,
        confidence=0.7,
        risk_assessment=risk,
        expected_return=0.0,
        expected_drawdown=0.0,
    )

    assert rejected.target_weight == 0.0
    assert rejected.risk_budget_reason == "risk_rejected_cash_allocation"
    assert flat.target_weight == 0.0
    assert flat.risk_budget_reason == "flat_side_cash_allocation"

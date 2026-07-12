from uuid import uuid4

from config import settings
from data.models import (
    AnalystReports,
    AlphaOutput,
    DebateTranscript,
    Decision,
    FinalSignal,
    PortfolioExposure,
    Recommendation,
    RecommendationSide,
    RecommendationStrategyType,
    RiskAssessment,
    TraderDecision,
)
from recommendation.portfolio import build_portfolio_allocation


def side_for_decision(decision: Decision) -> RecommendationSide:
    if decision == Decision.BUY:
        return RecommendationSide.LONG
    if decision == Decision.SELL:
        return RecommendationSide.SHORT
    return RecommendationSide.FLAT


def expected_trade_shape(decision: TraderDecision) -> tuple[float | None, float | None]:
    """Expected target return and stop drawdown from trader geometry."""
    if not decision.entry_zone:
        return None, None
    entry_mid = (decision.entry_zone[0] + decision.entry_zone[1]) / 2
    if entry_mid <= 0:
        return None, None
    if decision.decision == Decision.HOLD:
        return 0.0, 0.0

    target = decision.targets[0] if decision.targets else entry_mid
    if decision.decision == Decision.SELL:
        expected_return = (entry_mid - target) / entry_mid
        expected_drawdown = (entry_mid - decision.stop_loss) / entry_mid
    else:
        expected_return = (target - entry_mid) / entry_mid
        expected_drawdown = (decision.stop_loss - entry_mid) / entry_mid
    return round(expected_return, 4), round(expected_drawdown, 4)


def sector_benchmark_symbol(sector: str | None) -> str | None:
    return {
        "Technology": "XLK",
        "Financial Services": "XLF",
        "Healthcare": "XLV",
        "Consumer Cyclical": "XLY",
        "Consumer Defensive": "XLP",
        "Energy": "XLE",
        "Industrials": "XLI",
        "Basic Materials": "XLB",
        "Communication Services": "XLC",
        "Utilities": "XLU",
        "Real Estate": "XLRE",
    }.get(sector or "")


def expected_volatility_from_alpha(alpha_outputs: list[AlphaOutput] | None) -> float | None:
    values = [
        alpha.expected_volatility
        for alpha in alpha_outputs or []
        if alpha.expected_volatility is not None
    ]
    return max(values) if values else None


def strategy_type_from_alpha_outputs(
    *,
    side: RecommendationSide,
    alpha_outputs: list[AlphaOutput] | None,
    fallback_horizon_days: int,
) -> RecommendationStrategyType:
    if side == RecommendationSide.FLAT:
        return RecommendationStrategyType.SHORT_TERM

    aligned = [
        alpha for alpha in alpha_outputs or []
        if alpha.direction == side
    ]
    if aligned:
        return max(aligned, key=lambda alpha: alpha.confidence).strategy_type

    if fallback_horizon_days >= 60:
        return RecommendationStrategyType.LONG_TERM
    if fallback_horizon_days >= 15:
        return RecommendationStrategyType.QUANT
    return RecommendationStrategyType.SHORT_TERM


def build_recommendation(
    *,
    signal: FinalSignal,
    reports: AnalystReports,
    debate: DebateTranscript,
    trader_decision: TraderDecision,
    risk_assessment: RiskAssessment,
    alpha_outputs: list[AlphaOutput] | None = None,
    current_exposures: list[PortfolioExposure] | None = None,
    run_id: str | None = None,
    snapshot_id: str | None = None,
) -> Recommendation:
    expected_return, expected_drawdown = expected_trade_shape(trader_decision)
    recommendation_id = signal.recommendation_id or uuid4().hex
    side = side_for_decision(signal.decision)
    strategy_type = strategy_type_from_alpha_outputs(
        side=side,
        alpha_outputs=alpha_outputs,
        fallback_horizon_days=signal.holding_period_days,
    )
    portfolio_allocation = build_portfolio_allocation(
        recommendation_id=recommendation_id,
        ticker=signal.ticker,
        side=side,
        confidence=signal.confidence,
        risk_assessment=risk_assessment,
        expected_return=expected_return,
        expected_drawdown=expected_drawdown,
        expected_volatility=expected_volatility_from_alpha(alpha_outputs),
        alpha_outputs=alpha_outputs,
        sector=signal.sector,
        current_exposures=current_exposures,
    )

    return Recommendation(
        recommendation_id=recommendation_id,
        run_id=run_id,
        snapshot_id=snapshot_id,
        ticker=signal.ticker,
        strategy_type=strategy_type,
        horizon_days=signal.holding_period_days,
        decision=signal.decision,
        side=side,
        confidence=signal.confidence,
        expected_return=expected_return,
        expected_drawdown=expected_drawdown,
        benchmark_symbol="SPY",
        sector_benchmark_symbol=sector_benchmark_symbol(signal.sector),
        entry_zone=signal.entry_zone,
        stop_loss=signal.stop_loss,
        targets=signal.targets,
        thesis=signal.thesis,
        invalidation=signal.invalidation,
        alpha_outputs=alpha_outputs or [],
        committee_outputs={
            "analyst_reports": reports.model_dump(mode="json"),
            "debate": debate.model_dump(mode="json"),
            "trader_decision": trader_decision.model_dump(mode="json"),
            "risk_assessment": risk_assessment.model_dump(mode="json"),
            "portfolio_allocation": portfolio_allocation.model_dump(mode="json"),
        },
        risk_verdict=risk_assessment.verdict,
        risk_reasons=risk_assessment.rejection_reasons,
        portfolio_target_weight=portfolio_allocation.target_weight,
        model_versions={
            "local_provider": settings.local_provider,
            "local_model": settings.local_model,
            "cloud_model": settings.cloud_model,
            "debate_rounds": settings.debate_rounds,
        },
    )

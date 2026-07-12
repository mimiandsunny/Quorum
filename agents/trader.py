"""Trader Agent — runs on CLOUD LLM.

Synthesizes analyst reports and bull/bear debate into a trading decision
with precise entry zone, stop loss, targets, and holding period.
"""

import json
from datetime import date

from agents.llm import call_cloud
from config import settings
from data.models import (
    AlphaOutput,
    AnalystReports,
    DebateTranscript,
    TickerDataPackage,
    RegimeClassification,
    RecommendationSide,
    TraderDecision,
)
from recommendation.features import feature_payload

SYSTEM = """You are a professional trader at an investment firm. You receive analyst
reports, a multi-round bull/bear debate, and (when available) an impartial
judge's verdict scoring that debate. You must synthesize everything into a
PRECISE trading decision.

Rules:
1. Entry zone must be a tight range (within 3% width).
2. Bracket geometry MUST be valid:
   - For BUY: stop_loss STRICTLY LESS than entry_zone[0], and EVERY target
     STRICTLY GREATER than entry_zone[1]. Targets must be in ASCENDING order.
   - For SELL: stop_loss STRICTLY GREATER than entry_zone[1], and EVERY
     target STRICTLY LESS than entry_zone[0]. Targets must be in DESCENDING order.
   - Stop loss must NEVER fall inside the entry zone.
3. Stop loss must be at a technically meaningful level (support/resistance)
   AND respect rule 2.
4. Stop distance must be realistic for the stock's normal movement. Do NOT use
   a tiny stop to manufacture an attractive reward/risk ratio.
5. Targets should be based on technical levels and fundamental catalysts.
6. Target distance must be realistic too. Do NOT use a target so close that
   normal market noise consumes most of the trade edge.
7. Reward/risk ratio (target1 distance / stop distance) should be at least 2.0.
8. JUDGE VERDICT (when present) is a strong weighting signal:
   - winner=bull, score>=6, confidence>=0.6 → favor BUY
   - winner=bear, score<=4, confidence>=0.6 → favor SELL or HOLD (do NOT BUY)
   - confidence<0.5 → judge is uncertain; use your own synthesis
   - If you disagree with a high-confidence judge verdict, EXPLAIN WHY in thesis.
9. Confidence must honestly reflect the strength of the setup (0.0 to 1.0).
10. If the setup is unclear OR judge contradicts your direction without strong
   reason to overrule, use HOLD.
11. Holding period must be realistic (1-10 days for swing trades).
12. Deterministic alpha engines are guardrails:
   - If all alpha engines are FLAT or confidence is below 0.50, favor HOLD.
   - If alpha engines conflict with your intended direction, explain why or HOLD.
   - Do not use narrative alone to rescue a weak deterministic setup.
13. Recommendation calibration is a humility prior. If a similar bucket has
   poor win/outperformance history, lower confidence or HOLD unless today's
   evidence is materially stronger.
14. Respond with ONLY valid JSON."""


def _format_judge_section(debate: DebateTranscript) -> str:
    """Render the judge verdict block for the trader prompt.

    Returns "" when judge is unavailable (D10 ISOLATION fallback) — trader
    falls back to legacy weighted-by-confidence synthesis.
    """
    jv = getattr(debate, "judge_verdict", None)
    if jv is None:
        return ""
    rounds_count = len(getattr(debate, "rounds", None) or [])
    conceded = ", ".join(jv.conceded_points) if jv.conceded_points else "none"
    return f"""JUDGE VERDICT (impartial reviewer of the {rounds_count}-round debate):
- Winner: {jv.winner.value.upper()}
- Score: {jv.score:.1f}/10 (>5 = bull-favored, <5 = bear-favored)
- Confidence: {jv.confidence:.0%}
- Swing argument: {jv.swing_argument}
- Conceded points: {conceded}

"""


def _format_alpha_section(alpha_outputs: list[AlphaOutput] | None) -> str:
    if not alpha_outputs:
        return ""
    rows = []
    for alpha in alpha_outputs:
        rows.append({
            "strategy_type": alpha.strategy_type.value,
            "direction": alpha.direction.value,
            "horizon_days": alpha.horizon_days,
            "expected_return": alpha.expected_return,
            "expected_drawdown": alpha.expected_drawdown,
            "expected_volatility": alpha.expected_volatility,
            "confidence": alpha.confidence,
            "evidence": alpha.evidence,
            "invalidation": alpha.invalidation,
        })
    flat_or_weak = all(
        alpha.direction == RecommendationSide.FLAT or alpha.confidence < 0.50
        for alpha in alpha_outputs
    )
    guidance = (
        "All deterministic alpha engines are flat/weak; default to HOLD unless the committee evidence is exceptional."
        if flat_or_weak
        else "Use these deterministic outputs as guardrails around the LLM synthesis."
    )
    return f"""DETERMINISTIC ALPHA ENGINE OUTPUTS:
{json.dumps(rows)}
Guidance: {guidance}

"""


def _format_calibration_section(calibration_summary: str | None) -> str:
    if not calibration_summary:
        return ""
    return f"""{calibration_summary}
Guidance: Use calibration as a prior on confidence, not as a substitute for current evidence.

"""


def _format_trade_geometry_section(data: TickerDataPackage | None) -> str:
    if data is None:
        return ""

    try:
        features = feature_payload(data)
    except Exception:
        return ""

    risk = features.get("risk", {})
    atr_pct = risk.get("atr_14_pct")
    coefficient = settings.paper_atr_floor_coefficient
    min_stop_pct = 0.01
    min_target_pct = 0.02
    max_target_pct = 0.15
    if coefficient > 0 and atr_pct is not None and atr_pct > 0:
        min_stop_pct = max(min_stop_pct, coefficient * atr_pct)
        min_target_pct = max(min_target_pct, coefficient * atr_pct)
        max_target_pct = max(max_target_pct, 3.0 * atr_pct)

    max_stop_pct = settings.max_stop_pct
    stop_guidance = (
        "If the minimum stop distance is wider than the max stop distance, "
        "choose HOLD; do not force a trade."
        if max_stop_pct > 0 and min_stop_pct > max_stop_pct
        else "Use support/resistance, but never place the stop inside this minimum distance."
    )
    atr_text = f"{atr_pct:.2%}" if atr_pct is not None else "unavailable"

    return f"""TRADE GEOMETRY GUARDRAILS:
- Current price: ${data.technicals.current_price:.2f}
- ATR_14: {atr_text}
- Entry zone width must stay <= {settings.max_entry_zone_width_pct:.0%}.
- Stop distance from entry midpoint must be >= {min_stop_pct:.2%} and <= {max_stop_pct:.2%}.
  For BUY: stop_loss <= entry_mid * (1 - {min_stop_pct:.4f}).
  For SELL: stop_loss >= entry_mid * (1 + {min_stop_pct:.4f}).
  {stop_guidance}
- First target distance from entry midpoint must be >= {min_target_pct:.2%}.
  For BUY: target1 >= entry_mid * (1 + {min_target_pct:.4f}).
  For SELL: target1 <= entry_mid * (1 - {min_target_pct:.4f}).
- Any target farther than {max_target_pct:.2%} from entry midpoint is likely fantasy for this horizon unless directly supported by a nearby technical level.

"""


def decide(
    reports: AnalystReports,
    debate: DebateTranscript,
    today: date | None = None,
    digest_summary=None,
    regime: RegimeClassification | None = None,
    alpha_outputs: list[AlphaOutput] | None = None,
    calibration_summary: str | None = None,
    data: TickerDataPackage | None = None,
) -> TraderDecision:
    today = today or date.today()

    macro_sections = ""
    if regime and regime.confidence > 0:
        factors = ", ".join(regime.key_factors) if regime.key_factors else "none"
        macro_sections += f"""
MACRO REGIME: {regime.regime.value} ({regime.confidence:.0%})
Key factors: {factors}
Summary: {regime.summary}
"""
    # Distilled digest summary (~200 tokens) replaces the raw 5-7K-token
    # digest dump. Same distillation as the analysts and researchers see.
    if digest_summary is not None:
        from agents.digest_distiller import format_digest_summary
        macro_sections += format_digest_summary(digest_summary)

    prompt = f"""ANALYST REPORTS FOR {reports.ticker}:

TECHNICAL:
- Trend: {reports.technical.trend.value}
- Current Price Context: {reports.technical.summary}
- Key Levels: Support {reports.technical.key_levels.get("support", [])}, Resistance {reports.technical.key_levels.get("resistance", [])}
- Momentum: {reports.technical.momentum}

FUNDAMENTALS:
- Summary: {reports.fundamentals.summary}

SENTIMENT:
- Score: {reports.sentiment.overall_score} | Summary: {reports.sentiment.summary}

NEWS:
- Summary: {reports.news.summary}

BULL CASE (confidence-weighted evidence):
- Thesis: {debate.bull_case.thesis}
- Price Target: ${debate.bull_case.price_target:.2f}
- Key Evidence: {json.dumps([{"claim": e.claim, "weight": e.weight} for e in debate.bull_case.evidence])}
- Catalysts: {debate.bull_case.catalysts}

BEAR CASE (confidence-weighted evidence):
- Thesis: {debate.bear_case.thesis}
- Price Target: ${debate.bear_case.price_target:.2f}
- Key Evidence: {json.dumps([{"claim": e.claim, "weight": e.weight} for e in debate.bear_case.evidence])}
- Risks: {debate.bear_case.catalysts}

{_format_judge_section(debate)}{_format_alpha_section(alpha_outputs)}{_format_calibration_section(calibration_summary)}{_format_trade_geometry_section(data)}{macro_sections}Make your trading decision for {reports.ticker}.

Respond with JSON:
{{
  "ticker": "{reports.ticker}",
  "date": "{today.isoformat()}",
  "decision": "BUY" | "SELL" | "HOLD",
  "confidence": <0.0-1.0>,
  "entry_zone": [<lower>, <upper>],
  "stop_loss": <price>,
  "targets": [<target1>, <target2 optional>],
  "invalidation": "specific condition that invalidates this trade",
  "holding_period_days": <1-10>,
  "thesis": "2-3 sentence synthesis of why this decision, citing bull and bear evidence"
}}"""

    return call_cloud(prompt, TraderDecision, system=SYSTEM,
                      ticker=reports.ticker, stage="trader")

"""Technical Analyst.

Receives pre-computed indicators (RSI, MACD, MAs, support/resistance).
Interprets the data into a structured technical assessment.
Produces FACTUAL analysis only — no buy/sell recommendations.
"""

import logging

from agents.analysts.comparison import record_analyst_comparison
from agents.llm import call_analyst
from config import settings
from data.models import TechnicalAnalysis, TickerDataPackage, Trend

logger = logging.getLogger(__name__)

SYSTEM = """You are a technical analyst. You receive pre-computed technical indicators
for a stock. Your job is to INTERPRET the data — identify the trend, chart patterns,
momentum state, and key levels. You produce FACTUAL observations only.
Do NOT make buy/sell recommendations. Do NOT predict future prices.
Respond with ONLY valid JSON."""


def analyze(data: TickerDataPackage) -> TechnicalAnalysis:
    baseline = _analyze_deterministic(data)
    if settings.analyst_mode == "deterministic":
        record_analyst_comparison(
            ticker=data.ticker,
            analyst="technical",
            baseline=baseline,
            mode=settings.analyst_mode,
            used_result="deterministic_only",
        )
        return baseline

    t = data.technicals
    recent_prices = data.price_history[-20:] if len(data.price_history) >= 20 else data.price_history
    rsi = f"{t.rsi_14:.1f}" if t.rsi_14 is not None else "N/A"
    macd = f"{t.macd:.3f}" if t.macd is not None else "N/A"
    macd_signal = f"{t.macd_signal:.3f}" if t.macd_signal is not None else "N/A"
    macd_histogram = f"{t.macd_histogram:.3f}" if t.macd_histogram is not None else "N/A"

    price_summary = "\n".join(
        f"  {bar.date}: O={bar.open} H={bar.high} L={bar.low} C={bar.close} V={bar.volume:,}"
        for bar in recent_prices[-10:]
    )

    prompt = f"""Analyze the technical setup for {data.ticker}.

CURRENT PRICE: ${t.current_price:.2f}

INDICATORS:
- RSI(14): {rsi}
- MACD: {macd} | Signal: {macd_signal} | Histogram: {macd_histogram}
- 50-day MA: {"$" + f"{t.ma_50:.2f}" if t.ma_50 is not None else "N/A"}
- 200-day MA: {"$" + f"{t.ma_200:.2f}" if t.ma_200 is not None else "N/A"}

KEY LEVELS:
- Support: {", ".join(f"${s:.2f}" for s in t.support_levels) or "None identified"}
- Resistance: {", ".join(f"${r:.2f}" for r in t.resistance_levels) or "None identified"}

RECENT PRICE ACTION (last 10 days):
{price_summary}
{_baseline_section(baseline)}

Respond with JSON:
{{
  "ticker": "{data.ticker}",
  "trend": "bullish" | "bearish" | "neutral",
  "key_levels": {{"support": [numbers], "resistance": [numbers]}},
  "pattern": "chart pattern identified or 'No clear pattern'",
  "momentum": "momentum assessment (strong/weak/diverging, etc.)",
  "summary": "2-3 sentence factual summary of the technical setup"
}}"""

    try:
        result = call_analyst(prompt, TechnicalAnalysis, system=SYSTEM,
                              ticker=data.ticker, stage="analyst.technical")
        record_analyst_comparison(
            ticker=data.ticker,
            analyst="technical",
            baseline=baseline,
            mode=settings.analyst_mode,
            used_result="llm",
            llm_result=result,
        )
        return result
    except Exception as e:
        if settings.analyst_fallback == "deterministic":
            logger.warning(f"[{data.ticker}] Technical LLM failed; using deterministic baseline: {e}")
            record_analyst_comparison(
                ticker=data.ticker,
                analyst="technical",
                baseline=baseline,
                mode=settings.analyst_mode,
                used_result="deterministic_fallback",
                error=f"{type(e).__name__}: {e}",
            )
            return baseline
        record_analyst_comparison(
            ticker=data.ticker,
            analyst="technical",
            baseline=baseline,
            mode=settings.analyst_mode,
            used_result="error",
            error=f"{type(e).__name__}: {e}",
        )
        raise


def _baseline_section(baseline: TechnicalAnalysis) -> str:
    if not settings.analyst_include_deterministic_baseline:
        return ""
    return f"""

RULE-BASED BASELINE (use as a sanity check, not a final answer):
- Trend: {baseline.trend.value}
- Pattern: {baseline.pattern}
- Momentum: {baseline.momentum}
- Key Levels: Support {baseline.key_levels.get("support", [])}, Resistance {baseline.key_levels.get("resistance", [])}
- Summary: {baseline.summary}
"""


def _analyze_deterministic(data: TickerDataPackage) -> TechnicalAnalysis:
    t = data.technicals
    current = t.current_price

    bullish_votes = 0
    bearish_votes = 0

    if t.ma_50 is not None:
        bullish_votes += current > t.ma_50
        bearish_votes += current < t.ma_50
    if t.ma_200 is not None:
        bullish_votes += current > t.ma_200
        bearish_votes += current < t.ma_200
    if t.ma_50 is not None and t.ma_200 is not None:
        bullish_votes += t.ma_50 > t.ma_200
        bearish_votes += t.ma_50 < t.ma_200
    if t.macd is not None and t.macd_signal is not None:
        bullish_votes += t.macd > t.macd_signal
        bearish_votes += t.macd < t.macd_signal
    if t.rsi_14 is not None:
        bullish_votes += t.rsi_14 >= 55
        bearish_votes += t.rsi_14 <= 45

    if bullish_votes >= bearish_votes + 2:
        trend = Trend.BULLISH
    elif bearish_votes >= bullish_votes + 2:
        trend = Trend.BEARISH
    else:
        trend = Trend.NEUTRAL

    pattern = _price_action_pattern(data)
    momentum = _momentum_summary(t.rsi_14, t.macd, t.macd_signal, t.macd_histogram)

    ma_parts = []
    if t.ma_50 is not None:
        ma_parts.append(f"50-day MA ${t.ma_50:.2f}")
    if t.ma_200 is not None:
        ma_parts.append(f"200-day MA ${t.ma_200:.2f}")
    ma_text = " and ".join(ma_parts) if ma_parts else "moving averages unavailable"

    level_text = (
        f"support {t.support_levels or 'none'} and resistance {t.resistance_levels or 'none'}"
    )
    rsi_text = f"{t.rsi_14:.1f}" if t.rsi_14 is not None else "N/A"
    summary = (
        f"{data.ticker} is classified as {trend.value} at ${current:.2f} based on "
        f"{ma_text}, RSI {rsi_text}, and MACD positioning. "
        f"Key levels show {level_text}; recent action: {pattern.lower()}."
    )

    return TechnicalAnalysis(
        ticker=data.ticker,
        trend=trend,
        key_levels={
            "support": t.support_levels,
            "resistance": t.resistance_levels,
        },
        pattern=pattern,
        momentum=momentum,
        summary=summary,
    )


def _price_action_pattern(data: TickerDataPackage) -> str:
    recent = data.price_history[-10:]
    if len(recent) < 5:
        return "No clear pattern"

    highs = [bar.high for bar in recent]
    lows = [bar.low for bar in recent]
    if highs[-1] > highs[0] and lows[-1] > lows[0]:
        return "Higher highs and higher lows"
    if highs[-1] < highs[0] and lows[-1] < lows[0]:
        return "Lower highs and lower lows"
    return "No clear pattern"


def _momentum_summary(
    rsi: float | None,
    macd: float | None,
    macd_signal: float | None,
    macd_histogram: float | None,
) -> str:
    if rsi is None:
        rsi_text = "RSI unavailable"
    elif rsi >= 70:
        rsi_text = f"RSI is overbought at {rsi:.1f}"
    elif rsi >= 55:
        rsi_text = f"RSI is bullish at {rsi:.1f}"
    elif rsi <= 30:
        rsi_text = f"RSI is oversold at {rsi:.1f}"
    elif rsi <= 45:
        rsi_text = f"RSI is bearish at {rsi:.1f}"
    else:
        rsi_text = f"RSI is neutral at {rsi:.1f}"

    if macd is None or macd_signal is None:
        macd_text = "MACD unavailable"
    elif macd > macd_signal:
        hist_text = f" with histogram {macd_histogram:.3f}" if macd_histogram is not None else ""
        macd_text = f"MACD is above the signal line{hist_text}"
    elif macd < macd_signal:
        hist_text = f" with histogram {macd_histogram:.3f}" if macd_histogram is not None else ""
        macd_text = f"MACD is below the signal line{hist_text}"
    else:
        macd_text = "MACD is flat against the signal line"

    return f"{rsi_text}; {macd_text}."

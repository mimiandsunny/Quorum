"""Fundamentals Analyst.

Receives pre-fetched fundamental data (P/E, EPS, revenue growth, etc.).
Produces a structured valuation and financial health assessment.
FACTUAL analysis only — no buy/sell recommendations.
"""

import logging

from agents.analysts.comparison import record_analyst_comparison
from agents.llm import call_analyst
from config import settings
from data.models import FundamentalsAnalysis, TickerDataPackage

logger = logging.getLogger(__name__)

SYSTEM = """You are a fundamentals analyst. You receive pre-fetched fundamental data
for a stock. Your job is to ASSESS valuation, growth trajectory, and financial health
relative to the sector. You produce FACTUAL observations only.
Do NOT make buy/sell recommendations.
Respond with ONLY valid JSON."""


def analyze(data: TickerDataPackage) -> FundamentalsAnalysis:
    baseline = _analyze_deterministic(data)
    if settings.analyst_mode == "deterministic":
        record_analyst_comparison(
            ticker=data.ticker,
            analyst="fundamentals",
            baseline=baseline,
            mode=settings.analyst_mode,
            used_result="deterministic_only",
        )
        return baseline

    f = data.fundamentals

    prompt = f"""Analyze the fundamentals for {data.ticker}.

VALUATION:
- Trailing P/E: {f.pe_ratio or "N/A"}
- Forward P/E: {f.forward_pe or "N/A"}
- EPS (trailing): {f.eps or "N/A"}

GROWTH:
- Revenue Growth: {f"{f.revenue_growth:.1%}" if f.revenue_growth is not None else "N/A"}

FINANCIAL HEALTH:
- Debt/Equity: {f.debt_to_equity or "N/A"}
- Market Cap: {"$" + f"{f.market_cap:,.0f}" if f.market_cap else "N/A"}
- Dividend Yield: {f"{f.dividend_yield:.2%}" if f.dividend_yield is not None else "N/A"}

CONTEXT:
- Sector: {f.sector or "N/A"}
- Industry: {f.industry or "N/A"}
- Next Earnings: {f.earnings_date or "N/A"}

{"⚠️ Note: fundamentals data may be stale." if "fundamentals" in data.stale_sources else ""}
{_baseline_section(baseline)}

Respond with JSON:
{{
  "ticker": "{data.ticker}",
  "valuation_assessment": "Is it cheap, fair, or expensive vs sector? Cite specific ratios.",
  "growth_assessment": "Revenue/earnings trajectory assessment with numbers.",
  "financial_health": "Balance sheet strength assessment.",
  "sector_comparison": "How does it compare to sector averages?",
  "summary": "2-3 sentence factual summary of fundamental picture"
}}"""

    try:
        result = call_analyst(prompt, FundamentalsAnalysis, system=SYSTEM,
                              ticker=data.ticker, stage="analyst.fundamentals")
        record_analyst_comparison(
            ticker=data.ticker,
            analyst="fundamentals",
            baseline=baseline,
            mode=settings.analyst_mode,
            used_result="llm",
            llm_result=result,
        )
        return result
    except Exception as e:
        if settings.analyst_fallback == "deterministic":
            logger.warning(f"[{data.ticker}] Fundamentals LLM failed; using deterministic baseline: {e}")
            record_analyst_comparison(
                ticker=data.ticker,
                analyst="fundamentals",
                baseline=baseline,
                mode=settings.analyst_mode,
                used_result="deterministic_fallback",
                error=f"{type(e).__name__}: {e}",
            )
            return baseline
        record_analyst_comparison(
            ticker=data.ticker,
            analyst="fundamentals",
            baseline=baseline,
            mode=settings.analyst_mode,
            used_result="error",
            error=f"{type(e).__name__}: {e}",
        )
        raise


def _baseline_section(baseline: FundamentalsAnalysis) -> str:
    if not settings.analyst_include_deterministic_baseline:
        return ""
    return f"""

RULE-BASED BASELINE (use as a sanity check, not a final answer):
- Valuation: {baseline.valuation_assessment}
- Growth: {baseline.growth_assessment}
- Financial Health: {baseline.financial_health}
- Sector Comparison: {baseline.sector_comparison}
- Summary: {baseline.summary}
"""


def _analyze_deterministic(data: TickerDataPackage) -> FundamentalsAnalysis:
    f = data.fundamentals

    valuation = _valuation_assessment(f.pe_ratio, f.forward_pe)
    growth = _growth_assessment(f.revenue_growth, f.eps)
    health = _financial_health(f.debt_to_equity, f.market_cap, f.dividend_yield)

    if f.sector or f.industry:
        sector_comparison = (
            f"Sector context is {f.sector or 'N/A'} / {f.industry or 'N/A'}; "
            "sector-average valuation data is not available in the fetched package."
        )
    else:
        sector_comparison = "Sector and industry classifications are unavailable, so sector comparison is limited."

    stale_note = " Fundamentals data may be stale." if "fundamentals" in data.stale_sources else ""
    summary = f"{valuation} {growth} {health}{stale_note}"

    return FundamentalsAnalysis(
        ticker=data.ticker,
        valuation_assessment=valuation,
        growth_assessment=growth,
        financial_health=health,
        sector_comparison=sector_comparison,
        summary=summary,
    )


def _valuation_assessment(pe_ratio: float | None, forward_pe: float | None) -> str:
    parts = []
    if pe_ratio is None:
        parts.append("Trailing P/E is unavailable.")
    elif pe_ratio < 15:
        parts.append(f"Trailing P/E is {pe_ratio:.2f}, which screens inexpensive on an absolute basis.")
    elif pe_ratio <= 25:
        parts.append(f"Trailing P/E is {pe_ratio:.2f}, which screens moderate on an absolute basis.")
    else:
        parts.append(f"Trailing P/E is {pe_ratio:.2f}, which screens elevated on an absolute basis.")

    if forward_pe is not None:
        parts.append(f"Forward P/E is {forward_pe:.2f}.")
    return " ".join(parts)


def _growth_assessment(revenue_growth: float | None, eps: float | None) -> str:
    parts = []
    if revenue_growth is None:
        parts.append("Revenue growth is unavailable.")
    elif revenue_growth > 0.10:
        parts.append(f"Revenue growth is strong at {revenue_growth:.1%}.")
    elif revenue_growth > 0:
        parts.append(f"Revenue growth is positive at {revenue_growth:.1%}.")
    else:
        parts.append(f"Revenue growth is negative at {revenue_growth:.1%}.")

    if eps is None:
        parts.append("Trailing EPS is unavailable.")
    else:
        parts.append(f"Trailing EPS is {eps:.2f}.")
    return " ".join(parts)


def _financial_health(
    debt_to_equity: float | None,
    market_cap: float | None,
    dividend_yield: float | None,
) -> str:
    parts = []
    if debt_to_equity is None:
        parts.append("Debt/equity is unavailable.")
    elif debt_to_equity < 1:
        parts.append(f"Debt/equity is {debt_to_equity:.2f}, indicating modest leverage.")
    elif debt_to_equity <= 2:
        parts.append(f"Debt/equity is {debt_to_equity:.2f}, indicating moderate leverage.")
    else:
        parts.append(f"Debt/equity is {debt_to_equity:.2f}, indicating elevated leverage.")

    if market_cap is not None:
        parts.append(f"Market cap is ${market_cap:,.0f}.")

    if dividend_yield is None:
        parts.append("Dividend yield is unavailable.")
    elif dividend_yield > 0.20:
        parts.append(f"Dividend yield is {dividend_yield:.2%}, which looks like a data anomaly.")
    else:
        parts.append(f"Dividend yield is {dividend_yield:.2%}.")

    return " ".join(parts)

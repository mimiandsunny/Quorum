"""News Analyst.

Receives pre-fetched news items. Categorizes events by impact level and
provides macro context. FACTUAL analysis only.
"""

import logging

from agents.analysts.comparison import record_analyst_comparison
from agents.digest_distiller import format_digest_summary
from agents.llm import call_analyst
from config import settings
from data.models import NewsAnalysis, TickerDataPackage

logger = logging.getLogger(__name__)

SYSTEM = """You are a news analyst covering financial markets. You receive recent news
for a stock. Your job is to CATEGORIZE events by impact level (high/medium/low),
assess their relevance to the stock, and provide macro context.
Produce FACTUAL observations only. Do NOT make buy/sell recommendations.
Respond with ONLY valid JSON."""


def analyze(data: TickerDataPackage) -> NewsAnalysis:
    if not data.news:
        baseline = NewsAnalysis(
            ticker=data.ticker,
            events=[],
            macro_context="No recent news available.",
            summary="No recent news available for analysis.",
        )
        record_analyst_comparison(
            ticker=data.ticker,
            analyst="news",
            baseline=baseline,
            mode=settings.analyst_mode,
            used_result="no_news",
        )
        return baseline

    baseline = _analyze_deterministic(data)
    if settings.analyst_mode == "deterministic":
        record_analyst_comparison(
            ticker=data.ticker,
            analyst="news",
            baseline=baseline,
            mode=settings.analyst_mode,
            used_result="deterministic_only",
        )
        return baseline

    news_text = "\n".join(
        f"- [{item.source}] {item.headline}"
        + (f"\n  Summary: {item.summary}" if item.summary else "")
        for item in data.news[:10]
    )

    # Use the distilled digest summary (~200 tokens) instead of the raw
    # 5-7K-token blob. Distilled once per run in `run_all()`; this analyst
    # gets the same compact view as researchers + trader.
    digest_section = format_digest_summary(data.digest_summary)

    prompt = f"""Analyze recent news for {data.ticker}.

RECENT NEWS:
{news_text}

COMPANY CONTEXT:
- Sector: {data.fundamentals.sector or "N/A"}
- Industry: {data.fundamentals.industry or "N/A"}
{digest_section}
{"⚠️ Note: news data may be stale." if "news" in data.stale_sources else ""}
{_baseline_section(baseline)}

Respond with JSON:
{{
  "ticker": "{data.ticker}",
  "events": [
    {{
      "headline": "exact headline text",
      "impact": "high" | "medium" | "low",
      "relevance": "why this matters for the stock"
    }}
  ],
  "macro_context": "broader market/economic context that affects this stock",
  "summary": "2-3 sentence factual summary of the news landscape"
}}"""

    try:
        result = call_analyst(prompt, NewsAnalysis, system=SYSTEM,
                              ticker=data.ticker, stage="analyst.news")
        record_analyst_comparison(
            ticker=data.ticker,
            analyst="news",
            baseline=baseline,
            mode=settings.analyst_mode,
            used_result="llm",
            llm_result=result,
        )
        return result
    except Exception as e:
        if settings.analyst_fallback == "deterministic":
            logger.warning(f"[{data.ticker}] News LLM failed; using deterministic baseline: {e}")
            record_analyst_comparison(
                ticker=data.ticker,
                analyst="news",
                baseline=baseline,
                mode=settings.analyst_mode,
                used_result="deterministic_fallback",
                error=f"{type(e).__name__}: {e}",
            )
            return baseline
        record_analyst_comparison(
            ticker=data.ticker,
            analyst="news",
            baseline=baseline,
            mode=settings.analyst_mode,
            used_result="error",
            error=f"{type(e).__name__}: {e}",
        )
        raise


def _baseline_section(baseline: NewsAnalysis) -> str:
    if not settings.analyst_include_deterministic_baseline:
        return ""
    return f"""

RULE-BASED BASELINE (use as a sanity check, not a final answer):
- Macro Context: {baseline.macro_context}
- Summary: {baseline.summary}
- Events: {baseline.events}
"""


HIGH_IMPACT_TERMS = (
    "acquisition",
    "bankruptcy",
    "cpi",
    "earnings",
    "fed",
    "federal reserve",
    "fraud",
    "guidance",
    "inflation",
    "investigation",
    "jobs report",
    "lawsuit",
    "merger",
    "rate cut",
    "rate hike",
    "recession",
    "sec",
)

MEDIUM_IMPACT_TERMS = (
    "ai",
    "analyst",
    "buyback",
    "dividend",
    "downgrade",
    "etf",
    "launch",
    "partnership",
    "rally",
    "rebound",
    "record",
    "upgrade",
)


def _analyze_deterministic(data: TickerDataPackage) -> NewsAnalysis:
    events = []
    for item in data.news[:5]:
        text = f"{item.headline} {item.summary}".lower()
        impact = _impact_for_text(text)
        events.append({
            "headline": item.headline,
            "impact": impact,
            "relevance": _relevance_for_item(data, impact, item.summary),
        })

    high_count = sum(1 for event in events if event["impact"] == "high")
    medium_count = sum(1 for event in events if event["impact"] == "medium")

    if data.digest_summary is not None:
        macro_context = (
            "Distilled macro digest is available and will be included in downstream research; "
            f"headline scan found {high_count} high-impact and {medium_count} medium-impact items."
        )
    else:
        macro_context = (
            f"No external macro digest supplied; headline scan found {high_count} high-impact "
            f"and {medium_count} medium-impact items."
        )

    stale_note = " News data may be stale." if "news" in data.stale_sources else ""
    summary = (
        f"Recent news analysis used {len(events)} fetched headlines for {data.ticker}; "
        f"{high_count} were high impact and {medium_count} were medium impact.{stale_note}"
    )

    return NewsAnalysis(
        ticker=data.ticker,
        events=events,
        macro_context=macro_context,
        summary=summary,
    )


def _impact_for_text(text: str) -> str:
    if any(term in text for term in HIGH_IMPACT_TERMS):
        return "high"
    if any(term in text for term in MEDIUM_IMPACT_TERMS):
        return "medium"
    return "low"


def _relevance_for_item(data: TickerDataPackage, impact: str, summary: str) -> str:
    company_context = data.fundamentals.sector or data.fundamentals.industry or "the ticker"
    if summary:
        return f"{impact.title()} impact item for {company_context}; summary indicates: {summary[:220]}"
    return f"{impact.title()} impact headline for {company_context}; no source summary was provided."

"""Sentiment Analyst.

Receives pre-fetched news items with optional pre-computed sentiment scores.
Produces an overall sentiment assessment with source attribution.
FACTUAL analysis only — no buy/sell recommendations.
"""

import logging

from agents.analysts.comparison import record_analyst_comparison
from agents.llm import call_analyst
from config import settings
from data.models import SentimentAnalysis, SourceScore, TickerDataPackage

logger = logging.getLogger(__name__)

SYSTEM = """You are a sentiment analyst. You receive recent news headlines and summaries
for a stock. Your job is to ASSESS the overall market sentiment — positive, negative,
or mixed. Cite specific headlines. Produce FACTUAL observations only.
Do NOT make buy/sell recommendations.
Respond with ONLY valid JSON."""


def analyze(data: TickerDataPackage) -> SentimentAnalysis:
    if not data.news:
        baseline = SentimentAnalysis(
            ticker=data.ticker,
            overall_score=0.0,
            source_scores=[],
            volume_vs_avg=None,
            summary="No recent news available for sentiment analysis.",
        )
        record_analyst_comparison(
            ticker=data.ticker,
            analyst="sentiment",
            baseline=baseline,
            mode=settings.analyst_mode,
            used_result="no_news",
        )
        return baseline

    baseline = _analyze_deterministic(data)
    if settings.analyst_mode == "deterministic":
        record_analyst_comparison(
            ticker=data.ticker,
            analyst="sentiment",
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

    prompt = f"""Analyze the sentiment for {data.ticker} based on recent news.

RECENT NEWS:
{news_text}

CURRENT PRICE: ${data.technicals.current_price:.2f}

{"⚠️ Note: news data may be stale." if "news" in data.stale_sources else ""}
{_baseline_section(baseline)}

Respond with JSON:
{{
  "ticker": "{data.ticker}",
  "overall_score": <float from -1.0 (very negative) to 1.0 (very positive)>,
  "source_scores": [{{"source": "headline text", "score": <-1.0 to 1.0>}}, ...],
  "volume_vs_avg": null,
  "summary": "2-3 sentence factual summary of current sentiment with specific headline citations"
}}"""

    try:
        result = call_analyst(prompt, SentimentAnalysis, system=SYSTEM,
                              ticker=data.ticker, stage="analyst.sentiment")
        record_analyst_comparison(
            ticker=data.ticker,
            analyst="sentiment",
            baseline=baseline,
            mode=settings.analyst_mode,
            used_result="llm",
            llm_result=result,
        )
        return result
    except Exception as e:
        if settings.analyst_fallback == "deterministic":
            logger.warning(f"[{data.ticker}] Sentiment LLM failed; using deterministic baseline: {e}")
            record_analyst_comparison(
                ticker=data.ticker,
                analyst="sentiment",
                baseline=baseline,
                mode=settings.analyst_mode,
                used_result="deterministic_fallback",
                error=f"{type(e).__name__}: {e}",
            )
            return baseline
        record_analyst_comparison(
            ticker=data.ticker,
            analyst="sentiment",
            baseline=baseline,
            mode=settings.analyst_mode,
            used_result="error",
            error=f"{type(e).__name__}: {e}",
        )
        raise


def _baseline_section(baseline: SentimentAnalysis) -> str:
    if not settings.analyst_include_deterministic_baseline:
        return ""
    source_scores = [
        {"source": item.source, "score": item.score}
        for item in baseline.source_scores[:5]
    ]
    return f"""

RULE-BASED BASELINE (use as a sanity check, not a final answer):
- Overall Score: {baseline.overall_score}
- Source Scores: {source_scores}
- Summary: {baseline.summary}
"""


POSITIVE_TERMS = (
    "beat",
    "beats",
    "bullish",
    "gain",
    "gains",
    "growth",
    "higher",
    "optimism",
    "outperform",
    "profit",
    "rally",
    "rebound",
    "record",
    "raises",
    "strong",
    "surge",
    "upgrade",
)

NEGATIVE_TERMS = (
    "bearish",
    "concern",
    "crash",
    "cut",
    "decline",
    "delay",
    "downgrade",
    "fall",
    "falls",
    "lawsuit",
    "loss",
    "lower",
    "miss",
    "pressure",
    "probe",
    "recession",
    "risk",
    "selloff",
    "slump",
    "underperform",
    "warning",
    "weak",
)


def _analyze_deterministic(data: TickerDataPackage) -> SentimentAnalysis:
    scored = []
    for item in data.news[:10]:
        score = _score_news_item(item.headline, item.summary, item.sentiment_score)
        scored.append(SourceScore(source=item.headline, score=score))

    overall = sum(item.score for item in scored) / len(scored) if scored else 0.0
    overall = max(-1.0, min(1.0, overall))

    label = "positive" if overall > 0.15 else "negative" if overall < -0.15 else "mixed/neutral"
    drivers = sorted(scored, key=lambda item: abs(item.score), reverse=True)[:3]
    driver_text = "; ".join(
        f"{driver.source} ({driver.score:+.2f})" for driver in drivers if driver.score != 0
    )
    if not driver_text:
        driver_text = "headlines do not contain strong directional language"

    stale_note = " News data may be stale." if "news" in data.stale_sources else ""
    summary = (
        f"Overall sentiment is {label} at {overall:.2f}, based on keyword scoring of "
        f"{len(scored)} recent headlines. Main drivers: {driver_text}.{stale_note}"
    )

    return SentimentAnalysis(
        ticker=data.ticker,
        overall_score=round(overall, 2),
        source_scores=scored,
        volume_vs_avg=None,
        summary=summary,
    )


def _score_news_item(headline: str, summary: str, preset_score: float | None) -> float:
    if preset_score is not None:
        return round(max(-1.0, min(1.0, preset_score)), 2)

    text = f"{headline} {summary}".lower()
    positive = sum(1 for term in POSITIVE_TERMS if term in text)
    negative = sum(1 for term in NEGATIVE_TERMS if term in text)
    score = (positive - negative) * 0.2
    return round(max(-1.0, min(1.0, score)), 2)

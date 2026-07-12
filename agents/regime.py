"""Macro Regime Classifier.

Reads the daily ChatGPT macro digest and classifies the current market regime.
Runs once per pipeline run, before the ticker loop.
"""

import logging

from agents.llm import call_cloud, call_local
from config import settings
from data.models import MarketRegime, RegimeClassification

logger = logging.getLogger(__name__)

SYSTEM = """You are a macro strategist. You receive a daily market analysis digest.
Your job is to classify the current market regime into one of four categories:
- risk-on: markets favor growth, momentum, risk assets
- risk-off: markets favor safety, defensives, cash
- rotation: sector rotation underway, mixed signals
- neutral: no clear regime, balanced conditions

Be concise. Respond with ONLY valid JSON."""


def classify(digest: str | None) -> RegimeClassification:
    """Classify macro regime from ChatGPT digest. Returns NEUTRAL fallback on error."""
    if not digest:
        logger.info("No digest available, defaulting to NEUTRAL regime")
        return RegimeClassification(
            regime=MarketRegime.NEUTRAL,
            confidence=0.0,
            key_factors=[],
            summary="No macro data available.",
        )

    baseline = _classify_deterministic(digest)
    if settings.regime_mode == "deterministic":
        logger.info(f"Regime classified: {baseline.regime.value} ({baseline.confidence:.0%})")
        return baseline

    prompt = f"""Classify the current market regime based on this macro analysis:

{digest}

RULE-BASED BASELINE (use as a sanity check, not a final answer):
- Regime: {baseline.regime.value}
- Confidence: {baseline.confidence}
- Key Factors: {baseline.key_factors}
- Summary: {baseline.summary}

Respond with JSON:
{{
  "regime": "risk-on" | "risk-off" | "rotation" | "neutral",
  "confidence": <0.0-1.0>,
  "key_factors": ["factor 1", "factor 2", ...],
  "summary": "1-2 sentence regime summary"
}}"""

    try:
        if settings.regime_mode == "cloud":
            result = call_cloud(
                prompt,
                RegimeClassification,
                system=SYSTEM,
                max_retries=settings.regime_max_retries,
                stage="regime",
            )
        else:
            result = call_local(
                prompt,
                RegimeClassification,
                system=SYSTEM,
                max_retries=settings.regime_max_retries,
                stage="regime",
            )
        logger.info(f"Regime classified: {result.regime.value} ({result.confidence:.0%})")
        return result
    except Exception as e:
        if settings.regime_fallback == "deterministic":
            logger.warning(f"Regime LLM failed; using deterministic baseline: {e}")
            return baseline
        if settings.regime_fallback == "off":
            raise
        logger.error(f"Regime classification failed: {e}. Defaulting to NEUTRAL.")
        return RegimeClassification(
            regime=MarketRegime.NEUTRAL,
            confidence=0.0,
            key_factors=[],
            summary=f"Classification failed: {e}",
        )


RISK_ON_TERMS = (
    "bullish",
    "easing",
    "growth",
    "momentum",
    "rally",
    "rate cut",
    "record high",
    "risk-on",
    "soft landing",
)

RISK_OFF_TERMS = (
    "bearish",
    "credit stress",
    "hawkish",
    "inflation",
    "recession",
    "risk-off",
    "selloff",
    "slowdown",
    "volatility",
)

ROTATION_TERMS = (
    "breadth",
    "defensive",
    "dividend",
    "rotation",
    "sector rotation",
    "small cap",
    "value",
)


def _classify_deterministic(digest: str) -> RegimeClassification:
    text = digest.lower()
    scores = {
        MarketRegime.RISK_ON: _score_terms(text, RISK_ON_TERMS),
        MarketRegime.RISK_OFF: _score_terms(text, RISK_OFF_TERMS),
        MarketRegime.ROTATION: _score_terms(text, ROTATION_TERMS),
    }

    regime, top_score = max(scores.items(), key=lambda item: item[1])
    total = sum(scores.values())
    if top_score == 0:
        return RegimeClassification(
            regime=MarketRegime.NEUTRAL,
            confidence=0.2,
            key_factors=[],
            summary="Macro digest did not contain strong directional regime terms.",
        )

    confidence = min(0.9, max(0.35, top_score / max(total, 1)))
    key_factors = _matched_terms(text, {
        MarketRegime.RISK_ON: RISK_ON_TERMS,
        MarketRegime.RISK_OFF: RISK_OFF_TERMS,
        MarketRegime.ROTATION: ROTATION_TERMS,
    }[regime])

    return RegimeClassification(
        regime=regime,
        confidence=round(confidence, 2),
        key_factors=key_factors[:5],
        summary=f"Deterministic keyword classifier selected {regime.value} from the macro digest.",
    )


def _score_terms(text: str, terms: tuple[str, ...]) -> int:
    return sum(text.count(term) for term in terms)


def _matched_terms(text: str, terms: tuple[str, ...]) -> list[str]:
    return [term for term in terms if term in text]

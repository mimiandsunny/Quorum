"""Judge Agent — runs on CLOUD LLM.

Reads a multi-round bull/bear debate and produces a structured verdict
(winner, score, swing argument, conceded points). Trader consumes this as
weighted-input alongside the raw debate.

ISOLATION (per CEO D10): on any failure (network, malformed JSON,
exception), `judge()` returns None. The trader degrades to a
legacy weighted-by-confidence prompt. Judge failures NEVER block the
pipeline.
"""

import logging

from agents.llm import call_cloud
from data.models import JudgeVerdict, ResearchRound

logger = logging.getLogger(__name__)

SYSTEM = """You are an impartial judge at an investment firm's research committee.
You read a multi-round debate between a bull researcher and a bear researcher
about a single stock, and you produce a structured verdict.

Rules:
1. Score the debate 0.0-10.0 where >5.0 means bull made the stronger case,
   <5.0 means bear, and 5.0 means a tie.
2. The swing_argument is the SINGLE point that most influenced your verdict —
   the moment one side's evidence or rebuttal tipped the scales.
3. conceded_points are claims one side dropped, yielded on, or failed to
   defend in a later round.
4. confidence is HOW CLEAR-CUT the verdict is. Tight debates with
   well-matched evidence score low confidence; one-sided routs score high.
5. Do NOT introduce new evidence — judge only on what was said in the debate.
6. Respond with ONLY valid JSON."""


def _format_rounds(rounds: list[ResearchRound], ticker: str) -> str:
    """Render the full debate transcript for the judge prompt."""
    if not rounds:
        return f"NO ROUNDS PROVIDED FOR {ticker}"

    blocks = [f"DEBATE TRANSCRIPT FOR {ticker} — {len(rounds)} ROUNDS:\n"]
    for r in rounds:
        bull_evidence = "\n".join(
            f"      - [{e.weight:.2f}] {e.claim}" for e in r.bull.evidence
        )
        bear_evidence = "\n".join(
            f"      - [{e.weight:.2f}] {e.claim}" for e in r.bear.evidence
        )
        blocks.append(
            f"=== Round {r.round} ===\n"
            f"BULL (target ${r.bull.price_target}):\n"
            f"  Thesis: {r.bull.thesis}\n"
            f"  Evidence:\n{bull_evidence}\n"
            f"  Catalysts: {r.bull.catalysts}\n"
            f"  Acknowledged risks: {r.bull.risks}\n\n"
            f"BEAR (target ${r.bear.price_target}):\n"
            f"  Thesis: {r.bear.thesis}\n"
            f"  Evidence:\n{bear_evidence}\n"
            f"  Catalysts: {r.bear.catalysts}\n"
            f"  Acknowledged risks: {r.bear.risks}\n"
        )
    return "\n".join(blocks)


def judge(rounds: list[ResearchRound], ticker: str) -> JudgeVerdict | None:
    """Score a multi-round debate. Returns None on any failure (ISOLATION).

    Callers should treat None as "judge unavailable" and fall back to legacy
    trader prompt.
    """
    if not rounds:
        logger.warning(f"[judge:{ticker}] empty rounds list — returning None")
        return None

    transcript = _format_rounds(rounds, ticker)
    prompt = (
        f"{transcript}\n"
        f"Score this debate. Respond with JSON:\n"
        "{\n"
        '  "winner": "bull" | "bear" | "tie",\n'
        '  "score": <float 0.0-10.0; >5.0 means bull-favored>,\n'
        '  "swing_argument": "the single point that decided the verdict",\n'
        '  "conceded_points": ["claim that was dropped or yielded", ...],\n'
        '  "confidence": <float 0.0-1.0; how clear-cut the verdict was>\n'
        "}"
    )

    try:
        verdict = call_cloud(prompt, JudgeVerdict, system=SYSTEM,
                             ticker=ticker, stage="researcher.judge")
        logger.info(
            f"[judge:{ticker}] {verdict.winner.value} {verdict.score:.1f}/10 "
            f"(conf {verdict.confidence:.2f})"
        )
        return verdict
    except Exception as e:
        logger.warning(
            f"[judge:{ticker}] judge call failed: {type(e).__name__}: {e}. "
            "Returning None — trader will use legacy fallback."
        )
        return None

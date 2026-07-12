"""One-shot distillation of the daily macro digest.

Called ONCE at the start of `run_all()` instead of stuffing the raw digest
into every per-ticker LLM prompt. The resulting `DigestDistillation` is
~200-300 tokens; replaces a ~5,000-7,000-token raw blob that previously
got prefilled by 168+ LLM calls per 28-ticker run.

Fail-open: if the distillation call raises or produces unparseable output,
returns `None` and the pipeline continues with `regime` as the sole macro
signal. We deliberately do NOT fall back to the raw digest — the whole
point is to eliminate the raw-digest re-read cost.
"""

from __future__ import annotations

import logging

from agents.llm import call_local
from data.models import DigestDistillation

logger = logging.getLogger(__name__)


_SYSTEM = """You distill a daily macro market digest into a compact, structured \
summary that downstream analyst / researcher / trader agents will read once per \
ticker. Be concise, name themes and risks, no hedging filler. Skip any \
'Small-business opportunities' and 'Reflection on prior recommendations' \
sections — they are noise for the trading pipeline."""


def _prompt(digest: str) -> str:
    return f"""Distill this market digest:

---
{digest}
---

Extract:
- `tactical_view`: one or two sentences naming the current macro stance
  (e.g. "Risk-on but fragile: AI momentum cracked under macro pressure;
  rates and oil dominate near-term tone").
- `key_themes`: 3-5 short bullets of dominant themes WITH confidence
  (e.g. "AI infrastructure capex intact (high)").
- `macro_risks`: 3-5 short bullets of near-term risks to watch
  (e.g. "Nvidia earnings as referendum on AI valuation").
- `bottom_line`: 1-2 sentence punchline.

IGNORE the 'Small-business opportunities', 'Stock recommendations',
'Reflection on prior recommendations' sections — none of those help downstream
trading decisions (tickers are extracted elsewhere; reflection is stale).

Respond with valid JSON only matching the schema."""


def distill_digest(digest: str | None) -> DigestDistillation | None:
    """One LLM call → structured summary, or None when digest is empty / call fails."""
    if not digest or not digest.strip():
        return None
    try:
        result = call_local(
            _prompt(digest),
            DigestDistillation,
            system=_SYSTEM,
            max_retries=1,
            stage="digest.distill",
        )
        logger.info(
            f"Digest distilled: {len(result.key_themes)} themes, "
            f"{len(result.macro_risks)} risks"
        )
        return result
    except Exception as exc:
        logger.warning(f"Digest distillation failed: {exc}; downstream prompts will skip macro context")
        return None


def format_digest_summary(summary: DigestDistillation | None) -> str:
    """Compact markdown block (~200-300 tokens) suitable to inline in any
    downstream LLM prompt. Returns empty string when summary is None so
    callers can simply concatenate without conditionals.
    """
    if summary is None:
        return ""
    themes_block = "\n".join(f"  - {theme}" for theme in summary.key_themes)
    risks_block = "\n".join(f"  - {risk}" for risk in summary.macro_risks)
    return f"""
MACRO CONTEXT (distilled from today's digest):
- Tactical view: {summary.tactical_view}
- Key themes:
{themes_block}
- Risks to watch:
{risks_block}
- Bottom line: {summary.bottom_line}
"""

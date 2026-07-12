"""Shared scaffolding for bull, bear, and judge researchers.

Each researcher is a thin wrapper around `_format_reports()` and a stance-
specific prompt template. Multi-round debate appends prior-round context.
"""

from data.models import AnalystReports, ResearchCase


def _format_reports(reports: AnalystReports) -> str:
    """Single source of truth for the analyst-report block in researcher prompts."""
    return f"""ANALYST REPORTS FOR {reports.ticker}:

TECHNICAL ANALYSIS:
- Trend: {reports.technical.trend.value}
- Pattern: {reports.technical.pattern}
- Momentum: {reports.technical.momentum}
- Key Levels: Support {reports.technical.key_levels.get("support", [])}, Resistance {reports.technical.key_levels.get("resistance", [])}
- Summary: {reports.technical.summary}

FUNDAMENTALS:
- Valuation: {reports.fundamentals.valuation_assessment}
- Growth: {reports.fundamentals.growth_assessment}
- Financial Health: {reports.fundamentals.financial_health}
- Sector Comparison: {reports.fundamentals.sector_comparison}
- Summary: {reports.fundamentals.summary}

SENTIMENT:
- Overall Score: {reports.sentiment.overall_score} (-1 to 1)
- Summary: {reports.sentiment.summary}

NEWS:
- Macro Context: {reports.news.macro_context}
- Summary: {reports.news.summary}
- Events: {reports.news.events}"""


def _format_macro(digest_summary) -> str:
    """Returns the distilled macro context block for researcher prompts.

    Accepts a `DigestDistillation | None` (the post-distillation form), not
    the raw digest string. Distillation happens once per run in `run_all()`
    so researchers, analysts, and trader all consume the same compact view.
    """
    # Local import keeps `_base.py` free of an import cycle (digest_distiller
    # → data.models pulls a chain we don't want at module-load time here).
    from agents.digest_distiller import format_digest_summary

    return format_digest_summary(digest_summary)


def _format_track_record(track_record: str | None) -> str:
    if not track_record:
        return ""
    return f"\n{track_record}\n"


def _format_prior_round(prior: ResearchCase | None, role: str) -> str:
    """Render the opposing side's prior-round case for rebuttal context.

    role is the OPPOSING side's stance ('bull' or 'bear') — what the current
    researcher is rebutting.
    """
    if prior is None:
        return ""
    evidence_lines = "\n".join(
        f"  - [{e.weight:.2f}] {e.claim} (cite: {e.data_citation})"
        for e in prior.evidence
    )
    return f"""

PRIOR ROUND — {role.upper()} CASE TO REBUT:
- Thesis: {prior.thesis}
- Price Target: ${prior.price_target}
- Top Evidence:
{evidence_lines}
- Catalysts: {prior.catalysts}
- Risks (their acknowledged weaknesses): {prior.risks}
"""


def _round_directive(round_num: int, opposing_role: str) -> str:
    """Per-round directive added to the prompt tail."""
    if round_num == 1:
        return ""
    return (
        f"\nThis is ROUND {round_num}. Read the {opposing_role.upper()} case above "
        f"carefully. Your response MUST directly rebut at least 2 of their specific "
        f"claims with new evidence or counter-citations from the analyst reports. "
        f"Concede points where the {opposing_role} is correct — credibility comes "
        f"from honest acknowledgment.\n"
    )

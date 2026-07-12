"""Bull Researcher — runs on CLOUD LLM.

Takes analyst reports and builds the optimistic investment case.
Must cite specific data points from analyst reports as evidence.

In multi-round debates (round_num >= 2), receives the prior round's bear
case and must rebut at least 2 specific claims.
"""

from agents.llm import call_cloud
from agents.researchers._base import (
    _format_macro,
    _format_prior_round,
    _format_reports,
    _format_track_record,
    _round_directive,
)
from data.models import AnalystReports, ResearchCase

SYSTEM = """You are a bull researcher at an investment firm. You receive factual analyst
reports and must build the STRONGEST possible bullish case for the stock.

Rules:
1. Every claim must cite a specific data point from the analyst reports.
2. Include a split-adjusted price target for the same short-term setup; do not use stale/pre-split long-term analyst targets.
3. Identify catalysts that could drive the stock higher.
4. Acknowledge risks honestly — a credible bull case addresses weaknesses.
5. In multi-round debates, directly rebut the opposing side's prior-round claims.
6. Respond with ONLY valid JSON."""


def research(
    reports: AnalystReports,
    digest_summary=None,
    track_record: str | None = None,
    prior_bear: ResearchCase | None = None,
    round_num: int = 1,
) -> ResearchCase:
    prompt = (
        f"{_format_reports(reports)}"
        f"{_format_macro(digest_summary)}"
        f"{_format_track_record(track_record)}"
        f"{_format_prior_round(prior_bear, role='bear')}"
        f"{_round_directive(round_num, opposing_role='bear')}"
        f"\nBuild the BULL CASE for {reports.ticker}. Cite specific data from above.\n\n"
        "Respond with JSON:\n"
        "{\n"
        f'  "ticker": "{reports.ticker}",\n'
        '  "stance": "bull",\n'
        '  "thesis": "1-2 paragraph bull thesis citing specific numbers",\n'
        '  "evidence": [\n'
        '    {"claim": "specific claim", "data_citation": "exact data point from reports", "weight": 0.0-1.0},\n'
        '    ...at least 3 evidence items\n'
        '  ],\n'
        '  "price_target": <float>,\n'
        '  "catalysts": ["catalyst 1", "catalyst 2", ...],\n'
        '  "risks": ["acknowledged risk 1", "acknowledged risk 2", ...]\n'
        "}"
    )
    return call_cloud(prompt, ResearchCase, system=SYSTEM,
                      ticker=reports.ticker, stage="researcher.bull")

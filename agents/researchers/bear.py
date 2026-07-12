"""Bear Researcher — runs on CLOUD LLM.

Takes analyst reports and builds the pessimistic investment case.
Must cite specific data points from analyst reports as evidence.

In multi-round debates (round_num >= 2), receives the prior round's bull
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

SYSTEM = """You are a bear researcher at an investment firm. You receive factual analyst
reports and must build the STRONGEST possible bearish case for the stock.

Rules:
1. Every claim must cite a specific data point from the analyst reports.
2. Include a split-adjusted downside price target for the same short-term setup; do not use stale/pre-split long-term analyst targets.
3. Identify risks and catalysts that could drive the stock lower.
4. Acknowledge bullish factors honestly — a credible bear case addresses strengths.
5. In multi-round debates, directly rebut the opposing side's prior-round claims.
6. Respond with ONLY valid JSON."""


def research(
    reports: AnalystReports,
    digest_summary=None,
    track_record: str | None = None,
    prior_bull: ResearchCase | None = None,
    round_num: int = 1,
) -> ResearchCase:
    prompt = (
        f"{_format_reports(reports)}"
        f"{_format_macro(digest_summary)}"
        f"{_format_track_record(track_record)}"
        f"{_format_prior_round(prior_bull, role='bull')}"
        f"{_round_directive(round_num, opposing_role='bull')}"
        f"\nBuild the BEAR CASE for {reports.ticker}. Cite specific data from above.\n\n"
        "Respond with JSON:\n"
        "{\n"
        f'  "ticker": "{reports.ticker}",\n'
        '  "stance": "bear",\n'
        '  "thesis": "1-2 paragraph bear thesis citing specific numbers",\n'
        '  "evidence": [\n'
        '    {"claim": "specific claim", "data_citation": "exact data point from reports", "weight": 0.0-1.0},\n'
        '    ...at least 3 evidence items\n'
        '  ],\n'
        '  "price_target": <float>,\n'
        '  "catalysts": ["downside catalyst 1", "downside catalyst 2", ...],\n'
        '  "risks": ["risk to bear thesis 1 (i.e. what could go right)", ...]\n'
        "}"
    )
    return call_cloud(prompt, ResearchCase, system=SYSTEM,
                      ticker=reports.ticker, stage="researcher.bear")

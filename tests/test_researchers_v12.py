"""Unit tests for v1.2 researcher changes:

- agents/researchers/_base.py shared helpers (pure functions)
- bull/bear new params: round_num, prior_bull/prior_bear
- agents/researchers/judge.py ISOLATION fallback (returns None on any failure)

LLM calls are mocked via monkeypatch on agents.llm.call_cloud.
"""

import pytest

from data.models import (
    EvidenceItem,
    JudgeVerdict,
    JudgeWinner,
    ResearchCase,
    ResearchRound,
)


# ─── _base.py helpers (pure) ────────────────────────────

def test_format_track_record_none_returns_empty():
    from agents.researchers._base import _format_track_record
    assert _format_track_record(None) == ""


def test_format_track_record_with_text_wraps_with_newlines():
    from agents.researchers._base import _format_track_record
    out = _format_track_record("AAPL: 6/8 correct")
    assert "AAPL" in out
    assert out.startswith("\n") and out.endswith("\n")


def test_format_macro_none_returns_empty():
    from agents.researchers._base import _format_macro
    assert _format_macro(None) == ""


def test_format_macro_with_digest_includes_section_header():
    """_format_macro now consumes a DigestDistillation, not a raw string —
    the raw blob is replaced by the one-shot distillation summary."""
    from agents.researchers._base import _format_macro
    from data.models import DigestDistillation

    summary = DigestDistillation(
        tactical_view="risk-on but fragile, oil at $100",
        key_themes=["AI capex strong (high)"],
        macro_risks=["oil-driven inflation"],
        bottom_line="favor selective entries",
    )
    out = _format_macro(summary)
    assert "MACRO CONTEXT" in out
    assert "oil at $100" in out
    assert "AI capex strong" in out


def test_format_prior_round_none_returns_empty():
    from agents.researchers._base import _format_prior_round
    assert _format_prior_round(None, role="bear") == ""


def test_format_prior_round_renders_thesis_and_evidence():
    from agents.researchers._base import _format_prior_round
    case = ResearchCase(
        ticker="NVDA",
        stance="bear",
        thesis="overvalued at current multiples",
        evidence=[
            EvidenceItem(claim="P/E above 50", data_citation="fund.pe", weight=0.4),
            EvidenceItem(claim="weakening guidance", data_citation="news.events", weight=0.3),
        ],
        price_target=400.0,
        catalysts=["earnings miss"],
        risks=["AI demand surprise"],
    )
    out = _format_prior_round(case, role="bear")
    assert "BEAR CASE TO REBUT" in out
    assert "overvalued" in out
    assert "P/E above 50" in out
    assert "$400.0" in out


def test_round_directive_first_round_empty():
    from agents.researchers._base import _round_directive
    assert _round_directive(1, opposing_role="bear") == ""


def test_round_directive_second_round_demands_rebuttal():
    from agents.researchers._base import _round_directive
    out = _round_directive(2, opposing_role="bull")
    assert "ROUND 2" in out
    assert "rebut" in out.lower()
    assert "BULL" in out


# ─── bull / bear: round_num + prior_other plumbing ─────

def _fake_reports(ticker="NVDA"):
    """Build a minimal AnalystReports for prompt-construction tests."""
    from data.models import (
        AnalystReports,
        FundamentalsAnalysis,
        NewsAnalysis,
        SentimentAnalysis,
        TechnicalAnalysis,
        Trend,
    )
    return AnalystReports(
        ticker=ticker,
        technical=TechnicalAnalysis(
            ticker=ticker, trend=Trend.BULLISH,
            key_levels={"support": [100.0], "resistance": [120.0]},
            pattern="cup-and-handle", momentum="strong", summary="bullish",
        ),
        fundamentals=FundamentalsAnalysis(
            ticker=ticker, valuation_assessment="reasonable",
            growth_assessment="strong", financial_health="solid",
            sector_comparison="leader", summary="solid fundamentals",
        ),
        sentiment=SentimentAnalysis(
            ticker=ticker, overall_score=0.4, summary="positive",
        ),
        news=NewsAnalysis(
            ticker=ticker, events=[], macro_context="risk-on", summary="quiet",
        ),
    )


def _capture_call_cloud(monkeypatch, target_module):
    """Replace call_cloud in the target module with a capturing stub.

    Returns a dict with 'last_prompt'; the stub returns a minimal valid ResearchCase.
    """
    captured = {"last_prompt": None, "last_system": None, "call_count": 0}

    def _stub(prompt, output_model, system="", **_kwargs):
        captured["last_prompt"] = prompt
        captured["last_system"] = system
        captured["call_count"] += 1
        return ResearchCase(
            ticker="NVDA", stance="bull",
            thesis="stubbed", evidence=[
                EvidenceItem(claim="stub", data_citation="stub", weight=0.5),
            ],
            price_target=500.0, catalysts=["stub"], risks=["stub"],
        )

    monkeypatch.setattr(target_module, "call_cloud", _stub)
    return captured


def test_bull_round_1_no_rebuttal_directive(monkeypatch):
    from agents.researchers import bull
    captured = _capture_call_cloud(monkeypatch, bull)
    bull.research(_fake_reports(), round_num=1, prior_bear=None)
    assert "ROUND 2" not in captured["last_prompt"]
    assert "BEAR CASE TO REBUT" not in captured["last_prompt"]


def test_bull_round_2_includes_prior_bear_rebuttal_block(monkeypatch):
    from agents.researchers import bull
    captured = _capture_call_cloud(monkeypatch, bull)
    prior_bear = ResearchCase(
        ticker="NVDA", stance="bear", thesis="overvalued",
        evidence=[EvidenceItem(claim="high PE", data_citation="fund", weight=0.5)],
        price_target=400.0, catalysts=["miss"], risks=["AI"],
    )
    bull.research(_fake_reports(), round_num=2, prior_bear=prior_bear)
    assert "ROUND 2" in captured["last_prompt"]
    assert "BEAR CASE TO REBUT" in captured["last_prompt"]
    assert "overvalued" in captured["last_prompt"]


def test_bear_round_1_no_rebuttal_directive(monkeypatch):
    from agents.researchers import bear
    captured = _capture_call_cloud(monkeypatch, bear)
    bear.research(_fake_reports(), round_num=1, prior_bull=None)
    assert "ROUND 2" not in captured["last_prompt"]


def test_bear_round_2_includes_prior_bull_rebuttal_block(monkeypatch):
    from agents.researchers import bear
    captured = _capture_call_cloud(monkeypatch, bear)
    prior_bull = ResearchCase(
        ticker="NVDA", stance="bull", thesis="strong setup",
        evidence=[EvidenceItem(claim="momentum", data_citation="tech", weight=0.5)],
        price_target=600.0, catalysts=["earnings"], risks=["macro"],
    )
    bear.research(_fake_reports(), round_num=2, prior_bull=prior_bull)
    assert "ROUND 2" in captured["last_prompt"]
    assert "BULL CASE TO REBUT" in captured["last_prompt"]
    assert "strong setup" in captured["last_prompt"]


def test_bull_track_record_injected_when_provided(monkeypatch):
    from agents.researchers import bull
    captured = _capture_call_cloud(monkeypatch, bull)
    bull.research(_fake_reports(), track_record="NVDA: 7/9 correct")
    assert "NVDA: 7/9 correct" in captured["last_prompt"]


def test_bull_external_digest_included(monkeypatch):
    """Bull researcher now consumes the distilled DigestDistillation summary
    instead of the raw digest blob (`digest_summary` kwarg, not
    `external_digest`)."""
    from agents.researchers import bull
    from data.models import DigestDistillation

    captured = _capture_call_cloud(monkeypatch, bull)
    summary = DigestDistillation(
        tactical_view="oil at $100 driving inflation, AI capex up",
        key_themes=["AI capex strong (high)"],
        macro_risks=["oil-driven inflation"],
        bottom_line="favor selective entries",
    )
    bull.research(_fake_reports(), digest_summary=summary)
    assert "oil at $100" in captured["last_prompt"]
    assert "AI capex" in captured["last_prompt"]
    assert "MACRO CONTEXT" in captured["last_prompt"]


# ─── judge.py ISOLATION ─────────────────────────────────

def _round(r=1, bull_target=500, bear_target=400):
    return ResearchRound(
        round=r,
        bull=ResearchCase(
            ticker="NVDA", stance="bull", thesis=f"r{r} bull",
            evidence=[EvidenceItem(claim="x", data_citation="y", weight=0.5)],
            price_target=bull_target, catalysts=["c"], risks=["r"],
        ),
        bear=ResearchCase(
            ticker="NVDA", stance="bear", thesis=f"r{r} bear",
            evidence=[EvidenceItem(claim="x", data_citation="y", weight=0.5)],
            price_target=bear_target, catalysts=["c"], risks=["r"],
        ),
    )


def test_judge_empty_rounds_returns_none():
    from agents.researchers.judge import judge
    assert judge([], "NVDA") is None


def test_judge_happy_path_returns_verdict(monkeypatch):
    from agents.researchers import judge as judge_mod

    def _stub(prompt, output_model, system="", **_kwargs):
        return JudgeVerdict(
            winner=JudgeWinner.BULL, score=6.5,
            swing_argument="bull's R2 cite of momentum",
            conceded_points=["valuation premium"],
            confidence=0.7,
        )
    monkeypatch.setattr(judge_mod, "call_cloud", _stub)

    verdict = judge_mod.judge([_round(1), _round(2)], "NVDA")
    assert verdict is not None
    assert verdict.winner == JudgeWinner.BULL
    assert verdict.score == 6.5


def test_judge_llm_exception_returns_none(monkeypatch):
    """ISOLATION: any LLM failure must return None, NEVER raise."""
    from agents.researchers import judge as judge_mod

    def _boom(prompt, output_model, system="", **_kwargs):
        raise RuntimeError("openai 502")
    monkeypatch.setattr(judge_mod, "call_cloud", _boom)

    assert judge_mod.judge([_round(1)], "NVDA") is None


def test_judge_invalid_json_returns_none(monkeypatch):
    """call_cloud raises pydantic ValidationError on bad JSON → still None."""
    from pydantic import ValidationError

    from agents.researchers import judge as judge_mod

    def _bad(prompt, output_model, system="", **_kwargs):
        # Simulate pydantic validation failure
        raise ValidationError.from_exception_data("JudgeVerdict", [])
    monkeypatch.setattr(judge_mod, "call_cloud", _bad)

    assert judge_mod.judge([_round(1)], "NVDA") is None


def test_judge_format_rounds_includes_evidence_and_targets():
    """Pure format helper — verifies the prompt body includes both sides' content."""
    from agents.researchers.judge import _format_rounds
    out = _format_rounds([_round(1, bull_target=550, bear_target=420)], "NVDA")
    assert "DEBATE TRANSCRIPT FOR NVDA" in out
    assert "Round 1" in out
    assert "BULL (target $550.0)" in out
    assert "BEAR (target $420.0)" in out

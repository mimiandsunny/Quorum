"""LLM telemetry tests.

Pins the contract: every LLM call attempt produces exactly one llm_calls
row, success or failure. Telemetry failures must NEVER break the LLM call
itself (fail-open). Context (ticker/stage) flows through both kwargs and
the llm_call_context manager, with kwargs winning when both are set.
"""

import pytest

from agents import llm as llm_mod
from data.models import LLMCallTelemetry, TechnicalAnalysis, Trend


@pytest.fixture
def captured_calls(monkeypatch):
    """Capture every record_llm_call invocation across the test."""
    calls: list[LLMCallTelemetry] = []

    def _capture(call):
        calls.append(call)

    monkeypatch.setattr("data.storage.record_llm_call", _capture)
    return calls


def _valid_payload() -> str:
    return (
        '{"ticker":"NVDA","trend":"bullish",'
        '"key_levels":{"support":[450.0],"resistance":[510.0]},'
        '"pattern":"flag","momentum":"strong","summary":"OK"}'
    )


class _FakeOllamaClient:
    """Minimal ollama.Client stub. Returns a queue of payloads on each .chat()."""

    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.calls = 0

    def chat(self, **_kwargs):
        self.calls += 1
        payload = self._payloads.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return {
            "message": {"content": payload},
            "done_reason": "stop",
            "eval_count": 42,
        }


# ─── Telemetry recording ───────────────────────────────

def test_successful_ollama_call_records_one_row(monkeypatch, captured_calls):
    """One successful call → exactly one telemetry row, parse_ok=True."""
    fake = _FakeOllamaClient([_valid_payload()])
    monkeypatch.setattr(llm_mod, "_get_ollama_client", lambda: fake)
    monkeypatch.setattr(llm_mod.settings, "local_provider", "ollama")
    monkeypatch.setattr(llm_mod.settings, "local_model", "test-model")
    monkeypatch.setattr(llm_mod.settings, "local_llm_think", False)

    llm_mod.call_local("prompt", TechnicalAnalysis, max_retries=0)

    assert len(captured_calls) == 1
    row = captured_calls[0]
    assert row.provider == "ollama"
    assert row.model == "test-model"
    assert row.parse_ok is True
    assert row.error is None
    assert row.attempt == 0
    assert row.prompt_len == len("prompt")
    assert row.output_len > 0
    assert row.done_reason == "stop"
    assert row.eval_count == 42


def test_failed_call_records_row_with_error(monkeypatch, captured_calls):
    """Final failure → telemetry row has parse_ok=False and error message."""
    fake = _FakeOllamaClient([RuntimeError("ollama down")])
    monkeypatch.setattr(llm_mod, "_get_ollama_client", lambda: fake)
    monkeypatch.setattr(llm_mod.settings, "local_provider", "ollama")
    monkeypatch.setattr(llm_mod.settings, "local_model", "test-model")
    monkeypatch.setattr(llm_mod.settings, "local_llm_think", False)

    with pytest.raises(RuntimeError):
        llm_mod.call_local("prompt", TechnicalAnalysis, max_retries=0)

    assert len(captured_calls) == 1
    assert captured_calls[0].parse_ok is False
    assert "ollama down" in (captured_calls[0].error or "")


def test_retry_records_one_row_per_attempt(monkeypatch, captured_calls):
    """N attempts → N telemetry rows; the failed ones flagged parse_ok=False."""
    fake = _FakeOllamaClient([RuntimeError("first fail"), _valid_payload()])
    monkeypatch.setattr(llm_mod, "_get_ollama_client", lambda: fake)
    monkeypatch.setattr(llm_mod.settings, "local_provider", "ollama")
    monkeypatch.setattr(llm_mod.settings, "local_model", "test-model")
    monkeypatch.setattr(llm_mod.settings, "local_llm_think", False)

    llm_mod.call_local("prompt", TechnicalAnalysis, max_retries=1)

    assert len(captured_calls) == 2
    assert [c.parse_ok for c in captured_calls] == [False, True]
    assert [c.attempt for c in captured_calls] == [0, 1]


def test_telemetry_db_failure_does_not_break_llm_call(monkeypatch):
    """If record_llm_call raises, the LLM call still succeeds — fail-open."""
    fake = _FakeOllamaClient([_valid_payload()])
    monkeypatch.setattr(llm_mod, "_get_ollama_client", lambda: fake)
    monkeypatch.setattr(llm_mod.settings, "local_provider", "ollama")
    monkeypatch.setattr(llm_mod.settings, "local_model", "test-model")
    monkeypatch.setattr(llm_mod.settings, "local_llm_think", False)

    def _boom(call):
        raise RuntimeError("db unreachable")

    monkeypatch.setattr("data.storage.record_llm_call", _boom)

    # Must not raise — telemetry failure is swallowed inside _emit_call_telemetry
    result = llm_mod.call_local("prompt", TechnicalAnalysis, max_retries=0)
    assert result.trend == Trend.BULLISH


# ─── Context tagging (kwargs + contextvars) ───────────

def test_explicit_kwargs_tag_telemetry(monkeypatch, captured_calls):
    fake = _FakeOllamaClient([_valid_payload()])
    monkeypatch.setattr(llm_mod, "_get_ollama_client", lambda: fake)
    monkeypatch.setattr(llm_mod.settings, "local_provider", "ollama")
    monkeypatch.setattr(llm_mod.settings, "local_model", "test-model")
    monkeypatch.setattr(llm_mod.settings, "local_llm_think", False)

    llm_mod.call_local(
        "prompt", TechnicalAnalysis, max_retries=0,
        ticker="NVDA", stage="analyst.technical", run_id="run_42",
    )

    assert captured_calls[0].ticker == "NVDA"
    assert captured_calls[0].stage == "analyst.technical"
    assert captured_calls[0].run_id == "run_42"


def test_llm_call_context_tags_telemetry(monkeypatch, captured_calls):
    """When no kwargs are passed, telemetry reads from llm_call_context."""
    fake = _FakeOllamaClient([_valid_payload()])
    monkeypatch.setattr(llm_mod, "_get_ollama_client", lambda: fake)
    monkeypatch.setattr(llm_mod.settings, "local_provider", "ollama")
    monkeypatch.setattr(llm_mod.settings, "local_model", "test-model")
    monkeypatch.setattr(llm_mod.settings, "local_llm_think", False)

    with llm_mod.llm_call_context(ticker="AMD", stage="trader", run_id="run_99"):
        llm_mod.call_local("prompt", TechnicalAnalysis, max_retries=0)

    assert captured_calls[0].ticker == "AMD"
    assert captured_calls[0].stage == "trader"
    assert captured_calls[0].run_id == "run_99"


def test_kwargs_override_context(monkeypatch, captured_calls):
    """Explicit kwargs win over the ambient context."""
    fake = _FakeOllamaClient([_valid_payload()])
    monkeypatch.setattr(llm_mod, "_get_ollama_client", lambda: fake)
    monkeypatch.setattr(llm_mod.settings, "local_provider", "ollama")
    monkeypatch.setattr(llm_mod.settings, "local_model", "test-model")
    monkeypatch.setattr(llm_mod.settings, "local_llm_think", False)

    with llm_mod.llm_call_context(ticker="AMD", stage="trader"):
        llm_mod.call_local(
            "prompt", TechnicalAnalysis, max_retries=0,
            ticker="NVDA", stage="analyst.technical",
        )

    assert captured_calls[0].ticker == "NVDA"
    assert captured_calls[0].stage == "analyst.technical"


def test_context_resets_after_with_block(monkeypatch, captured_calls):
    """Context bleeds nowhere outside its with-block."""
    fake = _FakeOllamaClient([_valid_payload(), _valid_payload()])
    monkeypatch.setattr(llm_mod, "_get_ollama_client", lambda: fake)
    monkeypatch.setattr(llm_mod.settings, "local_provider", "ollama")
    monkeypatch.setattr(llm_mod.settings, "local_model", "test-model")
    monkeypatch.setattr(llm_mod.settings, "local_llm_think", False)

    with llm_mod.llm_call_context(ticker="NVDA"):
        llm_mod.call_local("prompt", TechnicalAnalysis, max_retries=0)
    llm_mod.call_local("prompt", TechnicalAnalysis, max_retries=0)

    assert captured_calls[0].ticker == "NVDA"
    assert captured_calls[1].ticker is None

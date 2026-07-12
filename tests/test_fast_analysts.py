import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import pytest

from agents import llm, regime
from agents.analysts import fundamentals, news, sentiment, technical
from agents.analysts.comparison import reset_analyst_comparison_logs
from config import settings
from data.models import (
    FundamentalsData,
    MarketRegime,
    NewsItem,
    OHLCVBar,
    TechnicalAnalysis,
    TechnicalIndicators,
    TickerDataPackage,
    Trend,
)


@pytest.fixture(autouse=True)
def _comparison_logs_to_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "analyst_comparison_log_enabled", True)
    monkeypatch.setattr(settings, "analyst_comparison_log_dir", str(tmp_path))
    monkeypatch.setattr(settings, "local_provider", "ollama")


def _sample_package() -> TickerDataPackage:
    start = date(2026, 4, 1)
    bars = [
        OHLCVBar(
            date=start + timedelta(days=i),
            open=100 + i,
            high=101 + i,
            low=99 + i,
            close=100 + i,
            volume=1_000_000 + i,
        )
        for i in range(10)
    ]
    return TickerDataPackage(
        ticker="TEST",
        fetch_timestamp=datetime(2026, 4, 20, 9, 30),
        price_history=bars,
        technicals=TechnicalIndicators(
            rsi_14=64.0,
            macd=1.2,
            macd_signal=0.8,
            macd_histogram=0.4,
            ma_50=103.0,
            ma_200=98.0,
            current_price=109.0,
            support_levels=[103.0, 99.0],
            resistance_levels=[112.0],
        ),
        fundamentals=FundamentalsData(
            pe_ratio=22.0,
            forward_pe=20.0,
            eps=5.1,
            revenue_growth=0.08,
            debt_to_equity=0.4,
            market_cap=10_000_000_000,
            sector="Technology",
            industry="Software",
            dividend_yield=0.01,
        ),
        news=[
            NewsItem(headline="TEST shares rally to record high", source="Example"),
            NewsItem(headline="Analyst upgrades TEST after earnings beat", source="Example"),
        ],
    )


def _read_comparison_records(tmp_path):
    files = sorted(tmp_path.glob("analyst_comparisons_*.jsonl"))
    assert files
    assert files[0].name.startswith("analyst_comparisons_")
    return [
        json.loads(line)
        for file in files
        for line in file.read_text().splitlines()
    ]


def test_reset_analyst_comparison_logs_removes_history(tmp_path):
    old_log = tmp_path / "analyst_comparisons_2026-04-20.jsonl"
    today_log = tmp_path / "analyst_comparisons_2026-04-21.jsonl"
    other_log = tmp_path / "stockinvest.log"
    old_log.write_text('{"old": true}\n')
    today_log.write_text('{"today": true}\n')
    other_log.write_text("keep me\n")

    reset_analyst_comparison_logs()

    assert not old_log.exists()
    assert not today_log.exists()
    assert other_log.read_text() == "keep me\n"


def test_deterministic_analysts_do_not_call_llm(monkeypatch):
    def fail_llm(*args, **kwargs):
        raise AssertionError("deterministic analyst mode should not call the LLM")

    monkeypatch.setattr(settings, "analyst_mode", "deterministic")
    monkeypatch.setattr(technical, "call_analyst", fail_llm)
    monkeypatch.setattr(fundamentals, "call_analyst", fail_llm)
    monkeypatch.setattr(sentiment, "call_analyst", fail_llm)
    monkeypatch.setattr(news, "call_analyst", fail_llm)

    pkg = _sample_package()

    tech = technical.analyze(pkg)
    fund = fundamentals.analyze(pkg)
    sent = sentiment.analyze(pkg)
    nws = news.analyze(pkg)

    assert tech.trend == Trend.BULLISH
    assert "Trailing P/E is 22.00" in fund.valuation_assessment
    assert sent.overall_score > 0
    assert len(nws.events) == 2


def test_llm_analysts_fall_back_to_deterministic(monkeypatch, tmp_path):
    def fail_llm(*args, **kwargs):
        raise TimeoutError("local model was too slow")

    monkeypatch.setattr(settings, "analyst_mode", "local")
    monkeypatch.setattr(settings, "analyst_fallback", "deterministic")
    monkeypatch.setattr(technical, "call_analyst", fail_llm)
    monkeypatch.setattr(fundamentals, "call_analyst", fail_llm)
    monkeypatch.setattr(sentiment, "call_analyst", fail_llm)
    monkeypatch.setattr(news, "call_analyst", fail_llm)

    pkg = _sample_package()

    assert technical.analyze(pkg).trend == Trend.BULLISH
    assert "Trailing P/E is 22.00" in fundamentals.analyze(pkg).valuation_assessment
    assert sentiment.analyze(pkg).overall_score > 0
    assert len(news.analyze(pkg).events) == 2

    records = _read_comparison_records(tmp_path)
    assert {record["analyst"] for record in records} == {
        "technical",
        "fundamentals",
        "sentiment",
        "news",
    }
    assert all(record["used_result"] == "deterministic_fallback" for record in records)
    assert all(record["error"].startswith("TimeoutError:") for record in records)
    assert all(record["date"] for record in records)


def test_llm_success_writes_comparison_log(monkeypatch, tmp_path):
    def fake_llm(*args, **kwargs):
        return TechnicalAnalysis(
            ticker="TEST",
            trend=Trend.NEUTRAL,
            key_levels={"support": [103.0], "resistance": [112.0]},
            pattern="LLM pattern",
            momentum="LLM momentum",
            summary="LLM summary",
        )

    monkeypatch.setattr(settings, "analyst_mode", "local")
    monkeypatch.setattr(settings, "analyst_fallback", "off")
    monkeypatch.setattr(technical, "call_analyst", fake_llm)

    result = technical.analyze(_sample_package())

    assert result.summary == "LLM summary"
    records = _read_comparison_records(tmp_path)
    assert len(records) == 1
    assert records[0]["analyst"] == "technical"
    assert records[0]["used_result"] == "llm"
    assert records[0]["baseline"]["trend"] == "bullish"
    assert records[0]["llm"]["trend"] == "neutral"
    assert records[0]["diff"]


def test_local_llm_uses_schema_format(monkeypatch):
    calls = []

    class FakeOllamaClient:
        def chat(self, **kwargs):
            calls.append(kwargs)
            return {
                "message": {
                    "content": json.dumps(
                        {
                            "ticker": "TEST",
                            "trend": "bullish",
                            "key_levels": {"support": [103.0], "resistance": [112.0]},
                            "pattern": "Higher highs",
                            "momentum": "Constructive",
                            "summary": "Price is above key moving averages.",
                        }
                    )
                }
            }

    monkeypatch.setattr(llm, "_ollama_client", FakeOllamaClient())
    monkeypatch.setattr(settings, "local_llm_num_predict", 321)

    result = llm.call_local("Analyze TEST.", TechnicalAnalysis, max_retries=0)

    assert result.ticker == "TEST"
    assert calls
    assert calls[0]["format"]["title"] == "TechnicalAnalysis"
    assert calls[0]["format"]["type"] == "object"
    assert calls[0]["think"] is False
    assert calls[0]["options"]["num_predict"] == 321
    assert calls[0]["options"]["temperature"] == 0.0
    assert "under 240 characters" in calls[0]["messages"][-1]["content"]
    assert '"properties"' not in calls[0]["messages"][-1]["content"]


def test_llama_cpp_local_llm_uses_schema_response_format(monkeypatch):
    calls = []

    class FakeLlamaCppClient:
        def create_chat_completion(self, **kwargs):
            calls.append(kwargs)
            return {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {
                                    "ticker": "TEST",
                                    "trend": "bullish",
                                    "key_levels": {"support": [103.0], "resistance": [112.0]},
                                    "pattern": "Higher highs",
                                    "momentum": "Constructive",
                                    "summary": "Price is above key moving averages.",
                                }
                            )
                        },
                    }
                ],
                "usage": {"completion_tokens": 42},
            }

    monkeypatch.setattr(llm, "_llama_cpp_client", FakeLlamaCppClient())
    monkeypatch.setattr(settings, "local_provider", "llama_cpp")
    monkeypatch.setattr(settings, "local_llm_num_predict", 456)

    result = llm.call_local("Analyze TEST.", TechnicalAnalysis, max_retries=0)

    assert result.ticker == "TEST"
    assert calls
    assert calls[0]["response_format"]["type"] == "json_object"
    assert calls[0]["response_format"]["schema"]["title"] == "TechnicalAnalysis"
    assert calls[0]["temperature"] == 0.0
    assert calls[0]["max_tokens"] == 456
    assert "under 240 characters" in calls[0]["messages"][-1]["content"]
    assert '"properties"' not in calls[0]["messages"][-1]["content"]


def test_llama_cpp_chat_completion_is_serialized():
    class FakeLlamaCppClient:
        def __init__(self):
            self.active = 0
            self.max_active = 0
            self.lock = threading.Lock()

        def create_chat_completion(self, **kwargs):
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            time.sleep(0.02)
            with self.lock:
                self.active -= 1
            return {"choices": [{"message": {"content": "{}"}}]}

    client = FakeLlamaCppClient()
    kwargs = {"messages": [{"role": "user", "content": "test"}]}

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(llm._create_llama_cpp_chat_completion, client, kwargs)
            for _ in range(2)
        ]
        for future in futures:
            future.result()

    assert client.max_active == 1


def test_local_llm_retries_invalid_json(monkeypatch):
    calls = []
    valid_content = json.dumps(
        {
            "ticker": "TEST",
            "trend": "bullish",
            "key_levels": {"support": [103.0], "resistance": [112.0]},
            "pattern": "Higher highs",
            "momentum": "Constructive",
            "summary": "Price is above key moving averages.",
        }
    )

    class FakeOllamaClient:
        def chat(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return {
                    "message": {"content": '{"ticker": "TEST", "trend": "bullish"'},
                    "done_reason": "length",
                    "eval_count": 321,
                }
            return {"message": {"content": valid_content}, "done_reason": "stop"}

    monkeypatch.setattr(llm, "_ollama_client", FakeOllamaClient())

    result = llm.call_local("Analyze TEST.", TechnicalAnalysis, max_retries=1)

    assert result.ticker == "TEST"
    assert len(calls) == 2
    assert "Return a NEW complete JSON object" in calls[1]["messages"][-1]["content"]
    assert "Do not continue the prior partial response" in calls[1]["messages"][-1]["content"]


def test_deterministic_regime_classifier(monkeypatch):
    monkeypatch.setattr(settings, "regime_mode", "deterministic")

    result = regime.classify(
        "The market rally broadened with growth momentum, record highs, and risk-on flows."
    )

    assert result.regime == MarketRegime.RISK_ON
    assert result.confidence > 0


def test_regime_falls_back_to_deterministic(monkeypatch):
    def fail_llm(*args, **kwargs):
        raise TimeoutError("local model was too slow")

    monkeypatch.setattr(settings, "regime_mode", "local")
    monkeypatch.setattr(settings, "regime_fallback", "deterministic")
    monkeypatch.setattr(regime, "call_local", fail_llm)

    result = regime.classify(
        "The market rally broadened with growth momentum, record highs, and risk-on flows."
    )

    assert result.regime == MarketRegime.RISK_ON
    assert result.confidence > 0

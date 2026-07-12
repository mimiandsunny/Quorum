# Fincept Terminal Architecture Review for StockInvest

Reviewed: 2026-04-22

Source repo: https://github.com/Fincept-Corporation/FinceptTerminal

Companion reverse review: [Using Fincept Terminal as the Base: What to Improve from StockInvest](./fincept-as-base-stockinvest-lessons-2026-04-22.md)

## Executive Summary

Fincept Terminal is useful to study because it is trying to be a full financial terminal, while StockInvest is currently a focused daily signal-generation system. The best lesson is not "rewrite StockInvest as a C++ desktop app." The useful lesson is architectural: Fincept separates UI, services, storage, Python analytics, data fan-out, and tool/agent integration into explicit layers, and its DataHub design gives every screen and agent one shared source of truth for market data.

For StockInvest, the highest-value improvements are:

1. Add a small in-process `DataHub` / data cache layer so yfinance, news, fundamentals, technicals, and macro digest reads are fetched once per run and shared by all stages.
2. Make every pipeline stage observable with structured timing, model metadata, cache status, and failure reason.
3. Create a formal topic/key registry for data and LLM outputs, similar to Fincept's `market:quote:AAPL` topic model.
4. Keep StockInvest's web dashboard and Python agent pipeline, but adopt Fincept's screen/service separation idea as route/view/service separation.
5. Add a phased architecture plan instead of expanding features opportunistically.

## Fincept Highlights

The project describes Fincept Terminal v4 as a native C++20 desktop finance terminal using Qt6 for UI/rendering and embedded Python for analytics. Its README positions the app as a "Bloomberg-terminal-class" native binary with no Electron/browser runtime.

Major product highlights from the repo:

- Native desktop shell: C++20, Qt6 Widgets, Qt6 Charts, platform-specific packaging.
- Embedded Python analytics: DCF, portfolio optimization, VaR, Sharpe, derivatives pricing, ML/factor tooling, and data scripts.
- AI agents: the README claims 37 agents across investor, trader, economic, and geopolitics frameworks, with local LLM and multi-provider support including Ollama.
- Broad data connectivity: the README claims 100+ connectors, including Yahoo Finance, FRED, IMF, World Bank, AkShare, DBnomics, government APIs, crypto, and alternative data overlays.
- Trading layer: crypto WebSockets, paper trading, algorithmic trading, and many broker adapters.
- Quant/analytics modules: QuantLib suite, AI Quant Lab, backtesting, and portfolio analytics.
- Workflow layer: node editor, MCP tools, and automation pipelines.
- Product maturity signals: releases, docs, CMake presets, platform packaging, contributor guides, and a long source-module list.

Important caveat: the repo README and GitHub release page disagree in some places because GitHub-rendered search snippets and raw README snapshots can drift. The releases page showed v4.0.1 as latest when reviewed, while one README rendering mentioned v4.0.2. For StockInvest planning, that version mismatch is not material; the architecture lessons are.

## Fincept Architecture Patterns Worth Learning

### 1. Layered Product Architecture

Fincept's architecture doc describes a stack with:

- UI layer: Qt widgets, charts, reusable terminal-style components.
- Application layer: screens, services, trading engine, MCP integration.
- Infrastructure layer: HTTP, SQLite, WebSocket, Python bridge.
- Platform layer: Qt platform abstraction across Windows, macOS, and Linux.

StockInvest already has a simpler version of this:

- Dashboard/API: `main.py` and `templates/dashboard.html`
- Pipeline orchestration: `agents/graph.py`
- Data fetching and technical computation: `data/pipeline.py`
- Persistence: `data/storage.py`
- Analyst/research/trader/risk agents: `agents/*`
- Scheduler: `scheduler.py`

The improvement is to make this layering explicit in docs and code boundaries. Today, some pieces know too much about neighboring pieces; for example, graph orchestration directly fetches and saves stage outputs. That is workable now, but it will get harder as more data sources, models, and scoring logic appear.

### 2. Screen/Service Separation

Fincept's doc states that screens render UI and services own fetching, caching, and processing. In StockInvest terms:

- FastAPI routes should only validate inputs and return views/API responses.
- Services should own run orchestration, signal querying, scoring, and digest management.
- Pipeline stages should not directly know dashboard behavior.

Practical StockInvest mapping:

| Fincept concept | StockInvest equivalent |
|---|---|
| `*Screen.cpp` | FastAPI route + Jinja template |
| `*Service.cpp` | Python service module |
| `DataHub` | proposed in-process data/cache registry |
| `PythonRunner` | local Python analytics and deterministic analyst code |
| `MCP tools` | future dashboard/agent tools |
| `Storage repositories` | `data/storage.py`, later split by table/domain |

### 3. DataHub: One Producer, Many Subscribers

Fincept's DataHub doc is the most relevant architecture artifact. Their stated problem is duplicate fetches: many widgets and screens each own timers and each call services independently. Their goal is one fetch per symbol/source, then fan-out to every subscriber.

Core DataHub ideas:

- Topic keys like `market:quote:AAPL`, `news:symbol:NVDA`, `econ:fred:GDP`, `agent:hedgefund:run:42`.
- Producers own refresh logic for topic patterns.
- Subscribers receive cached values immediately if fresh and receive future updates.
- TTL and minimum refresh interval are per topic.
- WebSocket topics are push-only.
- Scheduler refreshes only topics with subscribers.
- In-flight tracking prevents duplicate fetches.
- Hub stats expose subscriber count, last publish time, in-flight state, and publish counts.

StockInvest has a similar risk, even without a multi-screen desktop:

- Each ticker currently calls yfinance for price history, fundamentals, and news.
- Analyst comparison, scoring, dashboard, and future backtests may re-read or re-fetch overlapping data.
- LLM calls are expensive and slow, so duplicated prompts or retries need clear dedupe/observability.
- Dashboard and scorer query data separately after the run.

The right StockInvest adaptation is smaller than Fincept's C++ DataHub:

```text
datahub/
  topics.py        # topic names and TTL policy
  hub.py           # get/set/request/cache/stats
  producers.py     # yfinance, digest, score history producers
  snapshots.py     # immutable per-run data snapshots
```

Example topic keys:

```text
market:history:SPY:365d
market:fundamentals:SPY
market:news:SPY
technical:computed:SPY
digest:chatgpt:current
analyst:technical:SPY:2026-04-22
llm:local:technical:SPY:run_id
signal:final:SPY:2026-04-22
score:signal:SPY:2026-04-22
```

This would reduce redundant calls, make timing visible, and make it easier to explain why the dashboard shows a given value.

### 4. Explicit Storage/Repositores

Fincept uses SQLite repositories and migrations. StockInvest currently keeps schema and many query functions in `data/storage.py`. That is fine for a small app, but this file is becoming a domain hub for signals, reports, debate transcripts, run metadata, scoring, concentration warnings, and leaderboard queries.

Recommended next split:

```text
data/storage/
  connection.py
  schema.py
  signals.py
  analysts.py
  runs.py
  scores.py
  leaderboard.py
```

This mirrors Fincept's repository discipline without importing its C++ complexity.

### 5. Build and Runtime Discipline

Fincept's CMake file shows heavy attention to pinned toolchains, optional dependencies, stubs when optional capabilities are unavailable, compiler caches, and platform packaging. For StockInvest, the equivalent is:

- Pin Python dependencies with a lockfile.
- Add a startup environment check for Ollama availability, local model presence, database connectivity, and required API keys.
- Add optional-provider stubs: if news/fundamentals fail, record stale source and continue.
- Keep deterministic baselines available for analyst fallback.
- Add a "doctor" command:

```bash
python -m tools.doctor
```

The doctor should report:

- database reachable
- Ollama reachable
- configured local model present
- OpenAI key present only if cloud stages enabled
- yfinance smoke test passed
- latest completed run and last failure reason

### 6. Observability as a Product Feature

Fincept's DataHub includes `stats()` and an inspector screen. StockInvest already logs stage timings and model metadata, but the dashboard does not yet expose enough operational state.

Add a "Run Inspector" section to the dashboard:

- per ticker stage durations
- local model used
- `done_reason`, `eval_count`, and output length for local LLM calls
- fallback path used: LLM vs deterministic baseline
- stale data sources
- rejected risk reasons
- API/model error summaries
- cache hit/miss if DataHub is added

This is especially important while testing `qwen3.6:35b-a3b`, because success is not just "the pipeline finished"; success is "the model returns valid structured JSON with predictable latency and low fallback rate."

## What StockInvest Should Not Copy

Do not copy these parts unless the product goal changes:

- Native C++/Qt desktop shell. StockInvest is currently much easier to iterate as FastAPI + Python.
- Huge connector breadth. A focused set of reliable sources is better than 100 shallow connectors.
- Broker execution integrations. Keep StockInvest as research/signals until risk controls, audit trails, and paper trading are mature.
- Full node editor. A simpler YAML/Python pipeline config would solve most automation needs first.
- Broad "terminal" scope. Your edge is daily AI-assisted signal quality and local/cloud cost control, not recreating Bloomberg.

## Recommended StockInvest Improvements

### Phase 1: Stabilize Local LLM Analyst Runs

Goal: make local analyst output reliable before expanding features.

Work:

- Keep `ANALYST_MODE=local`.
- Keep `LOCAL_LLM_THINK=false` for Qwen structured JSON.
- Persist local LLM metadata per analyst call.
- Record parse failures and fallback counts in database, not only logs.
- Add a compact "analyst reliability" dashboard table.

Success metric:

- At least 95% valid JSON from local analysts across one week of normal runs.
- Average analyst stage time under the morning schedule budget.

### Phase 2: Add a Small DataHub

Goal: one source of truth for fetched ticker data.

Work:

- Add topic registry and TTL policy.
- Cache `TickerDataPackage` by `(ticker, date, price_history_days)`.
- Cache fundamentals/news separately so failures are visible and retryable.
- Add in-flight dedupe so a repeated run request does not duplicate active work.
- Add `hub.stats()` for dashboard display.

Success metric:

- No duplicate yfinance calls for the same ticker/source within one run.
- Dashboard can show data freshness per ticker.

### Phase 3: Split Storage by Domain

Goal: make persistence easier to extend.

Work:

- Move schema to `data/storage/schema.py`.
- Move signal CRUD to `data/storage/signals.py`.
- Move analyst reports and debate to their own repositories.
- Move scoring queries to `data/storage/scores.py`.
- Keep backward-compatible imports during migration.

Success metric:

- Storage changes can be tested per domain.
- Adding a new table does not require touching unrelated query code.

### Phase 4: Run Inspector and Model Benchmarking

Goal: turn model testing into repeatable evidence.

Work:

- Add `llm_calls` table:

```text
run_id
ticker
stage
model
provider
prompt_len
output_len
elapsed_seconds
done_reason
eval_count
parse_ok
fallback_used
error
created_at
```

- Add model comparison report:

```text
gemma4:26b vs qwen3.6:35b-a3b
valid JSON %
average latency
fallback %
average output length
signal changes
```

Success metric:

- Model choice is based on measured reliability and latency, not impressions from logs.

### Phase 5: Workflow Configuration

Goal: controlled flexibility without a full node editor.

Work:

- Add a simple run profile:

```yaml
name: qwen-local-test
tickers: [SPY, QQQ, AAPL]
analyst_mode: local
local_model: qwen3.6:35b-a3b
analysts_parallel_workers: 1
researchers_parallel: true
```

- Allow `python scheduler.py --profile qwen-local-test --now`.

Success metric:

- You can test Gemma vs Qwen without manually editing `.env`.

## Proposed Target Architecture

```text
FastAPI Dashboard/API
  |
  | reads run status, signals, scores, model telemetry
  v
Application Services
  - RunService
  - SignalService
  - ScoreService
  - DigestService
  - ModelBenchmarkService
  |
  v
Pipeline Graph
  fetch_data -> analysts -> researchers -> trader -> risk -> compose
  |
  v
DataHub / Snapshot Cache
  - topics
  - TTL policy
  - in-flight dedupe
  - producer stats
  |
  v
Producers
  - YFinance price/fundamentals/news
  - digest reader
  - deterministic technical calculator
  - local LLM caller
  |
  v
Storage
  - signals
  - analyst reports
  - debate transcripts
  - run metadata
  - signal scores
  - llm call telemetry
```

## Concrete First Implementation

The first improvement I would implement is not a full DataHub. I would start with telemetry because it immediately helps your Qwen/Gemma testing.

Add:

```text
data/storage/llm_calls.py
agents/llm_telemetry.py
```

Record every local LLM call with timing, model name, response metadata, parse status, and fallback status. Then add a dashboard table grouped by model and analyst.

Why first:

- It directly answers "why did Ollama return empty/truncated output?"
- It makes Qwen vs Gemma testing measurable.
- It is low-risk and does not change signal behavior.
- It creates the observability foundation needed before adding DataHub.

Then implement the DataHub once you know the pipeline bottlenecks from actual telemetry.

## Source Notes

- Fincept README: native C++20, Qt6, embedded Python, feature list, connectors, agents, trading, QuantLib, workflow claims.  
  https://github.com/Fincept-Corporation/FinceptTerminal

- Fincept architecture doc: layer model, screen/service separation, Python bridge, storage, security, threading.  
  https://raw.githubusercontent.com/Fincept-Corporation/FinceptTerminal/main/docs/ARCHITECTURE.md

- Fincept DataHub architecture: topic model, producer/subscriber design, TTL policy, scheduler, caching, observability, failure modes.  
  https://raw.githubusercontent.com/Fincept-Corporation/FinceptTerminal/main/fincept-qt/DATAHUB_ARCHITECTURE.md

- Fincept releases page: latest release metadata observed during review.  
  https://github.com/Fincept-Corporation/FinceptTerminal/releases

- StockInvest local references reviewed:
  - `agents/graph.py`
  - `data/pipeline.py`
  - `data/storage.py`
  - `agents/llm.py`
  - `main.py`
  - `scheduler.py`

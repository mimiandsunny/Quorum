# Using Fincept Terminal as the Base: What to Improve from StockInvest

Reviewed: 2026-04-22

Base project: https://github.com/Fincept-Corporation/FinceptTerminal

Local reference project: StockInvest

## Executive Summary

If Fincept Terminal becomes the base platform, StockInvest should not replace it. StockInvest should become a focused **daily AI investment committee module** inside it.

Fincept already has the stronger terminal shell: native desktop UI, broad data connectors, broker integrations, paper trading, portfolio screens, node workflows, MCP tooling, and an in-process DataHub concept. StockInvest's strength is different: it has a narrow, explainable, scheduled pipeline that turns market data into auditable signals, applies deterministic risk rules, and scores outcomes after the holding period.

The best hybrid is:

```text
Fincept Terminal = operating system for finance
StockInvest = disciplined signal-generation and self-grading engine
```

The top improvements Fincept can learn from StockInvest are:

1. Add an opinionated morning signal pipeline instead of only broad agent/chat tooling.
2. Make local/cloud model routing explicit by stage to control cost.
3. Require schema-validated JSON from all agents that produce trading decisions.
4. Put deterministic risk management after LLM reasoning and before any trade/paper order.
5. Score every signal after its holding period and feed track records back into future decisions.
6. Build a run inspector with model metadata, fallback rate, latency, and per-stage errors.
7. Treat "agent quality" as measured evidence, not personality or prompt branding.

## Why StockInvest Helps Fincept

Fincept's scope is broad. That is powerful, but breadth can make it hard for a user to answer one concrete question every morning:

> What should I buy, sell, hold, reject, and why?

StockInvest is narrow enough to answer that question end to end:

```text
data package
  -> technical / fundamentals / sentiment / news analysts
  -> bull and bear researchers
  -> trader synthesis
  -> deterministic risk manager
  -> final signal
  -> post-holding-period scorer
  -> agent leaderboard / track record
```

This gives Fincept a production-grade workflow pattern for AI investing:

- evidence first
- debate second
- decision third
- deterministic risk gate fourth
- performance scoring later

That pattern is more valuable than simply adding more agents.

## StockInvest Features to Bring into Fincept

### 1. Morning Investment Committee

Fincept has many screens and agents. StockInvest has one repeatable committee.

Add a Fincept screen/module called:

```text
Signal Committee
```

Core workflow:

```text
Universe selection
  -> daily macro/regime context
  -> per-ticker data package
  -> analyst panel
  -> bull/bear debate
  -> trader decision
  -> deterministic risk verdict
  -> approved/rejected signal table
  -> post-run scoring
```

This should be a first-class workflow, not only a chat prompt. The user should see:

- ticker
- decision
- confidence
- entry zone
- stop
- targets
- reward/risk
- position size
- risk verdict
- thesis
- bull case
- bear case
- rejected reasons
- later score

### 2. Stage-Based Model Routing

Fincept already claims multi-provider LLM support, including local LLMs and providers like OpenAI, Anthropic, Gemini, Groq, DeepSeek, MiniMax, OpenRouter, and Ollama. StockInvest adds a practical routing policy:

```text
cheap/local models -> repetitive analyst summarization
stronger cloud model -> debate and final synthesis
pure Python -> risk manager and scoring
```

Recommended Fincept routing table:

| Stage | Default model path | Reason |
|---|---|---|
| Technical analyst | deterministic + local LLM | technical indicators are mostly computed, LLM summarizes |
| Fundamentals analyst | deterministic + local LLM | mostly structured financial data |
| Sentiment/news analyst | local LLM with compact schema | high volume, cost-sensitive |
| Bull researcher | cloud or strong local | higher reasoning demand |
| Bear researcher | cloud or strong local | higher reasoning demand |
| Trader synthesis | cloud or best local | highest impact decision |
| Risk manager | deterministic code | must be reproducible |
| Scorer | deterministic code | must be auditable |

Fincept should expose this as a profile:

```yaml
profile: cost-controlled-local
analysts:
  provider: ollama
  model: qwen3.6:35b-a3b
  think: false
  max_retries: 1
researchers:
  provider: openai
  model: gpt-5-nano
trader:
  provider: openai
  model: gpt-5-nano
risk:
  provider: deterministic
```

The important lesson is that provider support is not enough. Fincept should make **routing policy** visible, testable, and budget-aware.

### 3. Schema-Bound Agent Outputs

StockInvest uses structured Pydantic models for analyst, debate, trader, risk, signal, and scoring outputs. Fincept should adopt the same discipline for any agent that affects research or trading.

Every decision-producing agent should return a schema like:

```json
{
  "ticker": "SPY",
  "decision": "BUY",
  "confidence": 0.72,
  "entry_zone": [640.0, 654.0],
  "stop_loss": 646.0,
  "targets": [700.0],
  "holding_period_days": 5,
  "thesis": "..."
}
```

Rules:

- No free-form trading decisions.
- No execution from natural language.
- Validate JSON before persisting.
- Retry once with a repair prompt.
- Fall back to deterministic baseline when local models fail.
- Store parse errors and model metadata.

This is especially important for local models through Ollama or llama.cpp. Fincept can have broad AI chat, but any signal path should be schema-first.

### 4. Local LLM Reliability Loop

StockInvest is currently testing local models like Gemma and Qwen through Ollama. The important pattern is not the exact model. The pattern is the reliability loop:

- force JSON schema mode when possible
- set `temperature=0.0` for first attempt
- use compact prompts
- disable model "thinking" for JSON when supported
- retry invalid JSON as a new complete object
- log `done_reason`, output length, prompt length, token/eval counts, and model name
- compare local LLM output to deterministic baseline

Fincept should add a local model benchmark panel:

```text
Model                 Valid JSON   Avg latency   Fallback %   Avg score
qwen3.6:35b-a3b       96%          34.2s         4%           0.61
gemma4:26b            88%          41.8s         12%          0.57
gemma4:31b-q4         91%          52.3s         9%           0.59
```

Without this, a multi-provider system becomes a model picker. With this, it becomes an evidence machine.

### 5. Deterministic Risk Manager After LLM Decision

This is one of StockInvest's strongest design choices.

The LLM can propose:

- direction
- confidence
- entry
- stop
- targets
- thesis

But deterministic code decides whether the trade is allowed.

StockInvest's current rules include:

- reject if confidence is below minimum
- reject if stop is too far from entry midpoint
- reject if holding period is too long
- reject if entry zone is too wide
- reject if reward/risk is below a hard minimum
- size by confidence tier
- halve size if reward/risk is below preferred threshold

Fincept should put the same risk gate in front of:

- paper trading
- broker order tickets
- strategy deployment
- AI-generated trade suggestions
- node-editor workflows that can submit orders

For Fincept, the risk manager should also add:

- account-level max daily loss
- max position size per symbol
- max sector concentration
- max correlated exposure
- no live order without user confirmation
- mandatory paper-trade mode for new strategies
- stale data rejection
- stop geometry checks, e.g. reject a BUY if stop sits inside/above the entry zone

The key principle:

```text
LLM may recommend. Deterministic risk manager must approve.
```

### 6. Signal Scoring and Agent Track Records

StockInvest scores signals after the holding period:

- direction correct
- entry hit
- stop hit
- target hit
- actual return
- composite score

Fincept should turn this into a first-class "Agent Performance" subsystem.

For every generated signal:

```text
signal_id
run_id
ticker
decision
entry_zone
stop_loss
targets
holding_period
agents_involved
model_versions
prompt_versions
data_snapshot_id
```

After the holding period:

```text
direction_correct
entry_hit
stop_hit
target_hit
actual_return_pct
score
```

Then compute:

- analyst accuracy by agent type
- model accuracy by model/provider
- ticker-specific agent accuracy
- market-regime-specific accuracy
- risk-manager rejection quality
- paper-trading P&L from approved signals
- opportunity cost from rejected signals

This makes Fincept's agents self-improving in a measured way. The system can then feed track records into future prompts:

```text
Technical analyst has been 7/10 correct for SPY recently.
Sentiment analyst has been 3/10 correct for SPY recently.
Weight technical evidence more heavily for this ticker.
```

### 7. Run Metadata and Auditability

StockInvest stores run metadata:

- run id
- start/end time
- attempted tickers
- completed tickers
- failed tickers
- errors
- concentration warnings

Fincept should extend that idea:

```text
run_id
profile_name
universe
data_snapshot_ids
model_profile
agent_versions
started_at
ended_at
status
errors
warnings
approved_signals
rejected_signals
paper_orders_created
```

This enables a professional "what happened this morning?" view:

- which tickers failed
- which model failed
- which source was stale
- which risk rule rejected a trade
- how many calls used local vs cloud
- how much the run cost
- whether the run was ready before market open

### 8. External Digest and Market Regime Context

StockInvest supports an external macro digest and classifies market regime once per run. Fincept has much richer macro/geopolitical data capabilities, but it should still compress them into a daily context packet for signal generation.

Add:

```text
Daily Context Pack
```

Inputs:

- macro news
- index trend
- VIX / volatility
- rates
- sector rotation
- geopolitical flags
- earnings calendar
- user-supplied notes

Output:

```json
{
  "date": "2026-04-22",
  "regime": "risk-on",
  "confidence": 0.68,
  "key_drivers": ["software rebound", "rates stable", "mega-cap breadth"],
  "risk_flags": ["overbought RSI in major indices", "high valuation"]
}
```

Then inject this packet into every analyst/research/trader stage.

### 9. Analyst Comparison Logs

StockInvest can compare deterministic baseline output to LLM analyst output. Fincept should adopt this because it has many more agents and providers.

For each analyst call:

```text
baseline_output
llm_output
diff
used_result
error
model
latency
```

Uses:

- detect hallucinations
- detect schema drift
- evaluate if local models are good enough
- show the user why an LLM result replaced or failed over to baseline
- build trust before letting an agent affect trading workflows

### 10. Decision Table First, Chat Second

Fincept has a broad AI terminal feel. StockInvest shows that for trading workflows, the user needs a decision table before a conversation.

Recommended Fincept screen layout:

```text
Signal Committee

Top strip:
  run status | local/cloud cost | model profile | data freshness | market regime

Main table:
  ticker | decision | confidence | entry | stop | targets | R/R | size | risk | score

Expandable row:
  technical
  fundamentals
  sentiment
  news
  bull case
  bear case
  trader thesis
  risk reasons
  model telemetry

Side panel:
  agent leaderboard
  model reliability
  concentration warnings
  failed tickers
```

Chat remains useful, but it should explain and interrogate the table, not replace it.

## Proposed Hybrid Architecture

The cleanest integration is to keep Fincept as the host and add StockInvest as a service/module.

```text
Fincept Native Shell
  |
  | Signal Committee screen
  v
SignalCommitteeService
  |
  | starts/runs/monitors
  v
StockInvest Engine
  |
  | data package -> analysts -> debate -> trader -> risk -> signal
  v
Fincept DataHub + Storage
  |
  | persists signals, reports, telemetry, scores
  v
Dashboard / Paper Trading / Portfolio / Node Editor
```

There are three viable integration styles.

### Option A: Embedded Python Module

Fincept calls StockInvest pipeline code through its Python bridge.

Pros:

- easiest to reuse StockInvest logic
- no network service required
- natural fit for Fincept's embedded Python analytics model

Cons:

- C++ host must manage Python env, dependencies, and long-running jobs carefully
- local LLM calls can block if not pushed to workers

Best for:

- first prototype
- paper-trading only

### Option B: Local HTTP Service

StockInvest runs as a local FastAPI service. Fincept calls it over localhost.

Pros:

- clean process boundary
- easier crash isolation
- current StockInvest app already uses FastAPI
- easy to test independently

Cons:

- more moving parts
- needs service lifecycle management
- security must restrict localhost access

Best for:

- serious development
- cleaner separation between Fincept UI and signal engine

### Option C: Native C++ Orchestrator with Python Agents

Rebuild the pipeline orchestration in Fincept C++ and call Python only for analytics/LLM helpers.

Pros:

- strongest native integration
- best long-term UX
- direct access to DataHub, paper trading, and broker modules

Cons:

- highest rewrite cost
- easy to lose the simplicity that makes StockInvest useful

Best for:

- later stage after the workflow proves value

Recommended path:

```text
Option B first, Option C later only if the workflow becomes central.
```

## Fincept Improvements Inspired by StockInvest

### Product Improvements

- Add "Morning Signal Committee" as a top-level workflow.
- Add approved/rejected signal table with expandable reasoning.
- Add model-cost and local/cloud routing profile.
- Add "paper trade from approved signal" button, never direct live execution by default.
- Add signal score history and agent leaderboard.
- Add model reliability dashboard.

### Architecture Improvements

- Add `SignalCommitteeService`.
- Add `RiskGateService` for deterministic approval.
- Add `SignalScoringService`.
- Add `ModelTelemetryRepository`.
- Add `SignalRepository`.
- Add topic keys for signal workflow:

```text
signal:run:<run_id>:status
signal:data:<ticker>:<date>
signal:analyst:<ticker>:technical:<run_id>
signal:debate:<ticker>:<run_id>
signal:decision:<ticker>:<run_id>
signal:risk:<ticker>:<run_id>
signal:score:<ticker>:<signal_date>
model:telemetry:<run_id>
```

### Safety Improvements

- Require deterministic risk approval before any paper/live order.
- Add hard kill switch for AI-generated order flow.
- Add stale-data rejection.
- Add stop-entry geometry checks.
- Add max daily AI-generated trade count.
- Add account-level drawdown guard.
- Add model output validation before action.

### Research Quality Improvements

- Store analyst reports and debate transcripts, not only final chat output.
- Score each signal after the holding period.
- Track per-agent accuracy by ticker and regime.
- Feed track records back into the debate.
- Compare deterministic and LLM analysts.
- Run A/B tests across local models.

## First Feature to Build in Fincept

Build the smallest useful StockInvest-inspired module:

```text
Signal Committee MVP
```

Scope:

- User chooses 5-20 tickers.
- System builds data packages.
- Local analysts produce structured JSON.
- Bull and bear researchers generate cases.
- Trader produces final decision.
- Deterministic risk gate approves/rejects.
- Results appear in a table.
- Signals are saved.
- No live trading.

Data stored:

```text
runs
signals
analyst_reports
debates
risk_assessments
llm_calls
signal_scores
```

Do not start with:

- broker execution
- node editor integration
- 100+ data connectors
- custom strategy marketplace
- live auto-trading

The first version should prove:

```text
Can Fincept produce better, auditable daily research decisions than a user manually reading charts/news?
```

## Migration Plan

### Phase 1: Prototype Outside Fincept UI

- Keep StockInvest running as local service.
- Add a simple Fincept button or external link that opens the StockInvest dashboard.
- Use one shared model profile and one ticker universe.

Success:

- Fincept user can run StockInvest workflow without touching terminal commands.

### Phase 2: Fincept Reads StockInvest Results

- Fincept imports signals from StockInvest database/API.
- Show signal table inside Fincept.
- Link each signal to thesis, bull/bear case, and risk reasons.

Success:

- Fincept becomes the viewing shell for StockInvest outputs.

### Phase 3: Fincept Controls Runs

- Fincept starts a run through API.
- Fincept shows live run progress.
- Fincept stores model profile and ticker universe.

Success:

- User can manage the whole workflow from Fincept.

### Phase 4: Shared DataHub

- StockInvest fetches data through Fincept DataHub or receives Fincept data snapshots.
- Reduce duplicate Yahoo/news/fundamental fetches.
- Add topic-based freshness display.

Success:

- One market-data fetch feeds terminal screens and signal engine.

### Phase 5: Paper Trading Integration

- Approved signals can create paper orders.
- Risk gate must pass again at order time.
- Scorer compares intended signal vs paper execution.

Success:

- Signal quality, execution quality, and risk discipline can be measured separately.

## What This Hybrid Would Beat

Most finance AI tools are either:

- broad chat interfaces with weak auditability
- chart dashboards without reasoning
- backtest tools without daily workflow
- broker terminals without AI accountability

Fincept + StockInvest could become:

```text
a terminal that generates, explains, gates, executes in paper mode, and scores its own research calls
```

That is a stronger product than either project alone.

## Final Recommendation

Use Fincept Terminal as the base only if your goal is a richer, terminal-style product. If your immediate goal is better daily signals, keep StockInvest as the primary project and borrow Fincept's DataHub ideas.

If you do choose Fincept as the base, do not start by porting all StockInvest code. Start by adding StockInvest's workflow discipline:

1. Morning Signal Committee
2. schema-bound agents
3. deterministic risk gate
4. signal scoring
5. model telemetry
6. local/cloud routing profiles

Those are the pieces that would make Fincept smarter, safer, and more measurable.

## Source Notes

- Fincept Terminal README and feature overview: https://github.com/Fincept-Corporation/FinceptTerminal
- Fincept architecture doc: https://raw.githubusercontent.com/Fincept-Corporation/FinceptTerminal/main/docs/ARCHITECTURE.md
- Fincept DataHub architecture: https://raw.githubusercontent.com/Fincept-Corporation/FinceptTerminal/main/fincept-qt/DATAHUB_ARCHITECTURE.md
- Local StockInvest files reviewed:
  - `agents/graph.py`
  - `agents/llm.py`
  - `agents/risk_manager.py`
  - `agents/scorer.py`
  - `data/pipeline.py`
  - `data/storage.py`


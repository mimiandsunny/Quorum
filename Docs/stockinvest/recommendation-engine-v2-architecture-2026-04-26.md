# StockInvest Recommendation Engine v2 Architecture

Date: 2026-04-26
Status: Design proposal
Scope: Architecture on top of the current StockInvest pipeline for short-term trading, long-term investing, and quant strategy evaluation.

## Goal

StockInvest should not be an app where an LLM says "BUY" and the system trusts it. The stronger architecture is:

```text
validated data
+ quant features
+ multi-horizon alpha engines
+ LLM research committee
+ deterministic risk
+ portfolio construction
+ immutable recommendation ledger
+ outcome scoring
+ calibrated reflection
```

The practical goal is not perfect prediction. The goal is a recommendation engine that is auditable, falsifiable, benchmark-aware, portfolio-aware, and able to learn from its own track record.

## Design Principle

The LLM should not be the recommendation engine. It should be the research committee.

Deterministic code should own:

- data validation
- feature computation
- strategy rules
- portfolio sizing
- risk gates
- execution safety
- scoring
- calibration

LLMs should own:

- narrative synthesis
- bull/bear debate
- thesis generation
- evidence critique
- uncertainty explanation
- human-readable reflection

## Current StockInvest Shape

The current pipeline is already a good base:

```text
tickers
-> data package
-> analysts
-> bull/bear researchers
-> judge
-> trader decision
-> deterministic risk manager
-> final signal
-> optional paper trade
-> scorer
```

Strengths:

- Structured Pydantic contracts for analysts, researchers, trader decisions, risk, signals, and scoring.
- Deterministic risk manager after LLM reasoning.
- Multi-round bull/bear debate with a judge.
- Paper-trading integration is isolated from signal persistence.
- Basic after-the-fact scoring already exists.
- Digest-driven watchlist can expand the ticker universe.

Current gaps to fix before calling the module "excellent":

- Signals are keyed by `(ticker, date)`, so same-day reruns overwrite recommendations.
- Short-side returns are not scored with side-correct return math.
- Agent track record is approximate and uses final signal direction as a proxy for each analyst.
- Sector and exposure data are not reliably stored, so concentration checks can be skipped.
- YFinance data anomalies can flow into LLM reasoning without enough validation.
- Current scoring is mostly directional, not benchmark-relative, risk-adjusted, or execution-aware.

## Target Architecture

```text
Universe Layer
  -> Data Snapshot Layer
  -> Feature Store
  -> Multi-Horizon Alpha Engines
  -> LLM Research Committee
  -> Recommendation Ledger
  -> Portfolio Construction
  -> Risk Manager
  -> Paper/Live Execution
  -> Outcome Scorer
  -> Reflection and Calibration
```

## 1. Universe Layer

The universe layer decides what symbols deserve analysis before expensive agents run.

Inputs:

- base watchlist from `config.py`
- digest watchlist from `data/external/digest_watchlist.json`
- liquidity screen
- momentum screen
- value/quality screen
- earnings/event calendar
- sector ETF constituents
- macro theme screen
- manual user watchlist

Output schema:

```json
{
  "symbol": "NVDA",
  "asset_type": "equity",
  "region": "US",
  "sector": "Technology",
  "industry": "Semiconductors",
  "source": ["core_watchlist", "ai_theme", "liquidity_screen"],
  "liquidity_score": 0.97,
  "valid_data_symbol": true,
  "reason_added": "AI infrastructure leader with high liquidity"
}
```

Rules:

- Validate the ticker before adding it to the daily run.
- Reject unsupported exchanges or convert them to the correct market-data symbol.
- Require minimum average dollar volume for tradeable recommendations.
- Keep ETFs, mega-cap stocks, volatile names, and long-term quality names in separate buckets.

This avoids spending full pipeline time on invalid or weak symbols.

## 2. Data Snapshot Layer

Every recommendation should point to an immutable data snapshot.

The snapshot records what the system knew at decision time:

- price history
- OHLCV bars
- technical indicators
- fundamentals
- sector and industry
- news items
- macro digest
- market regime
- benchmark prices
- volatility
- earnings dates
- data-source freshness
- data-quality warnings

Suggested table:

```text
data_snapshots
- snapshot_id
- run_id
- symbol
- captured_at
- source_versions
- price_payload
- fundamentals_payload
- news_payload
- macro_payload
- feature_payload
- data_quality_flags
```

Why this matters:

- Recommendations become reproducible.
- Scoring can use the exact inputs from the original decision.
- Data anomalies can be tracked instead of silently absorbed.
- Prompt regressions can be debugged.

## 3. Feature Store

Before the LLM sees a stock, compute deterministic features.

Short-term features:

- 1-day, 5-day, 20-day return
- RSI
- MACD
- ATR
- gap percentage
- realized volatility
- volume spike
- relative strength vs SPY, QQQ, and sector ETF
- distance to support and resistance
- distance from 20-day, 50-day, and 200-day moving averages
- earnings/event risk
- news sentiment score
- overnight risk flag

Long-term features:

- revenue growth
- EPS growth
- FCF yield
- ROE or ROIC
- debt trend
- margin trend
- valuation vs own history
- valuation vs sector
- earnings revision trend
- shareholder yield
- quality score
- moat proxy score

Quant features:

- beta
- rolling volatility
- max drawdown
- correlation cluster
- factor exposure
- momentum rank
- value rank
- quality rank
- low-vol rank
- regime sensitivity
- liquidity score

The LLM can interpret these features, but it should not be responsible for computing them.

## 4. Multi-Horizon Alpha Engines

Separate short-term trading from long-term investing. One signal should not pretend to serve both jobs.

### ShortTermAlphaEngine

Purpose: swing trades from 1 to 10 trading days.

Inputs:

- technical momentum
- relative strength
- volume
- news catalyst
- earnings/event calendar
- volatility and ATR
- regime

Outputs:

```json
{
  "strategy_type": "short_term",
  "direction": "long",
  "horizon_days": 5,
  "expected_return": 0.035,
  "expected_drawdown": -0.018,
  "confidence": 0.68,
  "entry_zone": [254.0, 257.0],
  "stop_loss": 246.5,
  "targets": [277.9],
  "evidence": ["relative strength vs QQQ", "bullish MACD", "positive catalyst"],
  "invalidation": "close below 20-day moving average"
}
```

### LongTermAlphaEngine

Purpose: investment ideas from 3 to 24 months.

Inputs:

- quality
- valuation
- growth
- FCF
- balance sheet
- industry structure
- macro theme
- earnings durability

Outputs:

```json
{
  "strategy_type": "long_term",
  "direction": "long",
  "horizon_days": 180,
  "expected_return": 0.18,
  "expected_drawdown": -0.12,
  "confidence": 0.64,
  "valuation_case": "fair-to-cheap vs growth durability",
  "thesis": "compounder with durable revenue growth and margin stability",
  "invalidation": "two consecutive quarters of margin compression or guide-down"
}
```

### QuantAlphaEngine

Purpose: systematic expected-return estimate.

Inputs:

- factor ranks
- historical forward returns
- regime bucket
- volatility
- correlation
- benchmark-relative behavior

Outputs:

```json
{
  "strategy_type": "quant",
  "direction": "long",
  "horizon_days": 20,
  "expected_return": 0.045,
  "expected_volatility": 0.21,
  "hit_rate_estimate": 0.57,
  "confidence": 0.61,
  "factor_drivers": ["momentum", "quality", "sector strength"]
}
```

### EventAlphaEngine

Purpose: handle earnings, product launches, litigation, macro releases, or M&A separately.

Rules:

- Never allow event trades to be treated like ordinary technical trades.
- Require explicit event date.
- Require expected volatility range.
- Require "do nothing" as a valid output when event risk is unpriced or unknowable.

## 5. LLM Research Committee

Keep the current analyst, bull, bear, judge, and trader pattern, but make it consume deterministic alpha engine outputs.

The LLM committee should answer:

- What is the strongest bull case?
- What is the strongest bear case?
- Which evidence is real and which is narrative?
- Does the short-term setup conflict with the long-term setup?
- What would prove the thesis wrong?
- Is confidence justified by historical calibration?

Committee flow:

```text
feature packet
+ alpha engine outputs
+ prior track record
-> bull researcher
-> bear researcher
-> judge
-> trader/investment committee synthesis
```

Important rule:

If deterministic alpha engines are weak or contradictory, the LLM should not rescue the trade with prose. It should produce HOLD or AVOID.

## 6. Recommendation Ledger

Replace signal overwrite behavior with immutable recommendations.

Suggested table:

```text
recommendations
- recommendation_id
- run_id
- snapshot_id
- symbol
- created_at
- strategy_type
- horizon_days
- decision
- side
- confidence
- expected_return
- expected_drawdown
- benchmark_symbol
- sector_benchmark_symbol
- entry_zone
- stop_loss
- targets
- thesis
- invalidation
- alpha_outputs
- committee_outputs
- risk_verdict
- portfolio_target_weight
- model_versions
```

Rules:

- Never overwrite a recommendation.
- Same-day reruns create a new recommendation version.
- Paper trades reference `recommendation_id`, not just `(ticker, date)`.
- Scoring references `recommendation_id`.
- Dashboard can still show latest recommendation per symbol.

This is the foundation for serious evaluation.

## 7. Portfolio Construction

StockInvest should graduate from per-trade sizing to portfolio-aware construction.

Inputs:

- confidence
- expected return
- expected drawdown
- volatility
- correlation with existing positions
- sector exposure
- factor exposure
- liquidity
- regime
- current open positions

Sizing methods:

- volatility targeting
- capped fractional Kelly
- risk parity
- max single-name exposure
- max sector exposure
- max factor exposure
- max daily new-risk budget

Output:

```json
{
  "symbol": "AMD",
  "target_weight": 0.025,
  "max_loss_budget": 0.005,
  "risk_budget_reason": "high conviction, but correlated with NVDA and QQQ"
}
```

Portfolio rules:

- Cap correlated AI/chip exposure.
- Cap same-sector recommendations.
- Cap total short exposure.
- Cap total gross exposure.
- Separate long-term positions from short-term swing trades.
- Allow cash as a deliberate allocation, not a failure.

## 8. Risk Manager v2

Keep deterministic risk management, but expand it.

Pre-trade risk checks:

- confidence floor
- reward/risk minimum
- stop distance maximum
- ATR-aware stop sanity
- entry-zone width maximum
- liquidity minimum
- earnings/event proximity
- short borrowability
- gap risk
- stale-data rejection
- invalid benchmark rejection

Portfolio risk checks:

- sector concentration
- correlation concentration
- factor exposure
- total gross exposure
- net exposure
- max daily new positions
- max daily risk budget
- max open positions
- max drawdown kill switch

The risk manager should be able to produce:

```text
APPROVED
APPROVED_SMALLER
REJECTED
PAPER_ONLY
HOLD_NO_TRADE
```

## 9. Execution Layer

Separate recommendation from execution.

```text
recommendation = "this is attractive"
portfolio construction = "own 2.5%"
execution = "submit order safely"
```

Execution should support:

- paper trading by default
- live trading only after explicit gate
- idempotency by `recommendation_id`
- bracket orders for short-term trades
- staged entries for long-term positions
- order status reconciliation
- slippage tracking
- fill quality tracking
- execution skip audit rows

Do not let execution failures roll back recommendations.

## 10. Outcome Scorer

The scorer is the learning engine. It must be side-correct, benchmark-relative, and execution-aware.

Score components:

- absolute return
- benchmark-relative return
- sector-relative return
- side-correct return
- target hit
- stop hit
- target/stop sequence
- max adverse excursion
- max favorable excursion
- time to target
- confidence calibration
- realized reward/risk
- portfolio contribution
- paper execution result
- thesis validity

For long trades:

```text
side_return = (exit_price - entry_price) / entry_price
```

For short trades:

```text
side_return = (entry_price - exit_price) / entry_price
```

Suggested scoring formula:

```text
final_score =
  0.30 * benchmark_relative_return_score
+ 0.20 * drawdown_control_score
+ 0.15 * target_stop_score
+ 0.15 * confidence_calibration_score
+ 0.10 * thesis_quality_score
+ 0.10 * portfolio_contribution_score
```

Confidence calibration examples:

- If confidence is 0.70, roughly 70 percent of similar recommendations should be profitable or outperforming over time.
- Penalize high-confidence failures more than low-confidence failures.
- Reward correct low-confidence HOLD decisions when the setup was genuinely poor.

## 11. Reflection and Calibration

Reflection should not be vague memory. It should be numeric calibration plus a short natural-language lesson.

Examples:

```text
Technical analyst:
- Strong on PLTR and COIN 5-day momentum.
- Weak when RSI > 80 and entries chase extended moves.

Fundamentals analyst:
- Useful for 90-day quality calls.
- Low value for 3-day swing trades.

News analyst:
- Overweights AI headlines.
- Needs penalty when news is not company-specific.

Trader:
- Overconfident when reward/risk is created by extremely tight stops.
- Better when judge confidence is above 0.65.

Risk manager:
- Rejections avoided losses in high-volatility names.
- Needs review when rejected names later strongly outperformed.
```

Reflection injection should include:

- ticker-level track record
- strategy-level track record
- regime-level track record
- analyst-level calibration
- trader confidence calibration
- risk rejection quality

This should influence:

- prompt context
- alpha engine weights
- confidence adjustment
- position sizing
- whether the system chooses HOLD

## 12. Backtesting and Research Harness

Borrow lessons from mature projects:

- QuantConnect LEAN: separate universe, alpha, portfolio construction, execution, and risk.
- Qlib: model workflow, factor evaluation, benchmark-relative backtesting, IC analysis.
- Backtrader: analyzers for drawdown, Sharpe, trade stats, MAE/MFE, and strategy diagnostics.
- OpenBB: standardized data access layer.
- FinRL: train-test-trade discipline, useful later after deterministic evaluation is strong.
- TradingAgents: multi-agent bull/bear discussion pattern, useful for research committee design.

StockInvest should add:

```text
recommendation/backtest.py
recommendation/analyzers.py
recommendation/factor_eval.py
recommendation/walk_forward.py
```

Backtest requirements:

- train/test date separation
- walk-forward validation
- no lookahead bias
- benchmark-relative metrics
- cost and slippage assumptions
- long and short support
- event calendar awareness
- portfolio-level drawdown
- rejected-signal opportunity-cost analysis

## 13. Proposed Package Layout

```text
recommendation/
  universe.py
  snapshots.py
  features.py
  alpha_short.py
  alpha_long.py
  alpha_quant.py
  alpha_event.py
  committee.py
  ledger.py
  portfolio.py
  risk.py
  execution.py
  scorer.py
  reflection.py
  backtest.py
  analyzers.py
```

Current modules can be reused:

```text
data/pipeline.py        -> snapshot source
agents/analysts/*      -> LLM analyst committee inputs
agents/researchers/*   -> bull/bear/judge committee
agents/trader.py       -> committee synthesis
agents/risk_manager.py -> risk v1 base
agents/paper_trader.py -> execution v1 base
agents/scorer.py       -> scorer v1 base
```

## 14. Dashboard Additions

The dashboard should separate:

- recommendations
- approved trades
- rejected trades
- paper positions
- scores
- calibration
- model versions

Useful views:

- Today: latest recommendations by symbol.
- Portfolio: proposed target weights and current exposure.
- Risk: rejected recommendations and reasons.
- Scores: performance by horizon, strategy, ticker, sector, and regime.
- Calibration: confidence bucket accuracy.
- Reflection: latest lessons injected into the agents.
- Audit: raw data snapshot and model outputs for any recommendation.

## 15. Implementation Roadmap

### Phase 1: Fix Evaluation Foundations

Priority:

1. Add immutable `recommendations` table.
2. Add `recommendation_id` to paper trades and scores.
3. Fix short-side return scoring.
4. Score vs benchmark and sector benchmark.
5. Store data snapshots.
6. Stop overwriting same-day recommendations.

Expected result:

StockInvest can honestly answer: "Was this exact recommendation right?"

### Phase 2: Improve Data and Universe

Priority:

1. Validate ticker symbols before daily run.
2. Add sector and industry to stored snapshots.
3. Add liquidity and data-quality gates.
4. Add earnings/event calendar.
5. Flag data anomalies before LLM prompts.

Expected result:

The system stops generating polished recommendations from weak inputs.

### Phase 3: Add Multi-Horizon Alpha

Priority:

1. Add `ShortTermAlphaEngine`.
2. Add `LongTermAlphaEngine`.
3. Add `QuantAlphaEngine`.
4. Update trader prompt to consume alpha outputs.
5. Let HOLD win when alpha engines disagree.

Expected result:

Short-term trades and long-term investments stop being mixed into one generic signal.

### Phase 4: Portfolio Construction

Priority:

1. Add target weights.
2. Add portfolio exposure checks.
3. Add sector and correlation caps.
4. Add volatility-adjusted sizing.
5. Add risk-budget dashboard.

Expected result:

The system recommends a portfolio, not just isolated trades.

### Phase 5: Reflection and Calibration

Priority:

1. Replace approximate agent alignment with real per-agent scoring.
2. Add confidence buckets.
3. Add strategy/ticker/regime track records.
4. Feed compact calibration summaries into agents.
5. Adjust confidence and sizing from historical performance.

Expected result:

The system gets less overconfident and learns which setups actually work.

## 16. First Concrete Engineering Tasks

Do these first:

1. Fix side-correct return scoring for `SELL`.
2. Create immutable `recommendations` storage.
3. Add `recommendation_id` to `paper_trades`.
4. Add benchmark-relative scoring.
5. Store sector in the data snapshot or signal metadata.
6. Replace fake per-agent alignment with analyst-output-based scoring.
7. Add data-quality gates for absurd fundamentals and invalid tickers.

These changes create the foundation for everything else.

## 17. Detailed Implementation Proposal

Build v2 as an additive recommendation layer over the current pipeline, not as a rewrite. Keep the existing `signals` table as the dashboard-friendly latest-state read model, but make the new source of truth an immutable `recommendations` ledger.

### Phase 1: Ledger Foundation

Add new Pydantic models in `data/models.py`:

- `DataSnapshot`
- `AlphaOutput`
- `Recommendation`
- `RecommendationScore`
- `PortfolioAllocation`

Add new storage tables in `data/storage.py`:

```text
data_snapshots
- snapshot_id
- run_id
- ticker
- captured_at
- price_payload
- fundamentals_payload
- news_payload
- macro_payload
- feature_payload
- data_quality_flags
```

```text
recommendations
- recommendation_id
- run_id
- snapshot_id
- ticker
- created_at
- strategy_type
- horizon_days
- decision
- side
- confidence
- expected_return
- expected_drawdown
- benchmark_symbol
- sector_benchmark_symbol
- entry_zone
- stop_loss
- targets
- thesis
- invalidation
- alpha_outputs
- committee_outputs
- risk_verdict
- risk_reasons
- portfolio_target_weight
- model_versions
```

```text
recommendation_scores
- recommendation_id
- score_date
- side_return_pct
- benchmark_return_pct
- sector_return_pct
- excess_return_pct
- mae_pct
- mfe_pct
- stop_hit
- target_hit
- confidence_bucket
- score
```

Also add nullable `recommendation_id` to `paper_trades`. The existing `signals` table can continue to be written for compatibility, but every new signal should also create a new immutable `recommendation_id`.

### Phase 2: Snapshots And Features

In `agents/graph.py`, after `build_data_package(ticker)` succeeds, persist a data snapshot and attach `snapshot_id` to `PipelineState`.

Create:

```text
recommendation/features.py
recommendation/snapshots.py
recommendation/quality.py
```

Compute deterministic feature payloads before any LLM prompt:

- 1-day, 5-day, and 20-day returns
- volatility, ATR, and benchmark sensitivity
- RSI, MACD, and moving-average distances
- relative strength vs `SPY`, `QQQ`, and sector benchmark
- liquidity score
- data-quality flags such as absurd dividend yield, missing sector, stale news, invalid price rows, or unsupported ticker symbol

This gives the LLM a clean feature packet instead of asking it to reason over raw messy market-data fields.

### Phase 3: Alpha Engines

Add deterministic engines before the trader stage:

```text
recommendation/alpha_short.py
recommendation/alpha_long.py
recommendation/alpha_quant.py
recommendation/alpha_event.py
```

Each engine returns an `AlphaOutput` with:

- direction
- horizon
- confidence
- expected return
- expected drawdown
- evidence
- invalidation

Update `agents/trader.py` so the trader prompt receives alpha outputs and follows one hard rule: if alpha engines are weak or contradictory, default to `HOLD`; prose cannot rescue the trade.

### Phase 4: Recommendation Persistence

Replace `compose_signal` with two writes:

1. `save_recommendation(recommendation)` creates the immutable ledger row.
2. `save_signal(signal)` mirrors the latest state for dashboard compatibility.

Paper trading idempotency should move from:

```text
ticker:date:strategy
```

to:

```text
recommendation_id:strategy
```

This fixes same-day reruns without risking duplicate broker orders.

### Phase 5: Scoring v2

`agents/scorer.py` already has side-correct `SELL` math, so extend it rather than replacing it.

Add:

- benchmark-relative return
- sector-relative return
- max adverse excursion
- max favorable excursion
- stop/target sequence
- execution-aware entry/exit when a paper trade exists
- confidence bucket calibration
- scoring by `strategy_type` and `horizon_days`

Keep `signal_scores` for legacy UI if useful, but write the true v2 results to `recommendation_scores` by `recommendation_id`.

### Phase 6: Portfolio And Risk

Add:

```text
recommendation/portfolio.py
recommendation/risk.py
```

Keep `agents/risk_manager.py` as the v1 compatibility layer, but v2 risk should add:

- sector cap
- correlation cap
- total gross and net exposure
- max daily new risk
- event proximity rejection
- stale-data rejection
- liquidity rejection
- expanded verdicts: `APPROVED`, `APPROVED_SMALLER`, `REJECTED`, `PAPER_ONLY`, `HOLD_NO_TRADE`

Portfolio output should become `portfolio_target_weight`, while paper strategy notional remains execution-specific.

### First Engineering Slice

Best first slice:

1. Add `recommendations` and `data_snapshots` tables.
2. Add Pydantic models and CRUD helpers.
3. Persist `snapshot_id` during `fetch_data`.
4. Persist immutable `Recommendation` during `compose_signal`.
5. Add `recommendation_id` to `paper_trades`, while keeping the old `(ticker, date)` path as a fallback.
6. Add tests proving same-day reruns create two recommendations and do not overwrite ledger history.

That creates the core v2 spine: reproducible input, immutable decision, execution linkage, and future-proof scoring.

## 18. Implementation Timeline Estimate

Estimated build time for the full v2 proposal:

| Scope | Estimate | What It Includes |
| --- | ---: | --- |
| Fast valuable slice | 3-5 focused days | Immutable `recommendations`, `data_snapshots`, `recommendation_id`, and compatibility writes to the existing `signals` table. |
| Usable v2 foundation | 1-2 weeks | Ledger foundation, snapshot persistence, paper-trade linkage, basic feature payloads, scorer upgrades, and same-day rerun history. |
| Strong internal version | 3-5 weeks | Multi-horizon alpha engines, improved deterministic risk manager, benchmark-relative and sector-relative scoring, confidence buckets, data-quality gates, and dashboard updates. |
| Polished serious system | 6-10 weeks | Portfolio construction, exposure and correlation controls, calibration loops, backtest harness, reflection summaries, better universe validation, and richer audit views. |
| Production/live-trading grade | 10-14+ weeks | Hardening, migrations, reconciliation edge cases, execution safety, monitoring, recovery workflows, and broader test coverage. |

The best first implementation slice is the 3-5 day foundation: add the immutable recommendation ledger and data snapshots while continuing to write the current `signals` table for dashboard compatibility. That creates the spine for reproducible scoring, paper-trade linkage, and future alpha-engine work without disrupting the existing app.

## 19. North Star

The best version of StockInvest is not a chatbot that gives stock picks.

It is a research and trading machine that:

- finds candidates
- validates data
- computes deterministic edge
- challenges the thesis
- sizes the position
- refuses bad trades
- records every recommendation immutably
- scores the outcome honestly
- calibrates future confidence

That is how StockInvest gets closer to excellent recommendations: not by claiming certainty, but by becoming disciplined, skeptical, measured, and self-correcting.

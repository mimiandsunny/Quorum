# TODOS

Deferred work surfaced during plan reviews. Each entry includes context so a future session can pick it up cold.

## Wave 2 → Wave 2.5 / Wave 3 deferred items

### TD-1: D5 — Gamma-hedging assistant (dealer GEX heat-map, magnet levels)

- **What:** Compute dealer net gamma exposure from OPRA OI; visualize as price-magnet heat-map.
- **Why:** Sophisticated traders use dealer GEX to anticipate intraday pinning and squeeze risk.
- **Pros:** Differentiating cockpit feature; pairs naturally with thesis-builder.
- **Cons:** Hard to do well. Naive sign-flipped OI sums are wrong; need a defensible dealer-positioning model. Easy to mislabel position gamma as dealer GEX (the K1 bug we already fixed).
- **Context:** D5 was EXPANSION + LABELED EXPERIMENTAL in v1.4 plan. Once D1/D2/D3/D4 ship, revisit with a real model (e.g., SqueezeMetrics-style).
- **Effort:** L (5-8h CC) — but mostly research, not code.
- **Priority:** P3
- **Depends on:** D1 (IBKR OPRA OI data), 60+ days of `option_chain_snapshots` history.

### TD-2: D6 — Paid options-flow scanner (Unusual Whales / Cheddar Flow)

- **What:** Subscribe to a paid flow feed for real "unusual" detection (block trades, sweeps, premium prints with aggressor side).
- **Why:** Today's `options/flow.py` scanner uses volume/OI ratios — which is what retail tools call "unusual" but pros call "noisy proxy." Real flow data shows institutional positioning.
- **Pros:** Materially better signal. Closes the gap between the platonic-ideal "unusual-flow tape" and what we ship.
- **Cons:** $50-100/mo recurring. Vendor lock-in. Doesn't compose with rest of cockpit until you re-build the flow scanner against the new data shape.
- **Context:** Deferred in v1.4 plan as "wait until D1-D4 prove daily use; revisit after 30 days of dogfooding."
- **Effort:** S (~2h CC infra) + recurring cost.
- **Priority:** P3
- **Depends on:** 30 days of Wave 2 dogfooding to prove daily use.

### TD-3: Wave 2.5 — Options paper-trading via IBKR

- **What:** Wire D4 thesis-builder output into IBKR paper-trade order routing (multi-leg orders, OCO bracket, position lifecycle).
- **Why:** Closes the "thesis → trade execution" loop that the platonic ideal points at.
- **Pros:** First time the cockpit becomes operational, not just observational.
- **Cons:** Real money infrastructure (even paper) introduces order-state-machine complexity, fill simulation, slippage modeling.
- **Context:** Open Q2 in v1.4 plan was "When does options paper-trading enter scope? Wave 2.5 or wave 3?" Decision: Wave 2.5.
- **Effort:** L (8-12h CC) — own plan.
- **Priority:** P2
- **Depends on:** Wave 2 dogfooded for 4+ weeks.

### TD-4: Wave 3 — IBKR live (real-money) account

- **What:** Open and fund a real IBKR Pro account; wire live order routing through the same pipeline as Wave 2.5 paper.
- **Why:** Real money is the only way to validate the trust-loop end-to-end.
- **Pros:** End of the simulation chain; actual P&L attribution.
- **Cons:** Real money risk. Compliance posture, audit logging, kill-switch infrastructure.
- **Context:** Open Q2 follow-up. Wave 3 minimum.
- **Effort:** XL (own plan).
- **Priority:** P3
- **Depends on:** Wave 2.5 paper-trading produces 60+ days of clean fills with predictable slippage.

### TD-5: Strategy-registry extraction

- **What:** When the 4th `OptionThesisStrategy` is added, extract the `Protocol` from the three day-1 functions (`bullish_debit_spread`, `bearish_protective_put`, `neutral_iron_condor`).
- **Why:** Day 1 we picked "three functions, no protocol" because the seams aren't real. The 4th strategy is the natural extraction point.
- **Pros:** Avoid premature abstraction now; commit to the right shape later.
- **Cons:** Discipline-dependent — if 5+ strategies arrive without extraction, you'll have an `if/elif` ladder in `build_option_thesis()`.
- **Context:** Cross-model tension 1 in CEO review (rev 2). Outside voice argued protocol-first was premature; we agreed.
- **Effort:** S (~1h CC).
- **Priority:** P3
- **Depends on:** 4th `compose_*_thesis()` function being needed.

### TD-6: D2 collars (multi-leg protective)

- **What:** Extend D2 risk overlay from protective puts (single-leg) to collars (long put + short call).
- **Why:** Plan's platonic ideal mentions collars as a primary D2 surface. Wave 2 ships protective puts only because collars need leg primitives that D4 introduces.
- **Pros:** Lower-cost protection (short call funds the put).
- **Cons:** Needs leg-primitive abstraction shared with D4.
- **Context:** OV5 in CEO review (rev 2).
- **Effort:** S (~1-2h CC) once D4 lands.
- **Priority:** P2
- **Depends on:** D4 leg primitives stable.

### TD-7: LAN/internet exposure with auth

- **What:** Add basic auth (env-configured single user, OR OAuth) to the FastAPI app; allow non-localhost binds.
- **Why:** Today the app binds 127.0.0.1 with no auth — defensible for solo localhost. If you ever want to access from phone or remote machine, this becomes the gating change.
- **Pros:** Mobile dogfooding, work-from-coffee-shop access.
- **Cons:** Auth surface = real security work. Position data + IBKR creds in scope.
- **Context:** Wave 2 explicitly does NOT change network bind (decision 15 in rev 2).
- **Effort:** M (~3-4h CC).
- **Priority:** P3
- **Depends on:** A real reason to need it. Solo localhost is fine until it's not.

### TD-8: 3 surgical fixes from prior office hours (still wave 1.5 territory)

- **What:** LLM call telemetry table, side-correct SELL scoring fix, sector field on snapshot/signal metadata.
- **Why:** Called out in v1.4 plan as "3 surgical fixes worth pulling out now" but technically wave 1.5 polish.
- **Pros:** Closes wave 1.5 cleanly; helps with retro analysis.
- **Cons:** Wave 1.5 contamination is now ACCEPTED (rev 2 decision), so the urgency is lower.
- **Context:** v1.4 plan, "Pre-Wave-2 Cleanup" section.
- **Effort:** S (~2-3h CC for all three).
- **Priority:** P2 — do during Wave 2 calendar slack if cleanup PR finishes early.

### TD-9: Wave 1.5 retro asterisk — quantify contamination

- **What:** When wave 1.5 retro runs, query `wave_2_started_at` annotation and compute "how much portfolio.py read traffic happened during the eval window?" to bound how confounded the data is.
- **Why:** "Asterisk in retro" is honest only if we measure the asterisk. Without instrumentation, it's just a vibe.
- **Pros:** Defensible Wave 1.5 conclusions.
- **Cons:** Tiny — just a query.
- **Context:** Cross-model tension 2 in CEO review (rev 2).
- **Effort:** S (~30min CC) at retro time.
- **Priority:** P2.
- **Depends on:** Wave 1.5 evaluation closing + Wave 2 deploy annotation having been written.

### TD-10: Re-run /plan-ceo-review at Wave 2 close to evaluate Wave 3 scope

- **What:** Once Wave 2 has 30 days of dogfooding, re-run /plan-ceo-review on Wave 3 (live trading) scope.
- **Why:** Wave 3 is real-money territory. Decisions made today on hypotheticals will be wrong.
- **Pros:** Re-evaluate with real usage data.
- **Cons:** None.
- **Context:** Standard cadence.
- **Effort:** N/A (review skill).
- **Priority:** P2.
- **Depends on:** Wave 2 in production for 30 days.

## Eng Review additions (2026-05-01)

These were surfaced during /plan-eng-review on rev 2 of the v1.4 plan and are deferred to keep Wave 2 scope clean.

### TD-11: Revisit `OptionChainFetcher` Protocol unification when 3rd fetcher arrives

- **What:** Eng review C1 picked two separate Protocols (`SyncFetcher` + `AsyncFetcher`) over a unified async Protocol. The dispatch in `options/refresh_job.py` is fine at 2 fetchers (yfinance sync + IBKR async). When a third fetcher (Polygon, Tradier, Alpaca, etc.) arrives, the dispatch surface needs a refactor — likely to the unified async shape with `asyncio.to_thread` wrapping for any sync impl.
- **Why:** Decision was made at 2 fetchers where dual-Protocol is genuinely simpler; at 3+ the case flips. Capturing the trigger so the rationale isn't re-derived from scratch.
- **Pros:** Clear reversal trigger, preserves rev 2 reasoning trail.
- **Cons:** Dormant entry if Wave 2.5 doesn't add a third fetcher.
- **Context:** Eng review C1, 2026-05-01. User explicitly preferred two-Protocols over recommended async-unification at the 2-fetcher state.
- **Effort:** S (~2-3h CC) when triggered.
- **Priority:** P3.
- **Depends on:** A 3rd fetcher being needed (Polygon backup, Tradier alt feed, etc.).

### TD-12: Manual "refresh thesis" cache-bust button on D4 cards

- **What:** D4 thesis cache (P1) invalidates only when chain refresh runs. During fast-moving sessions (9:30am open, mid-day catalyst), a user may want to force a fresh thesis without waiting for the next chain refresh cycle. Adds a button on each thesis card + a `?force=true` query param on the thesis endpoint that bypasses cache for that one call.
- **Why:** P1 traded staleness for speed (~50 LLM calls/day vs ~200). Manual escape hatch is the natural pressure-relief valve when staleness becomes painful.
- **Pros:** Cheap (~30min CC) when needed. Closes the UX gap the cache decision created.
- **Cons:** Premature without dogfood evidence — chain-refresh-driven invalidation may already be enough.
- **Context:** Eng review P1, 2026-05-01. Deferred until `option_thesis_feedback` data shows thumbs-down clusters around stale-thesis events, OR until user reports the friction.
- **Effort:** S (~30min CC) when triggered.
- **Priority:** P3.
- **Depends on:** Wave 2 dogfood evidence that chain-refresh-only invalidation is too coarse.

### TD-14: Pick a real typeface during /design-consultation, ship in Wave 2.5 — RESOLVED 2026-05-01

- **Decision:** IBM Plex Sans (body, UI, headings) + IBM Plex Mono (data, prices, contracts, code). Three weights: 400 / 600 / 700. Loaded from Bunny Fonts (privacy-respecting). See [DESIGN.md](../../mnt/data/my-app/stockinvest/DESIGN.md#typography) for the full spec.
- **Implementation timing:** Ship in Wave 2.5 (NOT Wave 2) to avoid contaminating the wave-1.5 evaluation window with a mid-Wave-2 visual character shift. CSS swap in `templates/dashboard.html` line 53 + Bunny Fonts `<link>` in `<head>`. ~15min CC when triggered.
- **Why this typeface:** Single-family system (Plex Sans + Plex Mono) signals "serious computing" without the Inter / Geist / Space Grotesk convergence trap. IBM Plex Mono's tabular-nums are designed for terminal + financial data, perfect for the dashboard's price columns. Anti-AI-slop choice that reflects the product's "hedge fund investment committee on your laptop" identity.
- **Original context:** Plan-design-review Pass 4 (2026-05-01). Resolved by /design-consultation 2026-05-01.
- **Status:** Decision made + DESIGN.md written. Implementation deferred to Wave 2.5 calendar slack.

### TD-13: IBKR HistoricalData backfill for newly-added universe tickers

- **What:** A5 gates the IV-rank screener on `>=60 days` of history. New tickers added mid-Wave-2 wait ~8 weeks before appearing in the screener. An off-hours weekend job using IBKR HistoricalData API would batch-backfill 60-252 days of chains for the new ticker, putting it in the screener Monday morning.
- **Why:** Cold-start gating (A5) is correct for statistical hygiene but creates real friction when adding tickers mid-wave (earnings catalysts, IPOs, sector rotations).
- **Pros:** New tickers usable in screener within days of being added, not months.
- **Cons:** IBKR HistoricalData calls for option chains burn pacing budget aggressively (~thousands of API calls per ticker per 60 days). Pacing capacity is unknown until OPRA paper-account eligibility is confirmed in P1 of D1.
- **Context:** Eng review TODO-3, 2026-05-01. Deferred because (a) universe is largely set day 1, (b) IBKR pacing budget for HistoricalData isn't measured yet.
- **Effort:** M (~3-4h CC) when triggered, contingent on IBKR pacing capacity.
- **Priority:** P3.
- **Depends on:** OPRA paper-account market-data eligibility verified (P1 of D1) + measured IBKR HistoricalData pacing budget + a real desire to add a mid-wave ticker.

from datetime import date, datetime
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, Field


# ─── Enums ───────────────────────────────────────────────

class Trend(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


class Decision(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class RiskVerdict(str, Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class MarketRegime(str, Enum):
    RISK_ON = "risk-on"
    RISK_OFF = "risk-off"
    ROTATION = "rotation"
    NEUTRAL = "neutral"


class PaperTradeStatus(str, Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    FILLED = "filled"
    UNFILLED_EOD = "unfilled_eod"
    CANCELLED = "cancelled"
    FAILED = "failed"
    EXECUTION_SKIPPED = "execution_skipped"


class JudgeWinner(str, Enum):
    BULL = "bull"
    BEAR = "bear"
    TIE = "tie"


class ReplayVerdict(str, Enum):
    PASS = "PASS"
    BLOCK = "BLOCK"
    INSUFFICIENT = "INSUFFICIENT"


class RecommendationSide(str, Enum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


class RecommendationStrategyType(str, Enum):
    SHORT_TERM = "short_term"
    LONG_TERM = "long_term"
    QUANT = "quant"
    EVENT = "event"


# ─── Data Pipeline Models (deterministic, pre-computed) ──

class OHLCVBar(BaseModel):
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: int


class TechnicalIndicators(BaseModel):
    rsi_14: float | None = None
    macd: float | None = None
    macd_signal: float | None = None
    macd_histogram: float | None = None
    ma_50: float | None = None
    ma_200: float | None = None
    current_price: float
    support_levels: list[float] = Field(default_factory=list)
    resistance_levels: list[float] = Field(default_factory=list)


class FundamentalsData(BaseModel):
    pe_ratio: float | None = None
    forward_pe: float | None = None
    eps: float | None = None
    revenue_growth: float | None = None
    debt_to_equity: float | None = None
    market_cap: float | None = None
    sector: str | None = None
    industry: str | None = None
    earnings_date: date | None = None
    dividend_yield: float | None = None


class NewsItem(BaseModel):
    headline: str
    source: str = ""
    published: datetime | None = None
    summary: str = ""
    sentiment_score: float | None = None  # pre-computed if available


class DigestDistillation(BaseModel):
    """Structured one-shot summary of the daily macro digest.

    Distilled once at the start of `run_all()` via a single LLM call, then
    passed (in this compact form) into every per-ticker prompt — instead
    of the raw 5-7K-token digest going through every analyst/researcher/
    trader call. Saves ~30 min on a 28-ticker run.
    """
    tactical_view: str = Field(description="1-2 sentence current macro stance")
    key_themes: list[str] = Field(
        default_factory=list,
        description="3-5 dominant themes from the digest, each one short line with confidence",
    )
    macro_risks: list[str] = Field(
        default_factory=list,
        description="3-5 named near-term risks to watch",
    )
    bottom_line: str = Field(description="1-2 sentence punchline / synthesis")


class TickerDataPackage(BaseModel):
    """Frozen data package passed to analysts. LLM interprets, never computes."""
    ticker: str
    fetch_timestamp: datetime
    price_history: list[OHLCVBar]
    benchmark_price_history: dict[str, list[OHLCVBar]] = Field(default_factory=dict)
    technicals: TechnicalIndicators
    fundamentals: FundamentalsData
    news: list[NewsItem] = Field(default_factory=list)
    stale_sources: list[str] = Field(default_factory=list)
    # `external_digest` is retained for snapshot/audit storage; the LLM
    # prompts now use `digest_summary` (distilled once per run) instead.
    external_digest: str | None = None
    digest_summary: DigestDistillation | None = None


class UniverseCandidate(BaseModel):
    """Ticker selected for analysis before expensive data/agent stages run."""
    symbol: str
    asset_type: str = "equity"
    region: str = "US"
    sector: str | None = None
    industry: str | None = None
    source: list[str] = Field(default_factory=list)
    liquidity_score: float | None = None
    valid_data_symbol: bool
    reason_added: str = ""
    rejected_reason: str | None = None


# ─── Recommendation v2 Ledger Models ────────────────────

class DataSnapshot(BaseModel):
    """Immutable input bundle captured before agents reason about a ticker."""
    snapshot_id: str = Field(default_factory=lambda: uuid4().hex)
    run_id: str | None = None
    ticker: str
    captured_at: datetime = Field(default_factory=datetime.now)
    source_versions: dict = Field(default_factory=dict)
    price_payload: dict = Field(default_factory=dict)
    fundamentals_payload: dict = Field(default_factory=dict)
    news_payload: list[dict] = Field(default_factory=list)
    macro_payload: dict = Field(default_factory=dict)
    feature_payload: dict = Field(default_factory=dict)
    data_quality_flags: list[str] = Field(default_factory=list)


class AlphaOutput(BaseModel):
    """Deterministic alpha-engine output. Populated by future v2 engines."""
    strategy_type: RecommendationStrategyType
    direction: RecommendationSide
    horizon_days: int = Field(ge=1)
    expected_return: float | None = None
    expected_drawdown: float | None = None
    expected_volatility: float | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)
    invalidation: str = ""


class Recommendation(BaseModel):
    """Immutable recommendation ledger row.

    Existing `signals` remain the latest-state read model; this model is the
    auditable v2 source of truth.
    """
    recommendation_id: str = Field(default_factory=lambda: uuid4().hex)
    run_id: str | None = None
    snapshot_id: str | None = None
    ticker: str
    created_at: datetime = Field(default_factory=datetime.now)
    strategy_type: RecommendationStrategyType = RecommendationStrategyType.SHORT_TERM
    horizon_days: int = Field(ge=1)
    decision: Decision
    side: RecommendationSide
    confidence: float = Field(ge=0.0, le=1.0)
    expected_return: float | None = None
    expected_drawdown: float | None = None
    benchmark_symbol: str = "SPY"
    sector_benchmark_symbol: str | None = None
    entry_zone: list[float] = Field(default_factory=list)
    stop_loss: float
    targets: list[float] = Field(default_factory=list)
    thesis: str
    invalidation: str
    alpha_outputs: list[AlphaOutput] = Field(default_factory=list)
    committee_outputs: dict = Field(default_factory=dict)
    risk_verdict: RiskVerdict
    risk_reasons: list[str] = Field(default_factory=list)
    portfolio_target_weight: float = 0.0
    model_versions: dict = Field(default_factory=dict)


class RecommendationScore(BaseModel):
    """Outcome scoring for one immutable recommendation."""
    recommendation_id: str
    score_date: date
    side_return_pct: float
    benchmark_return_pct: float | None = None
    sector_return_pct: float | None = None
    excess_return_pct: float | None = None
    mae_pct: float | None = None
    mfe_pct: float | None = None
    stop_hit: bool
    target_hit: list[bool] = Field(default_factory=list)
    confidence_bucket: str | None = None
    score: float
    execution_status: str | None = None
    execution_return_pct: float | None = None
    execution_slippage_pct: float | None = None


class PortfolioAllocation(BaseModel):
    """Portfolio-construction output for a recommendation."""
    recommendation_id: str
    ticker: str
    target_weight: float
    max_loss_budget: float | None = None
    risk_budget_reason: str = ""


class PortfolioExposure(BaseModel):
    """Existing portfolio exposure used to cap a new allocation."""
    ticker: str
    side: RecommendationSide
    target_weight: float = Field(ge=0.0, le=1.0)
    sector: str | None = None


# ─── Stage 1: Analyst Outputs (LOCAL LLM) ────────────────

class TechnicalAnalysis(BaseModel):
    """Output from the technical analyst agent."""
    ticker: str
    trend: Trend
    key_levels: dict[str, list[float]] = Field(
        description="{'support': [...], 'resistance': [...]}",
    )
    pattern: str = Field(description="Chart pattern identified, if any")
    momentum: str = Field(description="Momentum assessment")
    summary: str


class FundamentalsAnalysis(BaseModel):
    """Output from the fundamentals analyst agent."""
    ticker: str
    valuation_assessment: str
    growth_assessment: str
    financial_health: str
    sector_comparison: str
    summary: str


class SourceScore(BaseModel):
    """Sentiment score for a single news source/headline."""
    source: str
    score: float = Field(ge=-1.0, le=1.0)


class SentimentAnalysis(BaseModel):
    """Output from the sentiment analyst agent."""
    ticker: str
    overall_score: float = Field(ge=-1.0, le=1.0)
    source_scores: list[SourceScore] = Field(default_factory=list)
    volume_vs_avg: float | None = None
    summary: str


class NewsAnalysis(BaseModel):
    """Output from the news analyst agent."""
    ticker: str
    events: list[dict[str, str]] = Field(
        default_factory=list,
        description="[{'headline': ..., 'impact': high/medium/low, 'relevance': ...}]",
    )
    macro_context: str
    summary: str


class AnalystReports(BaseModel):
    """Bundle of all analyst outputs for a ticker."""
    ticker: str
    technical: TechnicalAnalysis
    fundamentals: FundamentalsAnalysis
    sentiment: SentimentAnalysis
    news: NewsAnalysis


# ─── Stage 2: Research Outputs (CLOUD LLM) ───────────────

class EvidenceItem(BaseModel):
    claim: str
    data_citation: str
    weight: float = Field(ge=0.0, le=1.0)


class ResearchCase(BaseModel):
    """Output from a bull or bear researcher."""
    ticker: str
    stance: str  # "bull" or "bear"
    thesis: str
    evidence: list[EvidenceItem]
    price_target: float
    catalysts: list[str]
    risks: list[str]


class ResearchRound(BaseModel):
    """One round of a multi-round bull/bear debate."""
    round: int = Field(ge=1)
    bull: ResearchCase
    bear: ResearchCase


class JudgeVerdict(BaseModel):
    """Output from the judge agent — scores a multi-round debate."""
    winner: JudgeWinner
    score: float = Field(ge=0.0, le=10.0, description="0-10, bull-favored when > 5")
    swing_argument: str = Field(description="The point that decided the verdict")
    conceded_points: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


class DebateTranscript(BaseModel):
    """Combined bull + bear research for a ticker.

    bull_case/bear_case are the canonical (final-round) cases for backward
    compatibility with v1.1 single-round storage. rounds and judge_verdict
    are populated for v1.2+ multi-round debates.
    """
    ticker: str
    bull_case: ResearchCase
    bear_case: ResearchCase
    rounds: list[ResearchRound] | None = None
    judge_verdict: JudgeVerdict | None = None


# ─── Stage 3: Trader Output (CLOUD LLM) ──────────────────

class TraderDecision(BaseModel):
    """Output from the trader agent."""
    ticker: str
    date: date
    decision: Decision
    confidence: float = Field(ge=0.0, le=1.0)
    entry_zone: list[float] = Field(min_length=2, max_length=2)
    stop_loss: float
    targets: list[float] = Field(min_length=1, max_length=3)
    invalidation: str
    holding_period_days: int = Field(ge=1)
    thesis: str


# ─── Stage 4: Risk Manager Output (deterministic Python) ─

class RiskAssessment(BaseModel):
    """Output from the deterministic risk manager."""
    ticker: str
    verdict: RiskVerdict
    position_size_pct: float = Field(ge=0.0, le=1.0)
    rejection_reasons: list[str] = Field(default_factory=list)
    adjustments: list[str] = Field(default_factory=list)
    reward_risk_ratio: float


# ─── Final Signal (stored + displayed) ───────────────────

class FinalSignal(BaseModel):
    """The complete output for one ticker on one day."""
    recommendation_id: str | None = None
    ticker: str
    date: date
    decision: Decision
    confidence: float
    entry_zone: list[float]
    stop_loss: float
    targets: list[float]
    invalidation: str
    holding_period_days: int
    thesis: str
    bull_case: str
    bear_case: str
    risk_verdict: RiskVerdict
    risk_reasons: list[str]
    position_size_pct: float
    reward_risk_ratio: float
    # Wave 2 prep: snapshot of yfinance sector/industry at signal time. Wave 1
    # signals will read NULL — backfilled to NULL via guard migration.
    sector: str | None = None
    industry: str | None = None


# ─── Run Metadata ────────────────────────────────────────

class RunMetadata(BaseModel):
    """Metadata for a single pipeline run."""
    run_id: str
    start_time: datetime
    end_time: datetime | None = None
    tickers_attempted: list[str]
    tickers_completed: list[str] = Field(default_factory=list)
    tickers_failed: list[str] = Field(default_factory=list)
    errors: list[dict[str, str]] = Field(default_factory=list)
    concentration_warnings: list[str] = Field(default_factory=list)
    total_cloud_calls: int = 0


# ─── Regime Classification ─────────────────────────────

class RegimeClassification(BaseModel):
    """Output from the macro regime classifier."""
    regime: MarketRegime
    confidence: float = Field(ge=0.0, le=1.0)
    key_factors: list[str] = Field(default_factory=list)
    summary: str


# ─── Digest Ticker Extraction ──────────────────────────

class DigestTicker(BaseModel):
    """A single ticker recommendation extracted from the macro digest."""
    symbol: str = Field(description="Trading symbol as it appears in the digest")
    exchange: str = Field(description="Exchange code: NYSE, NASDAQ, TSX, SSE, HKEX, etc.")


class DigestTickers(BaseModel):
    """Tickers extracted from the 'Stock Recommendations' section of a digest."""
    tickers: list[DigestTicker] = Field(default_factory=list)


# ─── Paper Trading (v1.2) ──────────────────────────────

class PaperTrade(BaseModel):
    """One paper-trade execution attempt for a (ticker, signal_date, strategy) signal.

    Wave 1.5: idempotency_key now includes strategy suffix so the same logical
    signal produces 3 rows (one per strategy) without UNIQUE constraint conflicts.
    """
    id: int | None = None
    recommendation_id: str | None = None
    ticker: str
    signal_date: date
    strategy: str = "balanced"  # 'aggressive' | 'balanced' | 'conservative'
    idempotency_key: str        # f"{ticker}:{date}:{strategy}"
    decision: Decision
    side: str  # 'buy' | 'sell_short'
    entry_limit: float
    stop_loss: float
    take_profit: float
    notional_pct: float = 0.015
    alpaca_order_id: str | None = None
    status: PaperTradeStatus
    status_reason: str | None = None
    submitted_at: datetime | None = None
    created_at: datetime | None = None


class PaperFill(BaseModel):
    """A single fill event from Alpaca for a paper_trade."""
    paper_trade_id: int
    strategy: str = "balanced"  # denormalized for fast per-strategy queries
    alpaca_fill_id: str
    side: str
    qty: float
    price: float
    filled_at: datetime


class PaperPosition(BaseModel):
    """Open or closed position derived from paper_fills."""
    paper_trade_id: int
    strategy: str = "balanced"  # denormalized for fast per-strategy queries
    qty: float
    avg_entry: float
    current_price: float | None = None
    unrealized_pnl: float | None = None
    realized_pnl: float | None = None
    closed_at: datetime | None = None
    close_reason: str | None = None  # 'stop_hit' | 'target_hit' | 'manual' | 'eod_force_close'


# ─── Wave 1.5: Per-strategy persistence ────────────────

class EquitySnapshot(BaseModel):
    """One nightly snapshot of a strategy's Alpaca account equity.

    Written by paper_reconciler.snapshot_equity() at 4:10 PM ET. Used by the
    dashboard summary panel sparkline AND by the drawdown circuit breaker
    (rolling 30-day peak).
    """
    id: int | None = None
    strategy: str
    snapshot_date: date
    account_equity: float
    cash: float
    positions_value: float
    daily_pnl: float | None = None  # null on first snapshot for a strategy
    created_at: datetime | None = None


class LLMCallTelemetry(BaseModel):
    """One row per LLM call attempt. Fail-open observability: if writing this
    row raises, the LLM call still succeeds. Tagged with ticker/stage/run_id
    when the caller has set llm_call_context, otherwise NULL.
    """
    id: int | None = None
    run_id: str | None = None
    ticker: str | None = None
    stage: str | None = None
    provider: str                # 'ollama' | 'llama_cpp' | 'openai'
    model: str
    attempt: int                 # 0-indexed retry attempt within one call
    prompt_len: int
    output_len: int
    elapsed_seconds: float
    done_reason: str | None = None
    eval_count: int | None = None
    parse_ok: bool
    fallback_used: bool = False  # reserved for analyst-level fallback flag
    error: str | None = None
    created_at: datetime | None = None


class StrategyPauseState(BaseModel):
    """Drawdown circuit breaker state for a strategy. Auto-paused when
    rolling 30-day drawdown crosses threshold; manually unpaused via
    STRATEGY_UNPAUSE env var on app restart, or DELETE FROM the table.
    """
    strategy: str
    paused_at: datetime
    paused_reason: str           # 'drawdown_threshold' | 'manual'
    paused_drawdown: float | None = None  # snapshot of the drawdown at trip time
    unpause_after: datetime | None = None  # reserved for wave 1.6; always NULL in 1.5


# ─── Historical Replay (v1.2) ──────────────────────────

class ReplayReport(BaseModel):
    """Output from the historical replay kill-gate."""
    verdict: ReplayVerdict
    signals_evaluated: int
    hit_rate: float | None = None
    mean_return_pct: float | None = None
    by_decision: dict[str, int] = Field(default_factory=dict)
    summary: str


# ─── Wave 2: Options cockpit infrastructure ────────────

class OptionRefreshJobStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class OptionRefreshJobSource(str, Enum):
    IBKR = "ibkr"
    YFINANCE = "yfinance"


class OptionRefreshJobFailure(BaseModel):
    ticker: str
    error_class: str
    message: str


class OptionRefreshJob(BaseModel):
    """A3: single-concurrency async chain-refresh job, persisted in Postgres.

    Serialization happens via a partial unique index on status='running' —
    INSERT ... ON CONFLICT DO NOTHING returns no row when one is already
    running, giving the API its 409 path.
    """
    job_id: str = Field(default_factory=lambda: uuid4().hex)
    status: OptionRefreshJobStatus
    source: OptionRefreshJobSource
    started_at: datetime | None = None
    completed_at: datetime | None = None
    total: int | None = None
    completed: int = 0
    failures: list[OptionRefreshJobFailure] = Field(default_factory=list)


class OptionIVLabel(str, Enum):
    CHEAP = "cheap"
    FAIR = "fair"
    RICH = "rich"
    INSUFFICIENT = "insufficient"   # A5 cold-start: <60 days of history


class OptionIVHistory(BaseModel):
    """A6 + P2: per-ticker daily IV snapshot. iv_rank_30d is computed against
    the prior 252 days of `atm_iv_30d` at insert time so the read path is a
    primary-key lookup, not a percentile scan.
    """
    ticker: str
    captured_at: datetime
    underlying_price: float
    atm_iv_30d: float | None = None
    atm_iv_60d: float | None = None
    atm_iv_90d: float | None = None
    term_structure_30_60: float | None = None
    term_structure_60_90: float | None = None
    iv_rank_30d: float | None = Field(default=None, ge=0.0, le=1.0)
    iv_label: OptionIVLabel | None = None


class OptionGreeksSource(str, Enum):
    PROVIDER = "provider"            # Greeks came from the chain provider (e.g. IBKR modelGreeks)
    PROVIDER_NAN = "provider_nan"    # Provider returned but key fields are NaN; degraded
    LOCAL_BS = "local_bs"            # Computed locally via Black-Scholes from OPRA mid
    NONE = "none"                    # No Greeks available; cost-only row


class OptionProtectiveCost(BaseModel):
    """D2: per-position protective-put cost snapshot. position_id is a logical
    reference to paper_positions.id (no FK — wave-1.5 freeze).
    """
    position_id: int
    contract_symbol: str
    cost_per_share: float
    cost_pct_of_position: float | None = None
    delta: float | None = None
    greeks_source: OptionGreeksSource | None = None
    computed_at: datetime | None = None


class DataProviderEventType(str, Enum):
    FETCHER_FALLBACK = "fetcher_fallback"        # decision 11: IBKR -> yfinance
    GATEWAY_STATE_CHANGE = "gateway_state_change"  # A4: TWS heartbeat
    IV_SUMMARY_SKIPPED = "iv_summary_skipped"    # A6: per-ticker partial-success


class DataProviderEvent(BaseModel):
    """Audit trail for data-quality state transitions. Read by /retro and
    operator alerts. Every fallback/gateway-flip/IV-skip writes one row.
    """
    id: int | None = None
    event_type: DataProviderEventType | str   # str escape for future event types
    ticker: str | None = None
    from_provider: str | None = None
    to_provider: str | None = None
    reason: str | None = None
    payload: dict = Field(default_factory=dict)
    occurred_at: datetime | None = None


class OptionThesisStatus(str, Enum):
    """A7 3-way thesis-build outcome (per plan rev 4 eng review).

    - SUCCESS: structured spread + LLM narrative both shipped.
    - STRUCTURED_ONLY: spread shipped but LLM call failed (decision 10) —
      user still gets a thesis card, narrative shown as "(rationale
      unavailable, retry pending)".
    - FAIL: no spread possible (chain too thin, no eligible strikes).

    Distinct labels because operator routing differs: STRUCTURED_ONLY pages
    LLM oncall, FAIL pages chain-ingest oncall.
    """
    SUCCESS = "success"
    STRUCTURED_ONLY = "structured_only"
    FAIL = "fail"


class OptionThesisLLMFailureReason(str, Enum):
    """Why the LLM call failed inside a STRUCTURED_ONLY attempt.

    Per plan A7 the metric is labeled `{strategy, reason}` — the 5 reasons
    here cover the failure modes from decision 10. Cross-tabbed against the
    `option_thesis_feedback` table to answer "is the LLM rationale adding
    value?"
    """
    TIMEOUT = "timeout"
    MALFORMED = "malformed"
    REFUSAL = "refusal"
    EMPTY = "empty"
    SERVER_ERROR = "server_error"


class OptionThesisAttempt(BaseModel):
    """One thesis-build attempt. Fail-open: writing this row MUST NEVER
    break the thesis pipeline. Cardinality is one per (ticker, strategy,
    attempt) — a retry produces a second row, not an update.

    `strategy` matches the day-1 D4 trio: 'bullish_debit_spread' /
    'bearish_protective_put' / 'neutral_iron_condor'. Open string so a
    fourth strategy doesn't require a schema change.
    """
    id: int | None = None
    ticker: str
    strategy: str
    status: OptionThesisStatus
    llm_failure_reason: OptionThesisLLMFailureReason | None = None
    elapsed_seconds: float | None = None
    attempted_at: datetime | None = None

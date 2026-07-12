import json
import logging
from datetime import date, datetime

import psycopg
from psycopg.rows import dict_row

from config import settings
from data.models import (
    AnalystReports,
    DataProviderEvent,
    DataProviderEventType,
    DataSnapshot,
    DebateTranscript,
    Decision,
    EquitySnapshot,
    FinalSignal,
    JudgeVerdict,
    LLMCallTelemetry,
    OptionIVHistory,
    OptionIVLabel,
    OptionProtectiveCost,
    OptionRefreshJob,
    OptionRefreshJobFailure,
    OptionRefreshJobSource,
    OptionRefreshJobStatus,
    OptionThesisAttempt,
    OptionThesisLLMFailureReason,
    OptionThesisStatus,
    PaperFill,
    PaperPosition,
    PaperTrade,
    PaperTradeStatus,
    Recommendation,
    RecommendationScore,
    ResearchCase,
    ResearchRound,
    RiskVerdict,
    RunMetadata,
    TraderDecision,
)

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS signals (
    ticker TEXT NOT NULL,
    date DATE NOT NULL,
    recommendation_id TEXT,
    decision TEXT NOT NULL,
    confidence REAL NOT NULL,
    entry_zone JSONB NOT NULL,
    stop_loss REAL NOT NULL,
    targets JSONB NOT NULL,
    invalidation TEXT NOT NULL,
    holding_period_days INTEGER NOT NULL,
    thesis TEXT NOT NULL,
    bull_case TEXT NOT NULL,
    bear_case TEXT NOT NULL,
    risk_verdict TEXT NOT NULL,
    risk_reasons JSONB NOT NULL DEFAULT '[]',
    position_size_pct REAL NOT NULL,
    reward_risk_ratio REAL NOT NULL,
    sector TEXT,
    industry TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (ticker, date)
);

CREATE TABLE IF NOT EXISTS analyst_reports (
    ticker TEXT NOT NULL,
    date DATE NOT NULL,
    reports JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (ticker, date)
);

CREATE TABLE IF NOT EXISTS debate_transcripts (
    ticker TEXT NOT NULL,
    date DATE NOT NULL,
    bull_case JSONB NOT NULL,
    bear_case JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (ticker, date)
);

CREATE TABLE IF NOT EXISTS trader_decisions (
    ticker TEXT NOT NULL,
    date DATE NOT NULL,
    decision JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (ticker, date)
);

CREATE TABLE IF NOT EXISTS run_metadata (
    run_id TEXT PRIMARY KEY,
    start_time TIMESTAMPTZ NOT NULL,
    end_time TIMESTAMPTZ,
    tickers_attempted JSONB NOT NULL DEFAULT '[]',
    tickers_completed JSONB NOT NULL DEFAULT '[]',
    tickers_failed JSONB NOT NULL DEFAULT '[]',
    errors JSONB NOT NULL DEFAULT '[]',
    concentration_warnings JSONB NOT NULL DEFAULT '[]',
    total_cloud_calls INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS signal_scores (
    ticker TEXT NOT NULL,
    signal_date DATE NOT NULL,
    score_date DATE NOT NULL,
    direction_correct BOOLEAN NOT NULL,
    entry_hit BOOLEAN NOT NULL,
    stop_hit BOOLEAN NOT NULL,
    target_hit JSONB NOT NULL DEFAULT '[]',
    actual_return_pct REAL NOT NULL,
    score REAL NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (ticker, signal_date)
);

CREATE TABLE IF NOT EXISTS data_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    run_id TEXT,
    ticker TEXT NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL,
    source_versions JSONB NOT NULL DEFAULT '{}',
    price_payload JSONB NOT NULL DEFAULT '{}',
    fundamentals_payload JSONB NOT NULL DEFAULT '{}',
    news_payload JSONB NOT NULL DEFAULT '[]',
    macro_payload JSONB NOT NULL DEFAULT '{}',
    feature_payload JSONB NOT NULL DEFAULT '{}',
    data_quality_flags JSONB NOT NULL DEFAULT '[]',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS recommendations (
    recommendation_id TEXT PRIMARY KEY,
    run_id TEXT,
    snapshot_id TEXT REFERENCES data_snapshots(snapshot_id),
    ticker TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    strategy_type TEXT NOT NULL,
    horizon_days INTEGER NOT NULL,
    decision TEXT NOT NULL,
    side TEXT NOT NULL,
    confidence REAL NOT NULL,
    expected_return REAL,
    expected_drawdown REAL,
    benchmark_symbol TEXT NOT NULL DEFAULT 'SPY',
    sector_benchmark_symbol TEXT,
    entry_zone JSONB NOT NULL DEFAULT '[]',
    stop_loss REAL NOT NULL,
    targets JSONB NOT NULL DEFAULT '[]',
    thesis TEXT NOT NULL,
    invalidation TEXT NOT NULL,
    alpha_outputs JSONB NOT NULL DEFAULT '[]',
    committee_outputs JSONB NOT NULL DEFAULT '{}',
    risk_verdict TEXT NOT NULL,
    risk_reasons JSONB NOT NULL DEFAULT '[]',
    portfolio_target_weight REAL NOT NULL DEFAULT 0,
    model_versions JSONB NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS recommendation_scores (
    recommendation_id TEXT PRIMARY KEY REFERENCES recommendations(recommendation_id),
    score_date DATE NOT NULL,
    side_return_pct REAL NOT NULL,
    benchmark_return_pct REAL,
    sector_return_pct REAL,
    excess_return_pct REAL,
    mae_pct REAL,
    mfe_pct REAL,
    stop_hit BOOLEAN NOT NULL,
    target_hit JSONB NOT NULL DEFAULT '[]',
    confidence_bucket TEXT,
    score REAL NOT NULL,
    execution_status TEXT,
    execution_return_pct REAL,
    execution_slippage_pct REAL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS option_chain_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL,
    source TEXT NOT NULL,
    underlying_price REAL NOT NULL,
    expirations JSONB NOT NULL DEFAULT '[]',
    contracts JSONB NOT NULL DEFAULT '[]',
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS agent_scores (
    ticker TEXT NOT NULL,
    signal_date DATE NOT NULL,
    agent_name TEXT NOT NULL,
    prediction_aligned BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (ticker, signal_date, agent_name)
);

CREATE TABLE IF NOT EXISTS paper_trades (
    id BIGSERIAL PRIMARY KEY,
    recommendation_id TEXT,
    ticker TEXT NOT NULL,
    signal_date DATE NOT NULL,
    strategy TEXT NOT NULL DEFAULT 'balanced',
    idempotency_key TEXT NOT NULL UNIQUE,
    decision TEXT NOT NULL,
    side TEXT NOT NULL,
    entry_limit NUMERIC NOT NULL,
    stop_loss NUMERIC NOT NULL,
    take_profit NUMERIC NOT NULL,
    notional_pct NUMERIC NOT NULL DEFAULT 0.015,
    alpaca_order_id TEXT,
    status TEXT NOT NULL,
    status_reason TEXT,
    submitted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (ticker, signal_date) REFERENCES signals(ticker, date)
);

CREATE TABLE IF NOT EXISTS paper_positions (
    paper_trade_id BIGINT PRIMARY KEY REFERENCES paper_trades(id),
    strategy TEXT NOT NULL DEFAULT 'balanced',
    qty NUMERIC NOT NULL,
    avg_entry NUMERIC NOT NULL,
    current_price NUMERIC,
    unrealized_pnl NUMERIC,
    realized_pnl NUMERIC,
    closed_at TIMESTAMPTZ,
    close_reason TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS paper_fills (
    id BIGSERIAL PRIMARY KEY,
    paper_trade_id BIGINT NOT NULL REFERENCES paper_trades(id),
    strategy TEXT NOT NULL DEFAULT 'balanced',
    alpaca_fill_id TEXT NOT NULL UNIQUE,
    side TEXT NOT NULL,
    qty NUMERIC NOT NULL,
    price NUMERIC NOT NULL,
    filled_at TIMESTAMPTZ NOT NULL
);

-- Wave 1.5: nightly equity snapshot per strategy. Drives dashboard sparkline +
-- drawdown circuit breaker (rolling 30-day peak).
CREATE TABLE IF NOT EXISTS paper_equity_snapshots (
    id BIGSERIAL PRIMARY KEY,
    strategy TEXT NOT NULL,
    snapshot_date DATE NOT NULL,
    account_equity NUMERIC NOT NULL,
    cash NUMERIC NOT NULL,
    positions_value NUMERIC NOT NULL,
    daily_pnl NUMERIC,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (strategy, snapshot_date)
);

-- Wave 1.5: drawdown circuit breaker pause state. One row per strategy
-- when paused; row deleted to unpause. Survives restarts via DB persistence.
CREATE TABLE IF NOT EXISTS strategy_pause_state (
    strategy TEXT PRIMARY KEY,
    paused_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    paused_reason TEXT NOT NULL,
    paused_drawdown NUMERIC,
    unpause_after TIMESTAMPTZ
);

-- Wave 2 prep: per-call LLM telemetry. Drives the dashboard reliability table
-- and answers "why did Ollama return empty/truncated output?" without grepping
-- logs. Fail-open: insertion failures are swallowed inside record_llm_call.
CREATE TABLE IF NOT EXISTS llm_calls (
    id BIGSERIAL PRIMARY KEY,
    run_id TEXT,
    ticker TEXT,
    stage TEXT,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    prompt_len INTEGER NOT NULL,
    output_len INTEGER NOT NULL,
    elapsed_seconds REAL NOT NULL,
    done_reason TEXT,
    eval_count INTEGER,
    parse_ok BOOLEAN NOT NULL,
    fallback_used BOOLEAN NOT NULL DEFAULT FALSE,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_created ON llm_calls(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_calls_model_stage ON llm_calls(model, stage);

CREATE INDEX IF NOT EXISTS idx_signals_date ON signals(date);
CREATE INDEX IF NOT EXISTS idx_signals_decision ON signals(decision);
CREATE INDEX IF NOT EXISTS idx_signal_scores_date ON signal_scores(signal_date);
CREATE INDEX IF NOT EXISTS idx_agent_scores_agent ON agent_scores(agent_name);
CREATE INDEX IF NOT EXISTS idx_data_snapshots_ticker_time
    ON data_snapshots(ticker, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_recommendations_ticker_created
    ON recommendations(ticker, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_recommendations_run
    ON recommendations(run_id);
CREATE INDEX IF NOT EXISTS idx_recommendation_scores_date
    ON recommendation_scores(score_date);
CREATE INDEX IF NOT EXISTS idx_option_chain_snapshots_ticker_captured
    ON option_chain_snapshots(ticker, captured_at DESC);

-- ─── Wave 2: options cockpit infrastructure ──────────────────────

-- A3: Postgres-backed async refresh job state. Replaces the in-memory
-- dict + filesystem lock pattern: one table, single source of truth,
-- survives uvicorn restart, cron + web serialize through the same
-- partial unique index. See plan rev 4 decision A3.
CREATE TABLE IF NOT EXISTS option_refresh_jobs (
    job_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,                            -- 'running' | 'completed' | 'failed'
    source TEXT NOT NULL,                            -- 'ibkr' | 'yfinance'
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    total INTEGER,
    completed INTEGER NOT NULL DEFAULT 0,
    failures JSONB NOT NULL DEFAULT '[]'             -- list[{ticker, error_class, message}]
);
-- Partial unique index enforces "at most one running job at a time".
-- INSERT ... ON CONFLICT DO NOTHING returns no row when a running
-- job exists, giving the API a clean 409 path.
CREATE UNIQUE INDEX IF NOT EXISTS idx_option_refresh_jobs_one_running
    ON option_refresh_jobs ((1)) WHERE status = 'running';
CREATE INDEX IF NOT EXISTS idx_option_refresh_jobs_started
    ON option_refresh_jobs (started_at DESC);

-- A6 + P2: per-ticker IV history. Chain ingest writes one row per
-- (ticker, captured_at). iv_rank_30d is computed against the prior
-- 252 days of atm_iv_30d at insert time (P2: cheaper read path than
-- on-demand percentile scan). NULL iv_rank_30d means <60 days of
-- history — A5 cold-start gate uses iv_label='insufficient' for those.
CREATE TABLE IF NOT EXISTS option_iv_history (
    ticker TEXT NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL,
    underlying_price REAL NOT NULL,
    atm_iv_30d REAL,
    atm_iv_60d REAL,
    atm_iv_90d REAL,
    term_structure_30_60 REAL,                       -- atm_iv_60d - atm_iv_30d
    term_structure_60_90 REAL,                       -- atm_iv_90d - atm_iv_60d
    iv_rank_30d REAL,                                -- 0.0-1.0 percentile, NULL when <60 days
    iv_label TEXT,                                   -- 'cheap' | 'fair' | 'rich' | 'insufficient'
    PRIMARY KEY (ticker, captured_at)
);
CREATE INDEX IF NOT EXISTS idx_option_iv_history_ticker_captured
    ON option_iv_history (ticker, captured_at DESC);

-- D2 + A1: per-position protective put cost. position_id is a logical
-- reference to paper_positions.id (no FK constraint to respect the
-- wave-1.5 freeze on the paper_positions schema). One row per
-- (position_id, computed_at) snapshot.
CREATE TABLE IF NOT EXISTS option_protective_costs (
    position_id BIGINT NOT NULL,
    contract_symbol TEXT NOT NULL,
    cost_per_share REAL NOT NULL,
    cost_pct_of_position REAL,
    delta REAL,
    greeks_source TEXT,                              -- 'provider' | 'provider_nan' | 'none'
    computed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (position_id, computed_at)
);
CREATE INDEX IF NOT EXISTS idx_option_protective_costs_position
    ON option_protective_costs (position_id, computed_at DESC);

-- Decisions 11 + A6: audit trail for data-quality state transitions.
-- Records IBKR→yfinance fallbacks (decision 11), TWS gateway state
-- transitions (A4 60s heartbeat), and per-ticker iv_summary_skipped
-- events (A6 partial-success path). Read by /retro and operator alerts.
CREATE TABLE IF NOT EXISTS data_provider_events (
    id BIGSERIAL PRIMARY KEY,
    event_type TEXT NOT NULL,                        -- 'fetcher_fallback' | 'gateway_state_change' | 'iv_summary_skipped' | ...
    ticker TEXT,                                     -- nullable: gateway events are global
    from_provider TEXT,
    to_provider TEXT,
    reason TEXT,
    payload JSONB,                                   -- arbitrary structured detail
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_data_provider_events_occurred
    ON data_provider_events (occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_data_provider_events_type
    ON data_provider_events (event_type, occurred_at DESC);

-- A7: per-attempt thesis-build outcome. 3-way status (success / structured_only / fail)
-- routes ops differently: STRUCTURED_ONLY pages LLM oncall, FAIL pages chain-ingest
-- oncall. `strategy` label matches D4's day-1 trio so the failure-reason metric
-- can split by strategy.
CREATE TABLE IF NOT EXISTS option_thesis_attempts (
    id BIGSERIAL PRIMARY KEY,
    ticker TEXT NOT NULL,
    strategy TEXT NOT NULL,                          -- 'bullish_debit_spread' | 'bearish_protective_put' | 'neutral_iron_condor' | ...
    status TEXT NOT NULL,                            -- 'success' | 'structured_only' | 'fail'
    llm_failure_reason TEXT,                         -- NULL except when status='structured_only'
    elapsed_seconds REAL,
    attempted_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_option_thesis_attempts_attempted
    ON option_thesis_attempts (attempted_at DESC);
CREATE INDEX IF NOT EXISTS idx_option_thesis_attempts_status
    ON option_thesis_attempts (status, attempted_at DESC);
CREATE INDEX IF NOT EXISTS idx_option_thesis_attempts_strategy
    ON option_thesis_attempts (strategy, status, attempted_at DESC);

-- P1: per-(ticker, strategy, recommendation_id) thesis cache. First click
-- computes the spread + LLM narrative; subsequent clicks read this row.
-- Invalidation = chain captured_at advanced past `chain_captured_at`, so the
-- read path joins against latest option_chain_snapshots.captured_at to decide
-- stale vs fresh. recommendation_id is nullable to support snapshot-only
-- theses (e.g., "show me a thesis on TSLA without a specific recommendation").
CREATE TABLE IF NOT EXISTS option_thesis_cache (
    ticker TEXT NOT NULL,
    strategy TEXT NOT NULL,
    recommendation_id TEXT,                          -- nullable: tied to v2 ledger row when present
    chain_captured_at TIMESTAMPTZ NOT NULL,          -- invalidation key
    computed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    structured_json JSONB NOT NULL,                  -- the structured spread payload
    narrative_text TEXT,                             -- LLM narrative (NULL when STRUCTURED_ONLY)
    llm_status TEXT NOT NULL,                        -- A7 'success' / 'structured_only' / 'fail'
    PRIMARY KEY (ticker, strategy, recommendation_id, chain_captured_at)
);
CREATE INDEX IF NOT EXISTS idx_option_thesis_cache_lookup
    ON option_thesis_cache (ticker, strategy, recommendation_id, chain_captured_at DESC);

-- Decision 26: thumbs-up/down feedback on each thesis card. Cross-tabs
-- against option_thesis_attempts to answer "is the LLM rationale actually
-- adding value?" One row per click — repeated votes from the same user
-- pile up, which is the signal we want (revisits + retraction).
CREATE TABLE IF NOT EXISTS option_thesis_feedback (
    id BIGSERIAL PRIMARY KEY,
    ticker TEXT NOT NULL,
    strategy TEXT NOT NULL,
    recommendation_id TEXT,                          -- nullable: snapshot-only theses allowed
    sentiment TEXT NOT NULL,                         -- 'up' | 'down'
    noted_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_option_thesis_feedback_lookup
    ON option_thesis_feedback (ticker, strategy, recommendation_id, noted_at DESC);
CREATE INDEX IF NOT EXISTS idx_option_thesis_feedback_sentiment
    ON option_thesis_feedback (sentiment, noted_at DESC);

-- Decision 26: cockpit panel view counter (Prometheus-shaped). Dashboard
-- fires a panel_view ping per panel on mount; lifetime totals roll up via
-- /api/metrics. Keeps the "is anyone actually using this panel?" question
-- answerable without external analytics.
CREATE TABLE IF NOT EXISTS cockpit_view_log (
    id BIGSERIAL PRIMARY KEY,
    panel TEXT NOT NULL,                             -- 'screener' | 'protect' | 'thesis' | 'flow' | ...
    viewed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_cockpit_view_log_panel
    ON cockpit_view_log (panel, viewed_at DESC);

-- Decision 26: deploy-time annotation. wave_2_started_at is the timestamp
-- the row was inserted (idempotent: only the first INSERT lands). /retro
-- filters wave-1.5 vs wave-2 P&L against this row's value.
CREATE TABLE IF NOT EXISTS deploy_annotations (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Partial index keeps the reconciler hot path fast as terminal statuses accumulate
CREATE INDEX IF NOT EXISTS idx_paper_trades_active
    ON paper_trades(status)
    WHERE status IN ('pending', 'submitted');
CREATE INDEX IF NOT EXISTS idx_paper_trades_signal
    ON paper_trades(ticker, signal_date);
CREATE INDEX IF NOT EXISTS idx_paper_equity_snapshots_strategy_date
    ON paper_equity_snapshots(strategy, snapshot_date DESC);
"""

POST_MIGRATION_SQL = """
CREATE INDEX IF NOT EXISTS idx_paper_trades_strategy
    ON paper_trades(strategy, signal_date);
CREATE INDEX IF NOT EXISTS idx_paper_trades_recommendation
    ON paper_trades(recommendation_id);
"""

# GUARD migrations: idempotent ALTERs for columns that didn't exist in v1.1.
# init_db() runs SCHEMA_SQL first, then these guards, then any SQL depending
# on guarded columns.
GUARD_MIGRATIONS = [
    # debate_transcripts gets multi-round storage in v1.2
    ("debate_transcripts", "rounds", "JSONB"),
    ("debate_transcripts", "judge_verdict", "JSONB"),
    # Wave 1.5: strategy column on existing wave-1 paper_* tables. Default
    # 'balanced' backfills existing rows so wave-1 functionality is preserved.
    ("paper_trades", "strategy", "TEXT NOT NULL DEFAULT 'balanced'"),
    ("paper_positions", "strategy", "TEXT NOT NULL DEFAULT 'balanced'"),
    ("paper_fills", "strategy", "TEXT NOT NULL DEFAULT 'balanced'"),
    # Wave 2 prep: persist yfinance sector/industry on each signal so future
    # portfolio/options work has it without re-fetching. Nullable — wave-1
    # rows stay NULL until the next time the same ticker generates a signal.
    ("signals", "sector", "TEXT"),
    ("signals", "industry", "TEXT"),
    ("signals", "recommendation_id", "TEXT"),
    # Recommendation v2: paper execution links to immutable recommendations
    # when available. Nullable so legacy signal-date rows remain valid.
    ("paper_trades", "recommendation_id", "TEXT"),
    # Recommendation v2 scoring: paper execution diagnostics remain nullable
    # so historic scores keep their recommendation-only meaning.
    ("recommendation_scores", "execution_status", "TEXT"),
    ("recommendation_scores", "execution_return_pct", "REAL"),
    ("recommendation_scores", "execution_slippage_pct", "REAL"),
]


def _column_exists(conn, table: str, column: str) -> bool:
    """True iff `table.column` exists. Safe when table doesn't exist (returns False)."""
    row = conn.execute(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = %s AND column_name = %s
        LIMIT 1
        """,
        (table, column),
    ).fetchone()
    return row is not None


def _ensure_column(conn, table: str, column: str, ddl: str) -> None:
    """Idempotent ALTER ADD COLUMN. No-op if column already exists."""
    if not _column_exists(conn, table, column):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        logger.info(f"Migration: added {table}.{column} ({ddl})")


def get_connection() -> psycopg.Connection:
    return psycopg.connect(settings.database_url, row_factory=dict_row)


def init_db() -> None:
    """Create tables if they don't exist, then apply additive migrations."""
    with get_connection() as conn:
        conn.execute(SCHEMA_SQL)
        for table, column, ddl in GUARD_MIGRATIONS:
            _ensure_column(conn, table, column, ddl)
        conn.execute(POST_MIGRATION_SQL)
        # Recommendation v2 briefly used recommendation_id-based paper keys.
        # Normalize one row per ticker/date/strategy back to the stable
        # wave-1.5 key so reruns dedupe against already-submitted orders.
        normalized = conn.execute(
            """
            WITH candidates AS (
                SELECT
                    id,
                    ticker || ':' || signal_date::text || ':' || strategy AS target_key,
                    ROW_NUMBER() OVER (
                        PARTITION BY ticker, signal_date, strategy
                        ORDER BY
                            CASE WHEN alpaca_order_id IS NOT NULL THEN 0 ELSE 1 END,
                            created_at DESC,
                            id DESC
                    ) AS key_rank
                FROM paper_trades
                WHERE recommendation_id IS NOT NULL
                  AND idempotency_key IN (
                      recommendation_id || ':' || strategy,
                      recommendation_id || ':' || strategy || ':balanced'
                  )
            ),
            safe_candidates AS (
                SELECT c.id, c.target_key
                FROM candidates c
                WHERE c.key_rank = 1
                  AND NOT EXISTS (
                      SELECT 1
                      FROM paper_trades existing
                      WHERE existing.idempotency_key = c.target_key
                  )
            )
            UPDATE paper_trades pt
            SET idempotency_key = safe_candidates.target_key
            FROM safe_candidates
            WHERE pt.id = safe_candidates.id
            """,
        )
        if normalized.rowcount > 0:
            logger.info(f"Migration: normalized {normalized.rowcount} v2 paper idempotency keys")

        # Clean up any remaining rec_id:strategy:balanced rows skipped above
        # because another row already owns the stable ticker/date/strategy key.
        cleaned = conn.execute(
            """
            UPDATE paper_trades pt
            SET idempotency_key = pt.recommendation_id || ':' || pt.strategy
            WHERE pt.recommendation_id IS NOT NULL
              AND pt.idempotency_key = pt.recommendation_id || ':' || pt.strategy || ':balanced'
              AND NOT EXISTS (
                  SELECT 1
                  FROM paper_trades existing
                  WHERE existing.idempotency_key = pt.recommendation_id || ':' || pt.strategy
              )
            """,
        )
        if cleaned.rowcount > 0:
            logger.info(f"Migration: cleaned {cleaned.rowcount} corrupted v2 paper idempotency keys")

        # Wave 1.5: backfill idempotency keys for old wave-1 rows that have
        # exactly "TICKER:YYYY-MM-DD" format. Do not touch v2 rec_id:strategy
        # keys; those are handled above.
        result = conn.execute(
            """
            UPDATE paper_trades
            SET idempotency_key = idempotency_key || ':balanced'
            WHERE idempotency_key ~ '^[A-Z][A-Z0-9.-]*:[0-9]{4}-[0-9]{2}-[0-9]{2}$'
            """,
        )
        if result.rowcount > 0:
            logger.info(f"Migration: backfilled {result.rowcount} idempotency keys with ':balanced' suffix")
        conn.commit()
    logger.info("Database initialized")


# ─── Options Chain Snapshots ────────────────────────────

def _normalize_option_snapshot_row(row: dict) -> dict:
    normalized = dict(row)
    for key, default in (
        ("expirations", []),
        ("contracts", []),
        ("metadata", {}),
    ):
        normalized[key] = _decode_json_value(normalized.get(key), default)
    return normalized


def save_option_chain_snapshot(snapshot) -> str:
    """Persist one option-chain snapshot and return its snapshot_id."""
    payload = snapshot.model_dump(mode="json")
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO option_chain_snapshots (
                snapshot_id, ticker, captured_at, source, underlying_price,
                expirations, contracts, metadata
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (snapshot_id) DO NOTHING
            """,
            (
                snapshot.snapshot_id,
                snapshot.ticker.upper(),
                snapshot.captured_at,
                snapshot.source,
                snapshot.underlying_price,
                json.dumps(payload["expirations"]),
                json.dumps(payload["contracts"]),
                json.dumps(payload["metadata"]),
            ),
        )
        conn.commit()
    return snapshot.snapshot_id


def get_latest_option_chain_snapshot(ticker: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM option_chain_snapshots
            WHERE ticker = %s
            ORDER BY captured_at DESC
            LIMIT 1
            """,
            (ticker.upper(),),
        ).fetchone()
    return _normalize_option_snapshot_row(row) if row else None


def get_latest_option_chain_snapshots(
    tickers: list[str] | None = None,
    limit: int = 50,
) -> list[dict]:
    symbols = [ticker.upper() for ticker in tickers or [] if ticker]
    with get_connection() as conn:
        if symbols:
            rows = conn.execute(
                """
                WITH ranked AS (
                    SELECT *,
                           ROW_NUMBER() OVER (
                               PARTITION BY ticker
                               ORDER BY captured_at DESC
                           ) AS rn
                    FROM option_chain_snapshots
                    WHERE ticker = ANY(%s)
                )
                SELECT *
                FROM ranked
                WHERE rn = 1
                ORDER BY captured_at DESC
                LIMIT %s
                """,
                (symbols, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                WITH ranked AS (
                    SELECT *,
                           ROW_NUMBER() OVER (
                               PARTITION BY ticker
                               ORDER BY captured_at DESC
                           ) AS rn
                    FROM option_chain_snapshots
                )
                SELECT *
                FROM ranked
                WHERE rn = 1
                ORDER BY captured_at DESC
                LIMIT %s
                """,
                (limit,),
            ).fetchall()
    return [_normalize_option_snapshot_row(row) for row in rows]


# ─── Recommendation v2 Ledger CRUD ──────────────────────

def save_data_snapshot(snapshot: DataSnapshot) -> str:
    """Persist an immutable data snapshot and return its snapshot_id."""
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO data_snapshots (
                snapshot_id, run_id, ticker, captured_at, source_versions,
                price_payload, fundamentals_payload, news_payload,
                macro_payload, feature_payload, data_quality_flags
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (snapshot_id) DO NOTHING
            """,
            (
                snapshot.snapshot_id,
                snapshot.run_id,
                snapshot.ticker,
                snapshot.captured_at,
                json.dumps(snapshot.source_versions),
                json.dumps(snapshot.price_payload),
                json.dumps(snapshot.fundamentals_payload),
                json.dumps(snapshot.news_payload),
                json.dumps(snapshot.macro_payload),
                json.dumps(snapshot.feature_payload),
                json.dumps(snapshot.data_quality_flags),
            ),
        )
        conn.commit()
    return snapshot.snapshot_id


def save_recommendation(recommendation: Recommendation) -> str:
    """Persist one immutable recommendation ledger row.

    There is intentionally no upsert. Same-day reruns should create a new
    recommendation_id so scoring can answer whether that exact decision worked.
    """
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO recommendations (
                recommendation_id, run_id, snapshot_id, ticker, created_at,
                strategy_type, horizon_days, decision, side, confidence,
                expected_return, expected_drawdown, benchmark_symbol,
                sector_benchmark_symbol, entry_zone, stop_loss, targets,
                thesis, invalidation, alpha_outputs, committee_outputs,
                risk_verdict, risk_reasons, portfolio_target_weight,
                model_versions
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                recommendation.recommendation_id,
                recommendation.run_id,
                recommendation.snapshot_id,
                recommendation.ticker,
                recommendation.created_at,
                recommendation.strategy_type.value,
                recommendation.horizon_days,
                recommendation.decision.value,
                recommendation.side.value,
                recommendation.confidence,
                recommendation.expected_return,
                recommendation.expected_drawdown,
                recommendation.benchmark_symbol,
                recommendation.sector_benchmark_symbol,
                json.dumps(recommendation.entry_zone),
                recommendation.stop_loss,
                json.dumps(recommendation.targets),
                recommendation.thesis,
                recommendation.invalidation,
                json.dumps([
                    alpha.model_dump(mode="json")
                    for alpha in recommendation.alpha_outputs
                ]),
                json.dumps(recommendation.committee_outputs),
                recommendation.risk_verdict.value,
                json.dumps(recommendation.risk_reasons),
                recommendation.portfolio_target_weight,
                json.dumps(recommendation.model_versions),
            ),
        )
        conn.commit()
    return recommendation.recommendation_id


def get_recommendation(recommendation_id: str) -> dict | None:
    """Return one immutable recommendation row by id."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM recommendations WHERE recommendation_id = %s",
            (recommendation_id,),
        ).fetchone()
    return dict(row) if row else None


def get_latest_recommendations(
    limit: int = 50,
    ticker: str | None = None,
) -> list[dict]:
    """Return recent immutable recommendations with optional outcome scores."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                r.*,
                rs.score_date,
                rs.side_return_pct,
                rs.benchmark_return_pct,
                rs.sector_return_pct,
                rs.excess_return_pct,
                rs.mae_pct,
                rs.mfe_pct,
                rs.stop_hit,
                rs.target_hit,
                rs.confidence_bucket,
                rs.score AS outcome_score,
                rs.execution_status,
                rs.execution_return_pct,
                rs.execution_slippage_pct
            FROM recommendations r
            LEFT JOIN recommendation_scores rs
              ON r.recommendation_id = rs.recommendation_id
            WHERE (%s::text IS NULL OR r.ticker = %s)
            ORDER BY r.created_at DESC
            LIMIT %s
            """,
            (ticker, ticker, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def _decode_json_value(value, default):
    if value is None:
        return default
    if isinstance(value, str):
        return json.loads(value)
    return value


def _normalize_recommendation_row(row: dict) -> dict:
    normalized = dict(row)
    for key, default in (
        ("entry_zone", []),
        ("targets", []),
        ("alpha_outputs", []),
        ("committee_outputs", {}),
        ("risk_reasons", []),
        ("model_versions", {}),
        ("target_hit", []),
    ):
        if key in normalized:
            normalized[key] = _decode_json_value(normalized[key], default)
    return normalized


def get_recommendations_by_date(target_date: date) -> list[dict]:
    """Return the latest immutable recommendation per ticker for a dashboard date."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            WITH ranked AS (
                SELECT
                    r.*,
                    rs.score_date,
                    rs.side_return_pct,
                    rs.benchmark_return_pct,
                    rs.sector_return_pct,
                    rs.excess_return_pct,
                    rs.mae_pct,
                    rs.mfe_pct,
                    rs.stop_hit,
                    rs.target_hit,
                    rs.confidence_bucket,
                    rs.score AS outcome_score,
                    rs.execution_status,
                    rs.execution_return_pct,
                    rs.execution_slippage_pct,
                    ROW_NUMBER() OVER (
                        PARTITION BY r.ticker
                        ORDER BY r.created_at DESC
                    ) AS ticker_rank
                FROM recommendations r
                LEFT JOIN recommendation_scores rs
                  ON r.recommendation_id = rs.recommendation_id
                WHERE r.created_at::date = %s
            )
            SELECT
                *
            FROM ranked
            WHERE ticker_rank = 1
            ORDER BY
                CASE risk_verdict WHEN 'APPROVED' THEN 0 ELSE 1 END,
                confidence DESC,
                created_at DESC
            """,
            (target_date,),
        ).fetchall()
    return [_normalize_recommendation_row(row) for row in rows]


def get_recommendation_audit_detail(recommendation_id: str) -> dict | None:
    """Return one recommendation with its inputs, outcome, and execution rows."""
    with get_connection() as conn:
        recommendation = conn.execute(
            """
            SELECT *
            FROM recommendations
            WHERE recommendation_id = %s
            """,
            (recommendation_id,),
        ).fetchone()
        if not recommendation:
            return None

        score = conn.execute(
            """
            SELECT *
            FROM recommendation_scores
            WHERE recommendation_id = %s
            """,
            (recommendation_id,),
        ).fetchone()

        snapshot = None
        snapshot_id = recommendation.get("snapshot_id")
        if snapshot_id:
            snapshot = conn.execute(
                """
                SELECT *
                FROM data_snapshots
                WHERE snapshot_id = %s
                """,
                (snapshot_id,),
            ).fetchone()

        paper_trades = conn.execute(
            """
            SELECT pt.*, pp.realized_pnl, pp.unrealized_pnl,
                   pp.closed_at, pp.close_reason
            FROM paper_trades pt
            LEFT JOIN paper_positions pp ON pp.paper_trade_id = pt.id
            WHERE pt.recommendation_id = %s
            ORDER BY
                CASE pt.strategy
                    WHEN 'aggressive' THEN 1
                    WHEN 'balanced' THEN 2
                    WHEN 'conservative' THEN 3
                    ELSE 4
                END,
                pt.created_at DESC
            """,
            (recommendation_id,),
        ).fetchall()

    return {
        "recommendation": dict(recommendation),
        "snapshot": dict(snapshot) if snapshot else None,
        "score": dict(score) if score else None,
        "paper_trades": [dict(row) for row in paper_trades],
    }


def get_unscored_recommendations(days_back: int = 10) -> list[dict]:
    """Return immutable recommendations whose holding period may need scoring."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT r.*
            FROM recommendations r
            LEFT JOIN recommendation_scores rs
              ON r.recommendation_id = rs.recommendation_id
            WHERE r.created_at::date >= CURRENT_DATE - %s
              AND rs.recommendation_id IS NULL
            ORDER BY r.created_at
            """,
            (days_back,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_recommendation_score_history(
    days: int = 90,
    limit: int = 100,
) -> list[dict]:
    """Return recent scored immutable recommendations for audit/dashboard views."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                r.recommendation_id,
                r.ticker,
                r.created_at,
                r.strategy_type,
                r.horizon_days,
                r.decision,
                r.side,
                r.confidence,
                r.expected_return,
                r.expected_drawdown,
                r.portfolio_target_weight,
                rs.score_date,
                rs.side_return_pct,
                rs.benchmark_return_pct,
                rs.sector_return_pct,
                rs.excess_return_pct,
                rs.mae_pct,
                rs.mfe_pct,
                rs.stop_hit,
                rs.target_hit,
                rs.confidence_bucket,
                rs.score,
                rs.execution_status,
                rs.execution_return_pct,
                rs.execution_slippage_pct
            FROM recommendation_scores rs
            JOIN recommendations r
              ON r.recommendation_id = rs.recommendation_id
            WHERE rs.score_date >= CURRENT_DATE - %s
            ORDER BY rs.score_date DESC, r.created_at DESC
            LIMIT %s
            """,
            (days, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def save_recommendation_score(score: RecommendationScore) -> None:
    """Persist or refresh the outcome score for an immutable recommendation."""
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO recommendation_scores (
                recommendation_id, score_date, side_return_pct,
                benchmark_return_pct, sector_return_pct, excess_return_pct,
                mae_pct, mfe_pct, stop_hit, target_hit, confidence_bucket,
                score, execution_status, execution_return_pct,
                execution_slippage_pct
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (recommendation_id) DO UPDATE SET
                score_date = EXCLUDED.score_date,
                side_return_pct = EXCLUDED.side_return_pct,
                benchmark_return_pct = EXCLUDED.benchmark_return_pct,
                sector_return_pct = EXCLUDED.sector_return_pct,
                excess_return_pct = EXCLUDED.excess_return_pct,
                mae_pct = EXCLUDED.mae_pct,
                mfe_pct = EXCLUDED.mfe_pct,
                stop_hit = EXCLUDED.stop_hit,
                target_hit = EXCLUDED.target_hit,
                confidence_bucket = EXCLUDED.confidence_bucket,
                score = EXCLUDED.score,
                execution_status = EXCLUDED.execution_status,
                execution_return_pct = EXCLUDED.execution_return_pct,
                execution_slippage_pct = EXCLUDED.execution_slippage_pct,
                created_at = NOW()
            """,
            (
                score.recommendation_id,
                score.score_date,
                score.side_return_pct,
                score.benchmark_return_pct,
                score.sector_return_pct,
                score.excess_return_pct,
                score.mae_pct,
                score.mfe_pct,
                score.stop_hit,
                json.dumps(score.target_hit),
                score.confidence_bucket,
                score.score,
                score.execution_status,
                score.execution_return_pct,
                score.execution_slippage_pct,
            ),
        )
        conn.commit()


def get_paper_trades_by_recommendation(recommendation_id: str) -> list[dict]:
    """Return paper execution rows linked to an immutable recommendation."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT pt.*, pp.qty, pp.avg_entry, pp.current_price,
                   pp.realized_pnl, pp.unrealized_pnl,
                   pp.closed_at, pp.close_reason
            FROM paper_trades pt
            LEFT JOIN paper_positions pp ON pp.paper_trade_id = pt.id
            WHERE pt.recommendation_id = %s
            ORDER BY
                CASE pt.strategy
                    WHEN 'aggressive' THEN 1
                    WHEN 'balanced' THEN 2
                    WHEN 'conservative' THEN 3
                    ELSE 4
                END,
                pt.created_at DESC
            """,
            (recommendation_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_recommendation_calibration(
    days: int = 90,
    min_samples: int = 1,
) -> list[dict]:
    """Aggregate recent recommendation outcomes into calibration buckets."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                r.strategy_type,
                r.horizon_days,
                r.side,
                COALESCE(rs.confidence_bucket, 'unknown') AS confidence_bucket,
                COUNT(*) AS total,
                AVG(r.confidence) AS avg_confidence,
                AVG(CASE WHEN rs.side_return_pct > 0 THEN 1.0 ELSE 0.0 END) AS win_rate,
                AVG(
                    CASE
                        WHEN COALESCE(rs.excess_return_pct, rs.side_return_pct) > 0
                        THEN 1.0 ELSE 0.0
                    END
                ) AS outperform_rate,
                AVG(rs.side_return_pct) AS avg_side_return_pct,
                AVG(rs.excess_return_pct) AS avg_excess_return_pct,
                AVG(rs.score) AS avg_score
            FROM recommendation_scores rs
            JOIN recommendations r
              ON r.recommendation_id = rs.recommendation_id
            WHERE rs.score_date >= CURRENT_DATE - %s
            GROUP BY
                r.strategy_type,
                r.horizon_days,
                r.side,
                COALESCE(rs.confidence_bucket, 'unknown')
            HAVING COUNT(*) >= %s
            ORDER BY total DESC, avg_score DESC
            """,
            (days, min_samples),
        ).fetchall()
    return [dict(r) for r in rows]


def get_recommendation_dashboard_summary(days: int = 30) -> dict:
    """Compact recommendation-ledger health summary for dashboard/API use."""
    with get_connection() as conn:
        row = conn.execute(
            """
            WITH recent AS (
                SELECT *
                FROM recommendations
                WHERE created_at::date >= CURRENT_DATE - %s
            ),
            score_summary AS (
                SELECT
                    COUNT(*) AS scored,
                    AVG(rs.score) AS avg_score,
                    AVG(rs.side_return_pct) AS avg_side_return_pct,
                    AVG(rs.excess_return_pct) AS avg_excess_return_pct
                FROM recommendation_scores rs
                JOIN recent r ON r.recommendation_id = rs.recommendation_id
            ),
            execution_summary AS (
                SELECT
                    COUNT(*) FILTER (WHERE pt.status = 'filled') AS filled,
                    COUNT(*) FILTER (WHERE pt.status = 'failed') AS failed,
                    COUNT(*) FILTER (WHERE pt.status = 'execution_skipped') AS skipped
                FROM paper_trades pt
                JOIN recent r ON r.recommendation_id = pt.recommendation_id
            )
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE risk_verdict = 'APPROVED') AS approved,
                COUNT(*) FILTER (WHERE risk_verdict = 'REJECTED') AS rejected,
                AVG(confidence) AS avg_confidence,
                AVG(portfolio_target_weight) AS avg_target_weight,
                score_summary.scored,
                score_summary.avg_score,
                score_summary.avg_side_return_pct,
                score_summary.avg_excess_return_pct,
                execution_summary.filled,
                execution_summary.failed,
                execution_summary.skipped
            FROM recent
            CROSS JOIN score_summary
            CROSS JOIN execution_summary
            GROUP BY
                score_summary.scored,
                score_summary.avg_score,
                score_summary.avg_side_return_pct,
                score_summary.avg_excess_return_pct,
                execution_summary.filled,
                execution_summary.failed,
                execution_summary.skipped
            """,
            (days,),
        ).fetchone()

    if not row:
        return {
            "days": days,
            "total": 0,
            "approved": 0,
            "rejected": 0,
            "scored": 0,
            "filled": 0,
            "failed": 0,
            "skipped": 0,
            "avg_confidence": None,
            "avg_target_weight": None,
            "avg_score": None,
            "avg_side_return_pct": None,
            "avg_excess_return_pct": None,
        }

    return {
        "days": days,
        "total": row["total"] or 0,
        "approved": row["approved"] or 0,
        "rejected": row["rejected"] or 0,
        "scored": row["scored"] or 0,
        "filled": row["filled"] or 0,
        "failed": row["failed"] or 0,
        "skipped": row["skipped"] or 0,
        "avg_confidence": (
            float(row["avg_confidence"]) if row["avg_confidence"] is not None else None
        ),
        "avg_target_weight": (
            float(row["avg_target_weight"]) if row["avg_target_weight"] is not None else None
        ),
        "avg_score": float(row["avg_score"]) if row["avg_score"] is not None else None,
        "avg_side_return_pct": (
            float(row["avg_side_return_pct"])
            if row["avg_side_return_pct"] is not None
            else None
        ),
        "avg_excess_return_pct": (
            float(row["avg_excess_return_pct"])
            if row["avg_excess_return_pct"] is not None
            else None
        ),
    }


def get_recommendation_track_records(
    days: int = 180,
    min_samples: int = 3,
) -> list[dict]:
    """Aggregate recommendation performance by ticker, strategy, side, and regime."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                r.ticker,
                r.strategy_type,
                r.side,
                COALESCE(ds.macro_payload->'regime'->>'regime', 'unknown') AS regime,
                COUNT(*) AS total,
                AVG(r.confidence) AS avg_confidence,
                AVG(CASE WHEN rs.side_return_pct > 0 THEN 1.0 ELSE 0.0 END) AS win_rate,
                AVG(
                    CASE
                        WHEN COALESCE(rs.excess_return_pct, rs.side_return_pct) > 0
                        THEN 1.0 ELSE 0.0
                    END
                ) AS outperform_rate,
                AVG(rs.side_return_pct) AS avg_side_return_pct,
                AVG(rs.excess_return_pct) AS avg_excess_return_pct,
                AVG(rs.score) AS avg_score
            FROM recommendation_scores rs
            JOIN recommendations r
              ON r.recommendation_id = rs.recommendation_id
            LEFT JOIN data_snapshots ds
              ON ds.snapshot_id = r.snapshot_id
            WHERE rs.score_date >= CURRENT_DATE - %s
            GROUP BY
                r.ticker,
                r.strategy_type,
                r.side,
                COALESCE(ds.macro_payload->'regime'->>'regime', 'unknown')
            HAVING COUNT(*) >= %s
            ORDER BY avg_score DESC, total DESC
            """,
            (days, min_samples),
        ).fetchall()
    return [dict(row) for row in rows]


# ─── Signal CRUD ─────────────────────────────────────────

def save_signal(signal: FinalSignal) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO signals (
                ticker, date, recommendation_id, decision, confidence, entry_zone, stop_loss,
                targets, invalidation, holding_period_days, thesis,
                bull_case, bear_case, risk_verdict, risk_reasons,
                position_size_pct, reward_risk_ratio, sector, industry
            ) VALUES (
                %(ticker)s, %(date)s, %(recommendation_id)s,
                %(decision)s, %(confidence)s, %(entry_zone)s,
                %(stop_loss)s, %(targets)s, %(invalidation)s,
                %(holding_period_days)s, %(thesis)s, %(bull_case)s,
                %(bear_case)s, %(risk_verdict)s, %(risk_reasons)s,
                %(position_size_pct)s, %(reward_risk_ratio)s,
                %(sector)s, %(industry)s
            )
            ON CONFLICT (ticker, date) DO UPDATE SET
                recommendation_id = COALESCE(EXCLUDED.recommendation_id, signals.recommendation_id),
                decision = EXCLUDED.decision,
                confidence = EXCLUDED.confidence,
                entry_zone = EXCLUDED.entry_zone,
                stop_loss = EXCLUDED.stop_loss,
                targets = EXCLUDED.targets,
                invalidation = EXCLUDED.invalidation,
                holding_period_days = EXCLUDED.holding_period_days,
                thesis = EXCLUDED.thesis,
                bull_case = EXCLUDED.bull_case,
                bear_case = EXCLUDED.bear_case,
                risk_verdict = EXCLUDED.risk_verdict,
                risk_reasons = EXCLUDED.risk_reasons,
                position_size_pct = EXCLUDED.position_size_pct,
                reward_risk_ratio = EXCLUDED.reward_risk_ratio,
                sector = COALESCE(EXCLUDED.sector, signals.sector),
                industry = COALESCE(EXCLUDED.industry, signals.industry),
                created_at = NOW()
            """,
            {
                "ticker": signal.ticker,
                "date": signal.date,
                "recommendation_id": signal.recommendation_id,
                "decision": signal.decision.value,
                "confidence": signal.confidence,
                "entry_zone": json.dumps(signal.entry_zone),
                "stop_loss": signal.stop_loss,
                "targets": json.dumps(signal.targets),
                "invalidation": signal.invalidation,
                "holding_period_days": signal.holding_period_days,
                "thesis": signal.thesis,
                "bull_case": signal.bull_case,
                "bear_case": signal.bear_case,
                "risk_verdict": signal.risk_verdict.value,
                "risk_reasons": json.dumps(signal.risk_reasons),
                "position_size_pct": signal.position_size_pct,
                "reward_risk_ratio": signal.reward_risk_ratio,
                "sector": signal.sector,
                "industry": signal.industry,
            },
        )
        conn.commit()


def get_signals_by_date(target_date: date) -> list[FinalSignal]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM signals WHERE date = %s ORDER BY confidence DESC",
            (target_date,),
        ).fetchall()
    return [_row_to_signal(r) for r in rows]


def get_signals_by_ticker(ticker: str, limit: int = 30) -> list[FinalSignal]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM signals WHERE ticker = %s ORDER BY date DESC LIMIT %s",
            (ticker, limit),
        ).fetchall()
    return [_row_to_signal(r) for r in rows]


def get_latest_run_status(target_date: date) -> dict | None:
    """Get the most recent pipeline run whose start_time falls on target_date.

    Returns dict with attempted, completed, failed lists and end_time, or None.
    """
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT run_id, start_time, end_time,
                   tickers_attempted, tickers_completed, tickers_failed
            FROM run_metadata
            WHERE start_time::date = %s
            ORDER BY start_time DESC
            LIMIT 1
            """,
            (target_date,),
        ).fetchone()
    if not row:
        return None

    def _as_list(value):
        if isinstance(value, str):
            return json.loads(value)
        return value or []

    return {
        "run_id": row["run_id"],
        "start_time": row["start_time"],
        "end_time": row["end_time"],
        "attempted": _as_list(row["tickers_attempted"]),
        "completed": _as_list(row["tickers_completed"]),
        "failed": _as_list(row["tickers_failed"]),
    }


def get_latest_concentration_warnings() -> list[str]:
    """Get concentration warnings from the most recent pipeline run."""
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT concentration_warnings FROM run_metadata
            WHERE end_time IS NOT NULL
            ORDER BY start_time DESC LIMIT 1
            """,
        ).fetchone()
    if not row or not row["concentration_warnings"]:
        return []
    warnings = row["concentration_warnings"]
    if isinstance(warnings, str):
        return json.loads(warnings)
    return warnings


def get_previous_signals(before_date: date) -> list[FinalSignal]:
    """Get signals from the most recent date before the given date."""
    with get_connection() as conn:
        # Find the most recent date with signals before the target date
        row = conn.execute(
            "SELECT MAX(date) as prev_date FROM signals WHERE date < %s",
            (before_date,),
        ).fetchone()
    if not row or row["prev_date"] is None:
        return []
    return get_signals_by_date(row["prev_date"])


def get_latest_signals(limit: int = 12) -> list[FinalSignal]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT ON (ticker) *
            FROM signals
            ORDER BY ticker, date DESC
            """,
        ).fetchall()
    return sorted([_row_to_signal(r) for r in rows], key=lambda s: s.confidence, reverse=True)[:limit]


def _row_to_signal(row: dict) -> FinalSignal:
    entry_zone = row["entry_zone"] if isinstance(row["entry_zone"], list) else json.loads(row["entry_zone"])
    targets = row["targets"] if isinstance(row["targets"], list) else json.loads(row["targets"])
    risk_reasons = row["risk_reasons"] if isinstance(row["risk_reasons"], list) else json.loads(row["risk_reasons"])
    return FinalSignal(
        recommendation_id=row.get("recommendation_id"),
        ticker=row["ticker"],
        date=row["date"],
        decision=Decision(row["decision"]),
        confidence=row["confidence"],
        entry_zone=entry_zone,
        stop_loss=row["stop_loss"],
        targets=targets,
        invalidation=row["invalidation"],
        holding_period_days=row["holding_period_days"],
        thesis=row["thesis"],
        bull_case=row["bull_case"],
        bear_case=row["bear_case"],
        risk_verdict=RiskVerdict(row["risk_verdict"]),
        risk_reasons=risk_reasons,
        position_size_pct=row["position_size_pct"],
        reward_risk_ratio=row["reward_risk_ratio"],
        sector=row.get("sector"),
        industry=row.get("industry"),
    )


# ─── Analyst Reports ─────────────────────────────────────

def save_analyst_reports(ticker: str, report_date: date, reports: AnalystReports) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO analyst_reports (ticker, date, reports)
            VALUES (%s, %s, %s)
            ON CONFLICT (ticker, date) DO UPDATE SET
                reports = EXCLUDED.reports, created_at = NOW()
            """,
            (ticker, report_date, reports.model_dump_json()),
        )
        conn.commit()


def get_analyst_reports(ticker: str, report_date: date) -> dict | None:
    """Return stored analyst reports for scorer/reflection use."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT reports FROM analyst_reports WHERE ticker = %s AND date = %s",
            (ticker, report_date),
        ).fetchone()
    if not row:
        return None
    reports = row["reports"]
    if isinstance(reports, str):
        return json.loads(reports)
    return reports


# ─── Debate Transcripts ──────────────────────────────────

def save_debate(ticker: str, report_date: date, debate: DebateTranscript) -> None:
    rounds_json = (
        json.dumps([r.model_dump(mode="json") for r in debate.rounds])
        if debate.rounds is not None
        else None
    )
    judge_json = debate.judge_verdict.model_dump_json() if debate.judge_verdict else None
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO debate_transcripts (
                ticker, date, bull_case, bear_case, rounds, judge_verdict
            ) VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (ticker, date) DO UPDATE SET
                bull_case = EXCLUDED.bull_case,
                bear_case = EXCLUDED.bear_case,
                rounds = EXCLUDED.rounds,
                judge_verdict = EXCLUDED.judge_verdict,
                created_at = NOW()
            """,
            (
                ticker,
                report_date,
                debate.bull_case.model_dump_json(),
                debate.bear_case.model_dump_json(),
                rounds_json,
                judge_json,
            ),
        )
        conn.commit()


def get_debate(ticker: str, report_date: date) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM debate_transcripts WHERE ticker = %s AND date = %s",
            (ticker, report_date),
        ).fetchone()
    return row


# ─── Run Metadata ────────────────────────────────────────

def save_run_metadata(meta: RunMetadata) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO run_metadata (
                run_id, start_time, end_time, tickers_attempted,
                tickers_completed, tickers_failed, errors,
                concentration_warnings, total_cloud_calls
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (run_id) DO UPDATE SET
                end_time = EXCLUDED.end_time,
                tickers_completed = EXCLUDED.tickers_completed,
                tickers_failed = EXCLUDED.tickers_failed,
                errors = EXCLUDED.errors,
                concentration_warnings = EXCLUDED.concentration_warnings,
                total_cloud_calls = EXCLUDED.total_cloud_calls
            """,
            (
                meta.run_id,
                meta.start_time,
                meta.end_time,
                json.dumps(meta.tickers_attempted),
                json.dumps(meta.tickers_completed),
                json.dumps(meta.tickers_failed),
                json.dumps(meta.errors),
                json.dumps(meta.concentration_warnings),
                meta.total_cloud_calls,
            ),
        )
        conn.commit()


# ─── Signal Scores ──────────────────────────────────────

def get_unscored_signals(days_back: int = 10) -> list[FinalSignal]:
    """Get signals from the past N days that haven't been scored yet."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT s.* FROM signals s
            LEFT JOIN signal_scores sc ON s.ticker = sc.ticker AND s.date = sc.signal_date
            WHERE s.date >= CURRENT_DATE - %s
              AND sc.ticker IS NULL
            ORDER BY s.date
            """,
            (days_back,),
        ).fetchall()
    return [_row_to_signal(r) for r in rows]


def save_signal_score(
    ticker: str,
    signal_date: date,
    score_date: date,
    direction_correct: bool,
    entry_hit: bool,
    stop_hit: bool,
    target_hit: list[bool],
    actual_return_pct: float,
    score: float,
) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO signal_scores (
                ticker, signal_date, score_date, direction_correct,
                entry_hit, stop_hit, target_hit, actual_return_pct, score
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (ticker, signal_date) DO UPDATE SET
                score_date = EXCLUDED.score_date,
                direction_correct = EXCLUDED.direction_correct,
                entry_hit = EXCLUDED.entry_hit,
                stop_hit = EXCLUDED.stop_hit,
                target_hit = EXCLUDED.target_hit,
                actual_return_pct = EXCLUDED.actual_return_pct,
                score = EXCLUDED.score,
                created_at = NOW()
            """,
            (ticker, signal_date, score_date, direction_correct,
             entry_hit, stop_hit, json.dumps(target_hit),
             actual_return_pct, score),
        )
        conn.commit()


def save_agent_score(
    ticker: str,
    signal_date: date,
    agent_name: str,
    prediction_aligned: bool,
) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO agent_scores (ticker, signal_date, agent_name, prediction_aligned)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (ticker, signal_date, agent_name) DO UPDATE SET
                prediction_aligned = EXCLUDED.prediction_aligned,
                created_at = NOW()
            """,
            (ticker, signal_date, agent_name, prediction_aligned),
        )
        conn.commit()


def get_agent_track_record(ticker: str, limit: int = 10) -> dict[str, dict]:
    """Get per-agent accuracy for a ticker's last N scored signals.

    Returns: {"technical": {"total": 8, "correct": 6}, ...}
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT agent_name,
                   COUNT(*) as total,
                   SUM(prediction_aligned::int) as correct
            FROM agent_scores
            WHERE ticker = %s
              AND signal_date IN (
                  SELECT DISTINCT signal_date FROM signal_scores
                  WHERE ticker = %s
                  ORDER BY signal_date DESC
                  LIMIT %s
              )
            GROUP BY agent_name
            """,
            (ticker, ticker, limit),
        ).fetchall()
    return {
        row["agent_name"]: {"total": row["total"], "correct": row["correct"]}
        for row in rows
    }


def get_agent_leaderboard() -> list[dict]:
    """Get global per-agent accuracy stats (all-time + last 7 days)."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                agent_name,
                COUNT(*) as total,
                SUM(prediction_aligned::int) as correct,
                SUM(CASE WHEN signal_date >= CURRENT_DATE - 7 THEN 1 ELSE 0 END) as recent_total,
                SUM(CASE WHEN signal_date >= CURRENT_DATE - 7 AND prediction_aligned THEN 1 ELSE 0 END) as recent_correct
            FROM agent_scores
            GROUP BY agent_name
            ORDER BY agent_name
            """,
        ).fetchall()
    return [dict(r) for r in rows]


def get_signal_scores_by_date(target_date: date) -> dict[str, float]:
    """Get score for each ticker on a given date. Returns {ticker: score}."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT ticker, score FROM signal_scores WHERE signal_date = %s",
            (target_date,),
        ).fetchall()
    return {row["ticker"]: row["score"] for row in rows}


def get_score_history(days: int = 30) -> list[dict]:
    """Get recent signal scores."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT ticker, signal_date, score_date, direction_correct,
                   entry_hit, stop_hit, actual_return_pct, score
            FROM signal_scores
            WHERE signal_date >= CURRENT_DATE - %s
            ORDER BY signal_date DESC, ticker
            """,
            (days,),
        ).fetchall()
    return [dict(r) for r in rows]


# ─── Paper Trading CRUD (v1.2) ─────────────────────────

def insert_paper_trade(trade: PaperTrade) -> int:
    """Insert a paper_trade row. Returns the new row id.

    Caller must check IntegrityError for duplicate idempotency_key (handled
    separately to allow status='duplicate_blocked' branching).
    """
    with get_connection() as conn:
        row = conn.execute(
            """
            INSERT INTO paper_trades (
                recommendation_id, ticker, signal_date, strategy,
                idempotency_key, decision, side, entry_limit, stop_loss,
                take_profit, notional_pct,
                alpaca_order_id, status, status_reason, submitted_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            RETURNING id
            """,
            (
                trade.recommendation_id,
                trade.ticker, trade.signal_date, trade.strategy, trade.idempotency_key,
                trade.decision.value, trade.side,
                trade.entry_limit, trade.stop_loss, trade.take_profit,
                trade.notional_pct, trade.alpaca_order_id,
                trade.status.value, trade.status_reason, trade.submitted_at,
            ),
        ).fetchone()
        conn.commit()
    return row["id"]


def paper_trade_exists(idempotency_key: str) -> bool:
    """Check if a paper_trade already exists for this key (idempotency guard)."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM paper_trades WHERE idempotency_key = %s LIMIT 1",
            (idempotency_key,),
        ).fetchone()
    return row is not None


def get_paper_trade_by_key(idempotency_key: str) -> dict | None:
    """Return the paper_trade row for this key, or None. Used by paper_trader to
    distinguish 'already submitted to Alpaca' (must block) from 'audit row only,
    no Alpaca interaction' (safe to delete + re-submit on a re-run).
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM paper_trades WHERE idempotency_key = %s LIMIT 1",
            (idempotency_key,),
        ).fetchone()
    return dict(row) if row else None


def get_paper_trade_by_signal_strategy(
    ticker: str,
    signal_date: date,
    strategy: str,
) -> dict | None:
    """Return the most protective paper row for a ticker/date/strategy.

    This guards same-day reruns across historical idempotency key formats.
    Broker-linked rows win because they represent real external state.
    """
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM paper_trades
            WHERE ticker = %s
              AND signal_date = %s
              AND strategy = %s
            ORDER BY
                CASE WHEN alpaca_order_id IS NOT NULL THEN 0 ELSE 1 END,
                created_at DESC,
                id DESC
            LIMIT 1
            """,
            (ticker, signal_date, strategy),
        ).fetchone()
    return dict(row) if row else None


def delete_paper_trade_by_key(idempotency_key: str) -> bool:
    """Delete a paper_trade row IFF it has never reached Alpaca (alpaca_order_id IS NULL).

    Returns True if a row was deleted, False otherwise. The WHERE clause is the
    safety: rows with an alpaca_order_id reflect real broker state and must
    never be deleted (would orphan the broker order from our records).
    """
    with get_connection() as conn:
        result = conn.execute(
            "DELETE FROM paper_trades WHERE idempotency_key = %s AND alpaca_order_id IS NULL",
            (idempotency_key,),
        )
        conn.commit()
        return result.rowcount > 0


def upsert_paper_trade(trade: PaperTrade) -> int:
    """INSERT-or-UPDATE keyed by idempotency_key. Returns the row id.

    Used by graph.py REJECTED branch and any path that wants the audit row to
    reflect the LATEST signal state for (ticker, signal_date) regardless of
    prior runs that day. NEVER use this from the Alpaca submission path —
    upserting over a row with a live alpaca_order_id would silently lose the
    broker linkage. Use insert_paper_trade for that path.
    """
    with get_connection() as conn:
        row = conn.execute(
            """
            INSERT INTO paper_trades (
                recommendation_id, ticker, signal_date, strategy,
                idempotency_key, decision, side, entry_limit, stop_loss,
                take_profit, notional_pct,
                alpaca_order_id, status, status_reason, submitted_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (idempotency_key) DO UPDATE SET
                recommendation_id = EXCLUDED.recommendation_id,
                strategy = EXCLUDED.strategy,
                decision = EXCLUDED.decision,
                side = EXCLUDED.side,
                entry_limit = EXCLUDED.entry_limit,
                stop_loss = EXCLUDED.stop_loss,
                take_profit = EXCLUDED.take_profit,
                notional_pct = EXCLUDED.notional_pct,
                status = EXCLUDED.status,
                status_reason = EXCLUDED.status_reason,
                submitted_at = EXCLUDED.submitted_at
            RETURNING id
            """,
            (
                trade.recommendation_id,
                trade.ticker, trade.signal_date, trade.strategy, trade.idempotency_key,
                trade.decision.value, trade.side,
                trade.entry_limit, trade.stop_loss, trade.take_profit,
                trade.notional_pct, trade.alpaca_order_id,
                trade.status.value, trade.status_reason, trade.submitted_at,
            ),
        ).fetchone()
        conn.commit()
    return row["id"]


def update_paper_trade_status(
    paper_trade_id: int,
    status: PaperTradeStatus,
    status_reason: str | None = None,
    alpaca_order_id: str | None = None,
) -> None:
    """Update a paper_trade row's status and optional metadata."""
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE paper_trades
            SET status = %s,
                status_reason = COALESCE(%s, status_reason),
                alpaca_order_id = COALESCE(%s, alpaca_order_id)
            WHERE id = %s
            """,
            (status.value, status_reason, alpaca_order_id, paper_trade_id),
        )
        conn.commit()


def get_active_paper_trades() -> list[dict]:
    """Reconciler hot path: pending or submitted trades that still need polling."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM paper_trades
            WHERE status IN ('pending', 'submitted')
            """,
        ).fetchall()
    return [dict(r) for r in rows]


def save_paper_fill(fill: PaperFill) -> None:
    """Idempotent on alpaca_fill_id (UNIQUE constraint)."""
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO paper_fills (
                paper_trade_id, strategy, alpaca_fill_id, side, qty, price, filled_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (alpaca_fill_id) DO NOTHING
            """,
            (fill.paper_trade_id, fill.strategy, fill.alpaca_fill_id, fill.side,
             fill.qty, fill.price, fill.filled_at),
        )
        conn.commit()


def upsert_paper_position(position: PaperPosition) -> None:
    """Insert or update a paper_positions row keyed by paper_trade_id."""
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO paper_positions (
                paper_trade_id, strategy, qty, avg_entry, current_price,
                unrealized_pnl, realized_pnl, closed_at, close_reason
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (paper_trade_id) DO UPDATE SET
                qty = EXCLUDED.qty,
                avg_entry = EXCLUDED.avg_entry,
                current_price = EXCLUDED.current_price,
                unrealized_pnl = EXCLUDED.unrealized_pnl,
                realized_pnl = EXCLUDED.realized_pnl,
                closed_at = COALESCE(EXCLUDED.closed_at, paper_positions.closed_at),
                close_reason = COALESCE(EXCLUDED.close_reason, paper_positions.close_reason),
                updated_at = NOW()
            """,
            (position.paper_trade_id, position.strategy, position.qty, position.avg_entry,
             position.current_price, position.unrealized_pnl,
             position.realized_pnl, position.closed_at, position.close_reason),
        )
        conn.commit()


def get_open_paper_positions() -> list[dict]:
    """Open positions = no closed_at. Used by reconciler price-update loop."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT pt.ticker, pt.signal_date, pt.recommendation_id, pt.strategy,
                   pt.side, pt.id as paper_trade_id, s.sector,
                   pp.qty, pp.avg_entry, pp.current_price, pp.unrealized_pnl
            FROM paper_positions pp
            JOIN paper_trades pt ON pp.paper_trade_id = pt.id
            LEFT JOIN signals s ON s.ticker = pt.ticker AND s.date = pt.signal_date
            WHERE pp.closed_at IS NULL
            """,
        ).fetchall()
    return [dict(r) for r in rows]


def get_paper_trades_by_date(signal_date: date) -> list[dict]:
    """For dashboard: all paper trades for a given signal date, joined with fills.

    Wave 1.5: returns 3 rows per signal (one per strategy) instead of 1.
    Dashboard route groups by (ticker, signal_date) to render 3 chips per row.
    Sort: ticker first, then strategy in canonical AGG/BAL/CON order.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT pt.*, pp.realized_pnl, pp.unrealized_pnl, pp.closed_at, pp.close_reason
            FROM paper_trades pt
            LEFT JOIN paper_positions pp ON pp.paper_trade_id = pt.id
            WHERE pt.signal_date = %s
            ORDER BY pt.ticker,
                     CASE pt.strategy
                        WHEN 'aggressive' THEN 1
                        WHEN 'balanced' THEN 2
                        WHEN 'conservative' THEN 3
                        ELSE 4
                     END
            """,
            (signal_date,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_metrics_counters() -> dict[str, int | float]:
    """Counters exposed at /api/metrics. Lifetime totals + per-strategy labels (E13).

    Wave 1.5: keeps the legacy unlabeled counters for backwards-compat with any
    external scrapers. Adds per-strategy `_<strategy>_total` suffixes for
    debugging when one strategy goes silent.
    """
    with get_connection() as conn:
        # Lifetime aggregate (legacy compat)
        row = conn.execute(
            """
            SELECT
                COUNT(*) FILTER (WHERE status = 'submitted') as submitted,
                COUNT(*) FILTER (WHERE status = 'filled') as filled,
                COUNT(*) FILTER (WHERE status = 'failed') as failed,
                COUNT(*) FILTER (WHERE status = 'execution_skipped') as skipped,
                COUNT(*) FILTER (WHERE status = 'unfilled_eod') as unfilled_eod
            FROM paper_trades
            """,
        ).fetchone()
        # Per-strategy breakdown (E13)
        per_strategy_rows = conn.execute(
            """
            SELECT
                strategy,
                COUNT(*) FILTER (WHERE status = 'submitted') as submitted,
                COUNT(*) FILTER (WHERE status = 'filled') as filled,
                COUNT(*) FILTER (WHERE status = 'failed') as failed,
                COUNT(*) FILTER (WHERE status = 'execution_skipped') as skipped,
                COUNT(*) FILTER (WHERE status = 'unfilled_eod') as unfilled_eod
            FROM paper_trades
            GROUP BY strategy
            """,
        ).fetchall()
        pnl_row = conn.execute(
            """
            SELECT COALESCE(SUM(realized_pnl), 0) as realized,
                   COALESCE(SUM(unrealized_pnl), 0) as unrealized
            FROM paper_positions
            """,
        ).fetchone()
        per_strategy_pnl_rows = conn.execute(
            """
            SELECT strategy,
                   COALESCE(SUM(realized_pnl), 0) as realized,
                   COALESCE(SUM(unrealized_pnl), 0) as unrealized
            FROM paper_positions
            GROUP BY strategy
            """,
        ).fetchall()
        # Drawdown breaker pause counts (lifetime)
        pause_rows = conn.execute(
            """
            SELECT strategy, COUNT(*) as n FROM strategy_pause_state GROUP BY strategy
            """,
        ).fetchall()

    counters: dict[str, int | float] = {
        "paper_trades_submitted_total": row["submitted"] or 0,
        "paper_trades_filled_total": row["filled"] or 0,
        "paper_trades_failed_total": row["failed"] or 0,
        "paper_trades_skipped_total": row["skipped"] or 0,
        "paper_trades_unfilled_eod_total": row["unfilled_eod"] or 0,
        "realized_pnl_total": float(pnl_row["realized"] or 0),
        "unrealized_pnl_total": float(pnl_row["unrealized"] or 0),
    }
    for r in per_strategy_rows:
        s = r["strategy"]
        counters[f"paper_trades_submitted_{s}_total"] = r["submitted"] or 0
        counters[f"paper_trades_filled_{s}_total"] = r["filled"] or 0
        counters[f"paper_trades_failed_{s}_total"] = r["failed"] or 0
        counters[f"paper_trades_skipped_{s}_total"] = r["skipped"] or 0
        counters[f"paper_trades_unfilled_eod_{s}_total"] = r["unfilled_eod"] or 0
    for r in per_strategy_pnl_rows:
        s = r["strategy"]
        counters[f"realized_pnl_{s}_total"] = float(r["realized"] or 0)
        counters[f"unrealized_pnl_{s}_total"] = float(r["unrealized"] or 0)
    for r in pause_rows:
        counters[f"strategy_paused_{r['strategy']}_total"] = r["n"] or 0
    # A7: thesis-build status + LLM failure-reason breakdowns. Lifetime totals
    # so /api/metrics scrapers see the same shape as the paper-trade counters.
    try:
        counters.update(get_option_thesis_metrics())
    except Exception as exc:
        logger.warning(f"get_option_thesis_metrics failed (counters skipped): {exc}")
    # A4: TWS gateway gauge. Read the most recent transition's payload so
    # /canary post-deploy gets a fresh signal within one heartbeat scrape
    # window. 0 when no probe has run yet — safe-default surface area.
    try:
        counters["tws_gateway_up"] = get_tws_gateway_up_gauge()
    except Exception as exc:
        logger.warning(f"tws_gateway_up gauge skipped: {exc}")
        counters["tws_gateway_up"] = 0
    # Decision 26: cockpit view + thesis feedback counters. Lifetime totals
    # so retro queries can answer "did anyone actually open this panel?"
    # without external analytics.
    try:
        counters.update(get_cockpit_view_metrics())
    except Exception as exc:
        logger.warning(f"get_cockpit_view_metrics skipped: {exc}")
    try:
        counters.update(get_option_thesis_feedback_metrics())
    except Exception as exc:
        logger.warning(f"get_option_thesis_feedback_metrics skipped: {exc}")
    return counters


def get_today_execution_stats(target_date: date) -> dict[str, int | float]:
    """Dashboard summary panel: today-only stats."""
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*) FILTER (WHERE status = 'submitted') as submitted,
                COUNT(*) FILTER (WHERE status = 'filled') as filled,
                COUNT(*) FILTER (WHERE status = 'failed') as failed,
                COUNT(*) FILTER (WHERE status = 'unfilled_eod') as unfilled
            FROM paper_trades
            WHERE signal_date = %s
            """,
            (target_date,),
        ).fetchone()
        open_row = conn.execute(
            """
            SELECT COUNT(*) as cnt,
                   COALESCE(SUM(unrealized_pnl), 0) as unrealized
            FROM paper_positions pp
            JOIN paper_trades pt ON pp.paper_trade_id = pt.id
            WHERE pt.signal_date = %s AND pp.closed_at IS NULL
            """,
            (target_date,),
        ).fetchone()
        realized_row = conn.execute(
            """
            SELECT COALESCE(SUM(realized_pnl), 0) as realized
            FROM paper_positions pp
            JOIN paper_trades pt ON pp.paper_trade_id = pt.id
            WHERE pt.signal_date = %s
            """,
            (target_date,),
        ).fetchone()
    return {
        "submitted": row["submitted"] or 0,
        "filled": row["filled"] or 0,
        "failed": row["failed"] or 0,
        "unfilled": row["unfilled"] or 0,
        "open_positions": open_row["cnt"] or 0,
        "unrealized_pnl": float(open_row["unrealized"] or 0),
        "realized_pnl": float(realized_row["realized"] or 0),
    }


# ─── Wave 1.5: Equity snapshots ────────────────────────

def insert_equity_snapshot(snapshot: EquitySnapshot) -> int | None:
    """Insert one nightly snapshot. UNIQUE (strategy, snapshot_date) prevents
    duplicates if the cron fires twice. Returns id on insert, None on conflict.
    """
    with get_connection() as conn:
        row = conn.execute(
            """
            INSERT INTO paper_equity_snapshots (
                strategy, snapshot_date, account_equity, cash, positions_value, daily_pnl
            ) VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (strategy, snapshot_date) DO NOTHING
            RETURNING id
            """,
            (snapshot.strategy, snapshot.snapshot_date, snapshot.account_equity,
             snapshot.cash, snapshot.positions_value, snapshot.daily_pnl),
        ).fetchone()
        conn.commit()
    return row["id"] if row else None


def get_recent_snapshots(strategy: str, limit: int = 30) -> list[dict]:
    """Last N snapshots for a strategy, newest first. Used by drawdown breaker
    (rolling 30-day peak) and the dashboard sparkline (last 30 days)."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM paper_equity_snapshots
            WHERE strategy = %s
            ORDER BY snapshot_date DESC
            LIMIT %s
            """,
            (strategy, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_latest_snapshot(strategy: str) -> dict | None:
    """Most recent snapshot for a strategy, or None if no snapshots yet."""
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT * FROM paper_equity_snapshots
            WHERE strategy = %s
            ORDER BY snapshot_date DESC
            LIMIT 1
            """,
            (strategy,),
        ).fetchone()
    return dict(row) if row else None


def get_strategy_summary_stats(window_days: int = 30) -> dict[str, dict]:
    """Dashboard E3 summary panel: per-strategy P&L (today/week/all-time) +
    sparkline data (last 30 days). Single round-trip via aggregate query.

    Returns: {strategy_name: {today_pnl, week_pnl, all_time_pnl,
                              equity_curve: [(date, equity), ...],
                              snapshot_count}}
    """
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            WITH ranked AS (
                SELECT strategy, snapshot_date, account_equity, daily_pnl,
                       ROW_NUMBER() OVER (PARTITION BY strategy ORDER BY snapshot_date DESC) as rn
                FROM paper_equity_snapshots
            )
            SELECT
                strategy,
                COALESCE(SUM(daily_pnl) FILTER (WHERE snapshot_date = current_date), 0) as today_pnl,
                COALESCE(SUM(daily_pnl) FILTER (WHERE snapshot_date >= current_date - 7), 0) as week_pnl,
                COALESCE(SUM(daily_pnl), 0) as all_time_pnl,
                COUNT(*) FILTER (WHERE snapshot_date >= current_date - {window_days}) as window_count,
                COUNT(*) as snapshot_count
            FROM ranked
            GROUP BY strategy
            """,
        ).fetchall()
        # Fetch equity curves per strategy separately (small N — 30 rows × 3 strategies)
        curve_rows = conn.execute(
            f"""
            SELECT strategy, snapshot_date, account_equity
            FROM paper_equity_snapshots
            WHERE snapshot_date >= current_date - {window_days}
            ORDER BY strategy, snapshot_date ASC
            """,
        ).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        s = r["strategy"]
        out[s] = {
            "today_pnl": float(r["today_pnl"] or 0),
            "week_pnl": float(r["week_pnl"] or 0),
            "all_time_pnl": float(r["all_time_pnl"] or 0),
            "equity_curve": [],
            "snapshot_count": r["snapshot_count"] or 0,
        }
    for r in curve_rows:
        s = r["strategy"]
        if s in out:
            out[s]["equity_curve"].append((r["snapshot_date"], float(r["account_equity"])))
    return out


# ─── Wave 1.5: Strategy pause state (drawdown breaker) ───

def get_strategy_pause(strategy: str) -> dict | None:
    """Current pause-state row for a strategy, or None if not paused."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM strategy_pause_state WHERE strategy = %s LIMIT 1",
            (strategy,),
        ).fetchone()
    return dict(row) if row else None


def insert_pause_if_absent(
    strategy: str,
    reason: str,
    paused_drawdown: float | None = None,
    unpause_after: datetime | None = None,
) -> bool:
    """INSERT ... ON CONFLICT DO NOTHING + RETURNING. Returns True iff this
    call inserted the row (caller should log circuit_breaker exactly once).
    Concurrent callers all see False except for the one that won the race.

    NEW5 fix from CEO plan spec review: prevents duplicate
    log_circuit_breaker fires when async fan-out hits the threshold from
    multiple signals in the same tick.
    """
    with get_connection() as conn:
        row = conn.execute(
            """
            INSERT INTO strategy_pause_state (
                strategy, paused_reason, paused_drawdown, unpause_after
            ) VALUES (%s, %s, %s, %s)
            ON CONFLICT (strategy) DO NOTHING
            RETURNING strategy
            """,
            (strategy, reason, paused_drawdown, unpause_after),
        ).fetchone()
        conn.commit()
    return row is not None


def delete_strategy_pause(strategy: str) -> bool:
    """Manual unpause. Returns True if a row was deleted."""
    with get_connection() as conn:
        result = conn.execute(
            "DELETE FROM strategy_pause_state WHERE strategy = %s",
            (strategy,),
        )
        conn.commit()
        return result.rowcount > 0


# ─── Wave 2 prep: LLM call telemetry ──────────────────────

def record_llm_call(call: LLMCallTelemetry) -> None:
    """Fail-open insert. Telemetry MUST NEVER break an LLM call — if the DB
    is unreachable or the schema drifts, log and move on. Caller invokes this
    inside a finally block.
    """
    try:
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO llm_calls (
                    run_id, ticker, stage, provider, model, attempt,
                    prompt_len, output_len, elapsed_seconds, done_reason,
                    eval_count, parse_ok, fallback_used, error
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    call.run_id, call.ticker, call.stage, call.provider, call.model,
                    call.attempt, call.prompt_len, call.output_len, call.elapsed_seconds,
                    call.done_reason, call.eval_count, call.parse_ok,
                    call.fallback_used, call.error,
                ),
            )
            conn.commit()
    except Exception as exc:
        logger.warning(f"record_llm_call failed (telemetry skipped): {exc}")


def get_llm_call_stats(window_hours: int = 24) -> list[dict]:
    """Per (model, stage) reliability summary over the last N hours.

    Drives the dashboard "LLM Reliability" panel. Computes valid_pct,
    avg_latency, avg_output_len, fallback_pct, total_calls.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                model,
                COALESCE(stage, '(unknown)') as stage,
                COUNT(*) as total,
                SUM(CASE WHEN parse_ok THEN 1 ELSE 0 END) as parsed,
                SUM(CASE WHEN fallback_used THEN 1 ELSE 0 END) as fallbacks,
                AVG(elapsed_seconds)::REAL as avg_latency_s,
                AVG(output_len)::REAL as avg_output_len,
                MAX(created_at) as last_call_at
            FROM llm_calls
            WHERE created_at >= NOW() - (%s || ' hours')::INTERVAL
            GROUP BY model, stage
            ORDER BY model, stage
            """,
            (str(window_hours),),
        ).fetchall()
    return [dict(r) for r in rows]


# ─── Wave 2: option_refresh_jobs CRUD (A3) ─────────────

def acquire_option_refresh_job(
    *,
    job_id: str,
    source: OptionRefreshJobSource,
    total: int | None = None,
) -> OptionRefreshJob | None:
    """Atomically claim the single-running slot. Returns the inserted job
    when claim succeeds, None when another job is already running.

    Serialization is enforced by `idx_option_refresh_jobs_one_running`, a
    partial unique index on `status='running'`. ON CONFLICT DO NOTHING
    short-circuits to no-row when the slot is taken.
    """
    with get_connection() as conn:
        row = conn.execute(
            """
            INSERT INTO option_refresh_jobs (job_id, status, source, total)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING job_id, status, source, started_at, completed_at,
                      total, completed, failures
            """,
            (job_id, OptionRefreshJobStatus.RUNNING.value, source.value, total),
        ).fetchone()
        conn.commit()
    if row is None:
        return None
    return _row_to_option_refresh_job(row)


def update_option_refresh_job_progress(job_id: str, completed: int) -> None:
    """Bump the per-ticker progress counter while the job is still running."""
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE option_refresh_jobs
               SET completed = %s
             WHERE job_id = %s AND status = %s
            """,
            (completed, job_id, OptionRefreshJobStatus.RUNNING.value),
        )
        conn.commit()


def complete_option_refresh_job(
    job_id: str,
    *,
    completed: int,
    failures: list[OptionRefreshJobFailure] | None = None,
) -> None:
    """Mark the job COMPLETED. Per-ticker failures are still recorded when
    the overall job succeeded — caller may have salvaged partial results.
    """
    failures_json = json.dumps([f.model_dump() for f in (failures or [])])
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE option_refresh_jobs
               SET status = %s,
                   completed = %s,
                   failures = %s::jsonb,
                   completed_at = NOW()
             WHERE job_id = %s
            """,
            (
                OptionRefreshJobStatus.COMPLETED.value,
                completed,
                failures_json,
                job_id,
            ),
        )
        conn.commit()


def fail_option_refresh_job(
    job_id: str,
    *,
    error_class: str,
    message: str,
) -> None:
    """Mark the job FAILED. Records a single global failure entry; per-ticker
    failures use the same `failures` JSONB column with explicit ticker keys.
    """
    failures_json = json.dumps([{"ticker": "*", "error_class": error_class, "message": message}])
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE option_refresh_jobs
               SET status = %s,
                   failures = %s::jsonb,
                   completed_at = NOW()
             WHERE job_id = %s
            """,
            (OptionRefreshJobStatus.FAILED.value, failures_json, job_id),
        )
        conn.commit()


def get_option_refresh_job(job_id: str) -> OptionRefreshJob | None:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT job_id, status, source, started_at, completed_at,
                   total, completed, failures
              FROM option_refresh_jobs
             WHERE job_id = %s
            """,
            (job_id,),
        ).fetchone()
    if row is None:
        return None
    return _row_to_option_refresh_job(row)


def get_running_option_refresh_job() -> OptionRefreshJob | None:
    """Returns the currently-running job, if any. The partial unique index
    guarantees ≤1 row matches.
    """
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT job_id, status, source, started_at, completed_at,
                   total, completed, failures
              FROM option_refresh_jobs
             WHERE status = %s
             LIMIT 1
            """,
            (OptionRefreshJobStatus.RUNNING.value,),
        ).fetchone()
    if row is None:
        return None
    return _row_to_option_refresh_job(row)


def get_latest_option_refresh_job() -> dict | None:
    """Return the most-recent refresh job row (any status) as a plain dict
    so the FastAPI handler can stream it directly. None when there has
    never been a refresh.
    """
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT job_id, status, source, started_at, completed_at,
                   total, completed, failures
              FROM option_refresh_jobs
             ORDER BY started_at DESC
             LIMIT 1
            """
        ).fetchone()
    if row is None:
        return None
    job = _row_to_option_refresh_job(row)
    return job.model_dump(mode="json")


def cleanup_zombie_option_refresh_jobs(stale_after_minutes: int = 10) -> int:
    """Mark `running` jobs older than the threshold as `failed`. Run on
    process startup so a uvicorn restart mid-job doesn't leave the slot
    permanently locked. Returns the number of rows transitioned.

    Default is 10 minutes per plan rev 4 A3. The full-50-ticker IBKR
    refresh budget is well under that bound, so any row that exceeds it
    is a crashed worker, not a slow one.
    """
    failures_json = json.dumps([
        {"ticker": "*", "error_class": "ZombieJob", "message": f"running > {stale_after_minutes}m, marked failed on startup"}
    ])
    with get_connection() as conn:
        cur = conn.execute(
            """
            UPDATE option_refresh_jobs
               SET status = %s,
                   failures = %s::jsonb,
                   completed_at = NOW()
             WHERE status = %s
               AND started_at < NOW() - (%s || ' minutes')::INTERVAL
            """,
            (
                OptionRefreshJobStatus.FAILED.value,
                failures_json,
                OptionRefreshJobStatus.RUNNING.value,
                str(stale_after_minutes),
            ),
        )
        conn.commit()
        return cur.rowcount or 0


def _row_to_option_refresh_job(row: dict) -> OptionRefreshJob:
    failures_raw = row.get("failures") or []
    if isinstance(failures_raw, str):
        failures_raw = json.loads(failures_raw)
    return OptionRefreshJob(
        job_id=row["job_id"],
        status=OptionRefreshJobStatus(row["status"]),
        source=OptionRefreshJobSource(row["source"]),
        started_at=row.get("started_at"),
        completed_at=row.get("completed_at"),
        total=row.get("total"),
        completed=row.get("completed", 0),
        failures=[OptionRefreshJobFailure(**f) for f in failures_raw],
    )


# ─── Wave 2: option_iv_history CRUD (A6 + P2) ──────────

# A5 cold-start gate: rank computed only when we have ≥60 days of prior
# atm_iv_30d. Below that, iv_label='insufficient' and iv_rank_30d=NULL.
_IV_RANK_MIN_DAYS = 60
_IV_RANK_LOOKBACK_DAYS = 252
_IV_LABEL_CHEAP_MAX = 0.30
_IV_LABEL_RICH_MIN = 0.70


def _label_for_iv_rank(rank: float | None) -> OptionIVLabel:
    if rank is None:
        return OptionIVLabel.INSUFFICIENT
    if rank <= _IV_LABEL_CHEAP_MAX:
        return OptionIVLabel.CHEAP
    if rank >= _IV_LABEL_RICH_MIN:
        return OptionIVLabel.RICH
    return OptionIVLabel.FAIR


def _compute_iv_rank_30d(prior_atm_iv: list[float], current_atm_iv: float) -> float | None:
    """Average-rank percentile of `current_atm_iv` against `prior_atm_iv`.

    Mirrors K4's average-rank fix in options/features.py — ties get the mean
    of their tied positions, removing the upper-rank bias. Returns None when
    fewer than _IV_RANK_MIN_DAYS prior observations are available.
    """
    eligible = [v for v in prior_atm_iv if v is not None]
    if len(eligible) < _IV_RANK_MIN_DAYS:
        return None
    population = eligible + [current_atm_iv]
    sorted_pop = sorted(population)
    n = len(sorted_pop)
    rank_buckets: dict[float, list[int]] = {}
    for index, value in enumerate(sorted_pop, start=1):
        rank_buckets.setdefault(value, []).append(index)
    average_rank = sum(rank_buckets[current_atm_iv]) / len(rank_buckets[current_atm_iv])
    if n <= 1:
        return None
    return round((average_rank - 1) / (n - 1), 4)


def insert_option_iv_history(
    history: OptionIVHistory,
    *,
    compute_rank: bool = True,
) -> OptionIVHistory:
    """Insert one (ticker, captured_at) IV row. When `compute_rank` is True
    and `iv_rank_30d` is not pre-set, ranks `atm_iv_30d` against the prior
    252 days of history for the same ticker. Idempotent: ON CONFLICT updates
    the rank/label so reruns recompute cleanly.

    Returns the model with `iv_rank_30d` and `iv_label` populated as written.
    """
    rank = history.iv_rank_30d
    label = history.iv_label
    if compute_rank and rank is None and history.atm_iv_30d is not None:
        prior = _fetch_prior_atm_iv_30d(history.ticker, history.captured_at)
        rank = _compute_iv_rank_30d(prior, history.atm_iv_30d)
        label = _label_for_iv_rank(rank)
    elif label is None:
        label = _label_for_iv_rank(rank)

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO option_iv_history (
                ticker, captured_at, underlying_price,
                atm_iv_30d, atm_iv_60d, atm_iv_90d,
                term_structure_30_60, term_structure_60_90,
                iv_rank_30d, iv_label
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (ticker, captured_at) DO UPDATE SET
                underlying_price = EXCLUDED.underlying_price,
                atm_iv_30d = EXCLUDED.atm_iv_30d,
                atm_iv_60d = EXCLUDED.atm_iv_60d,
                atm_iv_90d = EXCLUDED.atm_iv_90d,
                term_structure_30_60 = EXCLUDED.term_structure_30_60,
                term_structure_60_90 = EXCLUDED.term_structure_60_90,
                iv_rank_30d = EXCLUDED.iv_rank_30d,
                iv_label = EXCLUDED.iv_label
            """,
            (
                history.ticker, history.captured_at, history.underlying_price,
                history.atm_iv_30d, history.atm_iv_60d, history.atm_iv_90d,
                history.term_structure_30_60, history.term_structure_60_90,
                rank, label.value if label else None,
            ),
        )
        conn.commit()

    return history.model_copy(update={"iv_rank_30d": rank, "iv_label": label})


def _fetch_prior_atm_iv_30d(ticker: str, before: datetime) -> list[float]:
    """Pulls up to _IV_RANK_LOOKBACK_DAYS prior atm_iv_30d values strictly
    before `before`, NULL-filtered, oldest-first.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT atm_iv_30d
              FROM option_iv_history
             WHERE ticker = %s
               AND captured_at < %s
               AND atm_iv_30d IS NOT NULL
             ORDER BY captured_at DESC
             LIMIT %s
            """,
            (ticker, before, _IV_RANK_LOOKBACK_DAYS),
        ).fetchall()
    return [r["atm_iv_30d"] for r in rows]


def get_option_iv_history(ticker: str, *, limit: int = 252) -> list[OptionIVHistory]:
    """Newest-first IV history for a ticker, capped at `limit` rows."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT ticker, captured_at, underlying_price,
                   atm_iv_30d, atm_iv_60d, atm_iv_90d,
                   term_structure_30_60, term_structure_60_90,
                   iv_rank_30d, iv_label
              FROM option_iv_history
             WHERE ticker = %s
             ORDER BY captured_at DESC
             LIMIT %s
            """,
            (ticker, limit),
        ).fetchall()
    return [
        OptionIVHistory(
            ticker=r["ticker"],
            captured_at=r["captured_at"],
            underlying_price=r["underlying_price"],
            atm_iv_30d=r["atm_iv_30d"],
            atm_iv_60d=r["atm_iv_60d"],
            atm_iv_90d=r["atm_iv_90d"],
            term_structure_30_60=r["term_structure_30_60"],
            term_structure_60_90=r["term_structure_60_90"],
            iv_rank_30d=r["iv_rank_30d"],
            iv_label=OptionIVLabel(r["iv_label"]) if r["iv_label"] else None,
        )
        for r in rows
    ]


def count_option_iv_history_days(ticker: str) -> int:
    """Distinct calendar-day count for the ticker. Cold-start gate (A5)
    callers compare against `_IV_RANK_MIN_DAYS` to decide whether a ticker
    is eligible for the screener.
    """
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT COUNT(DISTINCT captured_at::date) AS days
              FROM option_iv_history
             WHERE ticker = %s
            """,
            (ticker,),
        ).fetchone()
    return int(row["days"]) if row and row.get("days") is not None else 0


# A5 cold-start gate: the IV-rank screener (D3) MUST exclude tickers with
# `iv_label='insufficient'`. Surfacing them would invite acting on a rank
# computed from <60 days of history — exactly the statistical hygiene
# failure mode A5 was added to prevent. Two helpers below:
#  - `get_iv_screener_rows`: latest row per ticker, gated rows excluded.
#  - `get_iv_cold_start_tickers`: explicit list of excluded tickers so the
#     dashboard can show "X tickers waiting on history" instead of silently
#     dropping them.
def get_iv_screener_rows(
    *,
    labels: list[OptionIVLabel] | None = None,
    tickers: list[str] | None = None,
    limit: int = 100,
) -> list[OptionIVHistory]:
    """Latest IV row per ticker, with the A5 cold-start gate applied.

    Rows where `iv_label='insufficient'` are excluded unconditionally —
    do NOT pass `OptionIVLabel.INSUFFICIENT` in `labels` to override; use
    `get_iv_cold_start_tickers` if you need to enumerate the excluded set.
    """
    label_values = (
        [lbl.value for lbl in labels if lbl != OptionIVLabel.INSUFFICIENT]
        if labels is not None
        else None
    )
    ticker_filter = [t.upper() for t in (tickers or []) if t]

    clauses = ["iv_label IS NOT NULL", "iv_label <> %s"]
    params: list = [OptionIVLabel.INSUFFICIENT.value]
    if label_values is not None:
        clauses.append("iv_label = ANY(%s)")
        params.append(label_values)
    if ticker_filter:
        clauses.append("ticker = ANY(%s)")
        params.append(ticker_filter)
    where = " AND ".join(clauses)
    params.append(limit)

    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT DISTINCT ON (ticker)
                   ticker, captured_at, underlying_price,
                   atm_iv_30d, atm_iv_60d, atm_iv_90d,
                   term_structure_30_60, term_structure_60_90,
                   iv_rank_30d, iv_label
              FROM option_iv_history
             WHERE {where}
             ORDER BY ticker, captured_at DESC
             LIMIT %s
            """,
            tuple(params),
        ).fetchall()
    return [
        OptionIVHistory(
            ticker=r["ticker"],
            captured_at=r["captured_at"],
            underlying_price=r["underlying_price"],
            atm_iv_30d=r["atm_iv_30d"],
            atm_iv_60d=r["atm_iv_60d"],
            atm_iv_90d=r["atm_iv_90d"],
            term_structure_30_60=r["term_structure_30_60"],
            term_structure_60_90=r["term_structure_60_90"],
            iv_rank_30d=r["iv_rank_30d"],
            iv_label=OptionIVLabel(r["iv_label"]) if r["iv_label"] else None,
        )
        for r in rows
    ]


def get_iv_cold_start_tickers() -> list[str]:
    """Tickers whose latest IV row is `iv_label='insufficient'` (A5 gate).

    Read by the dashboard "waiting on IV history" panel so cold-start
    exclusions are visible to the operator instead of silent drops.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT ON (ticker) ticker, iv_label
              FROM option_iv_history
             ORDER BY ticker, captured_at DESC
            """
        ).fetchall()
    return [r["ticker"] for r in rows if r["iv_label"] == OptionIVLabel.INSUFFICIENT.value]


# ─── Wave 2: option_protective_costs CRUD (D2) ─────────

def upsert_option_protective_cost(cost: OptionProtectiveCost) -> None:
    """Insert or update a (position_id, computed_at) protective-put cost row.

    The PK is (position_id, computed_at), so multiple snapshots per position
    accumulate over time. Latest-row reads use `get_latest_protective_costs`.
    """
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO option_protective_costs (
                position_id, contract_symbol, cost_per_share,
                cost_pct_of_position, delta, greeks_source, computed_at
            ) VALUES (%s, %s, %s, %s, %s, %s, COALESCE(%s, NOW()))
            ON CONFLICT (position_id, computed_at) DO UPDATE SET
                contract_symbol = EXCLUDED.contract_symbol,
                cost_per_share = EXCLUDED.cost_per_share,
                cost_pct_of_position = EXCLUDED.cost_pct_of_position,
                delta = EXCLUDED.delta,
                greeks_source = EXCLUDED.greeks_source
            """,
            (
                cost.position_id, cost.contract_symbol, cost.cost_per_share,
                cost.cost_pct_of_position, cost.delta,
                cost.greeks_source.value if cost.greeks_source else None,
                cost.computed_at,
            ),
        )
        conn.commit()


def get_latest_protective_costs(position_ids: list[int]) -> dict[int, OptionProtectiveCost]:
    """Latest cost per position for the given ids, keyed by position_id.

    Uses DISTINCT ON (position_id) ORDER BY position_id, computed_at DESC —
    one round trip, one row per position. Empty input returns {}.
    """
    if not position_ids:
        return {}
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT ON (position_id)
                   position_id, contract_symbol, cost_per_share,
                   cost_pct_of_position, delta, greeks_source, computed_at
              FROM option_protective_costs
             WHERE position_id = ANY(%s)
             ORDER BY position_id, computed_at DESC
            """,
            (position_ids,),
        ).fetchall()
    return {
        r["position_id"]: OptionProtectiveCost(
            position_id=r["position_id"],
            contract_symbol=r["contract_symbol"],
            cost_per_share=r["cost_per_share"],
            cost_pct_of_position=r["cost_pct_of_position"],
            delta=r["delta"],
            greeks_source=r["greeks_source"],
            computed_at=r["computed_at"],
        )
        for r in rows
    }


# ─── Wave 2: data_provider_events CRUD (decision 11 + A6) ─

def record_data_provider_event(event: DataProviderEvent) -> int | None:
    """Fail-open audit insert. Mirrors `record_llm_call` posture: a failed
    write must NEVER break the calling fetcher / gateway-monitor / IV-summary
    code path. Returns the new id, or None on failure.
    """
    event_type = (
        event.event_type.value
        if hasattr(event.event_type, "value")
        else str(event.event_type)
    )
    payload_json = json.dumps(event.payload or {})
    try:
        with get_connection() as conn:
            row = conn.execute(
                """
                INSERT INTO data_provider_events (
                    event_type, ticker, from_provider, to_provider,
                    reason, payload, occurred_at
                ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, COALESCE(%s, NOW()))
                RETURNING id
                """,
                (
                    event_type, event.ticker, event.from_provider, event.to_provider,
                    event.reason, payload_json, event.occurred_at,
                ),
            ).fetchone()
            conn.commit()
        return int(row["id"]) if row else None
    except Exception as exc:
        logger.warning(f"record_data_provider_event failed (audit skipped): {exc}")
        return None


# ─── Wave 2: option_thesis_attempts CRUD (A7) ──────────

def record_option_thesis_attempt(attempt: OptionThesisAttempt) -> int | None:
    """Fail-open per-attempt insert. A failed audit write must NEVER break
    the thesis pipeline — log and move on, mirroring `record_llm_call`.
    """
    reason = (
        attempt.llm_failure_reason.value
        if isinstance(attempt.llm_failure_reason, OptionThesisLLMFailureReason)
        else attempt.llm_failure_reason
    )
    try:
        with get_connection() as conn:
            row = conn.execute(
                """
                INSERT INTO option_thesis_attempts (
                    ticker, strategy, status, llm_failure_reason, elapsed_seconds, attempted_at
                ) VALUES (%s, %s, %s, %s, %s, COALESCE(%s, NOW()))
                RETURNING id
                """,
                (
                    attempt.ticker.upper(),
                    attempt.strategy,
                    attempt.status.value,
                    reason,
                    attempt.elapsed_seconds,
                    attempt.attempted_at,
                ),
            ).fetchone()
            conn.commit()
        return int(row["id"]) if row else None
    except Exception as exc:
        logger.warning(f"record_option_thesis_attempt failed (audit skipped): {exc}")
        return None


def get_option_thesis_metrics(window_hours: int | None = None) -> dict[str, int]:
    """Counters keyed for /api/metrics scrapers.

    Per plan A7 the failure-reason metric is `{strategy, reason}` labeled.
    The dict-key encoding is `option_thesis_llm_failure_reason_<strategy>_<reason>_total`
    so flat key/value scrapers see both dimensions without nested JSON.

    `window_hours=None` = lifetime totals (default). Pass a window for
    rate-style alerting (e.g. "structured_only in last 24h").
    """
    if window_hours is not None:
        window_filter = "AND attempted_at >= NOW() - (%s || ' hours')::INTERVAL"
        status_where = "WHERE attempted_at >= NOW() - (%s || ' hours')::INTERVAL"
        status_params: tuple = (str(window_hours),)
        reason_params: tuple = (OptionThesisStatus.STRUCTURED_ONLY.value, str(window_hours))
    else:
        window_filter = ""
        status_where = ""
        status_params = ()
        reason_params = (OptionThesisStatus.STRUCTURED_ONLY.value,)

    status_sql = f"""
        SELECT status, COUNT(*) AS n
          FROM option_thesis_attempts
          {status_where}
         GROUP BY status
    """
    reason_sql = f"""
        SELECT strategy, llm_failure_reason AS reason, COUNT(*) AS n
          FROM option_thesis_attempts
         WHERE status = %s
           {window_filter}
         GROUP BY strategy, llm_failure_reason
    """

    with get_connection() as conn:
        status_rows = conn.execute(status_sql, status_params).fetchall()
        reason_rows = conn.execute(reason_sql, reason_params).fetchall()

    counters: dict[str, int] = {
        f"option_thesis_{s.value}_total": 0 for s in OptionThesisStatus
    }
    for r in status_rows:
        counters[f"option_thesis_{r['status']}_total"] = int(r["n"] or 0)
    for r in reason_rows:
        strategy = r["strategy"] or "unknown"
        reason = r["reason"] or "unknown"
        counters[f"option_thesis_llm_failure_reason_{strategy}_{reason}_total"] = int(r["n"] or 0)
    return counters


# ─── Wave 2: option_thesis_cache CRUD (P1) ──────────

def upsert_option_thesis_cache(
    *,
    ticker: str,
    strategy: str,
    recommendation_id: str | None,
    chain_captured_at: datetime,
    structured_json: dict,
    narrative_text: str | None,
    llm_status: str,
) -> None:
    """Cache one thesis-build result. The (ticker, strategy, recommendation_id,
    chain_captured_at) tuple is the PK so re-running against the same chain
    snapshot UPDATEs in place instead of accumulating duplicates — but a NEW
    chain snapshot for the same ticker/strategy produces a new row, which is
    what enables the staleness check in `get_fresh_option_thesis_cache`.
    """
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO option_thesis_cache (
                ticker, strategy, recommendation_id, chain_captured_at,
                computed_at, structured_json, narrative_text, llm_status
            ) VALUES (%s, %s, %s, %s, NOW(), %s, %s, %s)
            ON CONFLICT (ticker, strategy, recommendation_id, chain_captured_at) DO UPDATE SET
                computed_at = NOW(),
                structured_json = EXCLUDED.structured_json,
                narrative_text = EXCLUDED.narrative_text,
                llm_status = EXCLUDED.llm_status
            """,
            (
                ticker.upper(),
                strategy,
                recommendation_id,
                chain_captured_at,
                json.dumps(structured_json),
                narrative_text,
                llm_status,
            ),
        )
        conn.commit()


def get_fresh_option_thesis_cache(
    *,
    ticker: str,
    strategy: str,
    recommendation_id: str | None,
) -> dict | None:
    """Return the cached thesis if `chain_captured_at` matches the latest
    snapshot for this ticker — otherwise None (caller must recompute).

    P1 invalidation rule: a fresh chain snapshot invalidates all cached
    theses for that ticker. We compare against the latest snapshot's
    captured_at instead of using a TTL because options data quality
    drifts unpredictably (gateway flips, fallback events) and recomputing
    on chain advancement keeps cache and reality in sync without
    operator intervention.
    """
    with get_connection() as conn:
        latest = conn.execute(
            """
            SELECT MAX(captured_at) AS captured_at
              FROM option_chain_snapshots
             WHERE ticker = %s
            """,
            (ticker.upper(),),
        ).fetchone()
        latest_captured_at = latest["captured_at"] if latest else None
        if latest_captured_at is None:
            return None
        row = conn.execute(
            """
            SELECT ticker, strategy, recommendation_id, chain_captured_at,
                   computed_at, structured_json, narrative_text, llm_status
              FROM option_thesis_cache
             WHERE ticker = %s
               AND strategy = %s
               AND recommendation_id IS NOT DISTINCT FROM %s
               AND chain_captured_at = %s
            """,
            (ticker.upper(), strategy, recommendation_id, latest_captured_at),
        ).fetchone()
    if not row:
        return None
    structured = row["structured_json"]
    if isinstance(structured, str):
        structured = json.loads(structured)
    return {
        "ticker": row["ticker"],
        "strategy": row["strategy"],
        "recommendation_id": row["recommendation_id"],
        "chain_captured_at": row["chain_captured_at"],
        "computed_at": row["computed_at"],
        "structured": structured,
        "narrative": row["narrative_text"],
        "llm_status": row["llm_status"],
    }


# ─── Decision 26: long-term-validation infra ──────────

def record_cockpit_view(panel: str) -> None:
    """Fail-open insert. A failed view-log row must NEVER break the
    dashboard — log and continue. Panels with low cardinality (~5 in
    Wave 2) keep the table small even at long retention.
    """
    if not panel:
        return
    try:
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO cockpit_view_log (panel) VALUES (%s)",
                (panel,),
            )
            conn.commit()
    except Exception as exc:
        logger.warning(f"record_cockpit_view failed (skipped): {exc}")


def get_cockpit_view_metrics() -> dict[str, int]:
    """Lifetime total per panel. Surfaces at /api/metrics as
    `cockpit_view_total_{panel}` for Prometheus-shaped scrapers.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT panel, COUNT(*) AS n
              FROM cockpit_view_log
             GROUP BY panel
            """
        ).fetchall()
    return {
        f"cockpit_view_total_{r['panel']}": int(r["n"] or 0)
        for r in rows
    }


def record_option_thesis_feedback(
    *,
    ticker: str,
    strategy: str,
    recommendation_id: str | None,
    sentiment: str,
) -> int | None:
    """Fail-open: a failed feedback insert never breaks the thumbs-click
    response. Returns the new row id on success, None on failure.

    `sentiment` is enforced to 'up' or 'down' here so a typo upstream
    doesn't pollute the metric. Anything else is dropped silently.
    """
    if sentiment not in ("up", "down"):
        logger.warning(f"record_option_thesis_feedback: bad sentiment {sentiment!r}")
        return None
    try:
        with get_connection() as conn:
            row = conn.execute(
                """
                INSERT INTO option_thesis_feedback (
                    ticker, strategy, recommendation_id, sentiment
                ) VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                (ticker.upper(), strategy, recommendation_id, sentiment),
            ).fetchone()
            conn.commit()
        return int(row["id"]) if row else None
    except Exception as exc:
        logger.warning(f"record_option_thesis_feedback failed: {exc}")
        return None


def get_option_thesis_feedback_metrics() -> dict[str, int]:
    """Lifetime + per-strategy thumbs-up/down breakdown for /api/metrics."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT strategy, sentiment, COUNT(*) AS n
              FROM option_thesis_feedback
             GROUP BY strategy, sentiment
            """
        ).fetchall()
    counters: dict[str, int] = {
        "option_thesis_feedback_up_total": 0,
        "option_thesis_feedback_down_total": 0,
    }
    for r in rows:
        counters[f"option_thesis_feedback_{r['sentiment']}_total"] = (
            counters.get(f"option_thesis_feedback_{r['sentiment']}_total", 0)
            + int(r["n"] or 0)
        )
        counters[f"option_thesis_feedback_{r['strategy']}_{r['sentiment']}_total"] = int(r["n"] or 0)
    return counters


def upsert_deploy_annotation(key: str, value: str) -> None:
    """Idempotent insert. The first call lands the value; subsequent calls
    leave the original timestamp intact. Used by wave_2_started_at so a
    uvicorn restart doesn't reset the wave-boundary marker.
    """
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO deploy_annotations (key, value)
            VALUES (%s, %s)
            ON CONFLICT (key) DO NOTHING
            """,
            (key, value),
        )
        conn.commit()


def get_deploy_annotation(key: str) -> dict | None:
    """Returns {value, recorded_at} for the given key, or None."""
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT value, recorded_at FROM deploy_annotations WHERE key = %s
            """,
            (key,),
        ).fetchone()
    if not row:
        return None
    return {"value": row["value"], "recorded_at": row["recorded_at"]}


def get_recent_data_provider_events(
    *,
    event_type: str | None = None,
    ticker: str | None = None,
    limit: int = 100,
) -> list[DataProviderEvent]:
    """Newest-first event log. Filterable by event_type and/or ticker so the
    /retro report can pull "all fetcher_fallback in last week" cheaply.
    """
    clauses = []
    params: list = []
    if event_type is not None:
        clauses.append("event_type = %s")
        params.append(event_type)
    if ticker is not None:
        clauses.append("ticker = %s")
        params.append(ticker)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT id, event_type, ticker, from_provider, to_provider,
                   reason, payload, occurred_at
              FROM data_provider_events
              {where}
             ORDER BY occurred_at DESC
             LIMIT %s
            """,
            tuple(params),
        ).fetchall()
    return [
        DataProviderEvent(
            id=r["id"],
            event_type=r["event_type"],
            ticker=r["ticker"],
            from_provider=r["from_provider"],
            to_provider=r["to_provider"],
            reason=r["reason"],
            payload=_decode_json_value(r.get("payload"), {}),
            occurred_at=r["occurred_at"],
        )
        for r in rows
    ]


def get_tws_gateway_up_gauge() -> int:
    """A4 gauge: 1 if the most recent gateway_state_change says 'up', else 0.

    Heartbeat emits state-change events only (not every probe), so the
    latest row is the current truth. Returns 0 when no probe has ever
    run — safe-default surface area.
    """
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT to_provider, payload
              FROM data_provider_events
             WHERE event_type = %s
             ORDER BY occurred_at DESC
             LIMIT 1
            """,
            (DataProviderEventType.GATEWAY_STATE_CHANGE.value,),
        ).fetchone()
    if row is None:
        return 0
    payload = _decode_json_value(row.get("payload"), {})
    if isinstance(payload, dict) and "up" in payload:
        return 1 if bool(payload["up"]) else 0
    return 1 if row.get("to_provider") == "up" else 0

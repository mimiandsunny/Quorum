from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    # API Keys
    openai_api_key: str = ""
    alpha_vantage_api_key: str = ""

    # Alpaca per-strategy creds (wave 1.5).
    # Balanced accepts the legacy ALPACA_API_KEY env var as a fallback so
    # wave-1 deploys keep working without env-var changes. _BALANCED takes
    # precedence when both are set.
    alpaca_api_key_balanced: str = Field(
        default="",
        validation_alias=AliasChoices("ALPACA_API_KEY_BALANCED", "ALPACA_API_KEY"),
    )
    alpaca_secret_key_balanced: str = Field(
        default="",
        validation_alias=AliasChoices("ALPACA_SECRET_KEY_BALANCED", "ALPACA_SECRET_KEY"),
    )
    alpaca_api_key_aggressive: str = ""
    alpaca_secret_key_aggressive: str = ""
    alpaca_api_key_conservative: str = ""
    alpaca_secret_key_conservative: str = ""
    alpaca_base_url: str = "https://paper-api.alpaca.markets"

    # Model routing
    analyst_mode: Literal["local", "cloud", "deterministic"] = "local"
    analyst_fallback: Literal["deterministic", "off"] = "deterministic"
    analyst_include_deterministic_baseline: bool = True
    analyst_comparison_log_enabled: bool = True
    analyst_comparison_log_dir: str = "logs"
    analyst_max_retries: int = 1
    regime_mode: Literal["local", "cloud", "deterministic"] = "local"
    regime_fallback: Literal["deterministic", "neutral", "off"] = "deterministic"
    regime_max_retries: int = 0
    local_provider: Literal["ollama", "llama_cpp"] = "ollama"
    local_model: str = "qwen3.6:27b"
    cloud_model: str = "gpt-5-nano"
    ollama_base_url: str = "http://localhost:11434"
    local_llm_timeout_seconds: int = 240
    local_llm_num_predict: int = 1800
    local_llm_think: bool = False
    llama_cpp_model_path: str = ""
    llama_cpp_chat_format: str = ""
    llama_cpp_n_ctx: int = 8192
    llama_cpp_n_gpu_layers: int = -1
    llama_cpp_n_threads: int = 0
    llama_cpp_verbose: bool = False
    analysts_parallel_workers: int = 1
    cloud_llm_timeout_seconds: int = 180
    researchers_parallel: bool = True
    ticker_timeout_seconds: int = 480

    # Ticker universe — Wave 2 expansion: 14 → 30. Cross-sectional IV-rank
    # screening (D3) needs breadth (5 tickers is not a screener per CEO plan
    # rev 2 decision Q3). Yfinance throttle is bounded by K5 inter-call
    # sleep; IBKR pacing stays well inside the 50/sec qualify budget.
    tickers: list[str] = Field(default=[
        # Indices
        "SPY", "QQQ", "IWM", "DIA",
        # Mega-cap tech
        "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "AVGO",
        # Growth + AI exposure
        "TSLA", "META", "ISRG", "AMD", "CRM", "ORCL",
        # Value / defensive
        "JPM", "JNJ", "BRK.B", "XOM", "WMT", "UNH",
        # Sector breadth
        "BAC", "CAT", "BA", "GE",
        # Volatile (signal + IV-rank testing)
        "COIN", "PLTR", "SHOP", "SMCI",
    ])

    # OV8 / plan rev 4: yfinance prototype phase runs options refresh
    # against a 10-ticker subset to avoid 429 cascades. Empty falls back
    # to the full `tickers` list (post-IBKR). Set in .env if needed.
    options_universe_subset: list[str] = Field(default=[
        "SPY", "QQQ", "AAPL", "MSFT", "NVDA",
        "TSLA", "META", "AMD", "COIN", "PLTR",
    ])

    # Risk manager thresholds
    min_confidence: float = 0.65
    max_stop_pct: float = 0.05
    max_hold_days: int = 10
    max_entry_zone_width_pct: float = 0.03
    min_reward_risk: float = 2.0

    # Position sizing tiers (confidence → portfolio %)
    size_tier_low: float = 0.02       # 0.65-0.75 confidence
    size_tier_mid: float = 0.03       # 0.75-0.85 confidence
    size_tier_high: float = 0.05      # 0.85+ confidence

    # Concentration risk
    max_sector_concentration: int = 3

    # Data pipeline
    price_history_days: int = 365
    database_url: str = "postgresql://localhost:5432/stockinvest"

    # Scheduler
    run_hour: int = 6
    run_minute: int = 30
    timezone: str = "America/New_York"

    # Multi-round debate (v1.2)
    debate_rounds: int = 2  # 1 = legacy single-shot; 2+ = with rebuttals

    # Paper trading (wave 1.5: 3 strategies via 3 Alpaca paper accounts)
    paper_trading_enabled: bool = False           # explicit opt-in
    paper_account_starting_value: float = 100_000 # used to size $/trade from notional_pct

    # Per-strategy notional fractions (read by agents/strategies.py STRATEGIES list)
    paper_notional_pct_aggressive: float = 0.030  # 3.0% per trade
    paper_notional_pct_balanced: float = 0.015    # 1.5% per trade (was the wave-1 default)
    paper_notional_pct_conservative: float = 0.005  # 0.5% per trade

    # Per-strategy drawdown breaker thresholds (rolling 30-day peak, negative)
    paper_drawdown_threshold_aggressive: float = -0.15
    paper_drawdown_threshold_balanced: float = -0.10
    paper_drawdown_threshold_conservative: float = -0.08

    # Per-strategy R/R cap. Caps the hallucinated-target class — ratios above
    # this signal the trader picked an unrealistic stop or fabricated target.
    # Lower = more selective. AGG=12 keeps marginal upside, BAL=8 is the
    # statistically-defensible cutoff, CON=6 demands grounded targets.
    paper_max_rr_aggressive: float = 12.0
    paper_max_rr_balanced: float = 8.0
    paper_max_rr_conservative: float = 6.0

    # ATR-based stop floor. Reject signals whose stop distance is below
    # `coefficient × ATR_14`. 1.0× ATR ≈ "stop survives one normal day."
    # Set to 0 to disable.
    paper_atr_floor_coefficient: float = 1.0

    # Legacy alias for code that hasn't migrated yet (DEPRECATED — remove in wave 1.6)
    paper_notional_pct: float = 0.015

    # Historical replay kill-gate
    replay_gate_enabled: bool = True
    replay_gate_skip: bool = False                # operator override (--skip-replay-gate)
    replay_gate_days_back: int = 10

    # Options liquidity-score thresholds (D9). Each value is the saturation
    # point: at or above the target the dimension scores 1.0; below it scales
    # linearly to 0.0. Defaults reproduce the wave-1 magic numbers (volume/500,
    # OI/1000, spread/0.25). Tune per strategy: small caps tolerate looser
    # spread but lower volume; large caps want tighter spread + higher volume.
    option_liquidity_volume_target: int = 500
    option_liquidity_oi_target: int = 1000
    option_liquidity_spread_target: float = 0.25

    # ─── Wave 2 D1: IBKR / OPRA chain provider ──────────
    # Per plan A2: this is the *preferred* provider, not the only one.
    # On per-ticker fetch failure (Gateway down, throttled, NaN Greeks, etc.)
    # the dispatch layer falls back to yfinance and records the fallback
    # in `data_provider_events`. Set to "yfinance" to force yfinance-only
    # (cheap dev / CI) without touching the dispatch code.
    options_data_provider: Literal["ibkr", "yfinance"] = "ibkr"

    # IB Gateway (paper) defaults: 4002 = paper Gateway, 4001 = live Gateway,
    # 7497 = paper TWS, 7496 = live TWS. Client-id namespace is per-process —
    # use a unique value per concurrent connection (refresh job vs heartbeat).
    ibkr_host: str = "127.0.0.1"
    ibkr_port: int = 4002
    ibkr_client_id_fetcher: int = 17           # arbitrary; unique per role
    ibkr_client_id_heartbeat: int = 18

    # Connection / fetch tuning. `connect_timeout` covers TWS handshake;
    # `fetch_timeout_per_expiration` budgets the per-expiration `reqMktData`
    # window so a wedged contract doesn't stall the whole snapshot.
    ibkr_connect_timeout_s: float = 10.0
    ibkr_fetch_timeout_per_expiration_s: float = 12.0
    # marketDataType: 1=live (needs subscription), 3=delayed (live if subscribed,
    # else delayed), 4=delayed-frozen. Default 3 because OPRA covers OPTION
    # quotes but not the underlying stock — IBKR errors 10089 when asked for
    # live US equity data without that separate sub. With type=3 the option
    # leg still streams live (OPRA), the stock leg falls back to free delayed.
    ibkr_market_data_type: int = 3

    # A4: TWS heartbeat probe cadence. The probe runs `ib.isConnected()` +
    # `ib.reqCurrentTimeAsync()`. A state transition writes one
    # `data_provider_events('gateway_state_change')` row; the gauge is read
    # at /api/metrics request time.
    ibkr_heartbeat_interval_s: float = 60.0

    # Greeks computation strategy.
    #   "local_bs"      → compute IV+Greeks ourselves via Black-Scholes from
    #                     OPRA bid/ask mid. Free, no extra entitlement needed.
    #   "ibkr_provider" → use IBKR's modelGreeks. Needs the underlying-equity
    #                     data sub (free with monthly commissions ≥ $30, or
    #                     ~$10/mo standalone). Greeks tagged 'provider'.
    # Flip this to 'ibkr_provider' the day you start trading and the equity
    # sub becomes free — no other code change needed.
    options_greeks_strategy: Literal["local_bs", "ibkr_provider"] = "local_bs"

    # Risk-free rate used by the local Black-Scholes solver. Only matters
    # when options_greeks_strategy == "local_bs". Default is the rough
    # short-end Treasury rate; override via env when it shifts materially.
    risk_free_rate: float = 0.045


settings = Settings()

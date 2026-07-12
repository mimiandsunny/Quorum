"""Paper Trader — submits bracket orders to Alpaca paper accounts (1 per strategy).

ISOLATION (per CEO D10): every failure path either inserts a paper_trades
row with a terminal status or returns a dict. NEVER raises out to the
LangGraph node. Signal persistence is inviolate.

Wave 1.5: every public entrypoint takes a `Strategy` (from agents/strategies.py)
so the same signal can produce 3 trades across 3 separate Alpaca accounts.
Per-strategy ISOLATION via try/except in the calling graph node.

Bracket order mapping:
  entry_limit  := signal.entry_zone[0]    # bottom of zone for BUY, top for SELL
  stop_loss    := signal.stop_loss
  take_profit  := signal.targets[0]       # only first target in wave 1
  TIF          := DAY (cancels at EOD if unfilled)

SELL signals submit as `sell_short` (Alpaca margin must be enabled).

Idempotency: `idempotency_key = f"{ticker}:{signal_date.isoformat()}:{strategy}"`.
Wave-1.5 suffix lets the same signal produce 3 unique rows.

Retry policy:
  - 429 (rate-limit), 503/504 (server), network errors → retry up to 3
    times with exponential backoff (1s, 4s, 16s).
  - 400, 401, 403 → no retry, terminal `status='failed'`.
"""

import logging
import math
import time
from datetime import datetime, timezone

from agents.strategies import Strategy
from config import settings
from data.models import (
    Decision,
    FinalSignal,
    PaperTrade,
    PaperTradeStatus,
)
from data.storage import (
    delete_paper_trade_by_key,
    get_paper_trade_by_key,
    get_paper_trade_by_signal_strategy,
    insert_paper_trade,
    update_paper_trade_status,
)

logger = logging.getLogger(__name__)


_RETRYABLE_STATUS_CODES = {429, 502, 503, 504}
_NON_RETRYABLE_STATUS_CODES = {400, 401, 403, 404, 422}
_RETRY_BACKOFFS = (1, 4, 16)  # seconds


# ─── Per-strategy Alpaca client cache ──────────────────

# One TradingClient per strategy. alpaca-py's TradingClient owns an httpx
# connection pool internally; sharing it across calls keeps TLS handshakes
# warm. Per-strategy caching is fine since each strategy uses its own
# Alpaca account creds.
_alpaca_clients: dict[str, object] = {}


def _get_alpaca_client(strategy: Strategy):
    """Lazy-init one TradingClient per strategy. Caches by strategy name."""
    if strategy.name in _alpaca_clients:
        return _alpaca_clients[strategy.name]

    from alpaca.trading.client import TradingClient

    if not strategy.api_key or not strategy.secret_key:
        raise RuntimeError(
            f"Alpaca creds missing for strategy '{strategy.name}' — "
            f"check ALPACA_API_KEY_{strategy.name.upper()} env var"
        )
    client = TradingClient(
        api_key=strategy.api_key,
        secret_key=strategy.secret_key,
        paper=True,
    )
    _alpaca_clients[strategy.name] = client
    return client


def _reset_alpaca_clients() -> None:
    """Test helper. Forces re-init on next _get_alpaca_client call."""
    _alpaca_clients.clear()


# ─── Validation helpers ─────────────────────────────────

def _idempotency_key(signal: FinalSignal, strategy: Strategy) -> str:
    """Wave 1.5: includes strategy suffix so 3 strategies on the same signal
    produce 3 distinct rows while same-day reruns dedupe broker orders."""
    return f"{signal.ticker}:{signal.date.isoformat()}:{strategy.name}"


def _validate_bracket_sanity(
    decision: Decision, entry: float, stop: float, target: float,
) -> str | None:
    """Return reason string if invalid, None if OK."""
    if decision == Decision.BUY:
        if not (stop < entry < target):
            return f"BUY bracket invalid: need stop({stop}) < entry({entry}) < target({target})"
    elif decision == Decision.SELL:
        if not (stop > entry > target):
            return f"SELL bracket invalid: need stop({stop}) > entry({entry}) > target({target})"
    return None


def _compute_qty(strategy: Strategy, entry_price: float) -> int:
    """Position size in WHOLE shares.

    Wave 1.5: flat notional per strategy (E2 confidence-scaling DROPPED in
    rev 3 per CEO outside-voice). Uses strategy.notional_pct directly.
    """
    notional_dollars = settings.paper_account_starting_value * strategy.notional_pct
    qty = math.floor(notional_dollars / entry_price)
    return max(qty, 1)  # at least 1 share if notional rounds to 0


def _build_skipped_trade(
    signal: FinalSignal, strategy: Strategy, side: str, reason: str,
) -> PaperTrade:
    """Build a PaperTrade row to record a skipped/invalid execution."""
    entry = signal.entry_zone[0] if signal.entry_zone else 0.0
    target = signal.targets[0] if signal.targets else 0.0
    return PaperTrade(
        recommendation_id=signal.recommendation_id,
        ticker=signal.ticker,
        signal_date=signal.date,
        strategy=strategy.name,
        idempotency_key=_idempotency_key(signal, strategy),
        decision=signal.decision,
        side=side,
        entry_limit=entry,
        stop_loss=signal.stop_loss,
        take_profit=target,
        notional_pct=strategy.notional_pct,
        status=PaperTradeStatus.EXECUTION_SKIPPED,
        status_reason=reason,
    )


# ─── Alpaca submission with retry ──────────────────────

def _classify_alpaca_error(exc: Exception) -> tuple[bool, str]:
    """Return (is_retryable, reason_str) for an Alpaca exception."""
    msg = str(exc)
    status_code = getattr(exc, "status_code", None) or getattr(exc, "code", None)

    if status_code is None:
        for code in list(_RETRYABLE_STATUS_CODES) + list(_NON_RETRYABLE_STATUS_CODES):
            if str(code) in msg:
                status_code = code
                break

    if status_code in _NON_RETRYABLE_STATUS_CODES:
        return False, f"alpaca_{status_code}: {msg[:200]}"
    if status_code in _RETRYABLE_STATUS_CODES:
        return True, f"alpaca_{status_code}: {msg[:200]}"
    if any(keyword in type(exc).__name__.lower() for keyword in ("timeout", "connection")):
        return True, f"network_error: {type(exc).__name__}: {msg[:200]}"
    return False, f"{type(exc).__name__}: {msg[:200]}"


def _submit_bracket(
    client,
    ticker: str,
    side_str: str,            # 'buy' | 'sell_short'
    qty: int,
    entry_limit: float,
    stop_loss: float,
    take_profit: float,
) -> tuple[str | None, str | None]:
    """Submit one bracket order with retry. Returns (alpaca_order_id, error_reason)."""
    from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
    from alpaca.trading.requests import (
        LimitOrderRequest,
        StopLossRequest,
        TakeProfitRequest,
    )

    side = OrderSide.SELL if side_str == "sell_short" else OrderSide.BUY

    request = LimitOrderRequest(
        symbol=ticker,
        qty=qty,
        side=side,
        time_in_force=TimeInForce.DAY,
        order_class=OrderClass.BRACKET,
        limit_price=round(entry_limit, 2),
        take_profit=TakeProfitRequest(limit_price=round(take_profit, 2)),
        stop_loss=StopLossRequest(stop_price=round(stop_loss, 2)),
    )

    last_error = None
    for attempt, backoff in enumerate(_RETRY_BACKOFFS, start=1):
        try:
            order = client.submit_order(request)
            order_id = getattr(order, "id", None) or getattr(order, "client_order_id", None)
            return str(order_id), None
        except Exception as exc:
            retryable, reason = _classify_alpaca_error(exc)
            last_error = reason
            if not retryable:
                logger.warning(f"[paper_trader:{ticker}] non-retryable: {reason}")
                return None, reason
            if attempt < len(_RETRY_BACKOFFS):
                logger.warning(
                    f"[paper_trader:{ticker}] attempt {attempt} retryable: {reason}. "
                    f"Sleeping {backoff}s..."
                )
                time.sleep(backoff)
            else:
                logger.error(f"[paper_trader:{ticker}] retries exhausted: {reason}")
    return None, last_error


# ─── Main entrypoint (called by LangGraph node, once per strategy) ──

def execute_paper_trade(signal: FinalSignal, strategy: Strategy) -> dict | None:
    """Submit the paper trade for a (signal, strategy) pair.

    Always returns a dict (never raises). Caller (graph node) wraps in
    asyncio.to_thread + per-strategy try/except for ISOLATION.
    """
    if not settings.paper_trading_enabled:
        return None  # caller treats None as "feature off"

    label = f"paper_trader:{signal.ticker}:{strategy.name}"

    # 1) Idempotency — TWO cases:
    #    a) Existing row has alpaca_order_id → real broker activity, BLOCK.
    #    b) Existing row has NO alpaca_order_id (audit-only — execution_skipped
    #       or failed-pre-submission from an earlier run) → safe to delete and
    #       proceed.
    key = _idempotency_key(signal, strategy)
    existing = get_paper_trade_by_key(key)
    if existing is None:
        signal_strategy_existing = get_paper_trade_by_signal_strategy(
            signal.ticker,
            signal.date,
            strategy.name,
        )
        if signal_strategy_existing and signal_strategy_existing.get("alpaca_order_id"):
            logger.info(
                f"[{label}] duplicate ticker/date/strategy with "
                f"alpaca_order_id={signal_strategy_existing['alpaca_order_id']} — blocking"
            )
            return {"status": "duplicate_blocked"}
    if existing is not None:
        if existing.get("alpaca_order_id"):
            logger.info(
                f"[{label}] duplicate idempotency_key with "
                f"alpaca_order_id={existing['alpaca_order_id']} — blocking"
            )
            return {"status": "duplicate_blocked"}
        deleted = delete_paper_trade_by_key(key)
        if not deleted:
            logger.warning(
                f"[{label}] race-condition on idempotency_key "
                "— refusing to delete, returning duplicate_blocked"
            )
            return {"status": "duplicate_blocked"}
        logger.info(
            f"[{label}] cleared stale audit row "
            f"(prior status={existing.get('status')}, reason={existing.get('status_reason')}) "
            "— re-attempting"
        )

    # 2) Validation
    if signal.decision == Decision.HOLD:
        trade = _build_skipped_trade(signal, strategy, side="hold", reason="hold_decision")
        insert_paper_trade(trade)
        return {"status": "execution_skipped", "reason": "hold_decision"}

    if not signal.entry_zone:
        trade = _build_skipped_trade(signal, strategy, side="buy", reason="invalid_entry_zone")
        insert_paper_trade(trade)
        return {"status": "execution_skipped", "reason": "invalid_entry_zone"}

    if not signal.targets:
        trade = _build_skipped_trade(signal, strategy, side="buy", reason="missing_targets")
        insert_paper_trade(trade)
        return {"status": "execution_skipped", "reason": "missing_targets"}

    side_str = "sell_short" if signal.decision == Decision.SELL else "buy"
    entry = signal.entry_zone[0]
    target = signal.targets[0]

    sanity_error = _validate_bracket_sanity(signal.decision, entry, signal.stop_loss, target)
    if sanity_error:
        trade = _build_skipped_trade(signal, strategy, side=side_str, reason="invalid_bracket")
        trade.status_reason = sanity_error
        insert_paper_trade(trade)
        logger.warning(f"[{label}] {sanity_error}")
        return {"status": "execution_skipped", "reason": "invalid_bracket"}

    # Per-strategy R/R cap — first per-strategy gate. Catches the
    # hallucinated-target class (R/R looks great because target is fabricated).
    # AGG=12 / BAL=8 / CON=6 by default; see config.paper_max_rr_*.
    if (
        strategy.max_reward_risk > 0
        and signal.reward_risk_ratio > strategy.max_reward_risk
    ):
        trade = _build_skipped_trade(signal, strategy, side=side_str, reason="rr_above_strategy_cap")
        trade.status_reason = (
            f"R/R {signal.reward_risk_ratio:.1f}x exceeds {strategy.name} cap "
            f"of {strategy.max_reward_risk:.1f}x"
        )
        insert_paper_trade(trade)
        logger.info(f"[{label}] {trade.status_reason}")
        return {"status": "execution_skipped", "reason": "rr_above_strategy_cap"}

    # 3) Build pending row, submit
    qty = _compute_qty(strategy, entry)
    pending_trade = PaperTrade(
        recommendation_id=signal.recommendation_id,
        ticker=signal.ticker,
        signal_date=signal.date,
        strategy=strategy.name,
        idempotency_key=key,
        decision=signal.decision,
        side=side_str,
        entry_limit=entry,
        stop_loss=signal.stop_loss,
        take_profit=target,
        notional_pct=strategy.notional_pct,
        status=PaperTradeStatus.PENDING,
        submitted_at=datetime.now(timezone.utc),
    )

    try:
        trade_id = insert_paper_trade(pending_trade)
    except Exception as exc:
        if "idempotency" in str(exc).lower() or "unique" in str(exc).lower():
            logger.info(f"[{label}] race-condition duplicate — skipping")
            return {"status": "duplicate_blocked"}
        logger.error(f"[{label}] DB insert failed: {exc}")
        return {"status": "db_error", "reason": str(exc)[:200]}

    try:
        client = _get_alpaca_client(strategy)
    except Exception as exc:
        update_paper_trade_status(
            trade_id, PaperTradeStatus.FAILED,
            status_reason=f"alpaca_client_init: {exc}",
        )
        logger.error(f"[{label}] Alpaca client init failed: {exc}")
        return {"status": "failed", "reason": "alpaca_client_init"}

    order_id, error_reason = _submit_bracket(
        client, signal.ticker, side_str, qty, entry, signal.stop_loss, target,
    )

    if order_id is None:
        update_paper_trade_status(
            trade_id, PaperTradeStatus.FAILED, status_reason=error_reason,
        )
        return {"status": "failed", "reason": error_reason}

    update_paper_trade_status(
        trade_id, PaperTradeStatus.SUBMITTED, alpaca_order_id=order_id,
    )
    logger.info(
        f"[{label}] submitted {side_str} {qty}@${entry:.2f} "
        f"stop=${signal.stop_loss:.2f} target=${target:.2f} alpaca_id={order_id}"
    )
    return {
        "status": "submitted",
        "alpaca_order_id": order_id,
        "qty": qty,
        "entry_limit": entry,
        "strategy": strategy.name,
    }

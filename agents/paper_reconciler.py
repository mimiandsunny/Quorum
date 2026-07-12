"""Paper-trade reconciler — wave 1.5 multi-strategy.

Polls each strategy's Alpaca account for the status of submitted paper trades,
records fills, updates positions, and marks unfilled orders at EOD.

Three entry points:
  - reconcile_active(): every 5 min during market hours. Iterates strategies
    in parallel via asyncio.to_thread (preserves the sync alpaca-py client's
    httpx connection pool — see CEO eng-review rev 4 for why).
  - close_unfilled_eod(): 4:05 PM ET. Marks still-pending bracket orders.
  - snapshot_equity(): 4:10 PM ET. Records per-strategy account equity for the
    dashboard sparkline AND drawdown circuit breaker (rolling 30-day peak).

Per-strategy ISOLATION via try/except wrappers — one bad Alpaca account never
blocks the others.
"""

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

from agents.strategies import Strategy, enabled_strategies
from data.models import EquitySnapshot, PaperFill, PaperPosition, PaperTradeStatus
from data.storage import (
    get_active_paper_trades,
    get_latest_snapshot,
    get_open_paper_positions,
    insert_equity_snapshot,
    save_paper_fill,
    update_paper_trade_status,
    upsert_paper_position,
)

logger = logging.getLogger(__name__)


# ─── Per-strategy client helpers ─────────────────────

def _get_client_for(strategy: Strategy):
    """Return the per-strategy TradingClient (cached in paper_trader._alpaca_clients)."""
    from agents.paper_trader import _get_alpaca_client
    return _get_alpaca_client(strategy)


def _get_data_client():
    """One shared StockHistoricalDataClient. Data API is account-agnostic;
    use the first enabled strategy's creds."""
    from alpaca.data.historical import StockHistoricalDataClient
    strategies = enabled_strategies()
    if not strategies:
        raise RuntimeError("no enabled strategies — cannot init data client")
    s = strategies[0]
    return StockHistoricalDataClient(s.api_key, s.secret_key)


def _get_order_safely(client, alpaca_order_id: str):
    """Returns the Alpaca order object or None on failure."""
    try:
        return client.get_order_by_id(alpaca_order_id)
    except Exception as exc:
        logger.warning(f"[reconciler] get_order failed for {alpaca_order_id}: {exc}")
        return None


def _get_latest_quote(data_client, symbol: str) -> float | None:
    """Best-effort current price for unrealized P&L. Returns None on failure."""
    try:
        from alpaca.data.requests import StockLatestTradeRequest
        req = StockLatestTradeRequest(symbol_or_symbols=symbol)
        latest = data_client.get_stock_latest_trade(req)
        trade = latest.get(symbol)
        return float(trade.price) if trade else None
    except Exception as exc:
        logger.debug(f"[reconciler] latest quote failed for {symbol}: {exc}")
        return None


# ─── Fill / bracket-leg processing (per-trade) ──────

def _process_fills(client, trade: dict) -> None:
    """Record any fills for a single paper_trade and upsert its position."""
    paper_trade_id = trade["id"]
    alpaca_order_id = trade["alpaca_order_id"]
    side = trade["side"]
    strategy = trade.get("strategy", "balanced")

    order = _get_order_safely(client, alpaca_order_id)
    if order is None:
        return

    filled_qty = float(getattr(order, "filled_qty", 0) or 0)
    if filled_qty <= 0:
        return

    avg_price = float(getattr(order, "filled_avg_price", 0) or 0)
    fill_id = f"{alpaca_order_id}:entry"

    fill = PaperFill(
        paper_trade_id=paper_trade_id,
        strategy=strategy,
        alpaca_fill_id=fill_id,
        side=side,
        qty=filled_qty,
        price=avg_price,
        filled_at=datetime.now(timezone.utc),
    )
    save_paper_fill(fill)

    position = PaperPosition(
        paper_trade_id=paper_trade_id,
        strategy=strategy,
        qty=filled_qty,
        avg_entry=avg_price,
        current_price=None,
        unrealized_pnl=0.0,
        realized_pnl=0.0,
    )
    upsert_paper_position(position)
    update_paper_trade_status(paper_trade_id, PaperTradeStatus.FILLED)
    logger.info(
        f"[reconciler:{strategy}] FILLED paper_trade={paper_trade_id} "
        f"alpaca={alpaca_order_id} {filled_qty}@${avg_price:.2f}"
    )


def _check_bracket_children(client, trade: dict) -> None:
    """Check if the bracket's stop or target child has filled (closing the position)."""
    paper_trade_id = trade["id"]
    alpaca_order_id = trade["alpaca_order_id"]
    side = trade["side"]
    strategy = trade.get("strategy", "balanced")

    order = _get_order_safely(client, alpaca_order_id)
    if order is None:
        return

    legs = getattr(order, "legs", None) or []
    for leg in legs:
        leg_status = getattr(leg, "status", None)
        if leg_status not in ("filled",):
            continue
        leg_filled_qty = float(getattr(leg, "filled_qty", 0) or 0)
        leg_avg_price = float(getattr(leg, "filled_avg_price", 0) or 0)
        leg_id = str(getattr(leg, "id", f"{alpaca_order_id}:leg"))
        leg_side = getattr(leg, "side", None)
        leg_side_str = leg_side.value if leg_side else "exit"

        leg_type = getattr(leg, "order_type", None) or getattr(leg, "type", None)
        leg_type_str = leg_type.value if hasattr(leg_type, "value") else str(leg_type)
        if "stop" in leg_type_str.lower():
            close_reason = "stop_hit"
        elif "limit" in leg_type_str.lower():
            close_reason = "target_hit"
        else:
            close_reason = "manual"

        save_paper_fill(PaperFill(
            paper_trade_id=paper_trade_id,
            strategy=strategy,
            alpaca_fill_id=leg_id,
            side=leg_side_str,
            qty=leg_filled_qty,
            price=leg_avg_price,
            filled_at=datetime.now(timezone.utc),
        ))

        open_positions = {
            p["paper_trade_id"]: p for p in get_open_paper_positions()
        }
        pos = open_positions.get(paper_trade_id)
        if not pos:
            continue
        entry = float(pos["avg_entry"])
        if side == "sell_short":
            realized = (entry - leg_avg_price) * leg_filled_qty
        else:
            realized = (leg_avg_price - entry) * leg_filled_qty

        upsert_paper_position(PaperPosition(
            paper_trade_id=paper_trade_id,
            strategy=strategy,
            qty=0.0,
            avg_entry=entry,
            current_price=leg_avg_price,
            unrealized_pnl=0.0,
            realized_pnl=round(realized, 2),
            closed_at=datetime.now(timezone.utc),
            close_reason=close_reason,
        ))
        logger.info(
            f"[reconciler:{strategy}] CLOSED paper_trade={paper_trade_id} {close_reason} "
            f"realized=${realized:.2f}"
        )


def _update_unrealized_pnl(data_client) -> None:
    """For each open position, refresh current_price and unrealized_pnl.
    Uses the shared data client (account-agnostic)."""
    for pos in get_open_paper_positions():
        symbol = pos["ticker"]
        qty = float(pos["qty"])
        entry = float(pos["avg_entry"])
        side = pos["side"]
        if qty <= 0:
            continue

        current = _get_latest_quote(data_client, symbol)
        if current is None:
            continue

        if side == "sell_short":
            unrealized = (entry - current) * qty
        else:
            unrealized = (current - entry) * qty

        upsert_paper_position(PaperPosition(
            paper_trade_id=pos["paper_trade_id"],
            strategy=pos.get("strategy", "balanced"),
            qty=qty,
            avg_entry=entry,
            current_price=current,
            unrealized_pnl=round(unrealized, 2),
        ))


# ─── Per-strategy reconcile (runs in to_thread) ─────

def _reconcile_one_strategy(strategy: Strategy, trades: list[dict]) -> None:
    """Reconcile all active trades for one strategy. Per-strategy ISOLATION
    via try/except — one strategy's failure doesn't propagate to others."""
    try:
        client = _get_client_for(strategy)
    except Exception as exc:
        logger.error(f"[reconciler:{strategy.name}] client init failed: {exc}")
        return

    for trade in trades:
        try:
            _process_fills(client, trade)
            _check_bracket_children(client, trade)
        except Exception as exc:
            logger.warning(
                f"[reconciler:{strategy.name}] failed processing trade "
                f"{trade.get('id')}: {exc}"
            )


# ─── Main entrypoints ───────────────────────────────

async def _reconcile_active_async() -> None:
    """Async fan-out across enabled strategies."""
    strategies = enabled_strategies()
    if not strategies:
        return

    # Group active trades by strategy. Trades with no alpaca_order_id are
    # pending without broker linkage — skip (will be handled at EOD).
    all_active = [t for t in get_active_paper_trades() if t.get("alpaca_order_id")]
    logger.info(f"[reconciler] {len(all_active)} active trades across {len(strategies)} strategies")

    by_strategy: dict[str, list[dict]] = {s.name: [] for s in strategies}
    for trade in all_active:
        s_name = trade.get("strategy", "balanced")
        if s_name in by_strategy:
            by_strategy[s_name].append(trade)

    # to_thread per strategy → parallelism without giving up the sync client
    tasks = [
        asyncio.to_thread(_reconcile_one_strategy, s, by_strategy[s.name])
        for s in strategies
        if by_strategy[s.name]
    ]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=False)

    # Unrealized P&L update uses one shared data client (account-agnostic data API).
    try:
        data_client = _get_data_client()
        await asyncio.to_thread(_update_unrealized_pnl, data_client)
    except Exception as exc:
        logger.warning(f"[reconciler] unrealized P&L update failed: {exc}")


def reconcile_active() -> None:
    """Sync entry point called by APScheduler. Wraps the async fan-out."""
    try:
        asyncio.run(_reconcile_active_async())
    except Exception as exc:
        logger.exception(f"[reconciler] reconcile_active unexpected: {exc}")


def close_unfilled_eod() -> None:
    """4:05 PM ET. Mark any still-submitted brackets as unfilled_eod across all strategies."""
    strategies = enabled_strategies()
    if not strategies:
        return

    all_active = get_active_paper_trades()
    closed = 0

    for strategy in strategies:
        try:
            client = _get_client_for(strategy)
        except Exception as exc:
            logger.error(f"[reconciler:{strategy.name}:eod] client init failed: {exc}")
            continue

        for trade in all_active:
            if trade.get("strategy") != strategy.name:
                continue
            alpaca_id = trade.get("alpaca_order_id")
            if not alpaca_id:
                update_paper_trade_status(
                    trade["id"], PaperTradeStatus.UNFILLED_EOD,
                    status_reason="no_alpaca_id_at_eod",
                )
                closed += 1
                continue
            order = _get_order_safely(client, alpaca_id)
            if order is None:
                continue
            filled_qty = float(getattr(order, "filled_qty", 0) or 0)
            if filled_qty <= 0:
                update_paper_trade_status(
                    trade["id"], PaperTradeStatus.UNFILLED_EOD,
                    status_reason="bracket_unfilled_at_eod",
                )
                closed += 1

    logger.info(f"[reconciler:eod] marked {closed} paper trades as unfilled_eod")


# ─── E4 nightly equity snapshot ─────────────────────

def _snapshot_one_strategy(strategy: Strategy, snapshot_date: date) -> None:
    """Query Alpaca account for one strategy and persist a snapshot row.
    On the first snapshot for a strategy, ALSO seed a synthetic prior-day row
    so the E3 dashboard sparkline renders immediately (NEW2 fix from CEO
    plan iter 2). Both rows have daily_pnl=NULL/0 since there's no prior to
    diff against on day 0.
    """
    try:
        client = _get_client_for(strategy)
        account = client.get_account()
    except Exception as exc:
        logger.error(f"[snapshot:{strategy.name}] account query failed: {exc}")
        return

    equity = float(getattr(account, "equity", 0) or 0)
    cash = float(getattr(account, "cash", 0) or 0)
    long_value = float(getattr(account, "long_market_value", 0) or 0)
    short_value = float(getattr(account, "short_market_value", 0) or 0)
    positions_value = long_value + abs(short_value)

    prior = get_latest_snapshot(strategy.name)
    if prior is None:
        # First-ever snapshot for this strategy — seed yesterday's row first
        # so snapshot_count >= 2 immediately and the sparkline can render.
        yesterday = snapshot_date - timedelta(days=1)
        seed = EquitySnapshot(
            strategy=strategy.name,
            snapshot_date=yesterday,
            account_equity=equity,
            cash=cash,
            positions_value=positions_value,
            daily_pnl=0.0,  # synthetic — no prior to diff
        )
        seed_id = insert_equity_snapshot(seed)
        if seed_id:
            logger.info(f"[snapshot:{strategy.name}] seeded synthetic prior-day row")
        daily_pnl = 0.0
    else:
        prior_equity = float(prior["account_equity"])
        daily_pnl = equity - prior_equity

    snapshot = EquitySnapshot(
        strategy=strategy.name,
        snapshot_date=snapshot_date,
        account_equity=equity,
        cash=cash,
        positions_value=positions_value,
        daily_pnl=daily_pnl,
    )
    inserted = insert_equity_snapshot(snapshot)
    if inserted:
        logger.info(
            f"[snapshot:{strategy.name}] equity=${equity:,.2f} "
            f"daily_pnl=${daily_pnl:+,.2f}"
        )
    else:
        logger.debug(f"[snapshot:{strategy.name}] already snapshotted today")


def snapshot_equity() -> None:
    """4:10 PM ET. Record per-strategy account equity for E3 dashboard +
    E9 drawdown breaker. Per-strategy try/except wraps each call."""
    today = date.today()
    for strategy in enabled_strategies():
        try:
            _snapshot_one_strategy(strategy, today)
        except Exception as exc:
            logger.exception(f"[snapshot:{strategy.name}] unexpected: {exc}")


def maybe_seed_snapshots_on_boot() -> None:
    """App-startup hook. If a strategy has zero snapshots, fire snapshot_equity
    immediately so the E3 dashboard isn't empty for the first 24h. Cheap no-op
    on subsequent boots once snapshots exist.
    """
    for strategy in enabled_strategies():
        if get_latest_snapshot(strategy.name) is None:
            try:
                _snapshot_one_strategy(strategy, date.today())
                logger.info(f"[snapshot:{strategy.name}] boot-seed complete")
            except Exception as exc:
                logger.warning(f"[snapshot:{strategy.name}] boot-seed failed: {exc}")

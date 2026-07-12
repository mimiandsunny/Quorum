"""Strategy registry for wave-1.5 multi-portfolio paper trading.

Three strategies run in parallel on the same daily AI-generated signals:
  - Aggressive    (3.0% notional per trade)
  - Balanced      (1.5% notional per trade) — legacy single-portfolio default
  - Conservative  (0.5% notional per trade)

Each strategy has its own Alpaca paper account (Plan A from CEO plan rev 5).
Cross-strategy isolation: per-strategy try/except in execute_paper_trades and
reconcile_active so one bad Alpaca account never blocks the others.

Adding a 4th strategy in wave 1.6 = append to STRATEGIES below + add 2 env
vars + add 2 config notional_pct fields. No graph/trader/reconciler changes.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Strategy:
    """Immutable description of one trading strategy."""
    name: str                           # 'aggressive' | 'balanced' | 'conservative'
    notional_pct: float                 # base notional fraction
    drawdown_pause_threshold: float     # negative, e.g. -0.15 = pause at 15% drawdown
    api_key: str                        # resolved Alpaca API key (empty if missing)
    secret_key: str                     # resolved Alpaca secret key (empty if missing)
    enabled: bool = True                # set False at startup if creds missing/invalid
    max_reward_risk: float = 8.0        # per-strategy R/R cap (rejects hallucinated targets)


def _build_registry() -> list[Strategy]:
    """Build the strategy list from settings. Called at module import time AND
    refreshable via reload_strategies() (used by tests)."""
    return [
        Strategy(
            name="aggressive",
            notional_pct=settings.paper_notional_pct_aggressive,
            drawdown_pause_threshold=settings.paper_drawdown_threshold_aggressive,
            api_key=settings.alpaca_api_key_aggressive,
            secret_key=settings.alpaca_secret_key_aggressive,
            enabled=bool(settings.alpaca_api_key_aggressive and settings.alpaca_secret_key_aggressive),
            max_reward_risk=settings.paper_max_rr_aggressive,
        ),
        Strategy(
            name="balanced",
            notional_pct=settings.paper_notional_pct_balanced,
            drawdown_pause_threshold=settings.paper_drawdown_threshold_balanced,
            api_key=settings.alpaca_api_key_balanced,
            secret_key=settings.alpaca_secret_key_balanced,
            enabled=bool(settings.alpaca_api_key_balanced and settings.alpaca_secret_key_balanced),
            max_reward_risk=settings.paper_max_rr_balanced,
        ),
        Strategy(
            name="conservative",
            notional_pct=settings.paper_notional_pct_conservative,
            drawdown_pause_threshold=settings.paper_drawdown_threshold_conservative,
            api_key=settings.alpaca_api_key_conservative,
            secret_key=settings.alpaca_secret_key_conservative,
            enabled=bool(settings.alpaca_api_key_conservative and settings.alpaca_secret_key_conservative),
            max_reward_risk=settings.paper_max_rr_conservative,
        ),
    ]


# Module-level registry. Mutable list of immutable Strategy frozen dataclasses
# so tests can monkeypatch the list contents without rebuilding the module.
STRATEGIES: list[Strategy] = _build_registry()


def reload_strategies() -> None:
    """Rebuild STRATEGIES from current settings. Used by tests after monkeypatching."""
    STRATEGIES.clear()
    STRATEGIES.extend(_build_registry())


def enabled_strategies() -> list[Strategy]:
    """Strategies whose creds resolved at startup. Pause-state check happens
    later in the per-signal loop (not here — avoids the double-query CL3
    fix from CEO plan spec review)."""
    return [s for s in STRATEGIES if s.enabled]


def get_strategy(name: str) -> Optional[Strategy]:
    """Lookup by name. Returns None if not found or disabled."""
    for s in STRATEGIES:
        if s.name == name:
            return s
    return None


def is_paused(strategy: Strategy) -> bool:
    """E9 drawdown circuit breaker. Returns True if the strategy is currently
    paused (either by manual pause row or by auto-trip on rolling-30-day
    drawdown threshold).

    Side effect: when drawdown crosses threshold, INSERTs a pause row via
    insert_pause_if_absent (atomic ON CONFLICT DO NOTHING + RETURNING) so
    log_circuit_breaker fires exactly once per breaker trip even under
    concurrent fan-out (NEW5 fix from CEO plan iter 2).
    """
    # Lazy import — keeps strategies.py importable even when DB isn't reachable
    # (e.g. config-only smoke tests). DB calls only happen when this is invoked.
    from data.storage import (
        get_recent_snapshots,
        get_strategy_pause,
        insert_pause_if_absent,
    )

    # 1. Check explicit pause state (manual or prior auto-pause)
    paused = get_strategy_pause(strategy.name)
    if paused:
        unpause_after = paused.get("unpause_after")
        if unpause_after is None or unpause_after > datetime.now(timezone.utc):
            return True

    # 2. Compute rolling-30-day drawdown
    snaps = get_recent_snapshots(strategy.name, limit=30)
    if len(snaps) < 5:
        return False  # insufficient data — don't pause on flimsy signal

    # snaps are newest-first
    equities = [float(s["account_equity"]) for s in snaps]
    peak = max(equities)
    current = equities[0]
    drawdown = (current - peak) / peak if peak > 0 else 0.0

    if drawdown < strategy.drawdown_pause_threshold:
        was_inserted = insert_pause_if_absent(
            strategy.name,
            reason="drawdown_threshold",
            paused_drawdown=drawdown,
            unpause_after=None,
        )
        if was_inserted:
            logger.warning(
                f"[circuit_breaker:{strategy.name}] TRIPPED drawdown={drawdown:.2%} "
                f"(threshold={strategy.drawdown_pause_threshold:.0%}, "
                f"peak=${peak:,.0f}, current=${current:,.0f})"
            )
        return True
    return False


def strategy_color_token(name: str) -> str:
    """CSS variable name for the strategy's accent color. Used by the
    dashboard summary panel + sparklines + paper-chip prefix accent.
    Maps to existing tokens in templates/dashboard.html — no new colors."""
    return {
        "aggressive": "var(--purple)",   # #a78bfa
        "balanced": "var(--accent)",     # #7eb8ff
        "conservative": "var(--yellow)", # #fbbf24
    }.get(name, "var(--text)")

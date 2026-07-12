"""Historical Replay — wave-1 kill-gate before paper trading goes live.

NOT a backtest. Replays the last N stored signals against actual yfinance
prices and computes simple directional accuracy. If hit rate is too low or
mean return is negative, refuses to enable paper trading without an
explicit override.

This is intentionally narrow: it reuses the v1.1 scorer's correctness
logic, doesn't simulate fills or slippage, and assumes signals already
exist in the DB. For a real backtest framework (cross-validation,
slippage models, parameter sweeps), see wave 2.

Usage:
    from agents.historical_replay import run_replay
    report = run_replay(days_back=10)
    if report.verdict == ReplayVerdict.BLOCK:
        sys.exit(1)
"""

import logging
from datetime import date, timedelta

import yfinance as yf

from data.models import Decision, ReplayReport, ReplayVerdict
from data.storage import get_signals_by_date

logger = logging.getLogger(__name__)

# Gate thresholds. Tuned for wave 1; revisit as data accumulates.
MIN_SIGNALS_FOR_VERDICT = 5
HIT_RATE_BLOCK_THRESHOLD = 0.40
MEAN_RETURN_BLOCK_THRESHOLD = -0.02


def _next_trading_day(d: date) -> date:
    """Advance past weekends. Doesn't handle holidays — those fall through
    to yfinance's empty-result path and are counted as 'skipped'.

    Resolves spec-review N6 partially. A real holiday calendar (NYSE) is a
    wave-2 add when we add `pandas_market_calendars` to requirements.
    """
    while d.weekday() >= 5:  # 5=Sat, 6=Sun
        d += timedelta(days=1)
    return d


def _fetch_close_at(ticker: str, target: date) -> float | None:
    """Fetch the actual close price at target date (advancing past weekends)."""
    target_trading = _next_trading_day(target)
    try:
        df = yf.Ticker(ticker).history(
            start=target_trading.isoformat(),
            end=(target_trading + timedelta(days=4)).isoformat(),  # buffer for holidays
            timeout=15,
        )
        if df.empty:
            return None
        return float(df.iloc[0]["Close"])
    except Exception as e:
        logger.warning(f"[replay:{ticker}] yfinance failed at {target_trading}: {e}")
        return None


def _evaluate_signal(signal, end_close: float) -> tuple[bool, float]:
    """Returns (direction_correct, side_correct_return_pct)."""
    entry_mid = (signal.entry_zone[0] + signal.entry_zone[1]) / 2 if signal.entry_zone else end_close
    if not entry_mid:
        return False, 0.0

    if signal.decision == Decision.BUY:
        correct = end_close > entry_mid
        return_pct = (end_close - entry_mid) / entry_mid
    elif signal.decision == Decision.SELL:
        correct = end_close < entry_mid
        return_pct = (entry_mid - end_close) / entry_mid
    else:
        return_pct = (end_close - entry_mid) / entry_mid
        correct = abs(return_pct) < 0.02
    return correct, return_pct


def run_replay(days_back: int = 10) -> ReplayReport:
    """Walk back days_back days, fetch each signal's outcome, score the lot.

    Returns a ReplayReport with verdict in {PASS, BLOCK, INSUFFICIENT}.
    """
    today = date.today()
    signals = []
    for offset in range(1, days_back + 1):
        d = today - timedelta(days=offset)
        signals.extend(get_signals_by_date(d))

    if len(signals) < MIN_SIGNALS_FOR_VERDICT:
        return ReplayReport(
            verdict=ReplayVerdict.INSUFFICIENT,
            signals_evaluated=len(signals),
            summary=(
                f"Only {len(signals)} signals in last {days_back} days "
                f"(need {MIN_SIGNALS_FOR_VERDICT} for verdict). "
                "Paper trading allowed without gate enforcement."
            ),
        )

    correct_count = 0
    returns = []
    by_decision: dict[str, int] = {}
    skipped = 0

    for signal in signals:
        target = signal.date + timedelta(days=signal.holding_period_days)
        end_close = _fetch_close_at(signal.ticker, target)
        if end_close is None:
            skipped += 1
            continue
        correct, ret = _evaluate_signal(signal, end_close)
        if correct:
            correct_count += 1
        returns.append(ret)
        key = signal.decision.value
        by_decision[key] = by_decision.get(key, 0) + 1

    evaluated = len(returns)
    if evaluated < MIN_SIGNALS_FOR_VERDICT:
        return ReplayReport(
            verdict=ReplayVerdict.INSUFFICIENT,
            signals_evaluated=evaluated,
            by_decision=by_decision,
            summary=(
                f"Only {evaluated} signals had retrievable close prices "
                f"({skipped} skipped). Need {MIN_SIGNALS_FOR_VERDICT} for verdict. "
                "Paper trading allowed."
            ),
        )

    hit_rate = correct_count / evaluated
    mean_return = sum(returns) / evaluated

    if hit_rate < HIT_RATE_BLOCK_THRESHOLD or mean_return < MEAN_RETURN_BLOCK_THRESHOLD:
        verdict = ReplayVerdict.BLOCK
        summary = (
            f"BLOCK: {evaluated} signals, hit rate {hit_rate:.1%}, "
            f"mean return {mean_return:+.2%}. Below threshold "
            f"(hit_rate {HIT_RATE_BLOCK_THRESHOLD:.0%} / "
            f"mean_return {MEAN_RETURN_BLOCK_THRESHOLD:+.0%}). "
            "Paper trading disabled. Use --skip-replay-gate to override."
        )
    else:
        verdict = ReplayVerdict.PASS
        summary = (
            f"PASS: {evaluated} signals, hit rate {hit_rate:.1%}, "
            f"mean return {mean_return:+.2%}. Paper trading enabled."
        )

    logger.info(f"[replay] {summary}")
    return ReplayReport(
        verdict=verdict,
        signals_evaluated=evaluated,
        hit_rate=round(hit_rate, 4),
        mean_return_pct=round(mean_return, 4),
        by_decision=by_decision,
        summary=summary,
    )


def main() -> int:
    """CLI entrypoint. Returns nonzero if BLOCK and --skip-replay-gate not passed."""
    import argparse
    parser = argparse.ArgumentParser(description="Run historical replay kill-gate.")
    parser.add_argument("--days-back", type=int, default=10)
    parser.add_argument(
        "--skip-replay-gate", action="store_true",
        help="Bypass BLOCK verdict (operator override).",
    )
    args = parser.parse_args()

    report = run_replay(days_back=args.days_back)
    print(report.summary)
    if report.verdict == ReplayVerdict.BLOCK and not args.skip_replay_gate:
        return 1
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())

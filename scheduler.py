"""Stockinvest scheduler.

Runs three recurring jobs:
  - Morning pipeline at 6:30 AM ET on trading days (configurable).
  - Paper-trade reconciler every 5 min during market hours (9:30-16:00 ET).
  - EOD unfilled-close at 4:05 PM ET (CEO D3 eng).
  - Afternoon scorer at 4:30 PM ET.

Uses BackgroundScheduler (CEO D1 eng) so the long-running pipeline doesn't
block the 5-min reconciler. Scheduler runs in background threads; main()
keeps the process alive with a sleep loop + signal handler.
"""

import logging
import signal
import subprocess
import sys
import time
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)


def is_market_day() -> bool:
    """Check if today is a trading day (weekday, not a market holiday)."""
    today = datetime.now().date()
    if today.weekday() >= 5:  # Saturday=5, Sunday=6
        return False

    # Major US market holidays — covered if pandas_market_calendars is installed
    try:
        import pandas_market_calendars as mcal
        nyse = mcal.get_calendar("NYSE")
        schedule = nyse.schedule(start_date=today, end_date=today)
        return not schedule.empty
    except ImportError:
        # Fall back to weekday check; the reconciler will no-op cleanly on holidays
        return True


def run_pipeline():
    """Execute the full pipeline for all tickers."""
    if not is_market_day():
        logger.info("Not a trading day. Skipping.")
        return

    logger.info("=" * 60)
    logger.info("STARTING MORNING PIPELINE")
    logger.info("=" * 60)

    try:
        from agents.graph import run_all
        signals = run_all()

        logger.info(f"Pipeline complete: {len(signals)} signals generated")

        # Desktop notification (macOS)
        buys = sum(1 for s in signals if s.decision.value == "BUY")
        sells = sum(1 for s in signals if s.decision.value == "SELL")
        _notify(
            "StockInvest Pipeline Complete",
            f"{len(signals)} signals: {buys} BUY, {sells} SELL",
        )
    except Exception as e:
        logger.exception(f"Pipeline failed: {e}")
        _notify("StockInvest Pipeline FAILED", str(e))


def _notify(title: str, message: str):
    """Send a macOS desktop notification."""
    if sys.platform == "darwin":
        try:
            subprocess.run(
                ["osascript", "-e", f'display notification "{message}" with title "{title}"'],
                capture_output=True,
                timeout=5,
            )
        except Exception:
            pass  # Notification is best-effort


def run_scorer():
    """Score past signals after market close."""
    if not is_market_day():
        logger.info("Not a trading day. Skipping scoring.")
        return

    logger.info("=" * 60)
    logger.info("STARTING AFTERNOON SCORER")
    logger.info("=" * 60)

    try:
        from agents.scorer import score_recommendations, score_signals
        signal_results = score_signals(days_back=10)
        recommendation_results = score_recommendations(days_back=10)
        logger.info(
            "Scorer complete: "
            f"{len(signal_results)} signals, "
            f"{len(recommendation_results)} recommendations scored"
        )
        _notify(
            "StockInvest Scorer Complete",
            f"{len(signal_results)} signals, "
            f"{len(recommendation_results)} recommendations scored",
        )
    except Exception as e:
        logger.exception(f"Scorer failed: {e}")
        _notify("StockInvest Scorer FAILED", str(e))


def run_reconciler():
    """5-minute paper-trade reconciler. Skipped outside market hours."""
    if not settings.paper_trading_enabled:
        return
    if not is_market_day():
        return
    try:
        from agents.paper_reconciler import reconcile_active
        reconcile_active()
    except Exception as e:
        logger.exception(f"Reconciler failed: {e}")


def run_eod_close():
    """4:05 PM ET: mark unfilled bracket orders as unfilled_eod."""
    if not settings.paper_trading_enabled:
        return
    if not is_market_day():
        return
    try:
        from agents.paper_reconciler import close_unfilled_eod
        close_unfilled_eod()
    except Exception as e:
        logger.exception(f"EOD close failed: {e}")


def run_snapshot_equity():
    """4:10 PM ET: snapshot per-strategy account equity (E4 wave 1.5).

    Drives dashboard sparkline + drawdown circuit breaker rolling-30-day peak.
    """
    if not settings.paper_trading_enabled:
        return
    if not is_market_day():
        return
    try:
        from agents.paper_reconciler import snapshot_equity
        snapshot_equity()
    except Exception as e:
        logger.exception(f"Equity snapshot failed: {e}")


def main():
    """Start the scheduler in the background and keep the process alive."""
    scheduler = BackgroundScheduler(timezone=settings.timezone)
    # misfire_grace_time=600 lets a job fire up to 10 min late after a process
    # restart, instead of being silently skipped (F4 fix from CEO eng-review).
    scheduler.add_job(
        run_pipeline,
        CronTrigger(
            hour=settings.run_hour,
            minute=settings.run_minute,
            timezone=settings.timezone,
        ),
        id="morning_pipeline",
        name="Morning Trading Pipeline",
        misfire_grace_time=600,
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        run_scorer,
        CronTrigger(hour=16, minute=30, timezone=settings.timezone),
        id="afternoon_scorer",
        name="Afternoon Signal and Recommendation Scorer",
        misfire_grace_time=600,
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        run_reconciler,
        # Every 5 min from 9:30 AM through 16:00 ET, Mon-Fri
        CronTrigger(
            day_of_week="mon-fri",
            hour="9-16",
            minute="*/5",
            timezone=settings.timezone,
        ),
        id="paper_reconciler",
        name="Paper Trade Reconciler",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )
    scheduler.add_job(
        run_eod_close,
        CronTrigger(
            day_of_week="mon-fri",
            hour=16, minute=5,
            timezone=settings.timezone,
        ),
        id="eod_close",
        name="EOD Unfilled Close",
        misfire_grace_time=600,
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        run_snapshot_equity,
        CronTrigger(
            day_of_week="mon-fri",
            hour=16, minute=10,
            timezone=settings.timezone,
        ),
        id="equity_snapshot",
        name="Per-Strategy Equity Snapshot",
        misfire_grace_time=600,
        coalesce=True,
        max_instances=1,
    )

    logger.info(
        f"Scheduler started (BackgroundScheduler). "
        f"Pipeline: {settings.run_hour:02d}:{settings.run_minute:02d} {settings.timezone}, "
        f"Reconciler: every 5min during market hours, "
        f"EOD close: 16:05 {settings.timezone}, "
        f"Scorer: 16:30 {settings.timezone}"
    )
    logger.info(f"Paper trading: {'ENABLED' if settings.paper_trading_enabled else 'DISABLED'}")
    logger.info("Press Ctrl+C to stop.")

    scheduler.start()

    # Keep the process alive. SIGTERM/SIGINT shut down cleanly.
    stop_event = {"stop": False}

    def _shutdown(signum, frame):
        logger.info(f"Received signal {signum}, shutting down...")
        stop_event["stop"] = True

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        while not stop_event["stop"]:
            time.sleep(1)
    finally:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped.")


if __name__ == "__main__":
    # If run directly, execute the pipeline immediately (useful for testing)
    import argparse
    parser = argparse.ArgumentParser(description="StockInvest Scheduler")
    parser.add_argument("--now", action="store_true", help="Run pipeline immediately")
    parser.add_argument("--score", action="store_true", help="Run scorer immediately")
    parser.add_argument("--reconcile", action="store_true", help="Run reconciler once")
    parser.add_argument("--eod", action="store_true", help="Run EOD close once")
    parser.add_argument("--snapshot", action="store_true", help="Run equity snapshot once")
    parser.add_argument("--schedule", action="store_true", help="Start the scheduler")
    args = parser.parse_args()

    if args.now:
        run_pipeline()
    elif args.score:
        run_scorer()
    elif args.reconcile:
        run_reconciler()
    elif args.eod:
        run_eod_close()
    elif args.snapshot:
        run_snapshot_equity()
    elif args.schedule:
        main()
    else:
        parser.print_help()

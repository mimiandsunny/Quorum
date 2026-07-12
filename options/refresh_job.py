"""Async chain-refresh job orchestration.

Wave 2 D1 P6-P7 (plan rev 4). Three responsibilities:

1. **Dispatch (C1)** — pick the right fetcher impl from config and route
   per its Protocol. Sync impls run via `asyncio.to_thread`; async impls
   are awaited directly.
2. **Per-ticker fallback (A2)** — preferred fetcher fails (Gateway down,
   throttled, NaN-laden Greeks, etc.) → fall back to yfinance for that
   ticker and audit the transition in `data_provider_events`.
3. **Job lifecycle (A3)** — acquire-lock via Postgres partial unique index;
   progress, complete, or fail the job through the storage CRUD added in
   the days-2-7 slice. Zombie cleanup happens at uvicorn startup, not here.

The actual chain-write + IV-summary path lives in
`options/iv_summary.py::persist_chain_with_iv_summary` (A6). This module
just orchestrates fetch → persist → audit, one ticker at a time.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable
from uuid import uuid4

from config import settings
from data.models import (
    DataProviderEvent,
    DataProviderEventType,
    OptionRefreshJob,
    OptionRefreshJobFailure,
    OptionRefreshJobSource,
)
from data.storage import (
    acquire_option_refresh_job,
    complete_option_refresh_job,
    fail_option_refresh_job,
    record_data_provider_event,
    update_option_refresh_job_progress,
)
from options.fetchers import YfinanceFetcher, is_async_fetcher
from options.iv_summary import persist_chain_with_iv_summary
from options.models import OptionChainSnapshot

logger = logging.getLogger(__name__)


def build_preferred_fetcher() -> object:
    """Returns the configured preferred fetcher instance.

    Imports IBKRFetcher lazily so a yfinance-only environment (CI, dev
    boxes without Gateway) doesn't pay the `ib_async` import cost.
    """
    if settings.options_data_provider == "ibkr":
        from options.ibkr_fetcher import IBKRFetcher
        return IBKRFetcher()
    return YfinanceFetcher()


def _fetcher_source(fetcher: object) -> OptionRefreshJobSource:
    name = getattr(fetcher, "source_name", "yfinance")
    return OptionRefreshJobSource.IBKR if name == "ibkr" else OptionRefreshJobSource.YFINANCE


async def _call_fetcher(
    fetcher: object, ticker: str, *, max_expirations: int = 4
) -> OptionChainSnapshot:
    """Per C1: route to the fetcher's idiomatic call shape.

    Sync impls block; we offload to a worker thread so the event loop
    keeps spinning. Async impls cooperate with the loop directly.
    """
    if is_async_fetcher(fetcher):
        coro: Awaitable[OptionChainSnapshot] = fetcher.fetch(
            ticker, max_expirations=max_expirations
        )
        return await coro
    return await asyncio.to_thread(
        fetcher.fetch, ticker, max_expirations=max_expirations
    )


async def _fetch_with_fallback(
    *,
    preferred: object,
    fallback: object | None,
    ticker: str,
    max_expirations: int,
) -> OptionChainSnapshot:
    """A2: preferred fetcher first; on failure, fall back to yfinance and
    record the transition. Re-raises if both fetchers fail — the caller
    accumulates the failure into the job's `failures` JSONB list.
    """
    preferred_source = getattr(preferred, "source_name", "unknown")
    try:
        return await _call_fetcher(preferred, ticker, max_expirations=max_expirations)
    except Exception as exc:
        if fallback is None or fallback is preferred:
            raise
        fallback_source = getattr(fallback, "source_name", "yfinance")
        record_data_provider_event(
            DataProviderEvent(
                event_type=DataProviderEventType.FETCHER_FALLBACK,
                ticker=ticker,
                from_provider=preferred_source,
                to_provider=fallback_source,
                reason=f"{type(exc).__name__}: {exc}",
                payload={"max_expirations": max_expirations},
            )
        )
        logger.warning(
            f"[{ticker}] preferred={preferred_source} failed ({type(exc).__name__}); "
            f"falling back to {fallback_source}"
        )
        return await _call_fetcher(fallback, ticker, max_expirations=max_expirations)


async def run_chain_refresh(
    tickers: list[str],
    *,
    max_expirations: int = 4,
    preferred: object | None = None,
) -> OptionRefreshJob:
    """Top-level entry point. Acquires the job slot, fetches each ticker
    serially, persists chain + IV summary per A6, and finalizes the row.

    Concurrency is intentionally serial within a job: ib_async's connection
    is per-IB-instance and IBKR's pacing budget is the bottleneck anyway.
    Refresh-of-refreshes parallelism happens by NOT running multiple jobs
    simultaneously — the partial unique index in option_refresh_jobs
    enforces that.
    """
    fetcher = preferred or build_preferred_fetcher()
    fallback = YfinanceFetcher() if getattr(fetcher, "source_name", None) != "yfinance" else None
    source = _fetcher_source(fetcher)

    job_id = uuid4().hex
    job = acquire_option_refresh_job(
        job_id=job_id,
        source=source,
        total=len(tickers),
    )
    if job is None:
        # Another job already holds the running slot. Caller (HTTP handler)
        # translates this to 409 Conflict.
        raise RuntimeError("Another option refresh job is already running")

    completed = 0
    failures: list[OptionRefreshJobFailure] = []
    started_at = time.monotonic()

    try:
        for ticker in tickers:
            try:
                snapshot = await _fetch_with_fallback(
                    preferred=fetcher,
                    fallback=fallback,
                    ticker=ticker,
                    max_expirations=max_expirations,
                )
                # A6 per-ticker txn lives in iv_summary.persist_chain_with_iv_summary —
                # offloaded to a worker thread because storage is sync psycopg.
                await asyncio.to_thread(persist_chain_with_iv_summary, snapshot)
            except Exception as exc:
                failures.append(
                    OptionRefreshJobFailure(
                        ticker=ticker,
                        error_class=type(exc).__name__,
                        message=str(exc),
                    )
                )
                logger.exception(f"[{ticker}] refresh failed: {type(exc).__name__}")
            completed += 1
            update_option_refresh_job_progress(job_id, completed)

        complete_option_refresh_job(job_id, completed=completed, failures=failures)
        logger.info(
            f"refresh job {job_id[:8]} complete: {completed}/{len(tickers)} processed, "
            f"{len(failures)} failures, {time.monotonic() - started_at:.1f}s"
        )
    except BaseException as exc:
        # CancelledError, KeyboardInterrupt, anything that escaped the
        # per-ticker try block — mark the job failed so the lock releases.
        fail_option_refresh_job(
            job_id, error_class=type(exc).__name__, message=str(exc) or repr(exc)
        )
        raise

    return OptionRefreshJob(
        job_id=job_id,
        status=job.status,        # acquire returned 'running'; complete updates DB but not this object
        source=source,
        total=len(tickers),
        completed=completed,
        failures=failures,
    )

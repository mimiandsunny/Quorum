"""Option-chain fetcher Protocols.

Two Protocols, not one (eng review C1, 2026-05-01):

- `SyncFetcher` — fits requests-style providers (yfinance). Caller wraps
  in `asyncio.to_thread` when invoked from the async refresh job.
- `AsyncFetcher` — fits native-async providers (IBKR via ib-insync's async
  surface). Caller awaits directly.

The dispatch layer in `options/refresh_job.py` (Wave 2 D1) checks which
Protocol the configured fetcher implements and routes accordingly. This
keeps each fetcher's surface idiomatic for its underlying client.

When a third fetcher arrives (Polygon, Tradier, Alpaca, etc.), revisit
the unification trade-off — at 3+ fetchers the case for a single async
Protocol with `asyncio.to_thread` wrapping any sync impl becomes stronger.
See TODOS.md TD-11 for the trigger criteria.
"""

from __future__ import annotations

import inspect
from typing import Protocol

from options.chain import fetch_yfinance_option_chain
from options.models import OptionChainSnapshot


class SyncFetcher(Protocol):
    """Synchronous chain fetcher. Implementations block the calling thread.

    The async refresh dispatcher wraps these in `asyncio.to_thread` so
    blocking I/O does not stall the event loop.
    """

    source_name: str

    def fetch(self, ticker: str, *, max_expirations: int = 4) -> OptionChainSnapshot:
        ...


class AsyncFetcher(Protocol):
    """Native-async chain fetcher. Implementations cooperate with the
    event loop directly — no `to_thread` wrapping needed.
    """

    source_name: str

    async def fetch(self, ticker: str, *, max_expirations: int = 4) -> OptionChainSnapshot:
        ...


def is_async_fetcher(fetcher: object) -> bool:
    """Runtime dispatch helper. typing.Protocol can't distinguish sync vs
    async impls structurally (both expose a `.fetch` attribute), so the
    refresh dispatcher uses `inspect.iscoroutinefunction` instead.
    """
    return inspect.iscoroutinefunction(getattr(fetcher, "fetch", None))


class YfinanceFetcher:
    """SyncFetcher wrapper around `fetch_yfinance_option_chain`.

    Permanent infra: yfinance is the A2 fallback path when IBKR/OPRA is
    unavailable (decision 11), so this wrapper is not throwaway scaffolding.
    """

    source_name: str = "yfinance"

    def fetch(self, ticker: str, *, max_expirations: int = 4) -> OptionChainSnapshot:
        return fetch_yfinance_option_chain(ticker, max_expirations=max_expirations)

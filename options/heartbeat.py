"""TWS Gateway heartbeat probe (Wave 2 D1 / plan A4).

A small asyncio task that probes IB Gateway every N seconds, emitting a
`data_provider_events('gateway_state_change')` row ONLY on state transitions
(not every probe). The `tws_gateway_up` gauge surfaced at /api/metrics
reads the most recent transition's payload to compute the current state.

Probe logic:
  1. If not connected → connectAsync(). If that raises, state = down.
  2. reqCurrentTimeAsync() — actual liveness check (isConnected can lag).
  3. State = up. Compare against previous; emit transition row if changed.

We hold one long-lived IB instance with its own client_id (separate from
the fetcher's) so concurrent fetches don't collide. The connection is
re-established cleanly on the next probe if it drops.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Optional

from config import settings
from data.models import DataProviderEvent, DataProviderEventType
from data.storage import record_data_provider_event

logger = logging.getLogger(__name__)


class TWSHeartbeat:
    def __init__(
        self,
        *,
        host: str | None = None,
        port: int | None = None,
        client_id: int | None = None,
        interval_s: float | None = None,
    ) -> None:
        self.host = host or settings.ibkr_host
        self.port = port or settings.ibkr_port
        self.client_id = client_id or settings.ibkr_client_id_heartbeat
        self.interval_s = interval_s or settings.ibkr_heartbeat_interval_s

        self._task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._ib = None
        # `None` = no probe yet; True/False = last observed state. Used to
        # gate the transition-only emission rule.
        self._last_state: bool | None = None
        self._last_probe_at: datetime | None = None

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="tws-heartbeat")
        logger.info(
            f"TWS heartbeat started: {self.host}:{self.port} every {self.interval_s}s"
        )

    async def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        await self._disconnect_quietly()

    async def _run(self) -> None:
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                up = await self._probe_once()
                self._last_probe_at = datetime.now()
                self._maybe_emit_transition(up)
            except Exception as exc:
                # Heartbeat must never crash the app — log and continue.
                logger.exception(f"TWS heartbeat probe raised unexpectedly: {exc}")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.interval_s)
                # If wait returned without timeout, stop was requested — exit.
                return
            except asyncio.TimeoutError:
                continue

    async def _probe_once(self) -> bool:
        # Local import: keeps `ib_async` optional for yfinance-only envs.
        from ib_async import IB

        if self._ib is None or not self._ib.isConnected():
            self._ib = IB()
            try:
                await asyncio.wait_for(
                    self._ib.connectAsync(self.host, self.port, clientId=self.client_id),
                    timeout=settings.ibkr_connect_timeout_s,
                )
            except Exception as exc:
                logger.debug(f"TWS heartbeat: connect failed ({type(exc).__name__}: {exc})")
                self._ib = None
                return False

        try:
            await asyncio.wait_for(self._ib.reqCurrentTimeAsync(), timeout=5.0)
            return True
        except Exception as exc:
            logger.debug(f"TWS heartbeat: reqCurrentTime failed ({type(exc).__name__}: {exc})")
            await self._disconnect_quietly()
            return False

    def _maybe_emit_transition(self, up: bool) -> None:
        # First probe: always emit so /api/metrics has a value to read.
        if self._last_state is None or self._last_state != up:
            previous = self._last_state
            from_state = "unknown" if previous is None else ("up" if previous else "down")
            to_state = "up" if up else "down"
            record_data_provider_event(
                DataProviderEvent(
                    event_type=DataProviderEventType.GATEWAY_STATE_CHANGE,
                    from_provider=from_state,
                    to_provider=to_state,
                    reason=f"heartbeat probe transition {from_state} -> {to_state}",
                    payload={"up": up, "host": self.host, "port": self.port},
                )
            )
            logger.info(f"TWS gateway state: {from_state} -> {to_state}")
        self._last_state = up

    async def _disconnect_quietly(self) -> None:
        if self._ib is None:
            return
        try:
            self._ib.disconnect()
        except Exception as exc:
            logger.debug(f"TWS heartbeat: disconnect raised ({type(exc).__name__}: {exc})")
        finally:
            self._ib = None


# Module-level singleton: one heartbeat per process. Lifespan in main.py
# starts/stops it; tests can poke directly.
_heartbeat: TWSHeartbeat | None = None


def get_heartbeat() -> TWSHeartbeat:
    global _heartbeat
    if _heartbeat is None:
        _heartbeat = TWSHeartbeat()
    return _heartbeat

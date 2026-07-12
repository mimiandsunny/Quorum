"""IBKRFetcher: AsyncFetcher impl backed by ib_async + IB Gateway.

Wave 2 D1 P5 (plan rev 4). Native-async fetcher; the dispatch in
`options/refresh_job.py` awaits `fetch()` directly (no `asyncio.to_thread`).

Connection model: one IB instance per `fetch()` call. Connect → fetch →
disconnect. This is intentionally simple — pooling adds session-lifecycle
bugs (disconnect-during-flight, client-id collision, restart races) that
buy nothing at our 50-ticker / per-day refresh cadence. If we ever grow
to intra-day refresh, revisit.

Greeks strategy is config-switchable (`settings.options_greeks_strategy`):
  - `local_bs`      → don't request modelGreeks at all. Compute IV +
                      delta/gamma/theta/vega via Black-Scholes from OPRA
                      bid/ask mid. Default; works with just the OPRA sub.
  - `ibkr_provider` → use IBKR's modelGreeks (needs the underlying-equity
                      data sub — free once monthly commissions ≥ $30).

`source='ibkr'` either way. `greeks_source` reports the actual provenance:
'local_bs', 'provider', 'provider_nan', or 'none'.

Per decision 16: no per-ticker prompt construction here — fetcher returns
typed `OptionChainSnapshot` with structured fields. Prompt-injection
hardening is upstream's problem, not the fetcher's.
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime
from typing import Any

from config import settings
from options.greeks import derive_greeks_from_mid
from options.models import (
    OptionChainSnapshot,
    OptionChainSource,
    OptionContractSnapshot,
    OptionType,
)

logger = logging.getLogger(__name__)


def _none_if_nan(value: float | None) -> float | None:
    """Normalize ib_async "no data" markers to None.

    ib_async uses two sentinels for missing prices:
      - `nan` (float NaN) — most price / Greek fields
      - `-1.0` — bid/ask/last when IBKR returns an explicit empty response
        (see `IBDefaults.emptyPrice` in ib_async)
    Both should be treated as missing so the BS solver and downstream
    code don't silently consume -1.0 as a real number.
    """
    if value is None:
        return None
    try:
        if math.isnan(value):
            return None
    except (TypeError, ValueError):
        return None
    if value == -1.0:
        return None
    return value


def _greeks_source_for(modelGreeks: Any) -> str:
    """Per A2: distinguish a NaN-laden Greeks payload from a clean one.

    `provider_nan` matters because the UI can still render the contract row
    (price + OI usable) but should badge the Greek columns as estimated.
    """
    if modelGreeks is None:
        return "none"
    delta = getattr(modelGreeks, "delta", None)
    iv = getattr(modelGreeks, "impliedVol", None)
    if _none_if_nan(delta) is None or _none_if_nan(iv) is None:
        return "provider_nan"
    return "provider"


class IBKRFetcher:
    """AsyncFetcher implementation. See module docstring."""

    source_name: str = "ibkr"

    def __init__(
        self,
        *,
        host: str | None = None,
        port: int | None = None,
        client_id: int | None = None,
        market_data_type: int | None = None,
        connect_timeout_s: float | None = None,
        per_expiration_timeout_s: float | None = None,
    ) -> None:
        self.host = host or settings.ibkr_host
        self.port = port or settings.ibkr_port
        self.client_id = client_id or settings.ibkr_client_id_fetcher
        self.market_data_type = market_data_type or settings.ibkr_market_data_type
        self.connect_timeout_s = connect_timeout_s or settings.ibkr_connect_timeout_s
        self.per_expiration_timeout_s = (
            per_expiration_timeout_s or settings.ibkr_fetch_timeout_per_expiration_s
        )

    async def fetch(self, ticker: str, *, max_expirations: int = 4) -> OptionChainSnapshot:
        # Local import keeps `ib_async` optional for environments that only
        # use the yfinance path (CI, dev boxes without Gateway running).
        from ib_async import IB, Stock

        symbol = ticker.upper()
        ib = IB()
        await asyncio.wait_for(
            ib.connectAsync(self.host, self.port, clientId=self.client_id),
            timeout=self.connect_timeout_s,
        )
        # marketDataType=1 (live) requires OPRA subscription. 3 = delayed,
        # 4 = delayed-frozen. Set by config so dev boxes can opt in to
        # delayed without code changes.
        ib.reqMarketDataType(self.market_data_type)

        try:
            stock = Stock(symbol, "SMART", "USD")
            (qualified,) = await ib.qualifyContractsAsync(stock)

            underlying_price = await self._fetch_underlying_price(ib, qualified)

            chain_params = await ib.reqSecDefOptParamsAsync(
                qualified.symbol, "", qualified.secType, qualified.conId
            )
            if not chain_params:
                raise ValueError(f"No option chain params returned for {symbol}")

            # Prefer SMART-routed chain when present; otherwise first listing.
            chain = next(
                (c for c in chain_params if c.exchange == "SMART"), chain_params[0]
            )
            expirations = sorted(chain.expirations)[:max_expirations]
            strikes_by_expiration = {
                exp: self._select_strikes_near_atm(chain.strikes, underlying_price)
                for exp in expirations
            }

            contracts = await self._fetch_all_expirations(
                ib=ib,
                symbol=symbol,
                expirations=expirations,
                strikes_by_expiration=strikes_by_expiration,
                underlying_price=underlying_price,
            )

            parsed_expirations = [
                datetime.strptime(exp, "%Y%m%d").date() for exp in expirations
            ]
            return OptionChainSnapshot(
                ticker=symbol,
                captured_at=datetime.now(),
                source=OptionChainSource.IBKR,
                underlying_price=underlying_price,
                expirations=parsed_expirations,
                contracts=contracts,
                metadata={
                    "max_expirations": max_expirations,
                    "exchange": chain.exchange,
                    "trading_class": chain.tradingClass,
                    "market_data_type": self.market_data_type,
                },
            )
        finally:
            try:
                ib.disconnect()
            except Exception as exc:
                logger.warning(f"[{symbol}] IB disconnect raised: {exc}")

    async def _fetch_underlying_price(self, ib, stock) -> float:
        # snapshot=True returns once last/close is populated, no streaming.
        # Field priority: live first, then delayed (DLY suffix populated when
        # marketDataType=3 falls back). `close` is yesterday's settlement —
        # acceptable last resort for sizing ATM strikes.
        (ticker,) = await ib.reqTickersAsync(stock)
        candidates: list[tuple[str, object]] = [
            ("last", getattr(ticker, "last", None)),
            ("close", getattr(ticker, "close", None)),
            ("delayedLast", getattr(ticker, "delayedLast", None)),
            ("delayedClose", getattr(ticker, "delayedClose", None)),
            ("bid", getattr(ticker, "bid", None)),
            ("ask", getattr(ticker, "ask", None)),
        ]
        for _, raw in candidates:
            value = _none_if_nan(raw)
            if value and value > 0:
                return float(value)
        # `marketPrice()` is a method on most ib_async versions; it picks
        # the best available tick (live → delayed → close).
        mp = getattr(ticker, "marketPrice", None)
        if callable(mp):
            try:
                value = _none_if_nan(mp())
                if value and value > 0:
                    return float(value)
            except Exception:
                pass
        raise ValueError(f"Unable to determine underlying price for {stock.symbol}")

    def _select_strikes_near_atm(
        self, strikes: list[float], underlying_price: float, *, half_window: int = 8
    ) -> list[float]:
        """Pick `2*half_window+1` strikes centered on underlying.

        Caps the per-expiration request count: ~17 strikes × call+put × 4
        expirations = ~136 reqMktData lines per ticker, well inside IBKR's
        50/sec qualify and 100-line concurrent ticker pacing.
        """
        if not strikes:
            return []
        sorted_strikes = sorted(strikes)
        nearest = min(
            range(len(sorted_strikes)),
            key=lambda i: abs(sorted_strikes[i] - underlying_price),
        )
        lo = max(0, nearest - half_window)
        hi = min(len(sorted_strikes), nearest + half_window + 1)
        return sorted_strikes[lo:hi]

    async def _fetch_all_expirations(
        self,
        *,
        ib,
        symbol: str,
        expirations: list[str],
        strikes_by_expiration: dict[str, list[float]],
        underlying_price: float,
    ) -> list[OptionContractSnapshot]:
        results: list[OptionContractSnapshot] = []
        for expiration in expirations:
            try:
                rows = await asyncio.wait_for(
                    self._fetch_one_expiration(
                        ib=ib,
                        symbol=symbol,
                        expiration=expiration,
                        strikes=strikes_by_expiration[expiration],
                        underlying_price=underlying_price,
                    ),
                    timeout=self.per_expiration_timeout_s,
                )
                results.extend(rows)
            except asyncio.TimeoutError:
                logger.warning(
                    f"[{symbol}] expiration {expiration} timed out after "
                    f"{self.per_expiration_timeout_s}s; skipping that slice"
                )
        return results

    async def _fetch_one_expiration(
        self,
        *,
        ib,
        symbol: str,
        expiration: str,
        strikes: list[float],
        underlying_price: float,
    ) -> list[OptionContractSnapshot]:
        from ib_async import Option

        if not strikes:
            return []

        contracts: list = []
        for strike in strikes:
            for right in ("C", "P"):
                contracts.append(Option(symbol, expiration, strike, right, "SMART"))

        qualified = await ib.qualifyContractsAsync(*contracts)
        qualified = [q for q in qualified if q is not None and q.conId]
        if not qualified:
            return []

        # reqTickersAsync returns ticker objects when each row's fields
        # are populated; ib_async waits internally up to ~11s per IBKR
        # snapshot pacing, then yields what it has.
        tickers = await ib.reqTickersAsync(*qualified)

        rows: list[OptionContractSnapshot] = []
        exp_date = datetime.strptime(expiration, "%Y%m%d").date()
        use_provider_greeks = settings.options_greeks_strategy == "ibkr_provider"

        for t in tickers:
            contract = t.contract
            bid = _none_if_nan(t.bid)
            ask = _none_if_nan(t.ask)
            last = _none_if_nan(t.last)

            # OI: IBKR exposes call OI on call rows and put OI on put rows.
            if contract.right == "C":
                oi = _none_if_nan(getattr(t, "callOpenInterest", None))
            else:
                oi = _none_if_nan(getattr(t, "putOpenInterest", None))

            iv = delta = gamma = theta = vega = None
            if use_provider_greeks:
                mg = t.modelGreeks
                iv = _none_if_nan(getattr(mg, "impliedVol", None)) if mg else None
                delta = _none_if_nan(getattr(mg, "delta", None)) if mg else None
                gamma = _none_if_nan(getattr(mg, "gamma", None)) if mg else None
                theta = _none_if_nan(getattr(mg, "theta", None)) if mg else None
                vega = _none_if_nan(getattr(mg, "vega", None)) if mg else None
            else:
                # local_bs path: solve IV from mid → derive Greeks. No
                # external request, no entitlement issue, no NaN noise.
                computed = derive_greeks_from_mid(
                    underlying_price=underlying_price,
                    strike=float(contract.strike),
                    expiration=exp_date,
                    option_type=contract.right,
                    bid=bid,
                    ask=ask,
                    last_price=last,
                    risk_free_rate=settings.risk_free_rate,
                )
                iv = computed["iv"]
                delta = computed["delta"]
                gamma = computed["gamma"]
                theta = computed["theta"]
                vega = computed["vega"]

            rows.append(
                OptionContractSnapshot(
                    contract_symbol=contract.localSymbol or f"{symbol}{expiration}{contract.right}{contract.strike}",
                    ticker=symbol,
                    expiration=exp_date,
                    option_type=OptionType.CALL if contract.right == "C" else OptionType.PUT,
                    strike=float(contract.strike),
                    bid=bid,
                    ask=ask,
                    last_price=last,
                    implied_volatility=iv,
                    open_interest=int(oi) if oi is not None else None,
                    volume=int(_none_if_nan(t.volume) or 0) if t.volume else None,
                    delta=delta,
                    gamma=gamma,
                    theta=theta,
                    vega=vega,
                )
            )
        return rows

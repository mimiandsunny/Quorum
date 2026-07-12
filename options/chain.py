from __future__ import annotations

import time
from datetime import datetime

from options.models import OptionChainSnapshot, OptionChainSource, OptionContractSnapshot, OptionType

# Tuning knob for K5: throttle between yfinance per-expiration calls so a single
# refresh doesn't burn the rate-limit budget. Moot once IBKRFetcher (Wave 2 D1)
# becomes the primary provider; kept as the fallback path's safety belt.
_YFINANCE_INTER_CALL_SLEEP_S = 0.1


def _none_if_nan(value):
    try:
        if value != value:
            return None
    except TypeError:
        pass
    return value


def _float_or_none(value) -> float | None:
    value = _none_if_nan(value)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value) -> int | None:
    value = _none_if_nan(value)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _underlying_price(ticker_obj) -> float:
    fast_info = getattr(ticker_obj, "fast_info", {}) or {}
    for key in ("last_price", "lastPrice", "regular_market_price"):
        value = _float_or_none(fast_info.get(key) if hasattr(fast_info, "get") else None)
        if value and value > 0:
            return value

    history = ticker_obj.history(period="5d")
    if history is not None and not history.empty:
        close = _float_or_none(history["Close"].dropna().iloc[-1])
        if close and close > 0:
            return close
    raise ValueError("Unable to determine underlying price")


def _contracts_from_frame(
    *,
    ticker: str,
    expiration,
    option_type: OptionType,
    frame,
) -> list[OptionContractSnapshot]:
    contracts = []
    if frame is None or frame.empty:
        return contracts
    for _, row in frame.iterrows():
        contract_symbol = row.get("contractSymbol")
        strike = _float_or_none(row.get("strike"))
        if not contract_symbol or strike is None:
            continue
        contracts.append(
            OptionContractSnapshot(
                contract_symbol=str(contract_symbol),
                ticker=ticker,
                expiration=expiration,
                option_type=option_type,
                strike=strike,
                bid=_float_or_none(row.get("bid")),
                ask=_float_or_none(row.get("ask")),
                last_price=_float_or_none(row.get("lastPrice")),
                implied_volatility=_float_or_none(row.get("impliedVolatility")),
                open_interest=_int_or_none(row.get("openInterest")),
                volume=_int_or_none(row.get("volume")),
            )
        )
    return contracts


def fetch_yfinance_option_chain(
    ticker: str,
    *,
    max_expirations: int = 4,
) -> OptionChainSnapshot:
    """Fetch a compact option-chain snapshot using yfinance.

    yfinance does not provide trade aggressor side or reliable Greeks, so this
    snapshot supports IV/liquidity/flow ranking first. Greeks can be filled by a
    richer provider later without changing the dashboard contract.
    """
    import yfinance as yf

    symbol = ticker.upper()
    ticker_obj = yf.Ticker(symbol)
    expirations = list(ticker_obj.options or [])[:max_expirations]
    if not expirations:
        raise ValueError(f"No option expirations found for {symbol}")

    underlying_price = _underlying_price(ticker_obj)
    contracts = []
    parsed_expirations = []
    for index, expiration_text in enumerate(expirations):
        if index > 0:
            time.sleep(_YFINANCE_INTER_CALL_SLEEP_S)
        expiration = datetime.strptime(expiration_text, "%Y-%m-%d").date()
        chain = ticker_obj.option_chain(expiration_text)
        contracts.extend(
            _contracts_from_frame(
                ticker=symbol,
                expiration=expiration,
                option_type=OptionType.CALL,
                frame=chain.calls,
            )
        )
        contracts.extend(
            _contracts_from_frame(
                ticker=symbol,
                expiration=expiration,
                option_type=OptionType.PUT,
                frame=chain.puts,
            )
        )
        parsed_expirations.append(expiration)

    return OptionChainSnapshot(
        ticker=symbol,
        captured_at=datetime.now(),
        source=OptionChainSource.YFINANCE,
        underlying_price=underlying_price,
        expirations=parsed_expirations,
        contracts=contracts,
        metadata={"max_expirations": max_expirations},
    )


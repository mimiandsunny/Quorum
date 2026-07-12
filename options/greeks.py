"""Black-Scholes pricing, Greeks, and IV solver.

Used as the local-compute alternative to IBKR's `modelGreeks` when the
account doesn't have the equity-data entitlement that IBKR's Greek engine
needs (the `NASDAQ.NMS/TOP/ALL` line in error 10091). Same math, different
provider — flip `settings.options_greeks_strategy` to `'ibkr_provider'`
once the equity sub is unlocked (free with monthly commissions ≥ $30).

Conventions:
- T is time-to-expiration in years (day-count = (calendar_days)/365).
- r is the continuously-compounded risk-free rate (default 4.5%).
- All Greeks are per 1.0 of the underlying / 1.0 of vol / 1 day for theta.
- IV solver uses bisection over [0.01, 5.0] — robust, ~25 iterations to
  reach 1e-4 tolerance which is the precision IBKR itself prints.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Literal

OptionRight = Literal["C", "P", "call", "put"]


def _is_call(option_type: OptionRight) -> bool:
    return str(option_type).lower() in {"c", "call"}


def _norm_cdf(x: float) -> float:
    # Standard normal CDF via erf — accurate to machine precision.
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _d1_d2(S: float, K: float, T: float, r: float, sigma: float) -> tuple[float, float]:
    if sigma <= 0 or T <= 0 or S <= 0 or K <= 0:
        raise ValueError("inputs must be positive for d1/d2")
    sqrt_t = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    return d1, d2


def bs_price(
    *, S: float, K: float, T: float, r: float, sigma: float, option_type: OptionRight
) -> float:
    """Standard Black-Scholes European-style option price (no dividends)."""
    d1, d2 = _d1_d2(S, K, T, r, sigma)
    discount = math.exp(-r * T)
    if _is_call(option_type):
        return S * _norm_cdf(d1) - K * discount * _norm_cdf(d2)
    return K * discount * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def bs_delta(
    *, S: float, K: float, T: float, r: float, sigma: float, option_type: OptionRight
) -> float:
    d1, _ = _d1_d2(S, K, T, r, sigma)
    if _is_call(option_type):
        return _norm_cdf(d1)
    return _norm_cdf(d1) - 1.0


def bs_gamma(*, S: float, K: float, T: float, r: float, sigma: float) -> float:
    d1, _ = _d1_d2(S, K, T, r, sigma)
    return _norm_pdf(d1) / (S * sigma * math.sqrt(T))


def bs_vega(*, S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Vega per 1.0 change in vol (multiply by 0.01 for "per 1 vol point")."""
    d1, _ = _d1_d2(S, K, T, r, sigma)
    return S * _norm_pdf(d1) * math.sqrt(T)


def bs_theta(
    *, S: float, K: float, T: float, r: float, sigma: float, option_type: OptionRight
) -> float:
    """Theta per 1 day (matches how IBKR + most brokers display it)."""
    d1, d2 = _d1_d2(S, K, T, r, sigma)
    pdf_d1 = _norm_pdf(d1)
    discount_term = r * K * math.exp(-r * T)
    sqrt_t = math.sqrt(T)
    if _is_call(option_type):
        annualized = -(S * pdf_d1 * sigma) / (2.0 * sqrt_t) - discount_term * _norm_cdf(d2)
    else:
        annualized = -(S * pdf_d1 * sigma) / (2.0 * sqrt_t) + discount_term * _norm_cdf(-d2)
    return annualized / 365.0


# IV solver bracket. Below 1% vol the price is dominated by intrinsic and
# numerical roots get unstable; above 500% the option is a lottery ticket
# anyway. Real equity options sit at 10-150% vol almost always.
_IV_LOWER = 0.01
_IV_UPPER = 5.0
_IV_TOL = 1e-4
_IV_MAX_ITERATIONS = 60


def implied_volatility(
    *,
    S: float,
    K: float,
    T: float,
    r: float,
    market_price: float,
    option_type: OptionRight,
) -> float | None:
    """Solve for sigma such that bs_price(...) ≈ market_price via bisection.

    Returns None when:
      - inputs invalid (non-positive S/K/T or negative price)
      - market price violates no-arbitrage bounds (mid < intrinsic, etc.)
      - bisection fails to converge inside [_IV_LOWER, _IV_UPPER]
    Bisection (vs Newton-Raphson): slower but cannot diverge near deep ITM
    or near-zero-vega contracts. Both run sub-millisecond per call so the
    speed difference is irrelevant at our 50-ticker scale.
    """
    if S <= 0 or K <= 0 or T <= 0 or market_price <= 0:
        return None

    is_call = _is_call(option_type)
    discount = math.exp(-r * T)
    intrinsic = max(0.0, (S - K * discount) if is_call else (K * discount - S))
    upper_bound = S if is_call else K * discount
    if market_price < intrinsic - _IV_TOL or market_price > upper_bound + _IV_TOL:
        return None

    lo, hi = _IV_LOWER, _IV_UPPER
    f_lo = bs_price(S=S, K=K, T=T, r=r, sigma=lo, option_type=option_type) - market_price
    f_hi = bs_price(S=S, K=K, T=T, r=r, sigma=hi, option_type=option_type) - market_price
    if f_lo * f_hi > 0:
        # Both ends on the same side — root is outside our bracket.
        return None

    for _ in range(_IV_MAX_ITERATIONS):
        mid = 0.5 * (lo + hi)
        f_mid = bs_price(S=S, K=K, T=T, r=r, sigma=mid, option_type=option_type) - market_price
        if abs(f_mid) < _IV_TOL:
            return round(mid, 4)
        if f_mid * f_lo < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return round(0.5 * (lo + hi), 4)


def years_to_expiration(expiration: date, *, as_of: date | datetime | None = None) -> float:
    """Calendar-day count divided by 365. Expiration day = end of US trading
    session — for a same-day expiry we return 1/365 (≈ 4 hours of intraday
    trading) instead of 0 so the IV solver doesn't divide by zero.
    """
    if as_of is None:
        as_of = date.today()
    elif isinstance(as_of, datetime):
        as_of = as_of.date()
    days = (expiration - as_of).days
    if days <= 0:
        return 1.0 / 365.0
    return days / 365.0


def derive_greeks_from_mid(
    *,
    underlying_price: float,
    strike: float,
    expiration: date,
    option_type: OptionRight,
    bid: float | None,
    ask: float | None,
    last_price: float | None = None,
    risk_free_rate: float,
    as_of: date | datetime | None = None,
) -> dict[str, float | None]:
    """One-stop helper: pick a usable mid → solve IV → derive all Greeks.

    Mid priority: bid+ask midpoint when both are positive, else last_price.
    Returns `{'iv', 'delta', 'gamma', 'theta', 'vega'}` with None when the
    inputs don't permit a stable solve (e.g., zero bid/ask, expired contract,
    arbitrage violation).
    """
    if bid is not None and ask is not None and bid > 0 and ask > 0 and ask >= bid:
        mid = (bid + ask) / 2.0
    elif last_price is not None and last_price > 0:
        mid = last_price
    else:
        return {"iv": None, "delta": None, "gamma": None, "theta": None, "vega": None}

    T = years_to_expiration(expiration, as_of=as_of)
    iv = implied_volatility(
        S=underlying_price,
        K=strike,
        T=T,
        r=risk_free_rate,
        market_price=mid,
        option_type=option_type,
    )
    if iv is None:
        return {"iv": None, "delta": None, "gamma": None, "theta": None, "vega": None}

    return {
        "iv": iv,
        "delta": round(
            bs_delta(S=underlying_price, K=strike, T=T, r=risk_free_rate, sigma=iv, option_type=option_type),
            4,
        ),
        "gamma": round(
            bs_gamma(S=underlying_price, K=strike, T=T, r=risk_free_rate, sigma=iv), 6
        ),
        "theta": round(
            bs_theta(S=underlying_price, K=strike, T=T, r=risk_free_rate, sigma=iv, option_type=option_type),
            4,
        ),
        "vega": round(
            bs_vega(S=underlying_price, K=strike, T=T, r=risk_free_rate, sigma=iv), 4
        ),
    }

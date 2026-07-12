from __future__ import annotations

import math
import statistics

from data.models import OHLCVBar, TickerDataPackage


SECTOR_BENCHMARKS = {
    "Technology": "XLK",
    "Financial Services": "XLF",
    "Healthcare": "XLV",
    "Consumer Cyclical": "XLY",
    "Consumer Defensive": "XLP",
    "Energy": "XLE",
    "Industrials": "XLI",
    "Basic Materials": "XLB",
    "Communication Services": "XLC",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
}


def _round(value: float | None, digits: int = 4) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, digits)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return min(max(value, low), high)


def _return_pct(bars: list[OHLCVBar], lookback_days: int) -> float | None:
    if len(bars) <= lookback_days:
        return None
    start = bars[-lookback_days - 1].close
    end = bars[-1].close
    if start <= 0:
        return None
    return _round((end - start) / start)


def _daily_returns(bars: list[OHLCVBar], limit: int) -> list[float]:
    recent = bars[-(limit + 1):]
    returns = []
    for prev, cur in zip(recent, recent[1:]):
        if prev.close > 0:
            returns.append((cur.close - prev.close) / prev.close)
    return returns


def _realized_volatility(bars: list[OHLCVBar], limit: int = 20) -> float | None:
    returns = _daily_returns(bars, limit)
    if len(returns) < 2:
        return None
    return _round(statistics.stdev(returns) * math.sqrt(252))


def _beta_to_benchmark(
    bars: list[OHLCVBar],
    benchmark_bars: list[OHLCVBar] | None,
    limit: int = 20,
) -> float | None:
    if not benchmark_bars:
        return None
    returns = _daily_returns(bars, limit)
    benchmark_returns = _daily_returns(benchmark_bars, limit)
    sample_size = min(len(returns), len(benchmark_returns))
    if sample_size < 2:
        return None

    returns = returns[-sample_size:]
    benchmark_returns = benchmark_returns[-sample_size:]
    benchmark_variance = statistics.variance(benchmark_returns)
    if benchmark_variance <= 0:
        return None
    covariance = statistics.covariance(returns, benchmark_returns)
    return _round(covariance / benchmark_variance)


def _max_drawdown_pct(bars: list[OHLCVBar], limit: int = 20) -> float | None:
    recent = bars[-(limit + 1):]
    if len(recent) < 2:
        return None

    peak = recent[0].close
    max_drawdown = 0.0
    for bar in recent:
        if bar.close <= 0:
            return None
        peak = max(peak, bar.close)
        if peak > 0:
            max_drawdown = min(max_drawdown, (bar.close - peak) / peak)
    return _round(max_drawdown)


def _average_true_range_pct(bars: list[OHLCVBar], limit: int = 14) -> float | None:
    if len(bars) < 2:
        return None
    recent = bars[-(limit + 1):]
    ranges = []
    for prev, cur in zip(recent, recent[1:]):
        true_range = max(
            cur.high - cur.low,
            abs(cur.high - prev.close),
            abs(cur.low - prev.close),
        )
        ranges.append(true_range)
    if not ranges or bars[-1].close <= 0:
        return None
    return _round((sum(ranges) / len(ranges)) / bars[-1].close)


def _distance_pct(value: float | None, current: float) -> float | None:
    if value is None or current <= 0:
        return None
    return _round((current - value) / current)


def _gap_pct(bars: list[OHLCVBar]) -> float | None:
    if len(bars) < 2:
        return None
    previous_close = bars[-2].close
    if previous_close <= 0:
        return None
    return _round((bars[-1].open - previous_close) / previous_close)


def _nearest_support_distance_pct(levels: list[float], current: float) -> float | None:
    if current <= 0:
        return None
    supports = [level for level in levels if level > 0 and level <= current]
    if not supports:
        return None
    return _round((current - max(supports)) / current)


def _nearest_resistance_distance_pct(levels: list[float], current: float) -> float | None:
    if current <= 0:
        return None
    resistances = [level for level in levels if level >= current]
    if not resistances:
        return None
    return _round((min(resistances) - current) / current)


def _avg_dollar_volume(bars: list[OHLCVBar], limit: int = 20) -> float | None:
    recent = bars[-limit:]
    if not recent:
        return None
    return _round(sum(bar.close * bar.volume for bar in recent) / len(recent), 2)


def _volume_spike(bars: list[OHLCVBar], limit: int = 20) -> float | None:
    if len(bars) < 2:
        return None
    recent = bars[-(limit + 1):-1]
    if not recent:
        return None
    avg_volume = sum(bar.volume for bar in recent) / len(recent)
    if avg_volume <= 0:
        return None
    return _round(bars[-1].volume / avg_volume)


def _liquidity_score(avg_dollar_volume: float | None) -> float | None:
    if avg_dollar_volume is None:
        return None
    return _round(min(max(avg_dollar_volume / 50_000_000, 0.0), 1.0))


def _relative_strength(stock_return: float | None, benchmark_return: float | None) -> float | None:
    if stock_return is None or benchmark_return is None:
        return None
    return _round(stock_return - benchmark_return)


def _factor_value_score(pe: float | None) -> float | None:
    if pe is None or pe <= 0:
        return None
    return _round(_clamp(1.0 - ((pe - 10.0) / 50.0)))


def _factor_quality_score(pkg: TickerDataPackage) -> float | None:
    fundamentals = pkg.fundamentals
    score = 0.50
    used = False

    if fundamentals.revenue_growth is not None:
        score += _clamp(fundamentals.revenue_growth, -0.25, 0.40) * 1.2
        used = True
    if fundamentals.debt_to_equity is not None:
        score -= min(max(fundamentals.debt_to_equity, 0.0), 3.0) * 0.10
        used = True
    if fundamentals.market_cap is not None:
        if fundamentals.market_cap >= 10_000_000_000:
            score += 0.10
        elif fundamentals.market_cap >= 1_000_000_000:
            score += 0.05
        else:
            score -= 0.05
        used = True

    return _round(_clamp(score)) if used else None


def _factor_momentum_score(
    returns: dict[str, float | None],
    technicals: dict[str, float | None],
) -> float | None:
    score = 0.50
    used = False

    if returns["20d"] is not None:
        score += _clamp(returns["20d"], -0.25, 0.30) * 1.5
        used = True
    if returns["5d"] is not None:
        score += _clamp(returns["5d"], -0.12, 0.15) * 2.0
        used = True
    if technicals["distance_to_ma_50_pct"] is not None:
        score += _clamp(technicals["distance_to_ma_50_pct"], -0.20, 0.20) * 0.8
        used = True
    if technicals["distance_to_ma_200_pct"] is not None:
        score += _clamp(technicals["distance_to_ma_200_pct"], -0.35, 0.35) * 0.4
        used = True

    return _round(_clamp(score)) if used else None


def _factor_low_vol_score(volatility: float | None) -> float | None:
    if volatility is None:
        return None
    return _round(_clamp(1.0 - (volatility / 0.80)))


def _factor_payload(
    *,
    pkg: TickerDataPackage,
    returns: dict[str, float | None],
    technicals: dict[str, float | None],
    realized_volatility: float | None,
    liquidity_score: float | None,
) -> dict:
    pe = pkg.fundamentals.forward_pe or pkg.fundamentals.pe_ratio
    factors = {
        "momentum_rank": _factor_momentum_score(returns, technicals),
        "value_rank": _factor_value_score(pe),
        "quality_rank": _factor_quality_score(pkg),
        "low_vol_rank": _factor_low_vol_score(realized_volatility),
        "liquidity_rank": liquidity_score,
    }
    populated = [value for value in factors.values() if value is not None]
    factors["composite_rank"] = _round(sum(populated) / len(populated)) if populated else None
    return factors


def feature_payload(pkg: TickerDataPackage) -> dict:
    """Compute deterministic v2 features from a data package."""
    bars = pkg.price_history
    current = pkg.technicals.current_price
    avg_dollar_volume_20 = _avg_dollar_volume(bars, 20)
    returns = {
        "1d": _return_pct(bars, 1),
        "5d": _return_pct(bars, 5),
        "20d": _return_pct(bars, 20),
        "60d": _return_pct(bars, 60),
    }
    realized_volatility_20 = _realized_volatility(bars, 20)
    liquidity_score = _liquidity_score(avg_dollar_volume_20)
    gap_pct = _gap_pct(bars)
    technicals = {
        "rsi_14": pkg.technicals.rsi_14,
        "macd": pkg.technicals.macd,
        "macd_signal": pkg.technicals.macd_signal,
        "macd_histogram": pkg.technicals.macd_histogram,
        "ma_50": pkg.technicals.ma_50,
        "ma_200": pkg.technicals.ma_200,
        "distance_to_ma_50_pct": _distance_pct(pkg.technicals.ma_50, current),
        "distance_to_ma_200_pct": _distance_pct(pkg.technicals.ma_200, current),
        "nearest_support_distance_pct": _nearest_support_distance_pct(
            pkg.technicals.support_levels, current,
        ),
        "nearest_resistance_distance_pct": _nearest_resistance_distance_pct(
            pkg.technicals.resistance_levels, current,
        ),
        "support_levels": pkg.technicals.support_levels,
        "resistance_levels": pkg.technicals.resistance_levels,
    }
    sector_benchmark = SECTOR_BENCHMARKS.get(pkg.fundamentals.sector or "")
    spy_bars = pkg.benchmark_price_history.get("SPY")
    qqq_bars = pkg.benchmark_price_history.get("QQQ")
    sector_bars = (
        pkg.benchmark_price_history.get(sector_benchmark)
        if sector_benchmark
        else None
    )
    spy_return_20 = _return_pct(spy_bars or [], 20)
    qqq_return_20 = _return_pct(qqq_bars or [], 20)
    sector_return_20 = _return_pct(sector_bars or [], 20)
    beta_20 = _beta_to_benchmark(bars, spy_bars, 20)

    return {
        "price_history_rows": len(bars),
        "current_price": current,
        "returns": returns,
        "risk": {
            "atr_14_pct": _average_true_range_pct(bars, 14),
            "realized_volatility_20d": realized_volatility_20,
            "max_drawdown_20d": _max_drawdown_pct(bars, 20),
            "gap_pct": gap_pct,
            "overnight_risk_flag": bool(gap_pct is not None and abs(gap_pct) >= 0.03),
            "beta_20d": beta_20,
            "benchmark_sensitivity": beta_20,
        },
        "relative_strength": {
            "self_5d": returns["5d"],
            "self_20d": returns["20d"],
            "vs_spy_20d": _relative_strength(returns["20d"], spy_return_20),
            "vs_qqq_20d": _relative_strength(returns["20d"], qqq_return_20),
            "vs_sector_20d": _relative_strength(returns["20d"], sector_return_20),
            "sector_benchmark": sector_benchmark,
        },
        "technicals": technicals,
        "liquidity": {
            "avg_dollar_volume_20d": avg_dollar_volume_20,
            "volume_spike_20d": _volume_spike(bars, 20),
            "liquidity_score": liquidity_score,
        },
        "fundamentals": {
            "sector": pkg.fundamentals.sector,
            "industry": pkg.fundamentals.industry,
            "market_cap": pkg.fundamentals.market_cap,
            "revenue_growth": pkg.fundamentals.revenue_growth,
            "pe_ratio": pkg.fundamentals.pe_ratio,
            "forward_pe": pkg.fundamentals.forward_pe,
            "debt_to_equity": pkg.fundamentals.debt_to_equity,
            "dividend_yield": pkg.fundamentals.dividend_yield,
            "earnings_date": pkg.fundamentals.earnings_date.isoformat()
            if pkg.fundamentals.earnings_date else None,
        },
        "factors": _factor_payload(
            pkg=pkg,
            returns=returns,
            technicals=technicals,
            realized_volatility=realized_volatility_20,
            liquidity_score=liquidity_score,
        ),
    }

import logging
import time
from datetime import datetime, timedelta, date
from pathlib import Path

import numpy as np
import pandas as pd
import ta
import yfinance as yf

from config import settings
from data.models import (
    FundamentalsData,
    NewsItem,
    OHLCVBar,
    TechnicalIndicators,
    TickerDataPackage,
)

logger = logging.getLogger(__name__)

DIGEST_PATH = Path(__file__).parent / "external" / "chatgpt_digest.txt"


def read_digest() -> str | None:
    """Read today's ChatGPT macro digest. Returns None if missing or stale."""
    if not DIGEST_PATH.exists():
        logger.info("No ChatGPT digest file found")
        return None
    mtime = datetime.fromtimestamp(DIGEST_PATH.stat().st_mtime)
    if mtime.date() != date.today():
        logger.info(f"ChatGPT digest is stale (last modified {mtime.date()})")
        return None
    content = DIGEST_PATH.read_text().strip()
    if not content:
        logger.info("ChatGPT digest file is empty")
        return None
    logger.info(f"ChatGPT digest loaded ({len(content)} chars)")
    return content


def fetch_price_history(ticker: str, days: int = settings.price_history_days) -> pd.DataFrame:
    """Fetch OHLCV data from YFinance. Returns empty DataFrame on failure."""
    try:
        stock = yf.Ticker(ticker)
        end = datetime.now()
        start = end - timedelta(days=days)
        df = stock.history(start=start, end=end, timeout=30)
        if df.empty:
            logger.warning(f"No price data returned for {ticker}")
        return df
    except Exception as e:
        logger.error(f"Failed to fetch price data for {ticker}: {e}")
        return pd.DataFrame()


def compute_technicals(df: pd.DataFrame) -> TechnicalIndicators:
    """Compute technical indicators deterministically from OHLCV data."""
    close = df["Close"]
    current_price = float(close.iloc[-1])

    # RSI
    rsi_14 = float(ta.momentum.rsi(close, window=14).iloc[-1])

    # MACD
    macd_ind = ta.trend.MACD(close)
    macd_val = float(macd_ind.macd().iloc[-1])
    macd_signal = float(macd_ind.macd_signal().iloc[-1])
    macd_hist = float(macd_ind.macd_diff().iloc[-1])

    # Moving averages
    ma_50 = float(close.rolling(50).mean().iloc[-1]) if len(close) >= 50 else None
    ma_200 = float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else None

    # Support/resistance from recent pivots (simplified: local min/max over 20-day windows)
    supports, resistances = _find_support_resistance(df)

    return TechnicalIndicators(
        rsi_14=rsi_14,
        macd=macd_val,
        macd_signal=macd_signal,
        macd_histogram=macd_hist,
        ma_50=ma_50,
        ma_200=ma_200,
        current_price=current_price,
        support_levels=supports,
        resistance_levels=resistances,
    )


def _find_support_resistance(
    df: pd.DataFrame, window: int = 20, num_levels: int = 3,
) -> tuple[list[float], list[float]]:
    """Find support/resistance from local minima/maxima of closing prices."""
    close = df["Close"]
    current = float(close.iloc[-1])
    supports = []
    resistances = []

    for i in range(window, len(close) - window):
        segment = close.iloc[i - window : i + window + 1]
        val = float(close.iloc[i])
        if val == float(segment.min()):
            if val < current:
                supports.append(round(val, 2))
        elif val == float(segment.max()):
            if val > current:
                resistances.append(round(val, 2))

    # Deduplicate nearby levels (within 1%) and take closest ones
    supports = _dedupe_levels(sorted(supports, reverse=True), pct=0.01)[:num_levels]
    resistances = _dedupe_levels(sorted(resistances), pct=0.01)[:num_levels]
    return supports, resistances


def _dedupe_levels(levels: list[float], pct: float = 0.01) -> list[float]:
    """Remove levels within pct of each other, keeping the first."""
    if not levels:
        return []
    result = [levels[0]]
    for lvl in levels[1:]:
        if abs(lvl - result[-1]) / result[-1] > pct:
            result.append(lvl)
    return result


def fetch_fundamentals(ticker: str) -> FundamentalsData:
    """Fetch fundamental data from YFinance. Falls back gracefully on missing fields."""
    try:
        stock = yf.Ticker(ticker)
        stock._request_timeout = 30
        info = stock.info or {}

        earnings_date = None
        if info.get("earningsTimestamp"):
            earnings_date = date.fromtimestamp(info["earningsTimestamp"])

        return FundamentalsData(
            pe_ratio=info.get("trailingPE"),
            forward_pe=info.get("forwardPE"),
            eps=info.get("trailingEps"),
            revenue_growth=info.get("revenueGrowth"),
            debt_to_equity=info.get("debtToEquity"),
            market_cap=info.get("marketCap"),
            sector=info.get("sector"),
            industry=info.get("industry"),
            earnings_date=earnings_date,
            dividend_yield=info.get("dividendYield"),
        )
    except Exception as e:
        logger.error(f"Failed to fetch fundamentals for {ticker}: {e}")
        return FundamentalsData()


def fetch_news(ticker: str) -> list[NewsItem]:
    """Fetch recent news from YFinance."""
    try:
        stock = yf.Ticker(ticker)
        stock._request_timeout = 15
        news_items = stock.news or []
        result = []
        for item in news_items[:10]:
            content = item.get("content", {})
            result.append(NewsItem(
                headline=content.get("title", item.get("title", "")),
                source=content.get("provider", {}).get("displayName", ""),
                summary=content.get("summary", ""),
            ))
        return result
    except Exception as e:
        logger.error(f"Failed to fetch news for {ticker}: {e}")
        return []


def build_data_package(ticker: str) -> TickerDataPackage | None:
    """Build a complete data package for one ticker. Returns None if price data fails."""
    stale_sources: list[str] = []
    t0 = time.monotonic()
    logger.info(f"[{ticker}] Building data package...")

    # Price data is required
    t1 = time.monotonic()
    logger.debug(f"[{ticker}] Fetching price history...")
    price_df = fetch_price_history(ticker)
    logger.debug(f"[{ticker}] Price history fetched in {time.monotonic() - t1:.1f}s ({len(price_df)} rows)")
    if price_df.empty:
        logger.error(f"Skipping {ticker}: no price data available")
        return None

    # Convert to OHLCVBar list
    price_history = []
    for idx, row in price_df.iterrows():
        price_history.append(OHLCVBar(
            date=idx.date(),
            open=round(float(row["Open"]), 2),
            high=round(float(row["High"]), 2),
            low=round(float(row["Low"]), 2),
            close=round(float(row["Close"]), 2),
            volume=int(row["Volume"]),
        ))

    # Technicals (computed from price data — should never fail if price data exists)
    t1 = time.monotonic()
    logger.debug(f"[{ticker}] Computing technicals...")
    technicals = compute_technicals(price_df)
    logger.debug(f"[{ticker}] Technicals computed in {time.monotonic() - t1:.1f}s")

    # Fundamentals (optional — mark stale if fails)
    t1 = time.monotonic()
    logger.debug(f"[{ticker}] Fetching fundamentals...")
    fundamentals = fetch_fundamentals(ticker)
    logger.debug(f"[{ticker}] Fundamentals fetched in {time.monotonic() - t1:.1f}s")
    if fundamentals.pe_ratio is None and fundamentals.market_cap is None:
        stale_sources.append("fundamentals")

    # News (optional — mark stale if fails)
    t1 = time.monotonic()
    logger.debug(f"[{ticker}] Fetching news...")
    news = fetch_news(ticker)
    logger.debug(f"[{ticker}] News fetched in {time.monotonic() - t1:.1f}s ({len(news)} items)")
    if not news:
        stale_sources.append("news")

    logger.info(f"[{ticker}] Data package built in {time.monotonic() - t0:.1f}s")

    return TickerDataPackage(
        ticker=ticker,
        fetch_timestamp=datetime.now(),
        price_history=price_history,
        technicals=technicals,
        fundamentals=fundamentals,
        news=news,
        stale_sources=stale_sources,
    )


def fetch_all_tickers(tickers: list[str] | None = None) -> list[TickerDataPackage]:
    """Fetch data for all tickers. Skips failures, logs errors."""
    tickers = tickers or settings.tickers
    packages = []
    for ticker in tickers:
        logger.info(f"Fetching data for {ticker}...")
        pkg = build_data_package(ticker)
        if pkg:
            packages.append(pkg)
            logger.info(f"  {ticker}: OK (stale: {pkg.stale_sources or 'none'})")
        else:
            logger.warning(f"  {ticker}: SKIPPED (no price data)")
    logger.info(f"Fetched {len(packages)}/{len(tickers)} tickers successfully")
    return packages

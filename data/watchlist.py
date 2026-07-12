"""Digest-driven watchlist.

Extracts ticker recommendations from the pasted ChatGPT/Gemini macro digest
and accumulates them into a persistent watchlist. `run_all` unions this with
the base `settings.tickers` universe.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path

from agents.llm import call_local
from config import settings
from data.models import DigestTickers

logger = logging.getLogger(__name__)

WATCHLIST_PATH = Path(__file__).parent / "external" / "digest_watchlist.json"
MANUAL_WATCHLIST_PATH = Path(__file__).parent / "external" / "manual_watchlist.json"

# yfinance suffix per exchange. Symbols without a suffix are assumed US-listed.
_EXCHANGE_SUFFIX = {
    "NYSE": "",
    "NASDAQ": "",
    "NYSEARCA": "",
    "AMEX": "",
    "NYSEAMERICAN": "",
    "TSX": ".TO",
    "TSXV": ".V",
    "TSE": ".TO",  # sometimes used informally for Toronto
}

_EXTRACTION_SYSTEM = """You extract ticker symbols from a daily market digest.

Return ONLY tickers that appear in a 'Stock Recommendations', 'Watchlist', or
equivalent explicit-picks section. DO NOT extract tickers from the 'Reflection
on Prior Recommendations' section. DO NOT extract tickers mentioned only in
passing commentary or macro discussion.

For each pick, return the canonical trading symbol and the exchange. If the
digest names the company but not the symbol (e.g. 'Amazon.com Inc.'), resolve
to the well-known primary-listing symbol (AMZN) and exchange (NASDAQ). If you
are not confident about the symbol, omit that entry rather than guess.

Respond with valid JSON only."""


def _extraction_prompt(digest: str) -> str:
    return f"""Extract ticker recommendations from this digest:

{digest}

Return JSON matching:
{{
  "tickers": [
    {{"symbol": "AMZN", "exchange": "NASDAQ"}},
    {{"symbol": "X", "exchange": "TSX"}}
  ]
}}

Rules:
- Only pull from explicit 'Stock Recommendations' / 'Watchlist' sections.
- Skip the 'Reflection on Prior Recommendations' section entirely.
- Use the raw exchange code (NYSE, NASDAQ, TSX, TSXV, SSE, HKEX, LSE, etc.).
- Do NOT add exchange suffixes like '.TO' — caller handles that."""


def _normalize_symbol(symbol: str, exchange: str) -> str | None:
    """Convert (symbol, exchange) to a yfinance-compatible ticker.

    Returns None for exchanges we don't currently support.
    """
    symbol = symbol.strip().upper()
    exchange = exchange.strip().upper()
    if not symbol:
        return None
    if exchange not in _EXCHANGE_SUFFIX:
        logger.info(f"Skipping {symbol} on unsupported exchange {exchange!r}")
        return None
    suffix = _EXCHANGE_SUFFIX[exchange]
    return f"{symbol}{suffix}" if suffix and not symbol.endswith(suffix) else symbol


def extract_tickers(digest: str) -> list[str]:
    """Call the local LLM to pull ticker picks out of a digest. Returns [] on failure."""
    if not digest.strip():
        return []
    try:
        result = call_local(
            _extraction_prompt(digest),
            DigestTickers,
            system=_EXTRACTION_SYSTEM,
            max_retries=1,
            stage="digest.extract_tickers",
        )
    except Exception as e:
        logger.warning(f"Digest ticker extraction failed: {e}")
        return []

    normalized: list[str] = []
    seen: set[str] = set()
    for t in result.tickers:
        norm = _normalize_symbol(t.symbol, t.exchange)
        if norm and norm not in seen:
            seen.add(norm)
            normalized.append(norm)
    logger.info(f"Extracted {len(normalized)} tickers from digest: {normalized}")
    return normalized


def _load_raw() -> dict:
    if not WATCHLIST_PATH.exists():
        return {"tickers": []}
    try:
        return json.loads(WATCHLIST_PATH.read_text())
    except Exception as e:
        logger.warning(f"Watchlist file corrupt, starting fresh: {e}")
        return {"tickers": []}


def update_watchlist(digest: str) -> list[str]:
    """Extract tickers from digest and merge into the persistent watchlist.

    Returns the list of symbols newly added in this call (for logging / UX).
    """
    tickers = extract_tickers(digest)
    if not tickers:
        return []

    data = _load_raw()
    existing = {entry["symbol"]: entry for entry in data.get("tickers", [])}
    today = date.today().isoformat()
    now = datetime.now().isoformat(timespec="seconds")

    added: list[str] = []
    for symbol in tickers:
        if symbol in existing:
            existing[symbol]["last_mentioned"] = today
            existing[symbol]["last_updated"] = now
        else:
            existing[symbol] = {
                "symbol": symbol,
                "first_added": today,
                "last_mentioned": today,
                "last_updated": now,
            }
            added.append(symbol)

    data["tickers"] = sorted(existing.values(), key=lambda e: e["symbol"])
    WATCHLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    WATCHLIST_PATH.write_text(json.dumps(data, indent=2))
    logger.info(f"Watchlist updated: +{len(added)} new, {len(existing)} total")
    return added


def load_watchlist_symbols() -> list[str]:
    """Return all symbols currently in the watchlist."""
    data = _load_raw()
    return [entry["symbol"] for entry in data.get("tickers", [])]


def load_manual_symbols() -> list[str]:
    """Return symbols from `manual_watchlist.json`.

    Tolerates two shapes so the user can hand-edit the file however they prefer:
      - bare strings: `["AMD", "PLTR"]`
      - objects:      `[{"symbol": "AMD"}, {"symbol": "PLTR"}]`
    Anything that's not a string or doesn't expose a `symbol` key is skipped.
    Returns [] when the file is missing or corrupt — never raises.
    """
    if not MANUAL_WATCHLIST_PATH.exists():
        return []
    try:
        data = json.loads(MANUAL_WATCHLIST_PATH.read_text())
    except Exception as e:
        logger.warning(f"manual_watchlist.json unreadable, ignoring: {e}")
        return []
    symbols: list[str] = []
    for entry in data.get("tickers", []):
        if isinstance(entry, str):
            symbols.append(entry)
        elif isinstance(entry, dict) and isinstance(entry.get("symbol"), str):
            symbols.append(entry["symbol"])
    return symbols


def merged_tickers() -> list[str]:
    """Base settings.tickers ∪ digest watchlist ∪ manual watchlist."""
    from data.universe import selected_tickers

    return selected_tickers(
        base_symbols=list(settings.tickers),
        digest_symbols=load_watchlist_symbols(),
        manual_symbols=load_manual_symbols(),
    )

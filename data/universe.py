from __future__ import annotations

import re

from config import settings
from data.models import UniverseCandidate

_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.-]{0,14}$")
_SUPPORTED_SUFFIXES = {"", ".TO", ".V"}
_REGION_BY_SUFFIX = {
    "": "US",
    ".TO": "CA",
    ".V": "CA",
}


def normalize_symbol(symbol: str) -> str | None:
    """Normalize a ticker into the supported yfinance symbol subset."""
    normalized = symbol.strip().upper().lstrip("$")
    if not normalized:
        return None
    normalized = normalized.replace("/", "-")
    if not _SYMBOL_RE.match(normalized):
        return None
    if normalized.count(".") > 1:
        return None

    suffix = ""
    if "." in normalized:
        suffix = "." + normalized.rsplit(".", 1)[1]
    if suffix not in _SUPPORTED_SUFFIXES:
        return None
    return normalized


def _region_for_symbol(symbol: str) -> str:
    suffix = ""
    if "." in symbol:
        suffix = "." + symbol.rsplit(".", 1)[1]
    return _REGION_BY_SUFFIX.get(suffix, "UNKNOWN")


def _merge_candidate(
    candidates: dict[str, UniverseCandidate],
    *,
    raw_symbol: str,
    source: str,
    reason_added: str,
) -> None:
    symbol = normalize_symbol(raw_symbol)
    if symbol is None:
        key = raw_symbol.strip().upper() or "<empty>"
        candidates.setdefault(
            key,
            UniverseCandidate(
                symbol=key,
                region="UNKNOWN",
                source=[source],
                valid_data_symbol=False,
                reason_added=reason_added,
                rejected_reason="unsupported_or_invalid_symbol",
            ),
        )
        return

    if symbol in candidates:
        if source not in candidates[symbol].source:
            candidates[symbol].source.append(source)
        return

    candidates[symbol] = UniverseCandidate(
        symbol=symbol,
        region=_region_for_symbol(symbol),
        source=[source],
        valid_data_symbol=True,
        reason_added=reason_added,
    )


def build_universe(
    *,
    base_symbols: list[str] | None = None,
    digest_symbols: list[str] | None = None,
    manual_symbols: list[str] | None = None,
) -> list[UniverseCandidate]:
    """Build a validated, source-tagged universe preserving base order."""
    candidates: dict[str, UniverseCandidate] = {}
    for symbol in base_symbols if base_symbols is not None else settings.tickers:
        _merge_candidate(
            candidates,
            raw_symbol=symbol,
            source="core_watchlist",
            reason_added="Configured base watchlist",
        )

    for symbol in digest_symbols or []:
        _merge_candidate(
            candidates,
            raw_symbol=symbol,
            source="digest_watchlist",
            reason_added="Mentioned in macro digest watchlist",
        )

    for symbol in manual_symbols or []:
        _merge_candidate(
            candidates,
            raw_symbol=symbol,
            source="manual_watchlist",
            reason_added="Manually supplied by operator",
        )

    return list(candidates.values())


def selected_tickers(
    *,
    base_symbols: list[str] | None = None,
    digest_symbols: list[str] | None = None,
    manual_symbols: list[str] | None = None,
) -> list[str]:
    """Return only valid symbols from the built universe."""
    return [
        candidate.symbol
        for candidate in build_universe(
            base_symbols=base_symbols,
            digest_symbols=digest_symbols,
            manual_symbols=manual_symbols,
        )
        if candidate.valid_data_symbol
    ]


def rejected_universe_candidates(
    *,
    base_symbols: list[str] | None = None,
    digest_symbols: list[str] | None = None,
    manual_symbols: list[str] | None = None,
) -> list[UniverseCandidate]:
    """Return rejected candidates for logging or diagnostics."""
    return [
        candidate
        for candidate in build_universe(
            base_symbols=base_symbols,
            digest_symbols=digest_symbols,
            manual_symbols=manual_symbols,
        )
        if not candidate.valid_data_symbol
    ]

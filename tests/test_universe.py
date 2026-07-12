from config import settings
from data.universe import (
    build_universe,
    normalize_symbol,
    rejected_universe_candidates,
    selected_tickers,
)
from data import watchlist


def test_normalize_symbol_supports_us_and_canadian_tickers():
    assert normalize_symbol(" nvda ") == "NVDA"
    assert normalize_symbol("$aapl") == "AAPL"
    assert normalize_symbol("shop.to") == "SHOP.TO"
    assert normalize_symbol("foo hk") is None
    assert normalize_symbol("0700.HK") is None


def test_build_universe_dedupes_and_tags_sources():
    universe = build_universe(
        base_symbols=["NVDA", "MSFT"],
        digest_symbols=["nvda", "SHOP.TO"],
        manual_symbols=["PLTR"],
    )
    by_symbol = {candidate.symbol: candidate for candidate in universe}

    assert [candidate.symbol for candidate in universe if candidate.valid_data_symbol] == [
        "NVDA",
        "MSFT",
        "SHOP.TO",
        "PLTR",
    ]
    assert by_symbol["NVDA"].source == ["core_watchlist", "digest_watchlist"]
    assert by_symbol["SHOP.TO"].region == "CA"
    assert by_symbol["PLTR"].source == ["manual_watchlist"]


def test_selected_tickers_filters_invalid_symbols():
    tickers = selected_tickers(
        base_symbols=["NVDA", "bad symbol"],
        digest_symbols=["ASML", "0700.HK"],
    )
    rejected = rejected_universe_candidates(
        base_symbols=["NVDA", "bad symbol"],
        digest_symbols=["ASML", "0700.HK"],
    )

    assert tickers == ["NVDA", "ASML"]
    assert {candidate.symbol for candidate in rejected} == {"BAD SYMBOL", "0700.HK"}
    assert all(candidate.rejected_reason == "unsupported_or_invalid_symbol" for candidate in rejected)


def test_merged_tickers_uses_universe_validation(monkeypatch):
    monkeypatch.setattr(watchlist.settings, "tickers", ["NVDA", "bad symbol"])
    monkeypatch.setattr(watchlist, "load_watchlist_symbols", lambda: ["PLTR", "0700.HK"])

    assert watchlist.merged_tickers() == ["NVDA", "PLTR"]


def test_default_config_tickers_are_not_accidentally_concatenated():
    assert "IWMAAPL" not in settings.tickers
    assert "GOOGLTSLA" not in settings.tickers
    assert "ISRGJPM" not in settings.tickers
    assert {"IWM", "AAPL", "GOOGL", "TSLA", "ISRG", "JPM"}.issubset(settings.tickers)

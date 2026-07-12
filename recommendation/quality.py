from data.models import TickerDataPackage


def data_quality_flags(pkg: TickerDataPackage) -> list[str]:
    """First-pass data quality flags for immutable snapshots and risk gates."""
    flags = list(pkg.stale_sources)

    if len(pkg.price_history) < 50:
        flags.append("short_price_history")
    if not pkg.price_history:
        flags.append("missing_price_history")
    if any(bar.close <= 0 or bar.open <= 0 or bar.high <= 0 or bar.low <= 0 for bar in pkg.price_history):
        flags.append("nonpositive_price")
    if any(bar.volume < 0 for bar in pkg.price_history):
        flags.append("negative_volume")

    if pkg.fundamentals.sector is None:
        flags.append("missing_sector")
    if pkg.fundamentals.industry is None:
        flags.append("missing_industry")
    if pkg.fundamentals.dividend_yield is not None and pkg.fundamentals.dividend_yield > 0.20:
        flags.append("dividend_yield_anomaly")
    if not pkg.news:
        flags.append("missing_news")

    return sorted(set(flags))

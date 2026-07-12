from data.models import DataSnapshot, RegimeClassification, TickerDataPackage
from recommendation.features import feature_payload
from recommendation.quality import data_quality_flags


def build_data_snapshot(
    pkg: TickerDataPackage,
    *,
    run_id: str | None = None,
    external_digest: str | None = None,
    regime: RegimeClassification | None = None,
) -> DataSnapshot:
    """Build the immutable v2 data snapshot for a ticker package."""
    macro_payload = {
        "external_digest": external_digest,
        "regime": regime.model_dump(mode="json") if regime else None,
    }

    return DataSnapshot(
        run_id=run_id,
        ticker=pkg.ticker,
        captured_at=pkg.fetch_timestamp,
        source_versions={"market_data": "yfinance", "schema": "recommendation_v2"},
        price_payload={
            "history": [bar.model_dump(mode="json") for bar in pkg.price_history],
            "benchmark_history": {
                symbol: [bar.model_dump(mode="json") for bar in bars]
                for symbol, bars in pkg.benchmark_price_history.items()
            },
            "technicals": pkg.technicals.model_dump(mode="json"),
        },
        fundamentals_payload=pkg.fundamentals.model_dump(mode="json"),
        news_payload=[item.model_dump(mode="json") for item in pkg.news],
        macro_payload=macro_payload,
        feature_payload=feature_payload(pkg),
        data_quality_flags=data_quality_flags(pkg),
    )

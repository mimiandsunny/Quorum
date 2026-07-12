from decimal import Decimal

from recommendation.calibration import build_calibration_summary


def test_build_calibration_summary_formats_recent_buckets():
    summary = build_calibration_summary([
        {
            "strategy_type": "short_term",
            "side": "long",
            "horizon_days": 5,
            "confidence_bucket": "65-75",
            "total": 6,
            "avg_confidence": Decimal("0.70"),
            "win_rate": Decimal("0.667"),
            "outperform_rate": Decimal("0.50"),
            "avg_side_return_pct": Decimal("0.042"),
            "avg_excess_return_pct": Decimal("-0.011"),
            "avg_score": Decimal("0.73"),
        }
    ])

    assert "RECOMMENDATION CALIBRATION" in summary
    assert "short_term/long/h5/65-75" in summary
    assert "n=6" in summary
    assert "avg_conf=70%" in summary
    assert "win=67%" in summary
    assert "avg_return=+4.2%" in summary
    assert "avg_excess=-1.1%" in summary


def test_build_calibration_summary_empty_rows_is_blank():
    assert build_calibration_summary([]) == ""

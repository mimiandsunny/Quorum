"""Ground-truth scorer. Evaluates past signals against actual price data.

Runs after market close (4:30 PM ET). For each unscored signal whose holding
period has elapsed, fetches actual OHLCV and computes a composite score.
"""

import json
import logging
from datetime import date, datetime, timedelta

import yfinance as yf

from data.models import Decision, FinalSignal, RecommendationScore, RecommendationSide
from data.storage import (
    get_analyst_reports,
    get_paper_trades_by_recommendation,
    get_unscored_recommendations,
    get_unscored_signals,
    save_agent_score,
    save_recommendation_score,
    save_signal_score,
)

logger = logging.getLogger(__name__)


def _fetch_holding_period_data(
    ticker: str, start: date, end: date,
) -> list[dict] | None:
    """Fetch daily OHLCV for the holding period. Returns None on failure."""
    try:
        stock = yf.Ticker(ticker)
        # Add 1 day buffer to end to ensure we get the end date
        df = stock.history(
            start=start.isoformat(),
            end=(end + timedelta(days=1)).isoformat(),
            timeout=30,
        )
        if df.empty:
            return None
        return [
            {
                "date": idx.date(),
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
            }
            for idx, row in df.iterrows()
        ]
    except Exception as e:
        logger.error(f"Failed to fetch holding period data for {ticker}: {e}")
        return None


def _as_list(value) -> list:
    """Normalize psycopg JSONB values and test fixtures to a Python list."""
    if isinstance(value, str):
        return json.loads(value)
    return value or []


def _as_date(value) -> date:
    """Normalize DB/test date-like values for holding-period checks."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    raise TypeError(f"Expected date-like value, got {type(value)!r}")


def _confidence_bucket(confidence: float) -> str:
    if confidence < 0.5:
        return "<50"
    if confidence < 0.65:
        return "50-65"
    if confidence < 0.75:
        return "65-75"
    if confidence < 0.85:
        return "75-85"
    return "85+"


def _side_return_pct(side: RecommendationSide, entry: float, exit_price: float) -> float:
    """Side-correct return: positive means the recommendation made money."""
    if side == RecommendationSide.SHORT:
        return (entry - exit_price) / entry
    return (exit_price - entry) / entry


def _benchmark_return_pct(bars: list[dict] | None) -> float | None:
    if not bars:
        return None
    start = bars[0]["close"]
    end = bars[-1]["close"]
    if not start:
        return None
    return (end - start) / start


def _execution_summary(
    *,
    side: RecommendationSide,
    entry_mid: float,
    end_close: float,
    paper_trades: list[dict] | None,
) -> dict:
    """Summarize paper execution separately from recommendation quality."""
    if not paper_trades:
        return {
            "execution_status": None,
            "execution_return_pct": None,
            "execution_slippage_pct": None,
        }

    status_rank = {
        "filled": 5,
        "submitted": 4,
        "pending": 3,
        "unfilled_eod": 2,
        "failed": 1,
        "execution_skipped": 1,
        "cancelled": 1,
    }
    best_status = max(
        (str(row.get("status") or "") for row in paper_trades),
        key=lambda status: status_rank.get(status, 0),
    )
    filled_rows = [
        row for row in paper_trades
        if row.get("avg_entry") is not None and row.get("qty") is not None
    ]
    if not filled_rows:
        return {
            "execution_status": best_status or None,
            "execution_return_pct": None,
            "execution_slippage_pct": None,
        }

    entry = float(filled_rows[0]["avg_entry"])
    if entry <= 0:
        return {
            "execution_status": best_status or None,
            "execution_return_pct": None,
            "execution_slippage_pct": None,
        }

    if side == RecommendationSide.SHORT:
        execution_return = (entry - end_close) / entry
        slippage = (entry - entry_mid) / entry_mid
    elif side == RecommendationSide.FLAT:
        execution_return = None
        slippage = None
    else:
        execution_return = (end_close - entry) / entry
        slippage = (entry_mid - entry) / entry_mid

    return {
        "execution_status": best_status or None,
        "execution_return_pct": (
            round(execution_return, 4)
            if execution_return is not None
            else None
        ),
        "execution_slippage_pct": (
            round(slippage, 4)
            if slippage is not None
            else None
        ),
    }


def _recommendation_side(recommendation: dict) -> RecommendationSide:
    side = recommendation.get("side")
    if side:
        return RecommendationSide(side)

    decision = Decision(recommendation["decision"])
    if decision == Decision.SELL:
        return RecommendationSide.SHORT
    if decision == Decision.HOLD:
        return RecommendationSide.FLAT
    return RecommendationSide.LONG


def _compute_recommendation_score(
    recommendation: dict,
    bars: list[dict],
    benchmark_bars: list[dict] | None = None,
    sector_bars: list[dict] | None = None,
    paper_trades: list[dict] | None = None,
    score_date: date | None = None,
) -> RecommendationScore:
    """Compute outcome score for one immutable recommendation."""
    entry_zone = [float(value) for value in _as_list(recommendation["entry_zone"])]
    if len(entry_zone) < 2:
        raise ValueError("recommendation entry_zone must contain at least two prices")

    targets = [float(value) for value in _as_list(recommendation.get("targets"))]
    side = _recommendation_side(recommendation)
    entry_mid = (entry_zone[0] + entry_zone[1]) / 2
    end_close = float(bars[-1]["close"])
    highs = [float(b["high"]) for b in bars]
    lows = [float(b["low"]) for b in bars]

    side_return = _side_return_pct(side, entry_mid, end_close)
    benchmark_return = _benchmark_return_pct(benchmark_bars)
    sector_return = _benchmark_return_pct(sector_bars)
    excess_return = (
        side_return - benchmark_return
        if benchmark_return is not None
        else None
    )

    stop_loss = float(recommendation["stop_loss"])
    if side == RecommendationSide.LONG:
        mae_pct = (min(lows) - entry_mid) / entry_mid
        mfe_pct = (max(highs) - entry_mid) / entry_mid
        stop_hit = min(lows) <= stop_loss
        target_hit = [max(highs) >= target for target in targets]
    elif side == RecommendationSide.SHORT:
        mae_pct = (entry_mid - max(highs)) / entry_mid
        mfe_pct = (entry_mid - min(lows)) / entry_mid
        stop_hit = max(highs) >= stop_loss
        target_hit = [min(lows) <= target for target in targets]
    else:
        mae_pct = (min(lows) - entry_mid) / entry_mid
        mfe_pct = (max(highs) - entry_mid) / entry_mid
        stop_hit = False
        target_hit = [False for _ in targets]

    score = 0.0
    if side == RecommendationSide.FLAT:
        if abs(side_return) < 0.02:
            score += 0.6
        if abs(mfe_pct) < 0.04 and abs(mae_pct) < 0.04:
            score += 0.2
    else:
        if side_return > 0:
            score += 0.4
        if excess_return is None or excess_return > 0:
            score += 0.2
        if target_hit and target_hit[0]:
            score += 0.2

    if not stop_hit:
        score += 0.2

    execution = _execution_summary(
        side=side,
        entry_mid=entry_mid,
        end_close=end_close,
        paper_trades=paper_trades,
    )

    return RecommendationScore(
        recommendation_id=recommendation["recommendation_id"],
        score_date=score_date or date.today(),
        side_return_pct=round(side_return, 4),
        benchmark_return_pct=(
            round(benchmark_return, 4)
            if benchmark_return is not None
            else None
        ),
        sector_return_pct=(
            round(sector_return, 4)
            if sector_return is not None
            else None
        ),
        excess_return_pct=(
            round(excess_return, 4)
            if excess_return is not None
            else None
        ),
        mae_pct=round(mae_pct, 4),
        mfe_pct=round(mfe_pct, 4),
        stop_hit=stop_hit,
        target_hit=target_hit,
        confidence_bucket=_confidence_bucket(float(recommendation["confidence"])),
        score=round(min(score, 1.0), 2),
        **execution,
    )


def _compute_score(signal: FinalSignal, bars: list[dict]) -> dict:
    """Compute composite score for a signal against actual price data.

    Returns dict with all score components.
    """
    entry_mid = (signal.entry_zone[0] + signal.entry_zone[1]) / 2
    end_close = bars[-1]["close"]

    # Side-correct return: positive = profit ON THE TRADE regardless of side.
    # BUY (long):   profit when price rises  → (end - entry) / entry
    # SELL (short): profit when price falls  → (entry - end) / entry
    # HOLD: report long math purely for diagnostics; not used for win/loss.
    if signal.decision == Decision.SELL:
        actual_return_pct = (entry_mid - end_close) / entry_mid
    else:
        actual_return_pct = (end_close - entry_mid) / entry_mid

    # Direction correct
    if signal.decision == Decision.BUY:
        direction_correct = end_close > entry_mid
    elif signal.decision == Decision.SELL:
        direction_correct = end_close < entry_mid
    else:  # HOLD
        direction_correct = abs(end_close - entry_mid) / entry_mid < 0.02

    # Entry zone reached during holding period
    lows = [b["low"] for b in bars]
    highs = [b["high"] for b in bars]
    entry_hit = any(
        b["low"] <= signal.entry_zone[1] and b["high"] >= signal.entry_zone[0]
        for b in bars
    )

    # Stop loss hit during holding period
    if signal.decision == Decision.BUY:
        stop_hit = min(lows) <= signal.stop_loss
    elif signal.decision == Decision.SELL:
        stop_hit = max(highs) >= signal.stop_loss
    else:
        stop_hit = False

    # Targets hit during holding period
    target_hit = []
    for target in signal.targets:
        if signal.decision == Decision.BUY:
            hit = max(highs) >= target
        elif signal.decision == Decision.SELL:
            hit = min(lows) <= target
        else:
            hit = False
        target_hit.append(hit)

    # Composite score (0.0 to 1.0)
    score = 0.0
    if direction_correct:
        score += 0.4
    if entry_hit:
        score += 0.2
    if target_hit and target_hit[0]:
        score += 0.2
    if not stop_hit:
        score += 0.2

    # Bonus: +0.1 if actual return exceeds first target distance
    if signal.targets and signal.decision in (Decision.BUY, Decision.SELL):
        target_distance = abs(signal.targets[0] - entry_mid) / entry_mid
        if abs(actual_return_pct) > target_distance:
            score = min(score + 0.1, 1.0)

    return {
        "direction_correct": direction_correct,
        "entry_hit": entry_hit,
        "stop_hit": stop_hit,
        "target_hit": target_hit,
        "actual_return_pct": round(actual_return_pct, 4),
        "score": round(score, 2),
    }


def _evaluate_agent_alignment(
    signal: FinalSignal, direction_correct: bool, actual_return_pct: float,
) -> dict[str, bool]:
    """Evaluate whether each analyst's prediction aligned with outcome.

    This is approximate, useful directionally. For BUY/SELL, alignment ==
    direction_correct (which already encodes "trade won" symmetrically across
    sides — see _compute_score). For HOLD, alignment == price stayed near
    entry; actual_return_pct is LONG math for HOLD, so abs(...) is the
    flat-price check.
    """
    alignments: dict[str, bool] = {}
    if signal.decision in (Decision.BUY, Decision.SELL):
        alignments["technical"] = direction_correct
    else:  # HOLD
        alignments["technical"] = abs(actual_return_pct) < 0.02
    alignments["fundamentals"] = alignments["technical"]
    alignments["sentiment"] = alignments["technical"]
    alignments["news"] = alignments["technical"]
    return alignments


def _market_return_from_trade_return(
    signal: FinalSignal,
    actual_return_pct: float,
) -> float:
    if signal.decision == Decision.SELL:
        return -actual_return_pct
    return actual_return_pct


def _stance_alignment(stance: str | None, market_return_pct: float) -> bool:
    if stance == "bullish":
        return market_return_pct > 0
    if stance == "bearish":
        return market_return_pct < 0
    return abs(market_return_pct) < 0.02


def _text_stance(text: str) -> str:
    normalized = text.lower()
    positive_terms = (
        "accelerating", "attractive", "bullish", "constructive", "expanding",
        "growth", "healthy", "positive", "reasonable", "solid", "strong",
        "undervalued",
    )
    negative_terms = (
        "bearish", "declining", "deteriorating", "expensive", "fragile",
        "negative", "overvalued", "rich", "risk", "slowing", "weak",
    )
    positives = sum(term in normalized for term in positive_terms)
    negatives = sum(term in normalized for term in negative_terms)
    if positives >= negatives + 1:
        return "bullish"
    if negatives >= positives + 1:
        return "bearish"
    return "neutral"


def _agent_stances_from_reports(reports: dict) -> dict[str, str]:
    technical = reports.get("technical") or {}
    fundamentals = reports.get("fundamentals") or {}
    sentiment = reports.get("sentiment") or {}
    news = reports.get("news") or {}

    sentiment_score = sentiment.get("overall_score")
    if sentiment_score is not None:
        if sentiment_score > 0.10:
            sentiment_stance = "bullish"
        elif sentiment_score < -0.10:
            sentiment_stance = "bearish"
        else:
            sentiment_stance = "neutral"
    else:
        sentiment_stance = _text_stance(str(sentiment.get("summary") or ""))

    fundamentals_text = " ".join(
        str(fundamentals.get(key) or "")
        for key in (
            "valuation_assessment",
            "growth_assessment",
            "financial_health",
            "sector_comparison",
            "summary",
        )
    )
    news_text = " ".join(
        [
            str(news.get("macro_context") or ""),
            str(news.get("summary") or ""),
            " ".join(
                str(event.get("impact") or "") + " " + str(event.get("relevance") or "")
                for event in news.get("events") or []
                if isinstance(event, dict)
            ),
        ]
    )

    return {
        "technical": str(technical.get("trend") or "neutral"),
        "fundamentals": _text_stance(fundamentals_text),
        "sentiment": sentiment_stance,
        "news": _text_stance(news_text),
    }


def _evaluate_agent_alignment_from_reports(
    signal: FinalSignal,
    reports: dict | None,
    actual_return_pct: float,
) -> dict[str, bool] | None:
    """Evaluate analysts against their own stored stance when available."""
    if not reports:
        return None

    market_return = _market_return_from_trade_return(signal, actual_return_pct)
    return {
        agent: _stance_alignment(stance, market_return)
        for agent, stance in _agent_stances_from_reports(reports).items()
    }


def score_recommendations(days_back: int = 10) -> list[dict]:
    """Score unscored immutable recommendations whose horizon has elapsed."""
    today = date.today()
    unscored = get_unscored_recommendations(days_back)
    logger.info(
        f"Found {len(unscored)} unscored recommendations in the past {days_back} days"
    )

    scored = []
    for recommendation in unscored:
        created_date = _as_date(recommendation["created_at"])
        end_date = created_date + timedelta(days=int(recommendation["horizon_days"]))
        if end_date > today:
            logger.debug(
                f"[{recommendation['ticker']}] Recommendation horizon ends {end_date}, skipping"
            )
            continue

        bars = _fetch_holding_period_data(recommendation["ticker"], created_date, end_date)
        if not bars:
            logger.warning(
                f"[{recommendation['ticker']}] No price data for recommendation horizon, skipping"
            )
            continue

        benchmark_bars = None
        benchmark_symbol = recommendation.get("benchmark_symbol")
        if benchmark_symbol:
            benchmark_bars = _fetch_holding_period_data(
                benchmark_symbol, created_date, end_date,
            )

        sector_bars = None
        sector_symbol = recommendation.get("sector_benchmark_symbol")
        if sector_symbol:
            sector_bars = _fetch_holding_period_data(
                sector_symbol, created_date, end_date,
            )

        paper_trades = get_paper_trades_by_recommendation(
            recommendation["recommendation_id"]
        )
        score = _compute_recommendation_score(
            recommendation,
            bars,
            benchmark_bars=benchmark_bars,
            sector_bars=sector_bars,
            paper_trades=paper_trades,
            score_date=today,
        )
        save_recommendation_score(score)

        logger.info(
            f"[{recommendation['ticker']}] Scored recommendation "
            f"{recommendation['recommendation_id']}: score={score.score:.2f}, "
            f"return={score.side_return_pct:.2%}"
        )
        scored.append({
            "recommendation_id": recommendation["recommendation_id"],
            "ticker": recommendation["ticker"],
            "created_at": created_date.isoformat(),
            "side": _recommendation_side(recommendation).value,
            "score_date": score.score_date.isoformat(),
            "side_return_pct": score.side_return_pct,
            "excess_return_pct": score.excess_return_pct,
            "score": score.score,
        })

    logger.info(f"Scored {len(scored)} recommendations")
    return scored


def score_signals(days_back: int = 10) -> list[dict]:
    """Score all unscored signals whose holding period has elapsed.

    Returns list of scored signal summaries.
    """
    today = date.today()
    unscored = get_unscored_signals(days_back)
    logger.info(f"Found {len(unscored)} unscored signals in the past {days_back} days")

    scored = []
    for signal in unscored:
        # Check if holding period has elapsed
        end_date = signal.date + timedelta(days=signal.holding_period_days)
        if end_date > today:
            logger.debug(
                f"[{signal.ticker}] Holding period ends {end_date}, skipping"
            )
            continue

        # Fetch actual price data for the holding period
        bars = _fetch_holding_period_data(signal.ticker, signal.date, end_date)
        if not bars:
            logger.warning(f"[{signal.ticker}] No price data for holding period, skipping")
            continue

        # Compute score
        result = _compute_score(signal, bars)
        logger.info(
            f"[{signal.ticker}] Scored {signal.date}: "
            f"score={result['score']:.2f}, direction={'correct' if result['direction_correct'] else 'wrong'}, "
            f"return={result['actual_return_pct']:.2%}"
        )

        # Save signal score
        save_signal_score(
            ticker=signal.ticker,
            signal_date=signal.date,
            score_date=today,
            **result,
        )

        # Evaluate and save per-agent alignment
        reports = get_analyst_reports(signal.ticker, signal.date)
        alignments = _evaluate_agent_alignment_from_reports(
            signal, reports, result["actual_return_pct"],
        ) or _evaluate_agent_alignment(
            signal, result["direction_correct"], result["actual_return_pct"],
        )
        for agent_name, aligned in alignments.items():
            save_agent_score(signal.ticker, signal.date, agent_name, aligned)

        scored.append({
            "ticker": signal.ticker,
            "signal_date": signal.date.isoformat(),
            "decision": signal.decision.value,
            **result,
        })

    logger.info(f"Scored {len(scored)} signals")
    return scored

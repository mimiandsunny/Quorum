"""LangGraph pipeline:
data → analysts → researchers (multi-round) → judge → trader
     → risk manager → compose_signal → execute_paper_trades → END.

Processes one ticker at a time. The outer loop (in scheduler.py) iterates
over tickers. execute_paper_trades is ISOLATED (D10 CEO): broker failures
never block signal persistence — failed paper trades surface as audit rows.
"""

import functools
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
from datetime import date, datetime
from typing import Callable, TypedDict

from langgraph.graph import END, StateGraph

from agents.analysts import fundamentals, news, sentiment, technical
from agents.analysts.comparison import reset_analyst_comparison_logs
from agents.paper_trader import _idempotency_key, execute_paper_trade
from agents.researchers import bear, bull
from agents.researchers.judge import judge as run_judge
from agents.risk_manager import assess_risk
from agents.strategies import enabled_strategies, is_paused
from agents.trader import decide
from agents.regime import classify as classify_regime
from agents.digest_distiller import distill_digest
from data.models import (
    AlphaOutput,
    AnalystReports,
    DebateTranscript,
    Decision,
    DigestDistillation,
    FinalSignal,
    JudgeVerdict,
    RegimeClassification,
    ResearchRound,
    RiskAssessment,
    RiskVerdict,
    RunMetadata,
    TickerDataPackage,
    TraderDecision,
)
from data.pipeline import build_data_package, read_digest
from data.storage import (
    get_agent_track_record,
    get_open_paper_positions,
    get_recommendation_calibration,
    save_data_snapshot,
    save_analyst_reports,
    save_debate,
    save_recommendation,
    save_run_metadata,
    save_signal,
)
from recommendation.alpha_event import analyze as analyze_event_alpha
from recommendation.alpha_long import analyze as analyze_long_term_alpha
from recommendation.alpha_quant import analyze as analyze_quant_alpha
from recommendation.alpha_short import analyze as analyze_short_term_alpha
from recommendation.calibration import build_calibration_summary
from recommendation.ledger import build_recommendation
from recommendation.portfolio import portfolio_exposures_from_open_positions
from recommendation.risk import assess_recommendation_risk
from recommendation.snapshots import build_data_snapshot
from recommendation.target_sanity import sanitize_research_case

logger = logging.getLogger(__name__)


# ─── State ───────────────────────────────────────────────

class PipelineState(TypedDict, total=False):
    run_id: str
    ticker: str
    snapshot_id: str
    data: TickerDataPackage
    analyst_reports: AnalystReports
    debate: DebateTranscript
    trader_decision: TraderDecision
    risk_assessment: RiskAssessment
    final_signal: FinalSignal
    alpha_outputs: list[AlphaOutput]
    external_digest: str | None
    digest_summary: DigestDistillation | None
    regime: RegimeClassification | None
    error: str
    # Pre-fetched data package injected by `run_all`'s parallel prefetch.
    # `fetch_data` reads this and skips the live yfinance round-trip when
    # present. Single-ticker call sites that don't prefetch (e.g. CLI debug)
    # leave it unset and the node falls back to a live fetch.
    prefetched_pkg: TickerDataPackage | None
    # Per-node wall-clock durations in seconds, accumulated by `_timed_node`.
    # Logged as a one-line summary at the end of `run_ticker` so a 6-hour run
    # tells you which stage actually consumed the time.
    _timings: dict[str, float]


# ─── Timing decorator ────────────────────────────────────

def _timed_node(name: str, fn: Callable[[PipelineState], PipelineState]) -> Callable[[PipelineState], PipelineState]:
    """Wrap a LangGraph node so each invocation logs its own duration AND
    accumulates the result into `state['_timings']`. Failures still log the
    elapsed time before re-raising so we see how far a crash got.
    """
    @functools.wraps(fn)
    def wrapper(state: PipelineState) -> PipelineState:
        ticker = state.get("ticker", "?")
        t0 = time.monotonic()
        try:
            result = fn(state)
        except Exception:
            elapsed = time.monotonic() - t0
            logger.info(f"[{ticker}] node={name} {elapsed:.1f}s FAIL")
            raise
        elapsed = time.monotonic() - t0
        logger.info(f"[{ticker}] node={name} {elapsed:.1f}s")
        timings = {**(state.get("_timings") or {}), **(result.get("_timings") or {})}
        timings[name] = round(elapsed, 2)
        return {**result, "_timings": timings}
    return wrapper


# ─── Node functions ──────────────────────────────────────

def fetch_data(state: PipelineState) -> PipelineState:
    """Fetch market data and build the data package.

    When `run_all` has done a parallel prefetch, the package arrives in
    `state['prefetched_pkg']` and we skip the live yfinance round-trip.
    Single-ticker call sites (CLI debug, tests) leave it unset and we
    fall back to a fresh fetch — keeping this node usable in isolation.
    """
    ticker = state["ticker"]
    pkg = state.get("prefetched_pkg")
    if pkg is None:
        logger.info(f"[{ticker}] Fetching data...")
        pkg = build_data_package(ticker)
    else:
        logger.info(f"[{ticker}] Using prefetched data package")
    if pkg is None:
        return {**state, "error": f"No price data available for {ticker}"}
    # Attach external digest to the data package for analyst access.
    # The raw digest is kept on the package for snapshot/audit storage,
    # but LLM prompts read `digest_summary` (distilled once per run) instead.
    pkg.external_digest = state.get("external_digest")
    pkg.digest_summary = state.get("digest_summary")

    try:
        alpha_outputs = [
            analyze_short_term_alpha(pkg),
            analyze_long_term_alpha(pkg),
            analyze_quant_alpha(pkg),
            analyze_event_alpha(pkg),
        ]
        logger.info(
            f"[{ticker}] Alpha outputs: "
            + ", ".join(
                f"{alpha.strategy_type.value}={alpha.direction.value}@{alpha.confidence:.0%}"
                for alpha in alpha_outputs
            )
        )
    except Exception as exc:
        logger.warning(f"[{ticker}] Short-term alpha failed; continuing without alpha: {exc}")
        alpha_outputs = []

    try:
        snapshot = build_data_snapshot(
            pkg,
            run_id=state.get("run_id"),
            external_digest=state.get("external_digest"),
            regime=state.get("regime"),
        )
        snapshot_id = save_data_snapshot(snapshot)
        logger.info(f"[{ticker}] Data snapshot saved: {snapshot_id}")
    except Exception as exc:
        logger.warning(f"[{ticker}] Data snapshot save failed; continuing without snapshot: {exc}")
        snapshot_id = None
    next_state = {**state, "data": pkg, "alpha_outputs": alpha_outputs}
    if snapshot_id:
        next_state["snapshot_id"] = snapshot_id
    return next_state


def run_analysts(state: PipelineState) -> PipelineState:
    """Run the 4 analysts in parallel. Each one is independent and returns its own analysis."""
    if "error" in state and state["error"]:
        return state

    data = state["data"]
    ticker = data.ticker
    logger.info(f"[{ticker}] Running analysts...")

    analysts = (
        ("technical", technical.analyze),
        ("fundamentals", fundamentals.analyze),
        ("sentiment", sentiment.analyze),
        ("news", news.analyze),
    )

    from config import settings as cfg

    results: dict[str, object] = {}
    with ThreadPoolExecutor(max_workers=cfg.analysts_parallel_workers) as executor:
        future_to_name = {executor.submit(fn, data): name for name, fn in analysts}
        for future in future_to_name:
            name = future_to_name[future]
            try:
                results[name] = future.result()
            except Exception as e:
                logger.error(f"[{ticker}] {name.title()} analyst failed: {e}")
                return {**state, "error": f"{name.title()} analyst failed: {e}"}

    tech = results["technical"]
    fund = results["fundamentals"]
    sent = results["sentiment"]
    nws = results["news"]
    logger.info(f"[{ticker}]   Technical: {tech.trend.value}")
    logger.info(f"[{ticker}]   Fundamentals: done")
    logger.info(f"[{ticker}]   Sentiment: {sent.overall_score:.2f}")
    logger.info(f"[{ticker}]   News: {len(nws.events)} events")

    reports = AnalystReports(
        ticker=ticker,
        technical=tech,
        fundamentals=fund,
        sentiment=sent,
        news=nws,
    )
    save_analyst_reports(ticker, date.today(), reports)
    return {**state, "analyst_reports": reports}


def _run_one_round(
    reports, digest_summary, track_record_text, prior_bull, prior_bear, round_num, parallel,
):
    """Run bull and bear for one debate round. Within-round parallelism preserved."""
    if parallel:
        with ThreadPoolExecutor(max_workers=2) as executor:
            bull_future = executor.submit(
                bull.research,
                reports,
                digest_summary=digest_summary,
                track_record=track_record_text,
                prior_bear=prior_bear,
                round_num=round_num,
            )
            bear_future = executor.submit(
                bear.research,
                reports,
                digest_summary=digest_summary,
                track_record=track_record_text,
                prior_bull=prior_bull,
                round_num=round_num,
            )
            return bull_future.result(), bear_future.result()
    return (
        bull.research(reports, digest_summary=digest_summary, track_record=track_record_text,
                      prior_bear=prior_bear, round_num=round_num),
        bear.research(reports, digest_summary=digest_summary, track_record=track_record_text,
                      prior_bull=prior_bull, round_num=round_num),
    )


def _sanitize_round_targets(rounds: list[ResearchRound], reference_price: float | None, reports) -> None:
    if reference_price is None:
        return
    key_levels = reports.technical.key_levels or {}
    supports = key_levels.get("support", [])
    resistances = key_levels.get("resistance", [])
    for research_round in rounds:
        for case in (research_round.bull, research_round.bear):
            if sanitize_research_case(
                case,
                reference_price,
                support_levels=supports,
                resistance_levels=resistances,
            ):
                logger.warning(
                    "[%s] adjusted implausible %s debate target in round %s",
                    reports.ticker,
                    case.stance,
                    research_round.round,
                )


def run_researchers(state: PipelineState) -> PipelineState:
    """Run multi-round bull/bear debate, then judge. Both cloud LLM."""
    if "error" in state and state["error"]:
        return state

    reports = state["analyst_reports"]
    ticker = reports.ticker
    data = state.get("data")
    reference_price = data.technicals.current_price if data else None
    logger.info(f"[{ticker}] Running researchers...")

    digest_summary = state.get("digest_summary")

    # Build track record section if enough scored signals exist
    track_record_text = None
    try:
        records = get_agent_track_record(ticker, limit=10)
        total_scored = max((r["total"] for r in records.values()), default=0)
        if total_scored >= 5:
            lines = [f"AGENT TRACK RECORDS FOR {ticker} (last {total_scored} signals):"]
            for agent in ("technical", "fundamentals", "sentiment", "news"):
                r = records.get(agent, {"total": 0, "correct": 0})
                if r["total"] > 0:
                    pct = r["correct"] / r["total"] * 100
                    lines.append(f"- {agent.title()} analyst: {r['correct']}/{r['total']} correct ({pct:.0f}%)")
            track_record_text = "\n".join(lines)
            logger.info(f"[{ticker}]   Track records injected ({total_scored} scored signals)")
    except Exception as e:
        logger.warning(f"[{ticker}] Failed to fetch track records: {e}")

    try:
        from config import settings as cfg

        rounds: list[ResearchRound] = []
        prior_bull = None
        prior_bear = None
        for r_idx in range(1, max(cfg.debate_rounds, 1) + 1):
            bull_case, bear_case = _run_one_round(
                reports, digest_summary, track_record_text,
                prior_bull, prior_bear,
                round_num=r_idx,
                parallel=cfg.researchers_parallel,
            )
            rounds.append(ResearchRound(round=r_idx, bull=bull_case, bear=bear_case))
            _sanitize_round_targets(rounds[-1:], reference_price, reports)
            prior_bull, prior_bear = bull_case, bear_case
            logger.info(
                f"[{ticker}]   R{r_idx} bull target ${bull_case.price_target:.2f} | "
                f"bear target ${bear_case.price_target:.2f}"
            )
    except Exception as e:
        logger.error(f"[{ticker}] Researcher stage failed: {e}")
        return {**state, "error": f"Researcher stage failed: {e}"}

    final_bull = rounds[-1].bull
    final_bear = rounds[-1].bear

    # Judge agent (D3 CEO). ISOLATION: returns None on any failure; trader degrades.
    judge_verdict: JudgeVerdict | None = None
    if len(rounds) >= 1:
        judge_verdict = run_judge(rounds, ticker)

    debate = DebateTranscript(
        ticker=ticker,
        bull_case=final_bull,
        bear_case=final_bear,
        rounds=rounds,
        judge_verdict=judge_verdict,
    )
    save_debate(ticker, date.today(), debate)
    return {**state, "debate": debate}


def run_trader(state: PipelineState) -> PipelineState:
    """Run trader agent to synthesize decision (cloud LLM)."""
    if "error" in state and state["error"]:
        return state

    reports = state["analyst_reports"]
    debate = state["debate"]
    ticker = reports.ticker
    logger.info(f"[{ticker}] Running trader...")

    calibration_summary = ""
    try:
        calibration_rows = get_recommendation_calibration(days=90, min_samples=3)
        calibration_summary = build_calibration_summary(calibration_rows)
        if calibration_summary:
            logger.info(f"[{ticker}] Recommendation calibration injected ({len(calibration_rows)} buckets)")
    except Exception as e:
        logger.warning(f"[{ticker}] Failed to fetch recommendation calibration: {e}")

    try:
        decision = decide(
            reports, debate,
            digest_summary=state.get("digest_summary"),
            regime=state.get("regime"),
            alpha_outputs=state.get("alpha_outputs"),
            calibration_summary=calibration_summary,
            data=state.get("data"),
        )
        logger.info(f"[{ticker}]   Decision: {decision.decision.value} @ {decision.confidence:.0%}")
    except Exception as e:
        logger.error(f"[{ticker}] Trader failed: {e}")
        return {**state, "error": f"Trader failed: {e}"}

    return {**state, "trader_decision": decision}


def run_risk_manager(state: PipelineState) -> PipelineState:
    """Run deterministic risk manager."""
    if "error" in state and state["error"]:
        return state

    decision = state["trader_decision"]
    ticker = decision.ticker
    logger.info(f"[{ticker}] Running risk manager...")

    assessment = assess_risk(decision)
    data = state.get("data")
    if data is not None:
        try:
            assessment = assess_recommendation_risk(
                decision=decision,
                data=data,
                base_assessment=assessment,
                alpha_outputs=state.get("alpha_outputs"),
            )
        except Exception as exc:
            logger.warning(f"[{ticker}] v2 risk overlay failed; using base risk assessment: {exc}")
    logger.info(f"[{ticker}]   Verdict: {assessment.verdict.value} | Size: {assessment.position_size_pct:.1%}")

    return {**state, "risk_assessment": assessment}


def compose_signal(state: PipelineState) -> PipelineState:
    """Compose the final signal from all stages and persist it."""
    if "error" in state and state["error"]:
        return state

    decision = state["trader_decision"]
    debate = state["debate"]
    risk = state["risk_assessment"]
    data = state.get("data")
    sector = data.fundamentals.sector if data else None
    industry = data.fundamentals.industry if data else None

    signal = FinalSignal(
        ticker=decision.ticker,
        date=decision.date,
        decision=decision.decision,
        confidence=decision.confidence,
        entry_zone=decision.entry_zone,
        stop_loss=decision.stop_loss,
        targets=decision.targets,
        invalidation=decision.invalidation,
        holding_period_days=decision.holding_period_days,
        thesis=decision.thesis,
        bull_case=debate.bull_case.thesis,
        bear_case=debate.bear_case.thesis,
        risk_verdict=risk.verdict,
        risk_reasons=risk.rejection_reasons,
        position_size_pct=risk.position_size_pct,
        reward_risk_ratio=risk.reward_risk_ratio,
        sector=sector,
        industry=industry,
    )

    try:
        current_exposures = portfolio_exposures_from_open_positions(
            get_open_paper_positions()
        )
        if current_exposures:
            logger.info(
                f"[{decision.ticker}] Portfolio exposure context: "
                f"{len(current_exposures)} open positions"
            )
    except Exception as exc:
        logger.warning(f"[{decision.ticker}] Portfolio exposure fetch failed; sizing without exposure caps: {exc}")
        current_exposures = []

    try:
        recommendation = build_recommendation(
            signal=signal,
            reports=state["analyst_reports"],
            debate=state["debate"],
            trader_decision=state["trader_decision"],
            risk_assessment=state["risk_assessment"],
            alpha_outputs=state.get("alpha_outputs"),
            current_exposures=current_exposures,
            run_id=state.get("run_id"),
            snapshot_id=state.get("snapshot_id"),
        )
        recommendation_id = save_recommendation(recommendation)
        signal.recommendation_id = recommendation_id
        logger.info(f"[{decision.ticker}] Recommendation saved: {recommendation_id}")
    except Exception as exc:
        logger.warning(f"[{decision.ticker}] Recommendation save failed; saving legacy signal only: {exc}")

    save_signal(signal)
    logger.info(f"[{decision.ticker}] Signal saved: {signal.decision.value} @ {signal.confidence:.0%}")

    return {**state, "final_signal": signal}


def execute_paper_trades(state: PipelineState) -> PipelineState:
    """ISOLATED execution stage (D10 CEO). Never blocks signal persistence.

    Wave 1.5: fans out across 3 strategies (Aggressive / Balanced /
    Conservative) via asyncio.to_thread + asyncio.gather. Each strategy gets
    its own per-strategy try/except so one bad Alpaca account never blocks
    the others. Pre-trade check: is_paused (drawdown breaker) skips strategies
    that have tripped their threshold.

    Concurrency: C2 hybrid (per CEO eng-review rev 4) — sync alpaca-py
    TradingClient stays in place (preserves connection pool across scheduler
    ticks); asyncio.to_thread releases the GIL during the network-bound
    Alpaca call so all 3 strategies run in parallel.
    """
    import asyncio

    if "error" in state and state["error"]:
        return state
    signal = state.get("final_signal")
    if signal is None:
        return state

    from config import settings as cfg
    if not cfg.paper_trading_enabled:
        return state

    ticker = signal.ticker
    risk = state.get("risk_assessment")
    strategies = enabled_strategies()
    if not strategies:
        logger.warning(f"[{ticker}] no enabled strategies — skipping paper trade fan-out")
        return state

    # REJECTED signals don't get paper-traded; UPSERT a per-strategy audit
    # row so each strategy's history reflects this signal too. Preserve any
    # existing broker-linked rows (alpaca_order_id is set).
    if risk is None or risk.verdict != RiskVerdict.APPROVED:
        try:
            from data.models import PaperTrade, PaperTradeStatus
            from data.storage import (
                get_paper_trade_by_key,
                get_paper_trade_by_signal_strategy,
                upsert_paper_trade,
            )
            for strategy in strategies:
                key = _idempotency_key(signal, strategy)
                existing = get_paper_trade_by_key(key)
                if existing is None:
                    existing = get_paper_trade_by_signal_strategy(
                        ticker,
                        signal.date,
                        strategy.name,
                    )
                if existing and existing.get("alpaca_order_id"):
                    logger.warning(
                        f"[{ticker}:{strategy.name}] signal flipped to REJECTED "
                        f"but earlier run submitted alpaca_order_id="
                        f"{existing['alpaca_order_id']} — preserving broker-linked row"
                    )
                    continue
                upsert_paper_trade(PaperTrade(
                    recommendation_id=signal.recommendation_id,
                    ticker=ticker,
                    signal_date=signal.date,
                    strategy=strategy.name,
                    idempotency_key=key,
                    decision=signal.decision,
                    side="hold" if signal.decision == Decision.HOLD else "rejected",
                    entry_limit=signal.entry_zone[0] if signal.entry_zone else 0.0,
                    stop_loss=signal.stop_loss,
                    take_profit=signal.targets[0] if signal.targets else 0.0,
                    notional_pct=strategy.notional_pct,
                    status=PaperTradeStatus.EXECUTION_SKIPPED,
                    status_reason="risk_rejected",
                ))
        except Exception as exc:
            logger.warning(f"[{ticker}] failed to log REJECTED skip: {exc}")
        return state

    # APPROVED → fan out across strategies in parallel.
    # ISOLATION: per-strategy try/except inside _safe_execute. Drawdown
    # breaker check happens inside the loop (one DB lookup per strategy
    # per signal — cheap, O(3) per ticker).
    try:
        asyncio.run(_execute_paper_trades_async(signal, strategies))
    except Exception as exc:
        logger.exception(f"[{ticker}] execute_paper_trades_async unexpected exception: {exc}")
    return state


async def _execute_paper_trades_async(signal, strategies):
    """Async fan-out over strategies. Each strategy's sync execute_paper_trade
    runs in a separate thread via asyncio.to_thread so the network-bound
    Alpaca call releases the GIL. asyncio.gather waits for all 3 to settle.
    """
    import asyncio

    tasks = []
    for strategy in strategies:
        # Pre-trade pause check (cheap DB lookup, sync, before spawning thread).
        # If breaker tripped, log skip and don't submit.
        try:
            if is_paused(strategy):
                logger.info(
                    f"[{signal.ticker}:{strategy.name}] strategy paused — skipping order"
                )
                continue
        except Exception as exc:
            logger.warning(f"[{signal.ticker}:{strategy.name}] is_paused check failed: {exc}")
            # On breaker error, default to NOT paused (fail-open) — losing one
            # trade is worse than skipping when we shouldn't.
        tasks.append(asyncio.to_thread(_safe_execute, signal, strategy))

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=False)


def _safe_execute(signal, strategy):
    """Per-strategy try/except wrapper. Logs and swallows so one strategy's
    failure doesn't propagate to asyncio.gather (which would cancel siblings)."""
    try:
        result = execute_paper_trade(signal, strategy)
        if result:
            logger.info(f"[{signal.ticker}:{strategy.name}] paper trade: {result}")
    except Exception as exc:
        logger.exception(
            f"[{signal.ticker}:{strategy.name}] execute_paper_trade unexpected: {exc}"
        )


# ─── Check for errors between stages ─────────────────────

def should_continue(state: PipelineState) -> str:
    if state.get("error"):
        return END
    return "continue"


# ─── Build the graph ─────────────────────────────────────

def build_graph() -> StateGraph:
    graph = StateGraph(PipelineState)

    # Each node wrapped in _timed_node so we can see where the time goes
    # in a 6-hour run. The decorator accumulates per-node durations into
    # state['_timings']; run_ticker emits the summary line at the end.
    graph.add_node("fetch_data",   _timed_node("fetch_data",   fetch_data))
    graph.add_node("analysts",     _timed_node("analysts",     run_analysts))
    graph.add_node("researchers",  _timed_node("researchers",  run_researchers))
    graph.add_node("trader",       _timed_node("trader",       run_trader))
    graph.add_node("risk_manager", _timed_node("risk_manager", run_risk_manager))
    graph.add_node("compose",      _timed_node("compose",      compose_signal))
    graph.add_node("execute_paper", _timed_node("execute_paper", execute_paper_trades))

    graph.set_entry_point("fetch_data")
    graph.add_conditional_edges("fetch_data", should_continue, {"continue": "analysts", END: END})
    graph.add_conditional_edges("analysts", should_continue, {"continue": "researchers", END: END})
    graph.add_conditional_edges("researchers", should_continue, {"continue": "trader", END: END})
    graph.add_conditional_edges("trader", should_continue, {"continue": "risk_manager", END: END})
    graph.add_conditional_edges("risk_manager", should_continue, {"continue": "compose", END: END})
    # compose → execute_paper is unconditional. execute_paper has its own
    # internal isolation; failures there don't roll back the saved signal.
    graph.add_edge("compose", "execute_paper")
    graph.add_edge("execute_paper", END)

    return graph


_compiled_graph = None


def get_graph():
    global _compiled_graph
    if _compiled_graph is None:
        _compiled_graph = build_graph().compile()
    return _compiled_graph


# ─── Run pipeline for a single ticker ────────────────────

TICKER_TIMEOUT_SECONDS = 300  # fallback if settings cannot be loaded


def run_ticker(
    ticker: str,
    external_digest: str | None = None,
    digest_summary: DigestDistillation | None = None,
    regime: RegimeClassification | None = None,
    run_id: str | None = None,
    prefetched_pkg: TickerDataPackage | None = None,
) -> FinalSignal | None:
    """Run the full pipeline for one ticker with a 5-minute timeout.

    `prefetched_pkg` is the optional output of `run_all`'s parallel
    prefetch. Pipeline-internal nodes don't care whether it was prefetched
    or built live; the `fetch_data` node consumes it transparently.

    `digest_summary` is the optional output of `run_all`'s one-shot
    distillation. Downstream stages (analysts, researchers, trader) read
    this compact form instead of the raw digest blob.

    Returns the signal or None on failure/timeout.
    """
    graph = get_graph()
    initial_state: PipelineState = {
        "ticker": ticker,
        "external_digest": external_digest,
        "digest_summary": digest_summary,
        "regime": regime,
        "run_id": run_id,
    }
    if prefetched_pkg is not None:
        initial_state["prefetched_pkg"] = prefetched_pkg

    def _invoke():
        return graph.invoke(initial_state)

    from config import settings as cfg
    timeout_seconds = cfg.ticker_timeout_seconds

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_invoke)
            result = future.result(timeout=timeout_seconds)
    except TimeoutError:
        logger.error(f"[{ticker}] Pipeline timed out after {timeout_seconds}s")
        return None
    except Exception as e:
        logger.error(f"[{ticker}] Pipeline crashed: {e}")
        return None

    timings = result.get("_timings") or {}
    if timings:
        parts = " ".join(f"{k}={v:.1f}s" for k, v in timings.items())
        total = sum(timings.values())
        logger.info(f"[{ticker}] timing summary: total={total:.1f}s {parts}")

    if result.get("error"):
        logger.error(f"[{ticker}] Pipeline failed: {result['error']}")
        return None
    return result.get("final_signal")


# ─── Concentration check ────────────────────────────────

def _check_concentration(signals: list[FinalSignal]) -> list[str]:
    """Check for sector concentration in BUY signals. Returns warning strings."""
    from config import settings as cfg
    from data.storage import get_connection
    import json as _json

    buy_tickers = [s.ticker for s in signals if s.decision == Decision.BUY]
    if len(buy_tickers) < 2:
        return []

    # Look up sectors from today's analyst reports
    sector_map = {}
    try:
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT ticker, reports FROM analyst_reports WHERE date = %s",
                (date.today(),),
            ).fetchall()
        for row in rows:
            reports_data = row["reports"]
            if isinstance(reports_data, str):
                reports_data = _json.loads(reports_data)
            sector = (reports_data.get("fundamentals", {}).get("sector")) or "Unknown"
            sector_map[row["ticker"]] = sector
    except Exception as e:
        logger.warning(f"Could not fetch sectors for concentration check: {e}")
        return []

    # Count how many sectors are unknown
    buy_sectors = [sector_map.get(t, "Unknown") for t in buy_tickers]
    unknown_pct = buy_sectors.count("Unknown") / len(buy_sectors) if buy_sectors else 0
    if unknown_pct > 0.5:
        logger.warning("Skipping concentration check: >50% of BUY signals have no sector data")
        return []

    # Group by sector
    from collections import Counter
    sector_counts = Counter(s for s in buy_sectors if s != "Unknown")

    warnings = []
    threshold = cfg.max_sector_concentration
    for sector, count in sector_counts.most_common():
        if count > threshold:
            tickers_in_sector = [t for t in buy_tickers if sector_map.get(t) == sector]
            warnings.append(
                f"{count} BUY signals in {sector} ({', '.join(tickers_in_sector)}) "
                f"exceeds threshold of {threshold}"
            )

    return warnings


# ─── Cancellation ────────────────────────────────────────
# A single module-level flag since only one pipeline run is expected at a time.
# `run_all` clears it on entry and checks it between tickers — granularity is
# bounded by `ticker_timeout_seconds`, worst case ~5 min to stop.

_cancel_event = threading.Event()


def request_cancel() -> None:
    _cancel_event.set()


def is_cancel_requested() -> bool:
    return _cancel_event.is_set()


# ─── Run pipeline for all tickers ────────────────────────

_PREFETCH_MAX_WORKERS = 4
_PREFETCH_MAX_RETRIES = 3


def _prefetch_one(ticker: str, max_retries: int) -> TickerDataPackage | None:
    """Single-ticker prefetch with exponential backoff on raised exceptions.

    `build_data_package` returns None for "no price data available" — that's
    usually terminal (delisted ticker or yfinance permanently rejecting the
    symbol), so we retry None once in case it was a transient throttle and
    then give up. Raised exceptions get full exponential backoff (1s, 2s, 4s).
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            pkg = build_data_package(ticker)
            if pkg is not None:
                return pkg
            if attempt == 0:
                logger.info(f"[{ticker}] prefetch returned None on attempt 1, retrying once")
                time.sleep(1.0)
                continue
            return None
        except Exception as exc:
            last_exc = exc
            wait = 2 ** attempt
            logger.warning(
                f"[{ticker}] prefetch attempt {attempt + 1}/{max_retries} "
                f"raised {type(exc).__name__}: {exc}; retrying in {wait}s"
            )
            time.sleep(wait)
    if last_exc is not None:
        raise last_exc
    return None


def _prefetch_data_packages(
    tickers: list[str],
    *,
    max_workers: int = _PREFETCH_MAX_WORKERS,
    max_retries: int = _PREFETCH_MAX_RETRIES,
) -> dict[str, TickerDataPackage]:
    """Parallel yfinance prefetch across tickers (LLM stages stay serial).

    `max_workers=4` is the chosen bound: yfinance 429s climb sharply above
    this on a 28-ticker batch. Failures are isolated per-ticker — a ticker
    that exhausts its retries simply doesn't end up in the cache, and the
    pipeline's `fetch_data` node falls back to a live fetch for that one.
    """
    results: dict[str, TickerDataPackage] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_prefetch_one, t, max_retries): t for t in tickers}
        for future in as_completed(futures):
            ticker = futures[future]
            try:
                pkg = future.result()
                if pkg is not None:
                    results[ticker] = pkg
            except Exception as exc:
                logger.warning(
                    f"[{ticker}] prefetch failed after {max_retries} retries: "
                    f"{type(exc).__name__}: {exc} (will retry in pipeline)"
                )
    return results


def run_all(tickers: list[str] | None = None) -> list[FinalSignal]:
    """Run the pipeline for all tickers. yfinance prefetch is parallel
    (4-wide pool + backoff); LLM-bound stages remain serial because the
    llama.cpp client serializes through `_llama_cpp_lock` anyway.

    Returns list of successful signals.
    """
    from data.watchlist import merged_tickers
    _cancel_event.clear()
    tickers = tickers or merged_tickers()
    reset_analyst_comparison_logs()

    run_id = f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    meta = RunMetadata(
        run_id=run_id,
        start_time=datetime.now(),
        tickers_attempted=tickers,
    )

    # Read digest and classify regime once for the whole run
    digest = read_digest()
    regime = classify_regime(digest)
    logger.info(f"Run {run_id}: digest={'yes' if digest else 'no'}, regime={regime.regime.value}")

    # Distill the digest ONCE per run instead of re-feeding the raw blob
    # into 168+ per-ticker LLM prompts (News analyst + bull + bear + trader).
    # Failure mode is silent: distillation returning None means downstream
    # prompts simply omit macro context (regime alone remains the signal).
    t_distill = time.monotonic()
    digest_summary = distill_digest(digest)
    logger.info(
        f"Digest distillation: {'ok' if digest_summary else 'skipped/failed'} "
        f"({time.monotonic() - t_distill:.1f}s)"
    )

    # Parallel yfinance prefetch (LLM stages still go serial, ticker-by-ticker).
    # Saves wall-clock on the fetch phase without touching the llama.cpp lock.
    logger.info(
        f"Prefetching data for {len(tickers)} tickers "
        f"(max_workers={_PREFETCH_MAX_WORKERS})..."
    )
    t_prefetch = time.monotonic()
    prefetched = _prefetch_data_packages(tickers)
    logger.info(
        f"Prefetched {len(prefetched)}/{len(tickers)} packages in "
        f"{time.monotonic() - t_prefetch:.1f}s"
    )

    signals = []
    cancelled = False
    for ticker in tickers:
        if _cancel_event.is_set():
            logger.info(f"Run {run_id} cancelled before processing {ticker}; skipping remaining tickers")
            cancelled = True
            break
        try:
            signal = run_ticker(
                ticker,
                external_digest=digest,
                digest_summary=digest_summary,
                regime=regime,
                run_id=run_id,
                prefetched_pkg=prefetched.get(ticker),
            )
            if signal:
                signals.append(signal)
                meta.tickers_completed.append(ticker)
            else:
                meta.tickers_failed.append(ticker)
        except Exception as e:
            logger.error(f"[{ticker}] Unexpected error: {e}")
            meta.tickers_failed.append(ticker)
            meta.errors.append({"ticker": ticker, "error": str(e)})

    if cancelled:
        done = set(meta.tickers_completed) | set(meta.tickers_failed)
        skipped = [t for t in tickers if t not in done]
        meta.tickers_failed.extend(skipped)
        meta.errors.append({"ticker": "", "error": f"Run cancelled by user; {len(skipped)} tickers skipped"})

    # Post-pipeline: concentration warnings
    concentration_warnings = _check_concentration(signals)
    for warning in concentration_warnings:
        logger.warning(f"CONCENTRATION: {warning}")
    meta.concentration_warnings = concentration_warnings

    meta.end_time = datetime.now()
    save_run_metadata(meta)

    logger.info(
        f"Run {run_id} complete: {len(signals)}/{len(tickers)} signals generated "
        f"in {(meta.end_time - meta.start_time).total_seconds():.0f}s"
        f"{' (cancelled)' if cancelled else ''}"
    )
    return signals

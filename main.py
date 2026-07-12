import logging
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from config import settings
from data.pipeline import DIGEST_PATH
from data.watchlist import update_watchlist
from data.storage import (
    cleanup_zombie_option_refresh_jobs,
    get_agent_leaderboard,
    get_debate,
    get_latest_concentration_warnings,
    get_latest_run_status,
    get_latest_signals,
    get_metrics_counters,
    get_paper_trades_by_date,
    get_previous_signals,
    get_recommendation_dashboard_summary,
    get_recommendations_by_date,
    get_signal_scores_by_date,
    get_signals_by_date,
    get_strategy_summary_stats,
    get_today_execution_stats,
    init_db,
)
from agents.strategies import STRATEGIES, strategy_color_token
from recommendation.target_sanity import (
    choose_replacement_target,
    is_implausible_target,
    target_sanity_note,
)

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "stockinvest.log"),
    ],
)

# Quieten noisy third-party loggers
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("yfinance").setLevel(logging.INFO)
logging.getLogger("peewee").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # A3: a uvicorn restart mid-refresh leaves the running-job slot
    # permanently locked unless we sweep stale rows on boot.
    zombie_count = cleanup_zombie_option_refresh_jobs()
    if zombie_count:
        logging.getLogger(__name__).warning(
            f"cleanup_zombie_option_refresh_jobs marked {zombie_count} stale job(s) as failed"
        )
    # Decision 26: deploy-time annotation. Idempotent — first INSERT wins
    # so the wave-2 boundary timestamp survives uvicorn restarts and the
    # /retro query filter against it stays stable.
    try:
        from data.storage import upsert_deploy_annotation

        upsert_deploy_annotation("wave_2_started_at", datetime.now().isoformat())
    except Exception as exc:
        logging.getLogger(__name__).warning(
            f"wave_2_started_at annotation skipped: {exc}"
        )
    # A4: TWS heartbeat. Only start when IBKR is the configured preferred
    # provider — yfinance-only environments (CI, dev boxes without Gateway)
    # would otherwise log a constant stream of connect-refused errors.
    if settings.options_data_provider == "ibkr":
        from options.heartbeat import get_heartbeat
        heartbeat = get_heartbeat()
        heartbeat.start()
        try:
            yield
        finally:
            await heartbeat.stop()
    else:
        yield


app = FastAPI(title="StockInvest", description="AI Trading Research Signals", lifespan=lifespan)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def format_price(value: float) -> str:
    return f"${value:,.2f}"


templates.env.filters["format_price"] = format_price


def _filter_recommendations(rows: list[dict], active_filter: str) -> list[dict]:
    if active_filter in {"BUY", "SELL", "HOLD"}:
        return [row for row in rows if row.get("decision") == active_filter]
    if active_filter in {"APPROVED", "REJECTED"}:
        return [row for row in rows if row.get("risk_verdict") == active_filter]
    return rows


def _parse_ticker_csv(tickers: str | None) -> list[str] | None:
    if not tickers:
        return None
    parsed = [ticker.strip().upper() for ticker in tickers.split(",") if ticker.strip()]
    return parsed or None


def _sanitize_debate_for_signal(debate: dict, signal) -> dict:
    """Hide stale/pre-split debate targets in the dashboard detail panel."""
    if not signal.entry_zone:
        return debate

    sanitized = deepcopy(debate)
    reference_price = sum(signal.entry_zone) / len(signal.entry_zone)
    fallback_targets = signal.targets or []
    if signal.stop_loss:
        fallback_targets = [*fallback_targets, signal.stop_loss]

    for research_round in sanitized.get("rounds") or []:
        for side in ("bull", "bear"):
            case = research_round.get(side) or {}
            target = case.get("price_target")
            if not is_implausible_target(target, reference_price):
                continue
            replacement = choose_replacement_target(
                case.get("stance", side),
                reference_price,
                fallback_targets=fallback_targets,
            )
            case["target_sanity"] = {
                "original": float(target),
                "replacement": replacement,
                "note": target_sanity_note(float(target), replacement, reference_price),
            }
            case["price_target"] = replacement
    return sanitized


def _digest_status() -> dict:
    """Check if today's digest exists."""
    if DIGEST_PATH.exists():
        mtime = datetime.fromtimestamp(DIGEST_PATH.stat().st_mtime)
        if mtime.date() == date.today():
            return {"has_digest": True, "digest_date": mtime.strftime("%H:%M")}
    return {"has_digest": False, "digest_date": None}


@app.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    report_date: date | None = Query(default=None),
    filter: str = Query(default="all"),
):
    target_date = report_date or date.today()
    signals = get_signals_by_date(target_date)
    recommendations = get_recommendations_by_date(target_date)

    if filter == "BUY":
        signals = [s for s in signals if s.decision.value == "BUY"]
    elif filter == "SELL":
        signals = [s for s in signals if s.decision.value == "SELL"]
    elif filter == "HOLD":
        signals = [s for s in signals if s.decision.value == "HOLD"]
    elif filter == "APPROVED":
        signals = [s for s in signals if s.risk_verdict.value == "APPROVED"]
    elif filter == "REJECTED":
        signals = [s for s in signals if s.risk_verdict.value == "REJECTED"]
    recommendations = _filter_recommendations(recommendations, filter)

    # Score badges for this date's signals
    score_map = get_signal_scores_by_date(target_date)

    # Agent leaderboard
    leaderboard = get_agent_leaderboard()

    # Signal changelog: compare today vs previous run
    changelog = []
    all_signals = get_signals_by_date(target_date)  # unfiltered for changelog
    prev_signals = get_previous_signals(target_date)
    if all_signals and prev_signals:
        prev_map = {s.ticker: s for s in prev_signals}
        for s in all_signals:
            prev = prev_map.get(s.ticker)
            if prev is None:
                changelog.append({
                    "ticker": s.ticker,
                    "type": "new",
                    "text": f"{s.ticker}: NEW {s.decision.value} ({s.confidence:.0%})",
                })
            elif s.decision != prev.decision:
                changelog.append({
                    "ticker": s.ticker,
                    "type": "changed",
                    "text": f"{s.ticker}: {prev.decision.value} \u2192 {s.decision.value} ({prev.confidence:.0%} \u2192 {s.confidence:.0%})",
                })
            elif abs(s.confidence - prev.confidence) >= 0.1:
                delta = s.confidence - prev.confidence
                arrow = "\u2191" if delta > 0 else "\u2193"
                changelog.append({
                    "ticker": s.ticker,
                    "type": "adjusted",
                    "text": f"{s.ticker}: {s.decision.value} confidence {arrow} {abs(delta):.0%} ({prev.confidence:.0%} \u2192 {s.confidence:.0%})",
                })

    run_status = get_latest_run_status(target_date)

    # Paper-trading execution stats + per-ticker rows for today.
    # Wave 1.5: 3 trades per ticker (one per strategy). Group by ticker as
    # {ticker: {strategy: pt_dict}} so the template can render 3 chips per row
    # in the canonical AGG/BAL/CON order.
    exec_stats = get_today_execution_stats(target_date)
    paper_trades = get_paper_trades_by_date(target_date)
    paper_trades_by_ticker: dict[str, dict[str, dict]] = {}
    paper_trades_by_recommendation: dict[str, dict[str, dict]] = {}
    for pt in paper_trades:
        paper_trades_by_ticker.setdefault(pt["ticker"], {})[pt.get("strategy", "balanced")] = pt
        recommendation_id = pt.get("recommendation_id")
        if recommendation_id:
            paper_trades_by_recommendation.setdefault(
                recommendation_id, {}
            )[pt.get("strategy", "balanced")] = pt

    for rec in recommendations:
        rec["paper_trades_by_strategy"] = paper_trades_by_recommendation.get(
            rec.get("recommendation_id"), {}
        )

    # Per-strategy summary panel data (E3): today/week/all-time P&L + sparkline.
    strategy_summary = get_strategy_summary_stats(window_days=30)
    # Render-ready strategy descriptors for the template (color token + display name)
    strategies_meta = [
        {
            "name": s.name,
            "display_name": s.name.title(),
            "short_name": s.name[:3].upper(),  # AGG / BAL / CON
            "notional_pct": s.notional_pct,
            "color_token": strategy_color_token(s.name),
            "enabled": s.enabled,
        }
        for s in STRATEGIES
    ]

    # Per-signal debate rounds + judge verdict for the timeline UI
    debate_by_ticker = {}
    for s in signals:
        d = get_debate(s.ticker, target_date)
        if d:
            debate_by_ticker[s.ticker] = _sanitize_debate_for_signal(d, s)

    option_tickers = sorted({
        *(s.ticker for s in all_signals),
        *(rec.get("ticker") for rec in recommendations if rec.get("ticker")),
    })
    if not option_tickers:
        option_tickers = settings.tickers[:8]
    try:
        from options.service import build_options_dashboard

        options_dashboard = build_options_dashboard(
            tickers=option_tickers,
            per_ticker_limit=3,
            total_limit=12,
        ).model_dump(mode="json")
    except Exception as exc:
        logging.getLogger(__name__).warning("Options dashboard unavailable: %s", exc)
        options_dashboard = {
            "snapshots": [],
            "candidates": [],
            "summary": {},
        }

    # D3 IV-rank screener: ranked + cold-start buckets per DR1/DR8. Fail-open
    # so a screener outage doesn't take down the dashboard — empty buckets
    # render the DR3-default-empty copy.
    try:
        from options.screener import build_iv_screener

        screener_result = build_iv_screener(limit=100)
        iv_screener = {
            "ranked": [row.model_dump(mode="json") for row in screener_result.ranked],
            "cold_start": screener_result.cold_start,
        }
    except Exception as exc:
        logging.getLogger(__name__).warning("IV screener unavailable: %s", exc)
        iv_screener = {"ranked": [], "cold_start": []}

    # D2 protective costs: latest cost keyed by recommendation ticker so the
    # v2-grid card can render the DR9 protect-field without per-row queries.
    # Open positions are joined to recommendation_id via paper_trades; we map
    # ticker→cost as a defensive fallback when the position_id linkage is
    # missing (e.g., a freshly-recommended ticker with no paper position yet).
    protective_by_ticker: dict[str, dict] = {}
    try:
        from data.storage import (
            get_latest_protective_costs,
            get_open_paper_positions,
        )

        open_positions = get_open_paper_positions()
        # paper_trade_id IS the position_id by FK convention (paper_positions PK).
        position_ids = [
            int(p["paper_trade_id"]) for p in open_positions
            if p.get("paper_trade_id") is not None
        ]
        ticker_by_position = {
            int(p["paper_trade_id"]): (p.get("ticker") or "").upper()
            for p in open_positions if p.get("paper_trade_id") is not None
        }
        cost_by_position = get_latest_protective_costs(position_ids)
        for pos_id, cost in cost_by_position.items():
            ticker = ticker_by_position.get(pos_id)
            if not ticker:
                continue
            # If a ticker has multiple open positions (3 strategies), keep
            # the most recent compute. dict insertion order respects the
            # storage layer's DISTINCT ON ordering.
            protective_by_ticker.setdefault(ticker, cost.model_dump(mode="json"))
    except Exception as exc:
        logging.getLogger(__name__).warning("Protective costs unavailable: %s", exc)

    # DR2 status row: latest refresh job. None = idle (status row collapses
    # to just the freshness pill).
    try:
        from data.storage import get_latest_option_refresh_job

        latest_refresh_job = get_latest_option_refresh_job()
    except Exception as exc:
        logging.getLogger(__name__).warning("Refresh job status unavailable: %s", exc)
        latest_refresh_job = None

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "signals": signals,
            "recommendations": recommendations,
            "report_date": target_date.isoformat(),
            "filter": filter,
            "score_map": score_map,
            "leaderboard": leaderboard,
            "changelog": changelog,
            "concentration_warnings": get_latest_concentration_warnings(),
            "run_status": run_status,
            "exec_stats": exec_stats,
            "recommendation_summary": get_recommendation_dashboard_summary(days=30),
            "paper_trades_by_ticker": paper_trades_by_ticker,
            "strategy_summary": strategy_summary,
            "strategies_meta": strategies_meta,
            "debate_by_ticker": debate_by_ticker,
            "options_dashboard": options_dashboard,
            "iv_screener": iv_screener,
            "protective_by_ticker": protective_by_ticker,
            "latest_refresh_job": latest_refresh_job,
            "option_refresh_tickers": ",".join(option_tickers),
            **_digest_status(),
        },
    )


@app.get("/api/signals")
async def api_signals(
    report_date: date | None = Query(default=None),
):
    target_date = report_date or date.today()
    return get_signals_by_date(target_date)


class DigestRequest(BaseModel):
    content: str


class OptionsRefreshRequest(BaseModel):
    tickers: list[str] | None = None
    max_expirations: int = Field(default=4, ge=1, le=12)


def _write_digest(content: str) -> tuple[str, str]:
    """Append to today's digest, or overwrite if the file is stale. Returns (mode, text_written)."""
    DIGEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    content = content.strip()
    if DIGEST_PATH.exists():
        mtime = datetime.fromtimestamp(DIGEST_PATH.stat().st_mtime)
        if mtime.date() == date.today():
            separator = f"\n\n--- [{datetime.now().strftime('%H:%M')} update] ---\n"
            existing = DIGEST_PATH.read_text().rstrip()
            combined = f"{existing}{separator}{content}\n"
            DIGEST_PATH.write_text(combined)
            return "append", combined
    DIGEST_PATH.write_text(content + "\n")
    return "overwrite", content


@app.post("/api/digest")
async def api_digest(req: DigestRequest, background: BackgroundTasks):
    """Save ChatGPT macro digest for today's pipeline run."""
    if not req.content.strip():
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=400, content={"error": "Digest content cannot be empty"})
    try:
        mode, full_text = _write_digest(req.content)
        # Ticker extraction hits the local LLM (slow); run it after the response is sent.
        background.add_task(update_watchlist, full_text)
        return {"status": "ok", "mode": mode, "date": date.today().isoformat()}
    except Exception as e:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/api/run")
def api_run():
    """Trigger a pipeline run. Sync def so FastAPI runs it in a threadpool,
    leaving the event loop free to handle /api/run/stop mid-run."""
    from agents.graph import run_all
    signals = run_all()
    status = get_latest_run_status(date.today()) or {}
    return {
        "status": "ok",
        "signals_generated": len(signals),
        "attempted": status.get("attempted", []),
        "completed": status.get("completed", []),
        "failed": status.get("failed", []),
    }


@app.post("/api/run/stop")
async def api_run_stop():
    """Signal the in-flight pipeline run to stop after the current ticker."""
    from agents.graph import request_cancel
    request_cancel()
    return {"status": "ok", "message": "Cancellation requested"}


@app.post("/api/score")
async def api_score(days_back: int = Query(default=10)):
    """Trigger scoring for recent signals and immutable recommendations."""
    from agents.scorer import score_recommendations, score_signals
    signals = score_signals(days_back=days_back)
    recommendations = score_recommendations(days_back=days_back)
    return {
        "status": "ok",
        "signals_scored": len(signals),
        "recommendations_scored": len(recommendations),
        "results": signals,
        "recommendation_results": recommendations,
    }


@app.get("/api/scores")
async def api_scores(days: int = Query(default=30)):
    """Get recent signal score history."""
    from data.storage import get_score_history
    return get_score_history(days=days)


@app.get("/api/recommendations")
async def api_recommendations(
    ticker: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
):
    """Get recent immutable recommendation ledger rows."""
    from data.storage import get_latest_recommendations
    return get_latest_recommendations(limit=limit, ticker=ticker)


@app.get("/api/recommendations/{recommendation_id}")
async def api_recommendation_detail(recommendation_id: str):
    """Get one immutable recommendation with snapshot, score, and execution audit."""
    from data.storage import get_recommendation_audit_detail

    detail = get_recommendation_audit_detail(recommendation_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Recommendation not found")
    return detail


@app.get("/api/recommendation_scores")
async def api_recommendation_scores(
    days: int = Query(default=90, ge=1, le=3650),
    limit: int = Query(default=100, ge=1, le=1000),
):
    """Get recent scored immutable recommendations."""
    from data.storage import get_recommendation_score_history
    return get_recommendation_score_history(days=days, limit=limit)


@app.get("/api/recommendation_calibration")
async def api_recommendation_calibration(
    days: int = Query(default=90, ge=1, le=3650),
    min_samples: int = Query(default=3, ge=1, le=1000),
):
    """Get recommendation calibration buckets and compact summary text."""
    from data.storage import get_recommendation_calibration
    from recommendation.calibration import build_calibration_summary

    buckets = get_recommendation_calibration(days=days, min_samples=min_samples)
    return {
        "buckets": buckets,
        "summary": build_calibration_summary(buckets),
    }


@app.get("/api/recommendation_summary")
async def api_recommendation_summary(
    days: int = Query(default=30, ge=1, le=3650),
):
    """Get compact recommendation-ledger health metrics."""
    from data.storage import get_recommendation_dashboard_summary
    return get_recommendation_dashboard_summary(days=days)


@app.get("/api/recommendation_track_records")
async def api_recommendation_track_records(
    days: int = Query(default=180, ge=1, le=3650),
    min_samples: int = Query(default=3, ge=1, le=1000),
):
    """Get recommendation performance grouped by ticker, strategy, side, and regime."""
    from data.storage import get_recommendation_track_records
    return get_recommendation_track_records(days=days, min_samples=min_samples)


@app.get("/api/options/dashboard")
async def api_options_dashboard(
    tickers: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
):
    """Get latest stored option-chain rankings for the cockpit."""
    from options.service import build_options_dashboard

    return build_options_dashboard(
        tickers=_parse_ticker_csv(tickers),
        per_ticker_limit=5,
        total_limit=limit,
    ).model_dump(mode="json")


@app.get("/api/options/screener")
async def api_options_screener(
    tickers: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
):
    """D3 — IV-rank screener. Returns `ranked` (≥60d history, sorted by
    iv_rank_30d desc) and `cold_start` (insufficient-history tickers in
    alphabetical order) per DR8's two-bucket wireframe.
    """
    from options.screener import build_iv_screener

    result = build_iv_screener(
        tickers=_parse_ticker_csv(tickers),
        limit=limit,
    )
    return {
        "ranked": [row.model_dump(mode="json") for row in result.ranked],
        "cold_start": result.cold_start,
        "total": result.total,
    }


@app.post("/api/options/protective_costs")
async def api_options_protective_costs():
    """D2 — recompute protective-put cost for every open paper position.

    Reads `paper_positions` via the frozen `get_open_paper_positions()` API
    (A1) and writes to `option_protective_costs`. Returns the summary
    counts (`computed`, `skipped_no_chain`, `skipped_no_put`).
    """
    from options.protective import refresh_protective_costs

    return refresh_protective_costs()


class ThesisFeedbackRequest(BaseModel):
    ticker: str
    strategy: str
    recommendation_id: str | None = None
    sentiment: str  # 'up' | 'down'


@app.post("/api/options/thesis/feedback")
async def api_options_thesis_feedback(req: ThesisFeedbackRequest):
    """Decision 26 — thesis-card thumbs-up/down. Fail-open: a failed
    feedback insert returns success-shape to the client because UX
    correctness here matters more than capturing every vote.
    """
    from data.storage import record_option_thesis_feedback

    feedback_id = record_option_thesis_feedback(
        ticker=req.ticker,
        strategy=req.strategy,
        recommendation_id=req.recommendation_id,
        sentiment=req.sentiment,
    )
    return {"status": "ok", "id": feedback_id}


class CockpitViewRequest(BaseModel):
    panel: str  # 'screener' | 'protect' | 'thesis' | 'flow' | ...


@app.post("/api/options/cockpit/view")
async def api_options_cockpit_view(req: CockpitViewRequest):
    """Decision 26 — panel-view counter. The dashboard fires one per
    panel on mount so /api/metrics can answer "which panels are
    actually used?" without external analytics.
    """
    from data.storage import record_cockpit_view

    record_cockpit_view(req.panel)
    return {"status": "ok"}


@app.get("/api/options/thesis/{ticker}/{strategy}")
async def api_options_thesis(
    ticker: str,
    strategy: str,
    recommendation_id: str | None = Query(default=None),
):
    """D4 — build (or fetch cached) thesis for one ticker/strategy combo.

    Uses the latest stored chain snapshot. P1 cache short-circuits the LLM
    cost when the chain hasn't advanced. STRUCTURED_ONLY responses still
    carry the spread; the dashboard shows the right column's
    "(rationale unavailable, retry pending)" copy per DR3.
    """
    from data.storage import get_latest_option_chain_snapshot
    from options.service import _snapshot_from_row
    from options.thesis import build_option_thesis

    row = get_latest_option_chain_snapshot(ticker)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"No option chain snapshot stored for {ticker.upper()}",
        )
    snapshot = _snapshot_from_row(row)
    result = build_option_thesis(
        snapshot=snapshot,
        strategy=strategy,
        recommendation_id=recommendation_id,
    )
    return {
        "status": result.status.value,
        "structure": result.structure.to_payload() if result.structure else None,
        "llm_failure_reason": (
            result.llm_failure_reason.value if result.llm_failure_reason else None
        ),
        "from_cache": result.from_cache,
        "elapsed_seconds": result.elapsed_seconds,
    }


@app.post("/api/options/refresh", status_code=202)
async def api_options_refresh(req: OptionsRefreshRequest):
    """Decision 17 — async refresh job. Returns 202 + job_id immediately;
    the actual fetch + persist + IV-summary work runs on the asyncio loop
    behind a single-running-job lock (A3 partial unique index).

    Poll status at GET `/api/options/refresh/{job_id}`. If a job is already
    in flight, returns 409 Conflict instead of queuing a second job.
    """
    import asyncio

    from fastapi.responses import JSONResponse
    from options.refresh_job import build_preferred_fetcher, run_chain_refresh

    tickers = [ticker.upper() for ticker in (req.tickers or settings.tickers) if ticker]
    if not tickers:
        raise HTTPException(status_code=400, detail="No tickers configured for refresh")

    # Fire-and-forget: the job persists its own state to Postgres, so the
    # client polls the GET endpoint instead of awaiting completion here.
    # Using create_task keeps this on the FastAPI event loop (the
    # IBKRFetcher's worker-thread loop is decision 5 / A2 internal).
    async def _runner():
        try:
            await run_chain_refresh(
                tickers, max_expirations=req.max_expirations,
                preferred=build_preferred_fetcher(),
            )
        except RuntimeError as exc:
            # Another job acquired the slot first; this attempt loses cleanly.
            logging.getLogger("main").info(
                f"refresh job declined: {exc}"
            )
        except Exception as exc:
            logging.getLogger("main").exception(f"refresh job crashed: {exc}")

    task = asyncio.create_task(_runner(), name="options-refresh-runner")
    # We don't await `task`; the runner persists its own job_id row.
    # `_pending_job_tasks` keeps a reference so the loop doesn't GC the task
    # before it finishes — without this, asyncio is free to drop it.
    _pending_job_tasks.add(task)
    task.add_done_callback(_pending_job_tasks.discard)

    return JSONResponse(
        status_code=202,
        content={"status": "queued", "tickers": tickers},
    )


# Module-level strong refs for in-flight refresh tasks. Cleared when each
# task completes (done callback above). Without this, asyncio.create_task
# returns a weakly-referenced task that the GC can drop mid-run.
_pending_job_tasks: set[object] = set()


@app.get("/api/options/refresh/{job_id}")
async def api_options_refresh_status(job_id: str):
    """Decision 17 — poll endpoint. Returns the option_refresh_jobs row
    as JSON, or 404 if the job_id is unknown.
    """
    from data.storage import get_option_refresh_job

    row = get_option_refresh_job(job_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Unknown job_id: {job_id}")
    return row


@app.get("/api/options/refresh")
async def api_options_refresh_latest():
    """Decision 17 — current/latest job summary. Lets the dashboard render
    DR2's '⏳ Refreshing X/Y' row without polling for a specific job_id.
    """
    from data.storage import get_latest_option_refresh_job

    row = get_latest_option_refresh_job()
    return row or {"status": "idle"}


@app.get("/api/metrics")
async def api_metrics():
    """Lifetime paper-trading counters + open P&L. Plain JSON (Prometheus-shaped names)."""
    return get_metrics_counters()


@app.get("/api/paper_trades")
async def api_paper_trades(report_date: date | None = Query(default=None)):
    """Paper trades for a given date, joined with current position state."""
    target_date = report_date or date.today()
    return get_paper_trades_by_date(target_date)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000)

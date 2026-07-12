"""Persistent comparison logs for deterministic and LLM analyst outputs."""

from __future__ import annotations

import difflib
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from config import settings

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def record_analyst_comparison(
    *,
    ticker: str,
    analyst: str,
    baseline: BaseModel,
    mode: str,
    used_result: str,
    llm_result: BaseModel | None = None,
    error: str | None = None,
) -> None:
    """Append one analyst comparison record to the dated JSONL log."""
    if not settings.analyst_comparison_log_enabled:
        return

    baseline_data = _jsonable(baseline)
    llm_data = _jsonable(llm_result) if llm_result is not None else None
    now = datetime.now()

    record = {
        "timestamp": now.isoformat(timespec="seconds"),
        "date": now.date().isoformat(),
        "ticker": ticker,
        "analyst": analyst,
        "analyst_mode": mode,
        "used_result": used_result,
        "error": error,
        "baseline": baseline_data,
        "llm": llm_data,
        "diff": _make_diff(baseline_data, llm_data) if llm_data is not None else [],
    }

    try:
        path = _log_path(now)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception as exc:
        logger.warning(f"Failed to write analyst comparison log: {exc}")


def reset_analyst_comparison_logs() -> None:
    """Remove prior analyst comparison JSONL logs before a new pipeline run."""
    if not settings.analyst_comparison_log_enabled:
        return

    try:
        log_dir = _log_dir()
        if not log_dir.exists():
            return
        for path in log_dir.glob("analyst_comparisons_*.jsonl"):
            path.unlink()
    except Exception as exc:
        logger.warning(f"Failed to reset analyst comparison logs: {exc}")


def _log_path(now: datetime) -> Path:
    return _log_dir() / f"analyst_comparisons_{now.date().isoformat()}.jsonl"


def _log_dir() -> Path:
    log_dir = Path(settings.analyst_comparison_log_dir)
    if not log_dir.is_absolute():
        log_dir = PROJECT_ROOT / log_dir
    return log_dir


def _jsonable(value: BaseModel | Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


def _make_diff(left: Any, right: Any) -> list[str]:
    left_lines = json.dumps(left, indent=2, sort_keys=True).splitlines()
    right_lines = json.dumps(right, indent=2, sort_keys=True).splitlines()
    return list(
        difflib.unified_diff(
            left_lines,
            right_lines,
            fromfile="deterministic",
            tofile="llm",
            lineterm="",
        ),
    )

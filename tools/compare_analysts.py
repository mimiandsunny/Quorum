"""Compare LLM analyst output against the deterministic baseline for one ticker."""

from __future__ import annotations

import argparse
import difflib
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.analysts import fundamentals, news, sentiment, technical
from config import settings
from data.pipeline import build_data_package
from data.models import TickerDataPackage


ANALYSTS = {
    "technical": technical,
    "fundamentals": fundamentals,
    "sentiment": sentiment,
    "news": news,
}

SETTINGS_FIELDS = (
    "analyst_mode",
    "analyst_fallback",
    "analyst_include_deterministic_baseline",
    "analyst_max_retries",
)


def main() -> int:
    args = parse_args()

    ticker = args.ticker.upper()
    data = build_data_package(ticker)
    if data is None:
        print(f"No data package could be built for {ticker}.", file=sys.stderr)
        return 1

    report = compare_analysts(
        data=data,
        analyst_names=args.analysts,
        llm_mode=args.llm_mode,
        include_baseline=not args.no_baseline_in_prompt,
        llm_fallback=args.llm_fallback,
        max_retries=args.max_retries,
    )

    if args.format == "json":
        output = json.dumps(report, indent=2, sort_keys=True)
    else:
        output = format_markdown(report)

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output)
        print(f"Wrote comparison report to {output_path}")
    else:
        print(output)

    return 0


def parse_args() -> argparse.Namespace:
    default_mode = settings.analyst_mode if settings.analyst_mode in {"local", "cloud"} else "local"
    parser = argparse.ArgumentParser(
        description="Compare deterministic analyst output with live LLM analyst output.",
    )
    parser.add_argument("ticker", help="Ticker to compare, for example SPY or AAPL.")
    parser.add_argument(
        "--analysts",
        nargs="+",
        choices=sorted(ANALYSTS),
        default=list(ANALYSTS),
        help="Analyst stages to compare.",
    )
    parser.add_argument(
        "--llm-mode",
        choices=("local", "cloud"),
        default=default_mode,
        help="LLM backend to use for the comparison side.",
    )
    parser.add_argument(
        "--llm-fallback",
        choices=("off", "deterministic"),
        default="off",
        help="Fallback for the LLM side. Use off to see real LLM failures.",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=0,
        help="LLM parse retry count during comparison.",
    )
    parser.add_argument(
        "--no-baseline-in-prompt",
        action="store_true",
        help="Do not include the deterministic baseline inside the LLM prompt.",
    )
    parser.add_argument(
        "--format",
        choices=("markdown", "json"),
        default="markdown",
        help="Report format.",
    )
    parser.add_argument(
        "--output",
        help="Optional file path for the report. Prints to stdout when omitted.",
    )
    return parser.parse_args()


def compare_analysts(
    data: TickerDataPackage,
    analyst_names: list[str],
    llm_mode: str,
    include_baseline: bool,
    llm_fallback: str,
    max_retries: int,
) -> dict[str, Any]:
    comparisons = {}
    for name in analyst_names:
        module = ANALYSTS[name]
        deterministic = run_with_settings(
            module=module,
            data=data,
            mode="deterministic",
            fallback="off",
            include_baseline=False,
            max_retries=0,
        )
        llm = run_with_settings(
            module=module,
            data=data,
            mode=llm_mode,
            fallback=llm_fallback,
            include_baseline=include_baseline,
            max_retries=max_retries,
        )
        comparisons[name] = {
            "deterministic": deterministic,
            "llm": llm,
            "diff": make_json_diff(
                deterministic.get("result"),
                llm.get("result"),
            ) if deterministic["ok"] and llm["ok"] else [],
        }

    return {
        "ticker": data.ticker,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "llm_mode": llm_mode,
        "llm_fallback": llm_fallback,
        "baseline_in_prompt": include_baseline,
        "analysts": comparisons,
    }


def run_with_settings(
    module: Any,
    data: TickerDataPackage,
    mode: str,
    fallback: str,
    include_baseline: bool,
    max_retries: int,
) -> dict[str, Any]:
    snapshot = {field: getattr(settings, field) for field in SETTINGS_FIELDS}
    settings.analyst_mode = mode
    settings.analyst_fallback = fallback
    settings.analyst_include_deterministic_baseline = include_baseline
    settings.analyst_max_retries = max_retries

    started = time.monotonic()
    try:
        result = module.analyze(data)
        return {
            "ok": True,
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "result": result.model_dump(mode="json"),
        }
    except Exception as e:
        return {
            "ok": False,
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "error": f"{type(e).__name__}: {e}",
        }
    finally:
        for field, value in snapshot.items():
            setattr(settings, field, value)


def make_json_diff(left: Any, right: Any) -> list[str]:
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


def format_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# Analyst Comparison: {report['ticker']}",
        "",
        f"- Generated: {report['generated_at']}",
        f"- LLM mode: {report['llm_mode']}",
        f"- LLM fallback: {report['llm_fallback']}",
        f"- Baseline included in LLM prompt: {report['baseline_in_prompt']}",
        "",
    ]

    for name, comparison in report["analysts"].items():
        deterministic = comparison["deterministic"]
        llm = comparison["llm"]

        lines.extend([
            f"## {name.title()}",
            "",
            status_line("Deterministic", deterministic),
            status_line("LLM", llm),
            "",
        ])

        if deterministic["ok"]:
            lines.extend([
                "### Deterministic JSON",
                "",
                "```json",
                json.dumps(deterministic["result"], indent=2, sort_keys=True),
                "```",
                "",
            ])
        else:
            lines.extend(["### Deterministic Error", "", deterministic["error"], ""])

        if llm["ok"]:
            lines.extend([
                "### LLM JSON",
                "",
                "```json",
                json.dumps(llm["result"], indent=2, sort_keys=True),
                "```",
                "",
                "### Unified Diff",
                "",
                "```diff",
                "\n".join(comparison["diff"]) or "No differences.",
                "```",
                "",
            ])
        else:
            lines.extend(["### LLM Error", "", llm["error"], ""])

    return "\n".join(lines)


def status_line(label: str, result: dict[str, Any]) -> str:
    status = "ok" if result["ok"] else "failed"
    return f"- {label}: {status} in {result['elapsed_seconds']}s"


if __name__ == "__main__":
    raise SystemExit(main())

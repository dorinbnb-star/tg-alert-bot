#!/usr/bin/env python3
"""Durable, decision-neutral scan diagnostics for the entry monitor."""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


def _read_rows(path: Path) -> tuple[list[dict], int]:
    rows: list[dict] = []
    invalid = 0
    if not path.is_file():
        return rows, invalid
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            invalid += 1
            continue
        if isinstance(value, dict):
            rows.append(value)
        else:
            invalid += 1
    return rows, invalid


def _write_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def prepare(log_path: Path, meta_path: Path, current_path: Path, matched_key: str) -> dict:
    rows, invalid = _read_rows(log_path)
    unique: dict[str, dict] = {}
    for row in rows:
        record_id = str(row.get("record_id", "")).strip()
        if not record_id:
            invalid += 1
            continue
        unique[record_id] = row
    cleaned = list(unique.values())
    _write_rows(log_path, cleaned)
    _write_rows(current_path, [])
    meta = {
        "restored_rows": len(cleaned),
        "cache_reset": not bool(matched_key) or invalid > 0,
        "invalid_rows_removed": invalid,
    }
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(meta, sort_keys=True) + "\n", encoding="utf-8")
    return meta


def load_meta(path: Path | None) -> dict:
    if path is None or not path.is_file():
        return {"restored_rows": 0, "cache_reset": True, "invalid_rows_removed": 0}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"restored_rows": 0, "cache_reset": True, "invalid_rows_removed": 0}
    return value if isinstance(value, dict) else {"restored_rows": 0, "cache_reset": True}


def runtime_fields(symbol: str) -> dict:
    run_id = os.getenv("GITHUB_RUN_ID", "local")
    attempt = os.getenv("GITHUB_RUN_ATTEMPT", "1")
    return {
        "record_id": f"{run_id}:{attempt}:{symbol}",
        "run_id": run_id,
        "run_attempt": attempt,
        "branch": os.getenv("GITHUB_REF_NAME", "local"),
        "event": os.getenv("GITHUB_EVENT_NAME", "local"),
        "symbol": symbol,
        "logged_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }


def append_row(log_path: Path, current_path: Path, meta_path: Path | None, row: dict) -> dict:
    meta = load_meta(meta_path)
    enriched = {
        **runtime_fields(str(row.get("symbol", "UNKNOWN"))),
        "restored_rows": int(meta.get("restored_rows", 0)),
        "cache_reset": bool(meta.get("cache_reset", True)),
        **row,
    }
    for path in (log_path, current_path):
        rows, _ = _read_rows(path)
        rows = [item for item in rows if item.get("record_id") != enriched["record_id"]]
        rows.append(enriched)
        _write_rows(path, rows)
    return enriched


def ensure_error_row(
    log_path: Path,
    current_path: Path,
    meta_path: Path,
    symbol: str,
    scan_outcome: str,
    tests_outcome: str,
) -> dict | None:
    record_id = runtime_fields(symbol)["record_id"]
    current, _ = _read_rows(current_path)
    if any(row.get("record_id") == record_id for row in current):
        return None
    return append_row(log_path, current_path, meta_path, {
        "symbol": symbol,
        "status": "SCAN_ERROR",
        "stopped_at": "WORKFLOW",
        "reason": "scanner produced no diagnostic row",
        "scan_step_outcome": scan_outcome or "skipped",
        "tests_step_outcome": tests_outcome or "unknown",
    })


def summarize(paths: list[Path]) -> dict:
    unique: dict[str, dict] = {}
    invalid = 0
    for path in paths:
        rows, bad = _read_rows(path)
        invalid += bad
        for row in rows:
            record_id = str(row.get("record_id", "")).strip()
            if record_id:
                unique[record_id] = row
            else:
                invalid += 1
    rows = list(unique.values())
    return {
        "rows": len(rows),
        "invalid_rows": invalid,
        "symbols": dict(Counter(str(row.get("symbol", "UNKNOWN")) for row in rows)),
        "statuses": dict(Counter(str(row.get("status", "UNKNOWN")) for row in rows)),
        "stopped_at": dict(Counter(str(row.get("stopped_at", "UNKNOWN")) for row in rows)),
        "cache_resets": sum(bool(row.get("cache_reset")) for row in rows),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--log", type=Path, required=True)
    prepare_parser.add_argument("--meta", type=Path, required=True)
    prepare_parser.add_argument("--current", type=Path, required=True)
    prepare_parser.add_argument("--matched-key", default="")
    ensure_parser = subparsers.add_parser("ensure-error")
    ensure_parser.add_argument("--log", type=Path, required=True)
    ensure_parser.add_argument("--meta", type=Path, required=True)
    ensure_parser.add_argument("--current", type=Path, required=True)
    ensure_parser.add_argument("--symbol", required=True)
    ensure_parser.add_argument("--scan-outcome", default="")
    ensure_parser.add_argument("--tests-outcome", default="")
    summary_parser = subparsers.add_parser("summarize")
    summary_parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args(argv)
    if args.command == "prepare":
        result = prepare(args.log, args.meta, args.current, args.matched_key)
    elif args.command == "ensure-error":
        result = ensure_error_row(
            args.log, args.current, args.meta, args.symbol, args.scan_outcome, args.tests_outcome,
        ) or {"status": "ROW_PRESENT"}
    else:
        expanded: list[Path] = []
        for path in args.paths:
            expanded.extend(path.rglob("*.jsonl") if path.is_dir() else [path])
        result = summarize(expanded)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

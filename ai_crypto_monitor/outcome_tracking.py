#!/usr/bin/env python3
"""Persistent theoretical outcome tracking for accepted entry alerts."""

from __future__ import annotations

import argparse
import io
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Protocol


STATE_VERSION = 1
SECOND_MS = 1_000
MINUTE_MS = 60_000
QUARTER_MS = 15 * MINUTE_MS
ARTIFACT_NAME = "open-alert-state"


class OutcomeStateError(RuntimeError):
    pass


class CandleLike(Protocol):
    start_ms: int
    high: float
    low: float
    close: float


@dataclass
class TrackedAlert:
    alert_id: str
    symbol: str
    direction: str
    theoretical_entry: float
    verified_entry: float
    stop: float
    target: float
    tracking_rr: float
    strategy_rr: float
    opened_at_ms: int
    valid_until_ms: int
    timeout_at_ms: int
    cursor_ms: int
    telegram_message_id: int | None = None


@dataclass(frozen=True)
class Outcome:
    status: str
    result_r: float
    price: float
    occurred_at_ms: int
    candle_start_ms: int | None
    same_candle_collision: bool = False


def ceil_boundary(value: int, duration: int) -> int:
    return ((value + duration - 1) // duration) * duration


def floor_boundary(value: int, duration: int) -> int:
    return (value // duration) * duration


def tracking_rr(direction: str, entry: float, stop: float, target: float) -> float:
    risk = abs(entry - stop)
    reward = target - entry if direction == "LONG" else entry - target
    if risk <= 0 or reward <= 0:
        raise ValueError("Niveluri invalide pentru R:R urmarit")
    return reward / risk


def timeout_r(alert: TrackedAlert, current_price: float) -> float:
    risk = abs(alert.verified_entry - alert.stop)
    if risk <= 0:
        raise ValueError("Risc invalid pentru timeout")
    move = current_price - alert.verified_entry
    if alert.direction == "SHORT":
        move = -move
    return move / risk


def evaluate_candles(alert: TrackedAlert, candles: Iterable[CandleLike], duration_ms: int) -> Outcome | None:
    for candle in sorted(candles, key=lambda item: item.start_ms):
        if alert.direction == "LONG":
            stop_hit = candle.low <= alert.stop
            target_hit = candle.high >= alert.target
        else:
            stop_hit = candle.high >= alert.stop
            target_hit = candle.low <= alert.target
        occurred_at = candle.start_ms + duration_ms
        if stop_hit:
            return Outcome(
                status="SL_HIT", result_r=-1.0, price=alert.stop,
                occurred_at_ms=occurred_at, candle_start_ms=candle.start_ms,
                same_candle_collision=target_hit,
            )
        if target_hit:
            return Outcome(
                status="TP_HIT", result_r=alert.tracking_rr, price=alert.target,
                occurred_at_ms=occurred_at, candle_start_ms=candle.start_ms,
            )
    return None


def tracking_windows(alert: TrackedAlert) -> list[tuple[str, int, int, int]]:
    """Return non-overlapping closed-candle windows after entry and before timeout."""
    first_second = ceil_boundary(alert.opened_at_ms, SECOND_MS)
    first_minute = ceil_boundary(alert.opened_at_ms, MINUTE_MS)
    first_quarter = ceil_boundary(alert.opened_at_ms, QUARTER_MS)
    last_quarter = floor_boundary(alert.timeout_at_ms, QUARTER_MS)
    last_minute = floor_boundary(alert.timeout_at_ms, MINUTE_MS)
    last_second = floor_boundary(alert.timeout_at_ms, SECOND_MS)
    return [
        ("1s", SECOND_MS, first_second, first_minute),
        ("1m", MINUTE_MS, first_minute, first_quarter),
        ("15m", QUARTER_MS, first_quarter, last_quarter),
        ("1m", MINUTE_MS, last_quarter, last_minute),
        ("1s", SECOND_MS, last_minute, last_second),
    ]


class OutcomeStore:
    def __init__(self, path: Path):
        self.path = path
        self.open: dict[str, TrackedAlert] = {}
        self.resolved: dict[str, dict] = {}
        self.recovery_status = "NEW"
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("version") != STATE_VERSION:
                raise ValueError("Versiune incompatibila pentru starea alertelor")
            self.open = {
                str(key): TrackedAlert(**value)
                for key, value in data.get("open", {}).items()
            }
            self.resolved = {
                str(key): dict(value)
                for key, value in data.get("resolved", {}).items()
            }
            self.recovery_status = str(data.get("recovery_status", "RESTORED"))

    def contains(self, alert_id: str) -> bool:
        return alert_id in self.open or alert_id in self.resolved

    def add(self, alert: TrackedAlert) -> None:
        if self.contains(alert.alert_id):
            return
        self.open[alert.alert_id] = alert
        self.save()

    def advance(self, alert_id: str, cursor_ms: int) -> None:
        alert = self.open[alert_id]
        if cursor_ms > alert.cursor_ms:
            alert.cursor_ms = cursor_ms
            self.save()

    def resolve(self, alert_id: str, outcome: Outcome, telegram_message_id: int | None) -> None:
        alert = self.open.pop(alert_id)
        self.resolved[alert_id] = {
            "symbol": alert.symbol,
            "direction": alert.direction,
            "status": outcome.status,
            "result_r": outcome.result_r,
            "price": outcome.price,
            "occurred_at_ms": outcome.occurred_at_ms,
            "candle_start_ms": outcome.candle_start_ms,
            "same_candle_collision": outcome.same_candle_collision,
            "telegram_message_id": telegram_message_id,
        }
        self.save()

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            value = {
                "version": STATE_VERSION,
                "recovery_status": self.recovery_status,
                "open": {key: asdict(item) for key, item in self.open.items()},
                "resolved": self.resolved,
            }
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError as exc:
            raise OutcomeStateError(f"Nu pot salva starea alertelor: {type(exc).__name__}") from None


def new_alert(
    *, alert_id: str, symbol: str, direction: str, theoretical_entry: float,
    verified_entry: float, stop: float, target: float, strategy_rr: float,
    opened_at_ms: int, valid_until_ms: int, timeout_hours: int,
    telegram_message_id: int | None,
) -> TrackedAlert:
    first_cursor = ceil_boundary(opened_at_ms, SECOND_MS)
    return TrackedAlert(
        alert_id=alert_id, symbol=symbol, direction=direction,
        theoretical_entry=theoretical_entry, verified_entry=verified_entry,
        stop=stop, target=target,
        tracking_rr=tracking_rr(direction, verified_entry, stop, target),
        strategy_rr=strategy_rr, opened_at_ms=opened_at_ms,
        valid_until_ms=valid_until_ms,
        timeout_at_ms=opened_at_ms + timeout_hours * 60 * 60_000,
        cursor_ms=first_cursor, telegram_message_id=telegram_message_id,
    )


def _request_json(url: str, token: str) -> dict:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ai-crypto-trader-state-restore/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        value = json.loads(response.read().decode("utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("Raspuns GitHub invalid")
    return value


def restore_latest_artifact(path: Path, repository: str, branch: str, token: str) -> str:
    if path.is_file():
        return "CACHE"
    query = urllib.parse.urlencode({"name": ARTIFACT_NAME, "per_page": "100"})
    api = f"https://api.github.com/repos/{repository}/actions/artifacts?{query}"
    payload = _request_json(api, token)
    candidates = [
        item for item in payload.get("artifacts", [])
        if not item.get("expired")
        and item.get("name") == ARTIFACT_NAME
        and item.get("workflow_run", {}).get("head_branch") == branch
    ]
    candidates.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
    if not candidates:
        store = OutcomeStore(path)
        store.recovery_status = "EMPTY_BOOTSTRAP"
        store.save()
        return "EMPTY_BOOTSTRAP"
    artifact = candidates[0]
    request = urllib.request.Request(
        str(artifact["archive_download_url"]),
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ai-crypto-trader-state-restore/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        archive = response.read(5_000_001)
    if len(archive) > 5_000_000:
        raise RuntimeError("Artifactul de stare este prea mare")
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        matches = [name for name in bundle.namelist() if Path(name).name == path.name]
        if len(matches) != 1:
            raise RuntimeError("Artifactul nu contine o stare unica")
        data = json.loads(bundle.read(matches[0]).decode("utf-8"))
    if data.get("version") != STATE_VERSION:
        raise RuntimeError("Artifactul are versiune incompatibila")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return "ARTIFACT"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("restore-artifact",))
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--branch", required=True)
    args = parser.parse_args(argv)
    token = os.getenv("GITHUB_TOKEN", "")
    if not token:
        raise SystemExit("GITHUB_TOKEN lipsa pentru recuperarea starii")
    try:
        status = restore_latest_artifact(args.state, args.repository, args.branch, token)
    except (OSError, ValueError, RuntimeError, urllib.error.URLError, zipfile.BadZipFile) as exc:
        raise SystemExit(f"Recuperarea starii a esuat: {type(exc).__name__}") from None
    print(json.dumps({"status": status, "state": str(args.state)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

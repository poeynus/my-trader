from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Optional
from zoneinfo import ZoneInfo


DEFAULT_LOG_DIR = Path("logs/trades")


def market_day(event: Dict[str, Any]) -> str:
    market = str(event.get("market") or "kr")
    zone = ZoneInfo("Asia/Seoul") if market == "kr" else ZoneInfo("America/New_York")
    try:
        timestamp = datetime.fromisoformat(str(event["time"]))
    except (KeyError, ValueError):
        timestamp = datetime.now(zone)
    return timestamp.astimezone(zone).date().isoformat()


def daily_path(event: Dict[str, Any], log_dir: Path = DEFAULT_LOG_DIR) -> Path:
    market = str(event.get("market") or "unknown")
    return log_dir / f"{market_day(event)}-{market}.jsonl"


def append_event(event: Dict[str, Any], *, log_dir: Path = DEFAULT_LOG_DIR,
                 explicit_path: Optional[Path] = None) -> Path:
    path = explicit_path or daily_path(event, log_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    return path


def read_event_lines(market: str, day: str, *, legacy_path: Path = Path("trades.jsonl"),
                     log_dir: Path = DEFAULT_LOG_DIR) -> Iterable[str]:
    paths = [legacy_path, log_dir / f"{day}-{market}.jsonl"]
    for path in paths:
        try:
            yield from path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            continue

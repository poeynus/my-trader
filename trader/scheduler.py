from __future__ import annotations

import json
import time
from datetime import datetime, time as clock_time
from pathlib import Path
from typing import Any, Dict
from zoneinfo import ZoneInfo

from .client import KISClient, SafetyError
from .report import DailyReporter
from .strategy import StrategyConfig
from .universe import UniverseSelector


SCHEDULES = {
    "kr": (ZoneInfo("Asia/Seoul"), clock_time(15, 40)),
    "us": (ZoneInfo("America/New_York"), clock_time(16, 10)),
}


class EndOfDayScheduler:
    def __init__(self, client: KISClient, config: StrategyConfig, state_path: Path = Path(".scheduler-state.json")):
        self.client, self.config, self.state_path = client, config, state_path
        try:
            self.state: Dict[str, Any] = json.loads(state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            self.state = {}

    def run_forever(self, poll_seconds: int = 30) -> None:
        if poll_seconds < 10:
            raise ValueError("스케줄러 확인 주기는 최소 10초입니다.")
        while True:
            if Path("STOP_TRADING").exists():
                raise SafetyError("STOP_TRADING 파일이 있어 스케줄러를 중지했습니다.")
            for market in ("kr", "us"):
                self.run_if_due(market)
            time.sleep(poll_seconds)

    def run_if_due(self, market: str) -> bool:
        timezone, close_time = SCHEDULES[market]
        now = datetime.now(timezone)
        day = now.date().isoformat()
        if now.weekday() >= 5 or now.time() < close_time or self.state.get(market) == day:
            return False
        selector = UniverseSelector(self.client, self.config)
        selected = selector.discover({market})
        merged = selector.merge_and_save(selected, {market})
        report = DailyReporter(self.client, self.config).generate(market, merged)
        self.state[market] = day
        self.state_path.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"market": market, "date": day, "report": str(report), "selected": len(selected)}, ensure_ascii=False), flush=True)
        return True

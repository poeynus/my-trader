from __future__ import annotations

import asyncio
import fcntl
import json
from dataclasses import replace
from datetime import datetime, time as clock_time
from pathlib import Path
from typing import Any, Dict
from zoneinfo import ZoneInfo

from .auto import AutoTrader
from .client import KISClient, SafetyError
from .execution import ExecutionManager
from .scheduler import EndOfDayScheduler
from .strategy import StrategyConfig
from .universe import UniverseSelector


ENTRY_SCHEDULES = {
    "kr": (ZoneInfo("Asia/Seoul"), clock_time(9, 0), clock_time(15, 20)),
    "us": (ZoneInfo("America/New_York"), clock_time(9, 35), clock_time(15, 50)),
}


class Autopilot:
    def __init__(self, client: KISClient, config: StrategyConfig, confirm_live: bool = False,
                 state_path: Path = Path(".autopilot-state.json"), lock_path: Path = Path(".autopilot.lock")):
        self.client, self.config, self.confirm_live = client, config, confirm_live
        self.state_path, self.lock_path = state_path, lock_path
        try:
            self.state: Dict[str, Any] = json.loads(state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            self.state = {"last_entry": {}}
        self.execution = ExecutionManager(client, config)
        self.eod = EndOfDayScheduler(client, config)
        self._lock_file = None

    def _acquire_lock(self) -> None:
        self._lock_file = self.lock_path.open("w")
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SafetyError("autopilot이 이미 실행 중입니다.") from exc
        self._lock_file.write(str(__import__("os").getpid()))
        self._lock_file.flush()

    async def run_forever(self, poll_seconds: int = 10) -> None:
        self._acquire_lock()
        if self.config.execution_mode == "live" and not self.confirm_live:
            raise SafetyError("live autopilot에는 --confirm-live가 필요합니다.")
        while True:
            if Path("STOP_TRADING").exists():
                raise SafetyError("STOP_TRADING 파일이 있어 autopilot을 중지했습니다.")
            for market in ("kr", "us"):
                await asyncio.to_thread(self.eod.run_if_due, market)
                if self._entry_due(market):
                    await self._run_entry(market)
            await asyncio.sleep(poll_seconds)

    def _entry_due(self, market: str) -> bool:
        zone, start, end = ENTRY_SCHEDULES[market]
        now = datetime.now(zone)
        return now.weekday() < 5 and start <= now.time() < end

    async def _run_entry(self, market: str) -> None:
        try:
            symbols = UniverseSelector.load(config=self.config)
        except (FileNotFoundError, ValueError, json.JSONDecodeError):
            symbols = await asyncio.to_thread(UniverseSelector(self.client, self.config).discover, {market})
            UniverseSelector.merge_and_save(symbols, {market})
        market_symbols = [x for x in symbols if x.market == market]
        runtime_config = replace(self.config, symbols=market_symbols)
        trader = AutoTrader(self.client, runtime_config, execution=self.execution)
        results = await asyncio.to_thread(trader.run_once, self.confirm_live)
        print(json.dumps({"event": "trading_cycle", "market": market, "results": results}, ensure_ascii=False), flush=True)

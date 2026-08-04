from __future__ import annotations

import asyncio
import fcntl
import json
from dataclasses import replace
from datetime import datetime, time as clock_time
from pathlib import Path
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from .auto import AutoTrader
from .client import KISClient, SafetyError
from .execution import ExecutionManager
from .scheduler import EndOfDayScheduler
from .strategy import StrategyConfig, SymbolConfig
from .universe import UniverseSelector
from .trade_log import append_event


MARKET_SCHEDULES = {
    # timezone, start, new-entry cutoff, forced liquidation, cycle end
    "kr": (ZoneInfo("Asia/Seoul"), clock_time(9, 0), clock_time(15, 0), clock_time(15, 15), clock_time(15, 29)),
    "us": (ZoneInfo("America/New_York"), clock_time(9, 35), clock_time(15, 35), clock_time(15, 45), clock_time(15, 59)),
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
        if self.config.has_live_market and not self.confirm_live:
            raise SafetyError("live autopilot에는 --confirm-live가 필요합니다.")
        print(json.dumps({"event": "autopilot_started", "modes": {m: self.config.mode_for(m) for m in ("kr", "us")},
                          "poll_seconds": poll_seconds, "time": datetime.now().astimezone().isoformat()},
                         ensure_ascii=False), flush=True)
        last_heartbeat: Optional[datetime] = None
        while True:
            if Path("STOP_TRADING").exists():
                raise SafetyError("STOP_TRADING 파일이 있어 autopilot을 중지했습니다.")
            for market in ("kr", "us"):
                try:
                    await asyncio.to_thread(self.eod.run_if_due, market)
                except Exception as exc:
                    self._cycle_error(market, "eod", exc)
                if self._cycle_due(market):
                    if self._refresh_due(market):
                        try:
                            await asyncio.to_thread(self._refresh_universe, market)
                        except Exception as exc:
                            self._cycle_error(market, "universe_refresh", exc)
                    allow_entries, force_exit = self._cycle_flags(market)
                    try:
                        await self._run_entry(market, allow_entries, force_exit)
                    except Exception as exc:
                        self._cycle_error(market, "trading_cycle", exc)
            now = datetime.now().astimezone()
            if last_heartbeat is None or (now - last_heartbeat).total_seconds() >= 300:
                print(json.dumps({"event": "heartbeat", "modes": {m: self.config.mode_for(m) for m in ("kr", "us")},
                                  "time": now.isoformat(), "kr_open": self._cycle_due("kr"),
                                  "us_open": self._cycle_due("us")}, ensure_ascii=False), flush=True)
                last_heartbeat = now
            await asyncio.sleep(poll_seconds)

    def _cycle_error(self, market: str, stage: str, exc: Exception) -> None:
        now = datetime.now(ZoneInfo("Asia/Seoul"))
        event = {"time": now.isoformat(), "market": market, "action": "error",
                 "reason": f"{stage}: {exc}", "stage": stage}
        print(json.dumps({"event": "cycle_error", **event}, ensure_ascii=False), flush=True)
        append_event(event)

    def _cycle_due(self, market: str) -> bool:
        zone, start, _, _, end = MARKET_SCHEDULES[market]
        now = datetime.now(zone)
        return now.weekday() < 5 and start <= now.time() < end

    def _cycle_flags(self, market: str) -> tuple[bool, bool]:
        zone, _, entry_cutoff, liquidation, _ = MARKET_SCHEDULES[market]
        current = datetime.now(zone).time()
        return current < entry_cutoff, current >= liquidation

    def _refresh_due(self, market: str) -> bool:
        zone = MARKET_SCHEDULES[market][0]
        now = datetime.now(zone)
        stamp = self.state.setdefault("last_refresh", {}).get(market)
        if not stamp:
            return True
        return (now - datetime.fromisoformat(stamp)).total_seconds() >= self.config.intraday_refresh_minutes * 60

    def _refresh_universe(self, market: str) -> None:
        selector = UniverseSelector(self.client, self.config)
        try:
            current = UniverseSelector.load(config=self.config)
        except (FileNotFoundError, ValueError, json.JSONDecodeError):
            current = []
        held_items: Dict[str, SymbolConfig] = {}
        if self.config.mode_for(market) == "dry_run":
            try:
                state = json.loads(Path(".trader-state.json").read_text(encoding="utf-8"))
                for key in state.get("virtual_positions", {}):
                    if not key.startswith(market + ":"):
                        continue
                    symbol = key.split(":", 1)[1].upper()
                    existing = next((x for x in current if x.market == market and x.symbol == symbol), None)
                    held_items[symbol] = existing or SymbolConfig(
                        market, symbol, "NASDAQ",
                        self.config.default_position_krw if market == "kr" else self.config.default_position_usd)
            except (FileNotFoundError, json.JSONDecodeError):
                pass
        else:
            rows = self.client.domestic_balance()["positions"] if market == "kr" else self.client.us_balance()["positions"]
            field = "pdno" if market == "kr" else "ovrs_pdno"
            qty_field = "hldg_qty" if market == "kr" else "ovrs_cblc_qty"
            exchange_map = {"NASD": "NASDAQ", "NYSE": "NYSE", "AMEX": "AMEX"}
            for row in rows:
                if float(row.get(qty_field) or 0) <= 0:
                    continue
                symbol = str(row.get(field, "")).upper()
                exchange = ("NASDAQ" if market == "kr"
                            else exchange_map.get(str(row.get("ovrs_excg_cd") or "").upper(), "NASDAQ"))
                held_items[symbol] = SymbolConfig(
                    market, symbol, exchange,
                    self.config.default_position_krw if market == "kr" else self.config.default_position_usd)
        held = set(held_items)
        exhausted = self._exhausted_symbols(market) - held
        fresh = selector.momentum_candidates(market, excluded_symbols=exhausted)
        # 장 종료 재선정에서 빠진 종목이라도 실제 잔고에 있으면 반드시 감시·청산한다.
        unique = dict(held_items)
        for item in fresh:
            if len(unique) >= self.config.max_monitored_per_market:
                break
            unique.setdefault(item.symbol, item)
        merged = [x for x in current if x.market != market] + list(unique.values())
        UniverseSelector.save(merged)
        now = datetime.now(MARKET_SCHEDULES[market][0])
        self.state.setdefault("last_refresh", {})[market] = now.isoformat()
        self.state_path.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"event": "universe_refreshed", "market": market, "symbols": list(unique),
                          "excluded_round_trip_limit": sorted(exhausted)}, ensure_ascii=False), flush=True)

    def _exhausted_symbols(self, market: str) -> set[str]:
        """오늘 종목별 왕복 한도를 소진해 신규 후보에서 제외할 종목을 반환한다."""
        try:
            trader_state = json.loads(Path(".trader-state.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return set()
        market_day = datetime.now(MARKET_SCHEDULES[market][0]).date().isoformat()
        intraday = trader_state.get("intraday", {}).get(f"{market}:{market_day}", {})
        return {str(symbol).upper() for symbol, count in intraday.get("round_trips", {}).items()
                if int(count) >= self.config.max_round_trips_per_symbol}

    async def _run_entry(self, market: str, allow_new_entries: bool, force_exit: bool) -> None:
        try:
            symbols = UniverseSelector.load(config=self.config)
        except (FileNotFoundError, ValueError, json.JSONDecodeError):
            symbols = await asyncio.to_thread(UniverseSelector(self.client, self.config).discover, {market})
            UniverseSelector.merge_and_save(symbols, {market})
        market_symbols = [x for x in symbols if x.market == market]
        runtime_config = replace(self.config, symbols=market_symbols)
        trader = AutoTrader(self.client, runtime_config, execution=self.execution)
        results = await asyncio.to_thread(trader.run_once, self.confirm_live, allow_new_entries, force_exit)
        print(json.dumps({"event": "trading_cycle", "market": market, "allow_new_entries": allow_new_entries,
                          "force_exit": force_exit, "results": results}, ensure_ascii=False), flush=True)

from __future__ import annotations

import json
import math
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from .client import KISClient, SafetyError
from .strategy import StrategyConfig, SymbolConfig, moving_average_signal
from .screener import HardScreener
from .execution import ExecutionManager


STATE_LOCK = threading.RLock()


class AutoTrader:
    def __init__(self, client: KISClient, config: StrategyConfig, state_path: Path = Path(".trader-state.json"), log_path: Path = Path("trades.jsonl"), execution: Optional[ExecutionManager] = None):
        self.client, self.config = client, config
        self.state_path, self.log_path = state_path, log_path
        self.state = self._load_state()
        self.execution = execution

    def _load_state(self) -> Dict[str, Any]:
        with STATE_LOCK:
            try:
                state = json.loads(self.state_path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                state = {}
        state.setdefault("virtual_positions", {})
        state.setdefault("last_actions", {})
        state.setdefault("intraday", {})
        return state

    def _intraday(self, market: str) -> Dict[str, Any]:
        zone = ZoneInfo("Asia/Seoul") if market == "kr" else ZoneInfo("America/New_York")
        key = market + ":" + datetime.now(zone).date().isoformat()
        return self.state["intraday"].setdefault(key, {"realized_pnl": 0.0, "round_trips": {}, "last_exit": {}})

    def record_exit(self, item: SymbolConfig, average_price: float, quantity: int, exit_price: float) -> None:
        with STATE_LOCK:
            self.state = self._load_state()
            self._record_exit(item, average_price, quantity, exit_price)

    def _record_exit(self, item: SymbolConfig, average_price: float, quantity: int, exit_price: float) -> None:
        intraday = self._intraday(item.market)
        intraday["realized_pnl"] = float(intraday["realized_pnl"]) + (exit_price - average_price) * quantity
        intraday["round_trips"][item.symbol] = int(intraday["round_trips"].get(item.symbol, 0)) + 1
        intraday["last_exit"][item.symbol] = datetime.now(timezone.utc).isoformat()
        self._save_state()

    def _save_state(self) -> None:
        self.state_path.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")

    def _log(self, event: Dict[str, Any]) -> None:
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"time": datetime.now(timezone.utc).isoformat(), **event}, ensure_ascii=False) + "\n")

    def run_once(self, confirm_live: bool = False) -> List[Dict[str, Any]]:
        with STATE_LOCK:
            return self._run_once_locked(confirm_live)

    def _run_once_locked(self, confirm_live: bool = False) -> List[Dict[str, Any]]:
        if Path("STOP_TRADING").exists():
            raise SafetyError("STOP_TRADING 파일이 있어 자동매매를 중지했습니다.")
        if self.config.execution_mode == "live" and not confirm_live:
            raise SafetyError("live 자동매매 실행에는 --confirm-live가 필요합니다.")
        positions = self._live_positions() if self.config.execution_mode == "live" else self.state["virtual_positions"]
        results = []
        for item in self.config.symbols:
            try:
                result = self._evaluate(item, positions, confirm_live)
            except Exception as exc:
                result = {"market": item.market, "symbol": item.symbol, "action": "error", "reason": str(exc)}
            self._log(result)
            results.append(result)
        self._save_state()
        return results

    def loop(self, interval_seconds: int, confirm_live: bool = False) -> None:
        if interval_seconds < 60:
            raise ValueError("API 호출 제한 보호를 위해 interval은 최소 60초입니다.")
        while True:
            print(json.dumps(self.run_once(confirm_live), ensure_ascii=False, indent=2), flush=True)
            time.sleep(interval_seconds)

    def _live_positions(self) -> Dict[str, Dict[str, float]]:
        positions: Dict[str, Dict[str, float]] = {}
        for row in self.client.domestic_balance()["positions"]:
            quantity = float(row.get("hldg_qty") or 0)
            if quantity > 0:
                positions["kr:" + str(row.get("pdno"))] = {"quantity": quantity, "average_price": float(row.get("pchs_avg_pric") or 0)}
        for row in self.client.us_balance()["positions"]:
            quantity = float(row.get("ovrs_cblc_qty") or 0)
            if quantity > 0:
                positions["us:" + str(row.get("ovrs_pdno")).upper()] = {"quantity": quantity, "average_price": float(row.get("pchs_avg_pric") or 0)}
        return positions

    def _evaluate(self, item: SymbolConfig, positions: Dict[str, Any], confirm_live: bool) -> Dict[str, Any]:
        key = item.market + ":" + item.symbol
        if item.market == "kr":
            current = float(self.client.domestic_quote(item.symbol)["stck_prpr"])
            rows = self.client.domestic_daily_prices(item.symbol, self.config.slow_period + 2)
            closes = [float(x["stck_clpr"]) for x in rows if x.get("stck_clpr")]
        else:
            current = float(self.client.us_quote(item.symbol, item.exchange)["last"])
            rows = self.client.us_daily_prices(item.symbol, item.exchange, self.config.slow_period + 2)
            closes = [float(x["clos"]) for x in rows if x.get("clos")]
        ma = moving_average_signal(closes, self.config.fast_period, self.config.slow_period)
        position = positions.get(key)
        action, reason, profit_percent = "hold", str(ma["signal"]), None
        if position:
            average = float(position["average_price"])
            profit_percent = (current / average - 1) * 100 if average else 0
            if profit_percent >= self.config.take_profit_percent:
                action, reason = "sell", "take_profit"
            elif profit_percent <= -self.config.stop_loss_percent:
                action, reason = "sell", "stop_loss"
            elif ma["signal"] == "cross_down":
                action, reason = "sell", "cross_down"
        elif float(ma["fast"]) > float(ma["slow"]):
            # 장기 추세가 상승인 동안에는 청산 후 대기시간을 거쳐 재진입할 수 있다.
            action, reason = "buy", "trend_up"
        base = {"market": item.market, "symbol": item.symbol, "price": current, "profit_percent": profit_percent,
                "fast_ma": round(float(ma["fast"]), 4), "slow_ma": round(float(ma["slow"]), 4),
                "action": action, "reason": reason, "mode": self.config.execution_mode}
        if action == "hold":
            return base
        if action == "buy":
            intraday = self._intraday(item.market)
            loss_limit = self.config.max_daily_loss_krw if item.market == "kr" else self.config.max_daily_loss_usd
            if float(intraday["realized_pnl"]) <= -loss_limit:
                return {**base, "action": "hold", "reason": "daily_loss_limit"}
            if int(intraday["round_trips"].get(item.symbol, 0)) >= self.config.max_round_trips_per_symbol:
                return {**base, "action": "hold", "reason": "round_trip_limit"}
            last_exit = intraday["last_exit"].get(item.symbol)
            if last_exit:
                elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last_exit)).total_seconds()
                if elapsed < self.config.reentry_cooldown_seconds:
                    return {**base, "action": "hold", "reason": "reentry_cooldown"}
            screen = HardScreener(self.client, self.config).screen(item)
            if not screen.approved:
                return {**base, "action": "hold", "reason": "screen_rejected:" + ",".join(screen.reasons)}
        if action == "buy":
            active_limit = self.config.max_active_investment_krw if item.market == "kr" else self.config.max_active_investment_usd
            active_exposure = sum(float(x["quantity"]) * float(x["average_price"])
                                  for k, x in positions.items() if k.startswith(item.market + ":"))
            quantity = math.floor(min(item.max_position, max(0, active_limit - active_exposure)) / current)
        else:
            quantity = int(float(position["quantity"]))
        if quantity < 1:
            return {**base, "action": "hold", "reason": "quantity_is_zero"}
        buffer = self.config.limit_buffer_percent / 100
        limit_price = current * (1 + buffer if action == "buy" else 1 - buffer)
        limit_price = round(limit_price) if item.market == "kr" else round(limit_price, 2)
        if self.config.execution_mode == "dry_run":
            if action == "buy":
                positions[key] = {"quantity": quantity, "average_price": limit_price}
            else:
                positions.pop(key, None)
            order_result: Dict[str, Any] = {"simulated": True}
        elif self.execution and action == "buy":
            order_result = self.execution.execute_buy(item, quantity, limit_price, confirm_live)
        elif self.execution and action == "sell":
            order_result = self.execution.execute_sell(item, quantity, limit_price, confirm_live)
        elif item.market == "kr":
            order_result = self.client.domestic_order(action, item.symbol, quantity, limit_price, confirm_live)
        else:
            order_result = self.client.us_order(action, item.symbol, item.exchange, quantity, limit_price, confirm_live)
        filled = quantity if self.config.execution_mode == "dry_run" else int(order_result.get("filled_quantity", 0))
        if action == "sell" and filled > 0:
            exit_price = limit_price if self.config.execution_mode == "dry_run" else float(order_result.get("average_price") or limit_price)
            self._record_exit(item, float(position["average_price"]), filled, exit_price)
        self.state["last_actions"][key] = [action, reason, datetime.now(timezone.utc).isoformat()]
        self._save_state()
        return {**base, "quantity": quantity, "limit_price": limit_price, "order": order_result}

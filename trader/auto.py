from __future__ import annotations

import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .client import KISClient, SafetyError
from .strategy import StrategyConfig, SymbolConfig, moving_average_signal
from .screener import HardScreener
from .execution import ExecutionManager


class AutoTrader:
    def __init__(self, client: KISClient, config: StrategyConfig, state_path: Path = Path(".trader-state.json"), log_path: Path = Path("trades.jsonl"), execution: Optional[ExecutionManager] = None):
        self.client, self.config = client, config
        self.state_path, self.log_path = state_path, log_path
        self.state = self._load_state()
        self.execution = execution

    def _load_state(self) -> Dict[str, Any]:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {"virtual_positions": {}, "last_actions": {}}

    def _save_state(self) -> None:
        self.state_path.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")

    def _log(self, event: Dict[str, Any]) -> None:
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"time": datetime.now(timezone.utc).isoformat(), **event}, ensure_ascii=False) + "\n")

    def run_once(self, confirm_live: bool = False) -> List[Dict[str, Any]]:
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
        elif ma["signal"] == "cross_up":
            action, reason = "buy", "cross_up"
        base = {"market": item.market, "symbol": item.symbol, "price": current, "profit_percent": profit_percent,
                "fast_ma": round(float(ma["fast"]), 4), "slow_ma": round(float(ma["slow"]), 4),
                "action": action, "reason": reason, "mode": self.config.execution_mode}
        if action == "hold":
            return base
        if action == "buy":
            screen = HardScreener(self.client, self.config).screen(item)
            if not screen.approved:
                return {**base, "action": "hold", "reason": "screen_rejected:" + ",".join(screen.reasons)}
        action_day = datetime.now(timezone.utc).date().isoformat()
        action_key = [action, reason, action_day]
        if self.state["last_actions"].get(key) == action_key:
            return {**base, "action": "hold", "reason": "duplicate_blocked"}
        quantity = math.floor(item.max_position / current) if action == "buy" else int(float(position["quantity"]))
        if quantity < 1:
            return {**base, "action": "hold", "reason": "quantity_is_zero"}
        buffer = self.config.limit_buffer_percent / 100
        limit_price = current * (1 + buffer if action == "buy" else 1 - buffer)
        limit_price = round(limit_price) if item.market == "kr" else round(limit_price, 2)
        # API 응답 유실 시 같은 주문이 반복되는 것을 막기 위해 전송 전에 기록한다.
        self.state["last_actions"][key] = action_key
        self._save_state()
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
        return {**base, "quantity": quantity, "limit_price": limit_price, "order": order_result}

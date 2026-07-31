from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .auto import AutoTrader
from .client import KISClient, SafetyError, US_QUOTE_EXCHANGES
from .strategy import StrategyConfig, SymbolConfig
from .execution import ExecutionManager


DOMESTIC_COLUMNS = [
    "symbol", "time", "last", "sign", "diff", "rate", "weighted", "open", "high", "low",
    "ask", "bid", "volume", "total_volume", "turnover",
] + [f"extra_{i}" for i in range(31)]
US_COLUMNS = ["symbol", "decimal", "local_date", "kr_date", "local_time", "kr_trade_date", "kr_time",
              "open", "high", "low", "last", "sign", "diff", "rate", "bid", "ask", "bid_size",
              "ask_size", "volume", "total_volume", "turnover", "bid_flag", "ask_flag", "session", "market_type"]


class RealtimeExitMonitor:
    def __init__(self, client: KISClient, config: StrategyConfig, log_path: Path = Path("trades.jsonl"), execution: Optional[ExecutionManager] = None):
        self.client, self.config, self.log_path = client, config, log_path
        self.submitted: set[str] = set()
        self.execution = execution

    def _positions(self) -> Dict[str, Dict[str, float]]:
        return AutoTrader(self.client, self.config)._live_positions()

    @staticmethod
    def _subscription(item: SymbolConfig, approval_key: str) -> Dict[str, Any]:
        if item.market == "kr":
            tr_id, tr_key = "H0STCNT0", item.symbol
        else:
            tr_id = "HDFSCNT0"
            tr_key = "D" + US_QUOTE_EXCHANGES[item.exchange] + item.symbol
        return {"header": {"approval_key": approval_key, "custtype": "P", "tr_type": "1", "content-type": "utf-8"},
                "body": {"input": {"tr_id": tr_id, "tr_key": tr_key}}}

    @staticmethod
    def parse_message(raw: str) -> List[Dict[str, Any]]:
        if not raw or raw[0] not in {"0", "1"}:
            return []
        parts = raw.split("|", 3)
        if len(parts) != 4:
            return []
        tr_id, count, values = parts[1], int(parts[2]), parts[3].split("^")
        columns = DOMESTIC_COLUMNS if tr_id == "H0STCNT0" else US_COLUMNS if tr_id == "HDFSCNT0" else []
        if not columns:
            return []
        width = len(columns)
        return [dict(zip(columns, values[i * width:(i + 1) * width])) for i in range(min(count, len(values) // width))]

    async def run(self, confirm_live: bool = False) -> None:
        if self.config.execution_mode != "live" or not confirm_live:
            raise SafetyError("실시간 자동청산은 live 모드와 --confirm-live가 모두 필요합니다.")
        if Path("STOP_TRADING").exists():
            raise SafetyError("STOP_TRADING 파일이 있어 자동매매를 중지했습니다.")
        try:
            import websockets
        except ImportError as exc:
            raise RuntimeError("실시간 감시에 websockets 패키지가 필요합니다: python3 -m pip install -r requirements.txt") from exc
        positions = self._positions()
        configured = {x.market + ":" + x.symbol: x for x in self.config.symbols}
        watched = [configured[key] for key in positions if key in configured]
        if not watched:
            raise ValueError("strategy.json에 등록된 보유 종목이 없습니다.")
        approval = self.client.websocket_approval_key()
        async with websockets.connect("ws://ops.koreainvestment.com:21000/tryitout", ping_interval=30) as ws:
            for item in watched:
                await ws.send(json.dumps(self._subscription(item, approval)))
                await asyncio.sleep(0.2)
            async for raw in ws:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                if raw.startswith("{"):
                    message = json.loads(raw)
                    if message.get("header", {}).get("tr_id") == "PINGPONG":
                        await ws.send(raw)
                    continue
                for tick in self.parse_message(raw):
                    await self._handle_tick(tick, configured, positions, confirm_live)

    async def _handle_tick(self, tick: Dict[str, Any], configured: Dict[str, SymbolConfig], positions: Dict[str, Dict[str, float]], confirm_live: bool) -> None:
        symbol = str(tick["symbol"]).upper()
        for prefix in ("DNAS", "DNYS", "DAMS"):
            if symbol.startswith(prefix):
                symbol = symbol[len(prefix):]
                break
        market = "kr" if symbol.isdigit() else "us"
        key = market + ":" + symbol
        if key not in configured or key not in positions or key in self.submitted:
            return
        executable = float(tick.get("bid") or tick.get("last") or 0)
        position, item = positions[key], configured[key]
        average = position["average_price"]
        profit = (executable / average - 1) * 100 if average else 0
        reason = "take_profit" if profit >= self.config.take_profit_percent else (
            "stop_loss" if profit <= -self.config.stop_loss_percent else "")
        if not reason:
            return
        self.submitted.add(key)
        quantity = int(position["quantity"])
        buffer = self.config.limit_buffer_percent / 100
        limit_price = round(executable * (1 - buffer)) if market == "kr" else round(executable * (1 - buffer), 2)
        try:
            if self.execution:
                order = await asyncio.to_thread(self.execution.execute_sell, item, quantity, limit_price, confirm_live)
            elif market == "kr":
                order = await asyncio.to_thread(self.client.domestic_order, "sell", symbol, quantity, limit_price, confirm_live)
            else:
                order = await asyncio.to_thread(self.client.us_order, "sell", symbol, item.exchange, quantity, limit_price, confirm_live)
            event = {"time": datetime.now(timezone.utc).isoformat(), "market": market, "symbol": symbol,
                     "action": "sell", "reason": reason, "profit_percent": profit, "price": executable, "order": order}
            filled = int(order.get("filled_quantity", 0)) if self.execution else quantity
            if filled > 0:
                exit_price = float(order.get("average_price") or limit_price) if self.execution else limit_price
                AutoTrader(self.client, self.config).record_exit(item, float(average), filled, exit_price)
        except Exception as exc:
            event = {"time": datetime.now(timezone.utc).isoformat(), "market": market, "symbol": symbol,
                     "action": "order_error", "reason": str(exc), "profit_percent": profit}
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

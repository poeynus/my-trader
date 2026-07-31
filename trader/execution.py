from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from .client import KISClient, SafetyError
from .strategy import StrategyConfig, SymbolConfig


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0


class ExecutionManager:
    def __init__(self, client: KISClient, config: StrategyConfig, state_path: Path = Path(".execution-state.json")):
        self.client, self.config, self.state_path = client, config, state_path
        try:
            self.state = json.loads(state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            self.state = {"daily_spend": {}}

    def _day(self, market: str) -> str:
        zone = ZoneInfo("Asia/Seoul") if market == "kr" else ZoneInfo("America/New_York")
        return datetime.now(zone).date().isoformat()

    def _spent(self, market: str) -> float:
        return _num(self.state["daily_spend"].get(market + ":" + self._day(market)))

    def _record_spend(self, market: str, amount: float) -> None:
        key = market + ":" + self._day(market)
        self.state["daily_spend"][key] = self._spent(market) + amount
        self.state_path.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")

    def execute_buy(self, item: SymbolConfig, quantity: int, price: float, confirmed: bool) -> Dict[str, Any]:
        market = item.market
        limit = self.config.max_daily_investment_krw if market == "kr" else self.config.max_daily_investment_usd
        remaining_budget = limit - self._spent(market)
        quantity = min(quantity, int(remaining_budget // price))
        if quantity < 1:
            raise SafetyError("일일 신규투자 한도를 모두 사용했거나 1주 매수금액보다 적습니다.")
        power = self.client.domestic_buying_power(item.symbol, price) if market == "kr" else self.client.us_buying_power(item.symbol, item.exchange, price)
        if market == "kr":
            available = int(_num(power.get("nrcvb_buy_qty")))
        else:
            available = int(max(_num(power.get(k)) for k in ("ovrs_max_ord_psbl_qty", "max_ord_psbl_qty", "ord_psbl_qty", "echm_af_ord_psbl_qty")))
        quantity = min(quantity, available)
        if quantity < 1:
            raise SafetyError("계좌의 주문 가능 수량이 0주입니다. 예수금과 통합증거금을 확인하세요.")
        result = self._submit_and_track(item, "buy", quantity, price, confirmed)
        if result["filled_quantity"] > 0:
            self._record_spend(market, result["filled_quantity"] * result["average_price"])
        return result

    def execute_sell(self, item: SymbolConfig, quantity: int, price: float, confirmed: bool) -> Dict[str, Any]:
        return self._submit_and_track(item, "sell", quantity, price, confirmed)

    def _submit_and_track(self, item: SymbolConfig, side: str, quantity: int, price: float, confirmed: bool) -> Dict[str, Any]:
        attempts, filled_total, last_order = [], 0, None
        remaining, current_price = quantity, price
        for attempt in range(self.config.max_retries + 1):
            response = (self.client.domestic_order(side, item.symbol, remaining, current_price, confirmed)
                        if item.market == "kr" else self.client.us_order(side, item.symbol, item.exchange, remaining, current_price, confirmed))
            output = response.get("output") or {}
            order_number = str(output.get("ODNO") or output.get("odno") or "")
            organization = str(output.get("KRX_FWDG_ORD_ORGNO") or output.get("ord_gno_brno") or "")
            if not order_number:
                raise SafetyError("주문 응답에 주문번호가 없어 재주문을 중단했습니다.")
            last_order = order_number
            status = self._wait(item, order_number, remaining)
            filled_total += status["filled"]
            attempts.append({"order_number": order_number, "requested": remaining, **status})
            remaining -= status["filled"]
            if remaining <= 0:
                break
            self._cancel_open(item, order_number, organization, remaining, confirmed)
            if attempt < self.config.max_retries:
                quote = self.client.domestic_quote(item.symbol) if item.market == "kr" else self.client.us_quote(item.symbol, item.exchange)
                market_price = _num(quote.get("stck_prpr") if item.market == "kr" else quote.get("last"))
                buffer = self.config.limit_buffer_percent / 100
                current_price = market_price * (1 + buffer if side == "buy" else 1 - buffer)
                current_price = round(current_price) if item.market == "kr" else round(current_price, 2)
        avg = sum(x["filled"] * x["average_price"] for x in attempts) / filled_total if filled_total else 0
        return {"order_number": last_order, "requested_quantity": quantity, "filled_quantity": filled_total,
                "remaining_quantity": quantity - filled_total, "average_price": avg, "attempts": attempts}

    def _wait(self, item: SymbolConfig, order_number: str, requested: int) -> Dict[str, Any]:
        deadline = time.monotonic() + self.config.fill_timeout_seconds
        last_filled, last_average = 0, 0.0
        while time.monotonic() < deadline:
            rows = self.client.domestic_today_orders(item.symbol) if item.market == "kr" else self.client.us_today_orders(item.symbol)
            for row in rows:
                if str(row.get("odno") or "") == order_number:
                    filled = int(_num(row.get("tot_ccld_qty") if item.market == "kr" else row.get("ft_ccld_qty")))
                    average = _num(row.get("avg_prvs") if item.market == "kr" else row.get("ft_ccld_unpr3"))
                    last_filled, last_average = filled, average
                    if filled >= requested:
                        return {"filled": filled, "average_price": average, "status": "filled"}
            time.sleep(3)
        return {"filled": last_filled, "average_price": last_average, "status": "partial" if last_filled else "timeout"}

    def _cancel_open(self, item: SymbolConfig, order_number: str, organization: str, remaining: int, confirmed: bool) -> None:
        if item.market == "kr":
            rows = self.client.domestic_open_orders()
            row = next((x for x in rows if str(x.get("odno")) == order_number), None)
            if row:
                self.client.domestic_cancel(order_number, str(row.get("ord_gno_brno") or organization), int(_num(row.get("psbl_qty") or remaining)), confirmed)
        else:
            rows = self.client.us_open_orders()
            row = next((x for x in rows if str(x.get("odno")) == order_number), None)
            if row:
                self.client.us_cancel(order_number, item.symbol, item.exchange, int(_num(row.get("nccs_qty") or remaining)), confirmed)

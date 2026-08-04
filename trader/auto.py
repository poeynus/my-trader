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
from .trade_log import append_event
from .pricing import round_domestic_order_price


STATE_LOCK = threading.RLock()
LOG_TIMEZONE = ZoneInfo("Asia/Seoul")
DAILY_CACHE: Dict[str, tuple[float, Dict[str, object]]] = {}
SCREEN_CACHE: Dict[str, tuple[float, Any]] = {}


class AutoTrader:
    def __init__(self, client: KISClient, config: StrategyConfig, state_path: Path = Path(".trader-state.json"), log_path: Optional[Path] = None, execution: Optional[ExecutionManager] = None):
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
        state.setdefault("position_meta", {})
        state.setdefault("price_samples", {})
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
        append_event({"time": datetime.now(LOG_TIMEZONE).isoformat(), **event}, explicit_path=self.log_path)

    def run_once(self, confirm_live: bool = False, allow_new_entries: bool = True,
                 force_exit: bool = False) -> List[Dict[str, Any]]:
        with STATE_LOCK:
            return self._run_once_locked(confirm_live, allow_new_entries, force_exit)

    def _run_once_locked(self, confirm_live: bool, allow_new_entries: bool, force_exit: bool) -> List[Dict[str, Any]]:
        if Path("STOP_TRADING").exists():
            raise SafetyError("STOP_TRADING 파일이 있어 자동매매를 중지했습니다.")
        live_markets = {x.market for x in self.config.symbols if self.config.mode_for(x.market) == "live"}
        if live_markets and not confirm_live:
            raise SafetyError("live 자동매매 실행에는 --confirm-live가 필요합니다.")
        live_positions = self._live_positions(live_markets) if live_markets else {}
        results = []
        for item in self.config.symbols:
            positions = live_positions if self.config.mode_for(item.market) == "live" else self.state["virtual_positions"]
            try:
                result = self._evaluate(item, positions, confirm_live, allow_new_entries, force_exit)
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

    def _live_positions(self, markets: Optional[set[str]] = None) -> Dict[str, Dict[str, float]]:
        markets = markets or {"kr", "us"}
        positions: Dict[str, Dict[str, float]] = {}
        if "kr" in markets:
            for row in self.client.domestic_balance()["positions"]:
                quantity = float(row.get("hldg_qty") or 0)
                if quantity > 0:
                    positions["kr:" + str(row.get("pdno"))] = {"quantity": quantity, "average_price": float(row.get("pchs_avg_pric") or 0)}
        if "us" in markets:
            for row in self.client.us_balance()["positions"]:
                quantity = float(row.get("ovrs_cblc_qty") or 0)
                if quantity > 0:
                    positions["us:" + str(row.get("ovrs_pdno")).upper()] = {"quantity": quantity, "average_price": float(row.get("pchs_avg_pric") or 0)}
        return positions

    def _ma(self, item: SymbolConfig) -> Dict[str, object]:
        key = item.market + ":" + item.symbol
        cached = DAILY_CACHE.get(key)
        if cached and time.monotonic() - cached[0] < 300:
            return cached[1]
        if item.market == "kr":
            rows = self.client.domestic_daily_prices(item.symbol, self.config.slow_period + 2)
            closes = [float(x["stck_clpr"]) for x in rows if x.get("stck_clpr")]
        else:
            rows = self.client.us_daily_prices(item.symbol, item.exchange, self.config.slow_period + 2)
            closes = [float(x["clos"]) for x in rows if x.get("clos")]
        ma = moving_average_signal(closes, self.config.fast_period, self.config.slow_period)
        DAILY_CACHE[key] = (time.monotonic(), ma)
        return ma

    def _screen(self, item: SymbolConfig) -> Any:
        key = item.market + ":" + item.symbol
        cached = SCREEN_CACHE.get(key)
        if cached and time.monotonic() - cached[0] < 900:
            return cached[1]
        result = HardScreener(self.client, self.config).screen(item)
        SCREEN_CACHE[key] = (time.monotonic(), result)
        return result

    def _intraday_entry_signal(self, key: str, current: float, volume: float,
                               daily_change_percent: float, slow_ma: float) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        cutoff = now.timestamp() - self.config.intraday_entry_lookback_seconds
        samples = self.state["price_samples"].setdefault(key, [])
        samples.append({"time": now.isoformat(), "price": current, "volume": volume})
        samples[:] = [x for x in samples if datetime.fromisoformat(x["time"]).timestamp() >= cutoff][-60:]
        result = {"ready": False, "momentum_percent": 0.0, "sample_count": len(samples),
                  "volume_rising": False, "breakout": False}
        if len(samples) < self.config.intraday_entry_min_samples:
            result["reason"] = "intraday_warmup"
            return result
        first = samples[0]
        elapsed = (now - datetime.fromisoformat(first["time"])).total_seconds()
        if elapsed < 60:
            result["reason"] = "intraday_warmup"
            return result
        start_price = float(first["price"])
        momentum = (current / start_price - 1) * 100 if start_price else 0.0
        previous_high = max(float(x["price"]) for x in samples[:-1])
        volume_rising = volume <= 0 or float(first.get("volume") or 0) <= 0 or volume > float(first["volume"])
        breakout = current >= previous_high
        result.update({"momentum_percent": momentum, "volume_rising": volume_rising,
                       "breakout": breakout, "elapsed_seconds": elapsed})
        if daily_change_percent <= 0:
            result["reason"] = "intraday_daily_change_not_positive"
        elif current < slow_ma:
            result["reason"] = "intraday_below_daily_support"
        elif momentum < self.config.intraday_entry_momentum_percent:
            result["reason"] = "intraday_no_momentum"
        elif not breakout:
            result["reason"] = "intraday_no_breakout"
        elif not volume_rising:
            result["reason"] = "intraday_volume_not_rising"
        else:
            result.update({"ready": True, "reason": "intraday_momentum"})
        return result

    def _evaluate(self, item: SymbolConfig, positions: Dict[str, Any], confirm_live: bool,
                  allow_new_entries: bool, force_exit: bool) -> Dict[str, Any]:
        key = item.market + ":" + item.symbol
        position = positions.get(key)
        domestic_market_name = "KOSPI"
        if item.market == "kr":
            quote = self.client.domestic_quote(item.symbol)
            current = float(quote["stck_prpr"])
            volume = float(quote.get("acml_vol") or 0)
            daily_change_percent = float(quote.get("prdy_ctrt") or 0)
            domestic_market_name = str(quote.get("rprs_mrkt_kor_name") or "KOSPI")
        else:
            quote = self.client.us_quote(item.symbol, item.exchange)
            current = float(quote["last"])
            volume = float(quote.get("tvol") or quote.get("volume") or 0)
            daily_change_percent = float(quote.get("rate") or quote.get("prdy_ctrt") or 0)
        # 마감 청산은 일봉 조회 실패와 무관하게 반드시 주문까지 진행한다.
        ma = {"signal": "hold", "fast": 0.0, "slow": 0.0} if force_exit and position else self._ma(item)
        action, reason, profit_percent = "hold", str(ma["signal"]), None
        intraday_signal = self._intraday_entry_signal(
            key, current, volume, daily_change_percent, float(ma["slow"])
        ) if not position and allow_new_entries else {"ready": False, "momentum_percent": 0.0, "sample_count": 0}
        if position:
            average = float(position["average_price"])
            profit_percent = (current / average - 1) * 100 if average else 0
            meta = self.state["position_meta"].setdefault(key, {"opened_at": datetime.now(timezone.utc).isoformat(), "peak_profit_percent": profit_percent})
            meta["peak_profit_percent"] = max(float(meta.get("peak_profit_percent", profit_percent)), profit_percent)
            opened_at = datetime.fromisoformat(meta["opened_at"])
            age_minutes = (datetime.now(timezone.utc) - opened_at).total_seconds() / 60
            market_zone = ZoneInfo("Asia/Seoul") if item.market == "kr" else ZoneInfo("America/New_York")
            overnight = opened_at.astimezone(market_zone).date() < datetime.now(market_zone).date()
            if force_exit:
                action, reason = "sell", "market_close"
            elif overnight:
                action, reason = "sell", "overnight_exit"
            elif profit_percent >= self.config.take_profit_percent:
                action, reason = "sell", "take_profit"
            elif profit_percent <= -self.config.stop_loss_percent:
                action, reason = "sell", "stop_loss"
            elif (float(meta["peak_profit_percent"]) >= self.config.trailing_stop_activation_percent
                  and profit_percent <= float(meta["peak_profit_percent"]) - self.config.trailing_stop_giveback_percent):
                action, reason = "sell", "trailing_stop"
            elif age_minutes >= self.config.time_stop_minutes and profit_percent <= -self.config.time_stop_loss_percent:
                action, reason = "sell", "time_stop"
            elif ma["signal"] == "cross_down":
                action, reason = "sell", "cross_down"
        elif allow_new_entries:
            # 일봉은 현재가가 장기 평균 위인지 확인하는 보조 필터로만 쓰고,
            # 실제 진입은 짧은 구간의 상승과 직전 고점 돌파로 결정한다.
            if intraday_signal["ready"]:
                action, reason = "buy", "intraday_momentum"
            else:
                reason = str(intraday_signal.get("reason", "hold"))
        base = {"market": item.market, "symbol": item.symbol, "price": current, "profit_percent": profit_percent,
                "fast_ma": round(float(ma["fast"]), 4), "slow_ma": round(float(ma["slow"]), 4),
                "intraday_momentum_percent": round(float(intraday_signal.get("momentum_percent", 0)), 4),
                "intraday_sample_count": int(intraday_signal.get("sample_count", 0)),
                "action": action, "reason": reason, "mode": self.config.mode_for(item.market)}
        if action == "hold":
            return base
        if action == "buy":
            intraday = self._intraday(item.market)
            loss_limit = self.config.max_daily_loss_krw if item.market == "kr" else self.config.max_daily_loss_usd
            if float(intraday["realized_pnl"]) <= -loss_limit:
                return {**base, "action": "hold", "reason": "daily_loss_limit"}
            if int(intraday["round_trips"].get(item.symbol, 0)) >= self.config.max_round_trips_per_symbol:
                return {**base, "action": "hold", "reason": "round_trip_limit"}
            if sum(int(x) for x in intraday["round_trips"].values()) >= self.config.max_daily_round_trips_per_market:
                return {**base, "action": "hold", "reason": "market_round_trip_limit"}
            last_exit = intraday["last_exit"].get(item.symbol)
            if last_exit:
                elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last_exit)).total_seconds()
                if elapsed < self.config.reentry_cooldown_seconds:
                    return {**base, "action": "hold", "reason": "reentry_cooldown"}
            screen = self._screen(item)
            if not screen.approved:
                return {**base, "action": "hold", "reason": "screen_rejected:" + ",".join(screen.reasons)}
        buffer = self.config.limit_buffer_percent / 100
        estimated_limit_price = current * (1 + buffer if action == "buy" else 1 - buffer)
        estimated_limit_price = (round_domestic_order_price(estimated_limit_price, action, domestic_market_name)
                                 if item.market == "kr" else round(estimated_limit_price, 2))
        if action == "buy":
            active_limit = self.config.max_active_investment_krw if item.market == "kr" else self.config.max_active_investment_usd
            active_exposure = sum(float(x["quantity"]) * float(x["average_price"])
                                  for k, x in positions.items() if k.startswith(item.market + ":"))
            quantity = math.floor(min(item.max_position, max(0, active_limit - active_exposure)) / estimated_limit_price)
        else:
            quantity = int(float(position["quantity"]))
        if quantity < 1:
            return {**base, "action": "hold", "reason": "quantity_is_zero"}
        limit_price = estimated_limit_price
        mode = self.config.mode_for(item.market)
        if mode == "dry_run":
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
        filled = quantity if mode == "dry_run" else int(order_result.get("filled_quantity", 0))
        if action == "buy" and filled > 0:
            self.state["position_meta"][key] = {"opened_at": datetime.now(timezone.utc).isoformat(), "peak_profit_percent": 0.0}
            if mode == "live":
                positions[key] = {"quantity": filled, "average_price": float(order_result.get("average_price") or limit_price)}
        if action == "sell" and filled > 0:
            exit_price = limit_price if mode == "dry_run" else float(order_result.get("average_price") or limit_price)
            self._record_exit(item, float(position["average_price"]), filled, exit_price)
            if filled >= quantity:
                self.state["position_meta"].pop(key, None)
                if mode == "live":
                    positions.pop(key, None)
        self.state["last_actions"][key] = [action, reason, datetime.now(timezone.utc).isoformat()]
        self._save_state()
        return {**base, "quantity": quantity, "limit_price": limit_price, "order": order_result}

from __future__ import annotations

import json
import math
import threading
import time
from datetime import datetime, time as clock_time, timezone
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
        state.setdefault("entry_setups", {})
        return state

    def _intraday(self, market: str) -> Dict[str, Any]:
        zone = ZoneInfo("Asia/Seoul") if market == "kr" else ZoneInfo("America/New_York")
        key = market + ":" + datetime.now(zone).date().isoformat()
        intraday = self.state["intraday"].setdefault(key, {"realized_pnl": 0.0, "estimated_costs": 0.0,
                                                           "round_trips": {}, "last_exit": {}, "blocked_symbols": {}})
        intraday.setdefault("estimated_costs", 0.0)
        intraday.setdefault("blocked_symbols", {})
        intraday.setdefault("first_exit_net_pnl", None)
        if "entry_count" not in intraday:
            intraday["entry_count"] = self._logged_entry_count(market, key.split(":", 1)[1])
        return intraday

    def _logged_entry_count(self, market: str, day: str) -> int:
        path = self.log_path or (Path("logs/trades") / f"{day}-{market}.jsonl")
        count = 0
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (FileNotFoundError, OSError):
            return 0
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            order = event.get("order") if isinstance(event.get("order"), dict) else {}
            filled = int(float(order.get("filled_quantity") or 0))
            if order.get("simulated"):
                filled = int(float(event.get("quantity") or 0))
            if event.get("market") == market and event.get("action") == "buy" and filled > 0:
                count += 1
        return count

    def _session_entry_quota(self, market: str) -> int:
        zone = ZoneInfo("Asia/Seoul") if market == "kr" else ZoneInfo("America/New_York")
        current = datetime.now(zone).time()
        if market == "kr":
            early, middle, late = clock_time(10, 0), clock_time(12, 0), clock_time(14, 0)
        else:
            early, middle, late = clock_time(10, 30), clock_time(12, 30), clock_time(14, 30)
        if current < early:
            return self.config.max_entries_early_session
        if current < middle:
            return self.config.max_entries_mid_session
        if current < late:
            return self.config.max_entries_late_session
        return self.config.max_daily_round_trips_per_market

    def record_exit(self, item: SymbolConfig, average_price: float, quantity: int, exit_price: float) -> None:
        with STATE_LOCK:
            self.state = self._load_state()
            self._record_exit(item, average_price, quantity, exit_price)

    def _record_exit(self, item: SymbolConfig, average_price: float, quantity: int, exit_price: float) -> None:
        intraday = self._intraday(item.market)
        trade_pnl = (exit_price - average_price) * quantity
        intraday["realized_pnl"] = float(intraday["realized_pnl"]) + trade_pnl
        if item.market == "kr":
            buy_rate = self.config.estimated_commission_percent_kr
            sell_rate = self.config.estimated_commission_percent_kr + self.config.estimated_sell_cost_percent_kr
        else:
            buy_rate = self.config.estimated_commission_percent_us
            sell_rate = self.config.estimated_commission_percent_us + self.config.estimated_sell_cost_percent_us
        trade_cost = average_price * quantity * buy_rate / 100 + exit_price * quantity * sell_rate / 100
        intraday["estimated_costs"] = float(intraday["estimated_costs"]) + trade_cost
        if intraday.get("first_exit_net_pnl") is None:
            intraday["first_exit_net_pnl"] = trade_pnl - trade_cost
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
                if any(text in str(exc) for text in ("주문 가능 수량이 0주", "해외ETP 거래 미신청")):
                    self._intraday(item.market)["blocked_symbols"][item.symbol] = str(exc)
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
                  "volume_rising": False, "setup_phase": "scanning"}
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
        volumes = [float(x.get("volume") or 0) for x in samples]
        previous_interval = volumes[-2] - volumes[-3] if len(volumes) >= 3 else 0
        latest_interval = volumes[-1] - volumes[-2] if len(volumes) >= 2 else 0
        volume_rising = volume <= 0 or (latest_interval > 0 and latest_interval >= previous_interval)
        result.update({"momentum_percent": momentum, "volume_rising": volume_rising,
                       "elapsed_seconds": elapsed})
        market = key.split(":", 1)[0]
        max_momentum = (self.config.intraday_entry_max_momentum_percent_kr if market == "kr"
                        else self.config.intraday_entry_max_momentum_percent_us)
        setup = self.state["entry_setups"].get(key)
        if setup:
            updated_at = datetime.fromisoformat(str(setup.get("updated_at", now.isoformat())))
            if (now - updated_at).total_seconds() > self.config.intraday_entry_lookback_seconds:
                self.state["entry_setups"].pop(key, None)
                setup = None
        if daily_change_percent <= 0:
            self.state["entry_setups"].pop(key, None)
            result["reason"] = "intraday_daily_change_not_positive"
        elif current < slow_ma:
            self.state["entry_setups"].pop(key, None)
            result["reason"] = "intraday_below_daily_support"
        elif momentum > max_momentum:
            self.state["entry_setups"].pop(key, None)
            result["reason"] = "intraday_overextended"
        else:
            if setup is None:
                if momentum < self.config.intraday_entry_momentum_percent:
                    result["reason"] = "intraday_no_momentum"
                else:
                    setup = {"phase": "armed", "peak": current, "pullback_low": current,
                             "confirmations": 0, "updated_at": now.isoformat()}
                    self.state["entry_setups"][key] = setup
                    result.update({"reason": "intraday_wait_pullback", "setup_phase": "armed"})
            else:
                setup["updated_at"] = now.isoformat()
                setup["peak"] = max(float(setup["peak"]), current)
                drawdown = (1 - current / float(setup["peak"])) * 100
                if setup["phase"] == "armed":
                    if drawdown > self.config.intraday_pullback_max_percent:
                        self.state["entry_setups"].pop(key, None)
                        result["reason"] = "intraday_pullback_too_deep"
                    elif drawdown >= self.config.intraday_pullback_min_percent:
                        setup.update({"phase": "pulled_back", "pullback_low": current, "confirmations": 0})
                        result.update({"reason": "intraday_wait_reclaim", "setup_phase": "pulled_back"})
                    else:
                        result.update({"reason": "intraday_wait_pullback", "setup_phase": "armed"})
                else:
                    setup["pullback_low"] = min(float(setup["pullback_low"]), current)
                    if (1 - current / float(setup["peak"])) * 100 > self.config.intraday_pullback_max_percent:
                        self.state["entry_setups"].pop(key, None)
                        result["reason"] = "intraday_pullback_too_deep"
                    else:
                        reclaim_price = float(setup["peak"]) * (1 - self.config.intraday_reclaim_buffer_percent / 100)
                        setup["confirmations"] = int(setup.get("confirmations", 0)) + 1 if current >= reclaim_price else 0
                        if int(setup["confirmations"]) < self.config.intraday_reclaim_confirmations:
                            result.update({"reason": "intraday_wait_reclaim", "setup_phase": "pulled_back"})
                        elif not volume_rising:
                            result.update({"reason": "intraday_volume_not_rising", "setup_phase": "pulled_back"})
                        else:
                            self.state["entry_setups"].pop(key, None)
                            result.update({"ready": True, "reason": "intraday_pullback_reclaim",
                                           "setup_phase": "confirmed"})
        return result

    def _entry_delay_active(self, market: str) -> bool:
        zone = ZoneInfo("Asia/Seoul") if market == "kr" else ZoneInfo("America/New_York")
        now = datetime.now(zone)
        opened = clock_time(9, 0) if market == "kr" else clock_time(9, 30)
        delay = self.config.entry_delay_minutes_kr if market == "kr" else self.config.entry_delay_minutes_us
        minutes = (now.hour * 60 + now.minute) - (opened.hour * 60 + opened.minute)
        return 0 <= minutes < delay

    def _entry_window_closed(self, market: str, now: Optional[datetime] = None) -> bool:
        if market != "kr":
            return False
        zone = ZoneInfo("Asia/Seoul")
        current = now.astimezone(zone) if now else datetime.now(zone)
        opened_minutes = 9 * 60
        elapsed = current.hour * 60 + current.minute - opened_minutes
        return elapsed >= self.config.entry_cutoff_minutes_kr

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
        opening_delay = not position and allow_new_entries and self._entry_delay_active(item.market)
        entry_window_closed = not position and allow_new_entries and self._entry_window_closed(item.market)
        intraday_signal = self._intraday_entry_signal(
            key, current, volume, daily_change_percent, float(ma["slow"])
        ) if not position and allow_new_entries and not opening_delay and not entry_window_closed else {
            "ready": False, "momentum_percent": 0.0, "sample_count": 0,
            "reason": "entry_opening_delay" if opening_delay else (
                "entry_window_closed" if entry_window_closed else "hold"),
        }
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
            # 상승을 바로 추격하지 않고 눌림과 재상승이 연속 확인된 경우에만 진입한다.
            if opening_delay:
                reason = "entry_opening_delay"
            elif entry_window_closed:
                reason = "entry_window_closed"
            elif intraday_signal["ready"]:
                action, reason = "buy", "intraday_pullback_reclaim"
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
            net_realized = float(intraday["realized_pnl"]) - float(intraday.get("estimated_costs", 0))
            market_entry_limit = self.config.max_daily_entries_kr if item.market == "kr" else self.config.max_daily_entries_us
            if net_realized <= -loss_limit:
                return {**base, "action": "hold", "reason": "daily_loss_limit"}
            if intraday.get("first_exit_net_pnl") is not None and float(intraday["first_exit_net_pnl"]) < 0:
                return {**base, "action": "hold", "reason": "first_trade_loss_limit"}
            if item.symbol in intraday.get("blocked_symbols", {}):
                return {**base, "action": "hold", "reason": "symbol_blocked_after_order_error"}
            if int(intraday["round_trips"].get(item.symbol, 0)) >= self.config.max_round_trips_per_symbol:
                return {**base, "action": "hold", "reason": "round_trip_limit"}
            if int(intraday["entry_count"]) >= self.config.max_daily_round_trips_per_market:
                return {**base, "action": "hold", "reason": "market_round_trip_limit"}
            if int(intraday["entry_count"]) >= market_entry_limit:
                return {**base, "action": "hold", "reason": "market_entry_limit"}
            if int(intraday["entry_count"]) >= self._session_entry_quota(item.market):
                return {**base, "action": "hold", "reason": "session_entry_quota"}
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
            self._intraday(item.market)["entry_count"] = int(self._intraday(item.market)["entry_count"]) + 1
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

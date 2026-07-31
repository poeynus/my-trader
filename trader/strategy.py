from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import List, Dict


class StrategyError(ValueError):
    pass


@dataclass(frozen=True)
class SymbolConfig:
    market: str
    symbol: str
    exchange: str
    max_position: float


@dataclass(frozen=True)
class StrategyConfig:
    execution_mode: str
    fast_period: int
    slow_period: int
    take_profit_percent: float
    stop_loss_percent: float
    limit_buffer_percent: float
    min_price_krw: float
    min_price_usd: float
    min_avg_turnover_krw: float
    min_avg_turnover_usd: float
    max_daily_change_percent: float
    auto_discover: bool
    candidates_per_market: int
    selected_per_market: int
    default_position_krw: float
    default_position_usd: float
    min_market_cap_krw: float
    min_market_cap_usd: float
    max_active_investment_krw: float
    max_active_investment_usd: float
    reentry_cooldown_seconds: int
    max_round_trips_per_symbol: int
    max_daily_loss_krw: float
    max_daily_loss_usd: float
    fill_timeout_seconds: int
    max_retries: int
    symbols: List[SymbolConfig]

    @classmethod
    def load(cls, path: Path) -> "StrategyConfig":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            config = cls(
                execution_mode=str(data.get("execution_mode", "dry_run")),
                fast_period=int(data.get("fast_period", 5)), slow_period=int(data.get("slow_period", 20)),
                take_profit_percent=float(data.get("take_profit_percent", 2.5)),
                stop_loss_percent=float(data.get("stop_loss_percent", 1.5)),
                limit_buffer_percent=float(data.get("limit_buffer_percent", 0.2)),
                min_price_krw=float(data.get("min_price_krw", 5000)),
                min_price_usd=float(data.get("min_price_usd", 5)),
                min_avg_turnover_krw=float(data.get("min_avg_turnover_krw", 5000000000)),
                min_avg_turnover_usd=float(data.get("min_avg_turnover_usd", 20000000)),
                max_daily_change_percent=float(data.get("max_daily_change_percent", 20)),
                auto_discover=bool(data.get("auto_discover", True)),
                candidates_per_market=int(data.get("candidates_per_market", 10)),
                selected_per_market=int(data.get("selected_per_market", 3)),
                default_position_krw=float(data.get("default_position_krw", 500000)),
                default_position_usd=float(data.get("default_position_usd", 500)),
                min_market_cap_krw=float(data.get("min_market_cap_krw", 100000000000)),
                min_market_cap_usd=float(data.get("min_market_cap_usd", 1000000000)),
                max_active_investment_krw=float(data.get("max_active_investment_krw", data.get("max_daily_investment_krw", 1500000))),
                max_active_investment_usd=float(data.get("max_active_investment_usd", data.get("max_daily_investment_usd", 1500))),
                reentry_cooldown_seconds=int(data.get("reentry_cooldown_seconds", 300)),
                max_round_trips_per_symbol=int(data.get("max_round_trips_per_symbol", 3)),
                max_daily_loss_krw=float(data.get("max_daily_loss_krw", 10000)),
                max_daily_loss_usd=float(data.get("max_daily_loss_usd", 10)),
                fill_timeout_seconds=int(data.get("fill_timeout_seconds", 60)),
                max_retries=int(data.get("max_retries", 1)),
                symbols=[SymbolConfig(str(x["market"]).lower(), str(x["symbol"]).upper(),
                                      str(x.get("exchange", "NASDAQ")).upper(), float(x["max_position"]))
                         for x in data.get("symbols", [])],
            )
        except FileNotFoundError as exc:
            raise StrategyError(f"전략 설정 파일이 없습니다: {path}") from exc
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise StrategyError(f"전략 설정 값이 올바르지 않습니다: {exc}") from exc
        config.validate()
        return config

    def validate(self) -> None:
        if self.execution_mode not in {"dry_run", "live"}:
            raise StrategyError("execution_mode은 dry_run 또는 live여야 합니다.")
        if not (1 <= self.fast_period < self.slow_period <= 100):
            raise StrategyError("이동평균 기간은 1 <= fast_period < slow_period <= 100이어야 합니다.")
        if self.take_profit_percent <= 0 or self.stop_loss_percent <= 0 or (not self.auto_discover and not self.symbols):
            raise StrategyError("익절·손절 비율과 symbols 설정을 확인하세요.")
        if any(x.market not in {"kr", "us"} or x.max_position <= 0 for x in self.symbols):
            raise StrategyError("market은 kr/us이고 max_position은 0보다 커야 합니다.")
        if not (1 <= self.selected_per_market <= self.candidates_per_market <= 30):
            raise StrategyError("선정 수는 1 <= selected_per_market <= candidates_per_market <= 30이어야 합니다.")
        if self.max_active_investment_krw <= 0 or self.max_active_investment_usd <= 0:
            raise StrategyError("동시 투자 한도는 0보다 커야 합니다.")
        if not (30 <= self.reentry_cooldown_seconds <= 86400):
            raise StrategyError("재진입 대기시간은 30~86400초여야 합니다.")
        if not (1 <= self.max_round_trips_per_symbol <= 20):
            raise StrategyError("종목별 하루 왕복 횟수는 1~20회여야 합니다.")
        if self.max_daily_loss_krw <= 0 or self.max_daily_loss_usd <= 0:
            raise StrategyError("일일 손실 한도는 0보다 커야 합니다.")
        if not (10 <= self.fill_timeout_seconds <= 600 and 0 <= self.max_retries <= 3):
            raise StrategyError("체결 제한시간은 10~600초, 재주문은 0~3회여야 합니다.")


def moving_average_signal(closes_newest_first: List[float], fast: int, slow: int) -> Dict[str, object]:
    if len(closes_newest_first) < slow + 1:
        raise StrategyError(f"이동평균 계산에 최소 {slow + 1}개 종가가 필요합니다.")
    closes = list(reversed(closes_newest_first))
    current_fast, current_slow = sum(closes[-fast:]) / fast, sum(closes[-slow:]) / slow
    previous_fast = sum(closes[-fast - 1:-1]) / fast
    previous_slow = sum(closes[-slow - 1:-1]) / slow
    signal = "cross_up" if previous_fast <= previous_slow and current_fast > current_slow else (
        "cross_down" if previous_fast >= previous_slow and current_fast < current_slow else "hold")
    return {"signal": signal, "fast": current_fast, "slow": current_slow}

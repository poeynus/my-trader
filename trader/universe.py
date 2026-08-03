from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Optional, Set

from .client import KISClient
from .screener import HardScreener
from .strategy import StrategyConfig, SymbolConfig, moving_average_signal


class UniverseSelector:
    def __init__(self, client: KISClient, config: StrategyConfig):
        self.client, self.config = client, config

    def discover(self, markets: Optional[Set[str]] = None) -> List[SymbolConfig]:
        markets = markets or {"kr", "us"}
        candidates = ([] if "kr" not in markets else self._domestic_candidates()) + ([] if "us" not in markets else self._us_candidates())
        screener = HardScreener(self.client, self.config)
        selected: List[SymbolConfig] = []
        counts = {"kr": 0, "us": 0}
        for item in candidates:
            if counts[item.market] >= self.config.selected_per_market:
                continue
            try:
                result = screener.screen(item)
                trend_up = self._trend_up(item)
            except Exception:
                continue
            if result.approved and trend_up:
                selected.append(item)
                counts[item.market] += 1
        return selected

    def momentum_candidates(self, market: str) -> List[SymbolConfig]:
        """장중 순위에서 빠르게 감시 후보를 갱신한다. 실제 진입 전 하드필터와 추세는 다시 확인한다."""
        ranked: List[tuple[tuple[float, float, float], SymbolConfig]] = []
        if market == "kr":
            for row in self.client.domestic_turnover_rank():
                symbol, price = str(row.get("mksc_shrn_iscd") or ""), float(row.get("stck_prpr") or 0)
                rate, volume_increase = float(row.get("prdy_ctrt") or 0), float(row.get("vol_inrt") or 0)
                shares, turnover = float(row.get("lstn_stcn") or 0), float(row.get("acml_tr_pbmn") or 0)
                if (rate > 0 and len(symbol) == 6 and self.config.min_price_krw <= price <= self.config.default_position_krw
                        and price * shares >= self.config.min_market_cap_krw):
                    ranked.append(((rate, volume_increase, turnover), SymbolConfig("kr", symbol, "NASDAQ", self.config.default_position_krw)))
        else:
            for exchange in ("NASDAQ", "NYSE", "AMEX"):
                caps = {str(x.get("symb")): float(x.get("tomv") or x.get("mcap") or 0)
                        for x in self.client.us_market_cap_rank(exchange)}
                for row in self.client.us_turnover_rank(exchange):
                    symbol, price = str(row.get("symb") or "").upper(), float(row.get("last") or 0)
                    rate, turnover = float(row.get("rate") or 0), float(row.get("tamt") or 0)
                    if (rate > 0 and symbol in caps and self.config.min_price_usd <= price <= self.config.default_position_usd
                            and caps[symbol] >= self.config.min_market_cap_usd):
                        ranked.append(((rate, turnover, 0), SymbolConfig("us", symbol, exchange, self.config.default_position_usd)))
        ranked.sort(key=lambda x: x[0], reverse=True)
        return [item for _, item in ranked[:self.config.max_monitored_per_market]]

    def _trend_up(self, item: SymbolConfig) -> bool:
        days = self.config.slow_period + 2
        if item.market == "kr":
            rows = self.client.domestic_daily_prices(item.symbol, days)
            closes = [float(x["stck_clpr"]) for x in rows if x.get("stck_clpr")]
        else:
            rows = self.client.us_daily_prices(item.symbol, item.exchange, days)
            closes = [float(x["clos"]) for x in rows if x.get("clos")]
        signal = moving_average_signal(closes, self.config.fast_period, self.config.slow_period)
        return float(signal["fast"]) > float(signal["slow"])

    def _domestic_candidates(self) -> List[SymbolConfig]:
        rows = self.client.domestic_turnover_rank()
        result = []
        for row in rows:
            price = float(row.get("stck_prpr") or 0)
            shares = float(row.get("lstn_stcn") or 0)
            symbol = str(row.get("mksc_shrn_iscd") or "")
            if (len(symbol) == 6 and self.config.min_price_krw <= price <= self.config.default_position_krw
                    and price * shares >= self.config.min_market_cap_krw):
                result.append(SymbolConfig("kr", symbol, "NASDAQ", self.config.default_position_krw))
            if len(result) >= self.config.candidates_per_market:
                break
        return result

    def _us_candidates(self) -> List[SymbolConfig]:
        ranked: List[tuple[float, SymbolConfig]] = []
        for exchange in ("NASDAQ", "NYSE", "AMEX"):
            turnover = self.client.us_turnover_rank(exchange)
            caps = {str(x.get("symb")): float(x.get("tomv") or x.get("mcap") or 0) for x in self.client.us_market_cap_rank(exchange)}
            for row in turnover:
                symbol = str(row.get("symb") or "").upper()
                price, amount = float(row.get("last") or 0), float(row.get("tamt") or 0)
                if (symbol in caps and self.config.min_price_usd <= price <= self.config.default_position_usd
                        and caps[symbol] >= self.config.min_market_cap_usd):
                    ranked.append((amount, SymbolConfig("us", symbol, exchange, self.config.default_position_usd)))
        ranked.sort(key=lambda x: x[0], reverse=True)
        return [item for _, item in ranked[:self.config.candidates_per_market]]

    @staticmethod
    def save(symbols: List[SymbolConfig], path: Path = Path(".auto-universe.json")) -> None:
        path.write_text(json.dumps([asdict(x) for x in symbols], ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def load(path: Path = Path(".auto-universe.json"), config: Optional[StrategyConfig] = None) -> List[SymbolConfig]:
        data = json.loads(path.read_text(encoding="utf-8"))
        symbols = [SymbolConfig(**x) for x in data]
        if config:
            symbols = [SymbolConfig(x.market, x.symbol, x.exchange,
                                    config.default_position_krw if x.market == "kr" else config.default_position_usd)
                       for x in symbols]
        return symbols

    @classmethod
    def merge_and_save(cls, symbols: List[SymbolConfig], markets: Set[str], path: Path = Path(".auto-universe.json")) -> List[SymbolConfig]:
        try:
            existing = cls.load(path)
        except (FileNotFoundError, ValueError, json.JSONDecodeError):
            existing = []
        merged = [x for x in existing if x.market not in markets] + symbols
        cls.save(merged, path)
        return merged

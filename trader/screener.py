from __future__ import annotations

from dataclasses import dataclass
from typing import List

from .client import KISClient
from .strategy import StrategyConfig, SymbolConfig


@dataclass(frozen=True)
class ScreenResult:
    market: str
    symbol: str
    approved: bool
    reasons: List[str]
    price: float
    average_turnover: float


class HardScreener:
    def __init__(self, client: KISClient, config: StrategyConfig):
        self.client, self.config = client, config

    def screen(self, item: SymbolConfig) -> ScreenResult:
        reasons: List[str] = []
        if item.market == "kr":
            quote = self.client.domestic_quote(item.symbol)
            price = float(quote.get("stck_prpr") or 0)
            rows = self.client.domestic_daily_prices(item.symbol, 21)
            turnovers = [float(x.get("stck_clpr") or 0) * float(x.get("acml_vol") or 0) for x in rows[:20]]
            minimum, min_turnover = self.config.min_price_krw, self.config.min_avg_turnover_krw
            blocked = {"temp_stop_yn": "거래정지", "invt_caful_yn": "투자주의", "short_over_yn": "단기과열",
                       "mang_issu_cls_code": "관리종목"}
            for field, label in blocked.items():
                if str(quote.get(field, "N")) not in {"N", "00", ""}:
                    reasons.append(label)
            if str(quote.get("mrkt_warn_cls_code", "00")) != "00":
                reasons.append("시장경고")
        else:
            quote = self.client.us_quote(item.symbol, item.exchange)
            price = float(quote.get("last") or 0)
            rows = self.client.us_daily_prices(item.symbol, item.exchange, 21)
            turnovers = [float(x.get("clos") or 0) * float(x.get("tvol") or 0) for x in rows[:20]]
            minimum, min_turnover = self.config.min_price_usd, self.config.min_avg_turnover_usd
        average_turnover = sum(turnovers) / len(turnovers) if turnovers else 0
        if price < minimum:
            reasons.append("저가주")
        if average_turnover < min_turnover:
            reasons.append("거래대금부족")
        if len(rows) >= 2:
            previous = float((rows[1].get("stck_clpr") if item.market == "kr" else rows[1].get("clos")) or 0)
            if previous and abs(price / previous - 1) * 100 > self.config.max_daily_change_percent:
                reasons.append("당일변동과다")
        return ScreenResult(item.market, item.symbol, not reasons, reasons, price, average_turnover)

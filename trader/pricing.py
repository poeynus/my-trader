from __future__ import annotations

import math


def domestic_tick_size(price: float, market_name: str = "KOSPI") -> int:
    """KRX 주권 정규시장 호가 단위. KOSDAQ은 10만원 이상도 100원 단위다."""
    if price < 1_000:
        return 1
    if price < 5_000:
        return 5
    if price < 10_000:
        return 10
    if price < 50_000:
        return 50
    if price < 100_000:
        return 100
    if "KOSDAQ" in market_name.upper():
        return 100
    if price < 500_000:
        return 500
    return 1_000


def round_domestic_order_price(price: float, side: str, market_name: str = "KOSPI") -> int:
    """매수는 위 호가, 매도는 아래 호가로 보수적으로 맞춘다."""
    rounded = float(price)
    for _ in range(2):
        tick = domestic_tick_size(rounded, market_name)
        rounded = math.ceil(price / tick) * tick if side == "buy" else math.floor(price / tick) * tick
    return int(rounded)

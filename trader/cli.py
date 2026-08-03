from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from .client import KISClient
from .config import ConfigurationError, Settings
from .http import APIError
from .auto import AutoTrader
from .strategy import StrategyConfig, StrategyError
from .realtime import RealtimeExitMonitor
from .screener import HardScreener
from .universe import UniverseSelector
from .report import DailyReporter
from .scheduler import EndOfDayScheduler
from .autopilot import Autopilot
from .execution import ExecutionManager


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="한국투자증권 국내·미국주식 실전 API CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("config-check", help="환경설정 확인(비밀값은 출력하지 않음)")

    quote = sub.add_parser("quote", help="현재가 조회")
    quote.add_argument("market", choices=["kr", "us"])
    quote.add_argument("symbol")
    quote.add_argument("--exchange", choices=["NASDAQ", "NYSE", "AMEX"], default="NASDAQ")

    balance = sub.add_parser("balance", help="잔고 조회")
    balance.add_argument("market", choices=["kr", "us"])

    order = sub.add_parser("order", help="매수·매도 주문")
    order.add_argument("market", choices=["kr", "us"])
    order.add_argument("side", choices=["buy", "sell"])
    order.add_argument("symbol")
    order.add_argument("quantity", type=int)
    order.add_argument("--price", type=float, required=True, help="지정가. 국내 시장가는 0, 미국은 0 불가")
    order.add_argument("--exchange", choices=["NASDAQ", "NYSE", "AMEX"], default="NASDAQ")
    order.add_argument("--confirm", action="store_true", help="실제 API로 주문 전송")

    cancel = sub.add_parser("cancel", help="미체결 주문 전량 취소")
    cancel.add_argument("market", choices=["kr", "us"])
    cancel.add_argument("order_number")
    cancel.add_argument("quantity", type=int)
    cancel.add_argument("--symbol", help="미국 취소 시 필수")
    cancel.add_argument("--organization-number", help="국내 취소 시 주문 응답의 KRX_FWDG_ORD_ORGNO")
    cancel.add_argument("--exchange", choices=["NASDAQ", "NYSE", "AMEX"], default="NASDAQ")
    cancel.add_argument("--confirm", action="store_true")
    auto = sub.add_parser("auto", help="이동평균·익절·손절 자동매매")
    auto.add_argument("action", choices=["discover", "screen", "run", "cycle", "loop", "watch", "report", "eod", "scheduler", "autopilot"])
    auto.add_argument("--strategy", default="strategy.json")
    auto.add_argument("--interval", type=int, default=300, help="loop 주기(초, 최소 60)")
    auto.add_argument("--confirm-live", action="store_true")
    auto.add_argument("--market", choices=["kr", "us"], help="report/eod 대상 시장")
    return parser


def main(argv: Any = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        settings = Settings.from_env()
        if args.command == "config-check":
            _print({"environment": "real", "account": "****" + settings.account_number[-4:], "product_code": settings.account_product_code, "real_trading_enabled": settings.enable_real_trading, "max_order_krw": settings.max_order_krw, "max_order_usd": settings.max_order_usd})
            return
        client = KISClient(settings)
        if args.command == "auto":
            strategy = StrategyConfig.load(Path(args.strategy))
            selector = UniverseSelector(client, strategy)
            universe_path = Path(".auto-universe.json")
            if args.action == "scheduler":
                EndOfDayScheduler(client, strategy).run_forever()
                return
            if args.action == "autopilot":
                try:
                    asyncio.run(Autopilot(client, strategy, args.confirm_live).run_forever())
                except KeyboardInterrupt:
                    _print({"event": "autopilot_stopped", "reason": "keyboard_interrupt"})
                return
            if args.action in {"report", "eod"}:
                if not args.market:
                    raise ValueError("report/eod에는 --market kr 또는 --market us가 필요합니다.")
                markets = {args.market}
                if args.action == "eod":
                    selected_market = selector.discover(markets)
                    symbols = selector.merge_and_save(selected_market, markets, universe_path)
                else:
                    symbols = selector.load(universe_path, strategy)
                report_path = DailyReporter(client, strategy).generate(args.market, symbols)
                _print({"report": str(report_path), "selected": [vars(x) for x in symbols if x.market == args.market]})
                return
            if args.action in {"discover", "cycle"}:
                symbols = selector.discover()
                selector.save(symbols, universe_path)
                if args.action == "discover":
                    result = [vars(x) for x in symbols]
                    _print(result)
                    return
                strategy = replace(strategy, symbols=symbols)
            elif strategy.auto_discover:
                try:
                    strategy = replace(strategy, symbols=selector.load(universe_path, strategy))
                except (FileNotFoundError, ValueError, json.JSONDecodeError):
                    symbols = selector.discover()
                    selector.save(symbols, universe_path)
                    strategy = replace(strategy, symbols=symbols)
            execution = ExecutionManager(client, strategy) if strategy.has_live_market else None
            auto = AutoTrader(client, strategy, execution=execution)
            if args.action == "screen":
                screener = HardScreener(client, strategy)
                result = [vars(screener.screen(item)) for item in strategy.symbols]
            elif args.action == "watch":
                asyncio.run(RealtimeExitMonitor(client, strategy, execution=execution).run(args.confirm_live))
                return
            elif args.action == "loop":
                auto.loop(args.interval, args.confirm_live)
                return
            else:
                result = auto.run_once(args.confirm_live)
        elif args.command == "quote":
            result = client.domestic_quote(args.symbol) if args.market == "kr" else client.us_quote(args.symbol, args.exchange)
        elif args.command == "balance":
            result = client.domestic_balance() if args.market == "kr" else client.us_balance()
        elif args.command == "order":
            result = client.domestic_order(args.side, args.symbol, args.quantity, args.price, args.confirm) if args.market == "kr" else client.us_order(args.side, args.symbol, args.exchange, args.quantity, args.price, args.confirm)
        else:
            if args.market == "kr":
                if not args.organization_number:
                    raise ValueError("국내 취소에는 --organization-number가 필요합니다.")
                result = client.domestic_cancel(args.order_number, args.organization_number, args.quantity, args.confirm)
            else:
                if not args.symbol:
                    raise ValueError("미국 취소에는 --symbol이 필요합니다.")
                result = client.us_cancel(args.order_number, args.symbol, args.exchange, args.quantity, args.confirm)
        _print(result)
    except (ConfigurationError, StrategyError, ValueError, APIError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        raise SystemExit(2)

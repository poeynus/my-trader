from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from .client import KISClient
from .strategy import SymbolConfig


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _fmt(value: float, currency: str) -> str:
    return f"{value:,.0f} {currency}" if currency == "KRW" else f"{value:,.2f} {currency}"


class DailyReporter:
    def __init__(self, client: KISClient, reports_dir: Path = Path("reports"), trades_path: Path = Path("trades.jsonl")):
        self.client, self.reports_dir, self.trades_path = client, reports_dir, trades_path
        self.reports_dir.mkdir(parents=True, exist_ok=True)

    def generate(self, market: str, selected: List[SymbolConfig]) -> Path:
        timezone = ZoneInfo("Asia/Seoul") if market == "kr" else ZoneInfo("America/New_York")
        now = datetime.now(timezone)
        day = now.date().isoformat()
        snapshot = self._snapshot(market, day, selected)
        previous = self._previous_snapshot(market, day)
        events = self._events(market, day, timezone)
        path = self.reports_dir / f"{day}-{market}.md"
        path.write_text(self._markdown(snapshot, previous, events, now), encoding="utf-8")
        self._save_snapshot(market, snapshot)
        return path

    def _snapshot(self, market: str, day: str, selected: List[SymbolConfig]) -> Dict[str, Any]:
        balance = self.client.domestic_balance() if market == "kr" else self.client.us_balance()
        positions = []
        for row in balance["positions"]:
            if market == "kr":
                symbol, name = str(row.get("pdno") or ""), str(row.get("prdt_name") or "")
                quantity, average = _number(row.get("hldg_qty")), _number(row.get("pchs_avg_pric"))
                current, cost, value = _number(row.get("prpr")), _number(row.get("pchs_amt")), _number(row.get("evlu_amt"))
                pnl, rate = _number(row.get("evlu_pfls_amt")), _number(row.get("evlu_pfls_rt"))
            else:
                symbol, name = str(row.get("ovrs_pdno") or ""), str(row.get("ovrs_item_name") or row.get("prdt_name") or "")
                quantity, average = _number(row.get("ovrs_cblc_qty")), _number(row.get("pchs_avg_pric"))
                current = _number(row.get("now_pric2"))
                cost, value = _number(row.get("frcr_pchs_amt1")), _number(row.get("ovrs_stck_evlu_amt"))
                pnl, rate = _number(row.get("frcr_evlu_pfls_amt")), _number(row.get("evlu_pfls_rt"))
            if quantity > 0:
                positions.append({"symbol": symbol, "name": name, "quantity": quantity, "average_price": average,
                                  "current_price": current, "cost": cost, "value": value, "unrealized_pnl": pnl, "return_percent": rate})
        cost = sum(x["cost"] for x in positions)
        value = sum(x["value"] for x in positions)
        pnl = sum(x["unrealized_pnl"] for x in positions)
        return {"date": day, "market": market, "currency": "KRW" if market == "kr" else "USD",
                "selected": [asdict(x) for x in selected if x.market == market], "positions": positions,
                "metrics": {"position_count": len(positions), "cost": cost, "value": value,
                            "unrealized_pnl": pnl, "unrealized_return_percent": (pnl / cost * 100 if cost else 0)}}

    def _snapshot_path(self, market: str) -> Path:
        return self.reports_dir / f"snapshots-{market}.json"

    def _read_snapshots(self, market: str) -> List[Dict[str, Any]]:
        try:
            return json.loads(self._snapshot_path(market).read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def _previous_snapshot(self, market: str, day: str) -> Optional[Dict[str, Any]]:
        prior = [x for x in self._read_snapshots(market) if x.get("date") < day]
        return prior[-1] if prior else None

    def _save_snapshot(self, market: str, snapshot: Dict[str, Any]) -> None:
        snapshots = [x for x in self._read_snapshots(market) if x.get("date") != snapshot["date"]]
        snapshots.append(snapshot)
        snapshots.sort(key=lambda x: x["date"])
        self._snapshot_path(market).write_text(json.dumps(snapshots, ensure_ascii=False, indent=2), encoding="utf-8")

    def _events(self, market: str, day: str, timezone: ZoneInfo) -> List[Dict[str, Any]]:
        try:
            lines = self.trades_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        events = []
        for line in lines:
            try:
                event = json.loads(line)
                event_day = datetime.fromisoformat(event["time"]).astimezone(timezone).date().isoformat()
                if event_day == day and event.get("market") == market:
                    events.append(event)
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
        return events

    def _markdown(self, current: Dict[str, Any], previous: Optional[Dict[str, Any]], events: List[Dict[str, Any]], now: datetime) -> str:
        market_name = "국내" if current["market"] == "kr" else "미국"
        currency, metrics = current["currency"], current["metrics"]
        lines = [f"# {current['date']} {market_name}주식 일간 보고서", "", f"생성 시각: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}", "",
                 "## 요약", "", "| 항목 | 값 |", "|---|---:|",
                 f"| 보유 종목 | {metrics['position_count']}개 |", f"| 매입금액 | {_fmt(metrics['cost'], currency)} |",
                 f"| 평가금액 | {_fmt(metrics['value'], currency)} |", f"| 미실현 손익 | {_fmt(metrics['unrealized_pnl'], currency)} |",
                 f"| 미실현 수익률 | {metrics['unrealized_return_percent']:.2f}% |"]
        lines += ["", "## 전일 대비", ""]
        if previous:
            pm = previous["metrics"]
            lines += ["| 항목 | 변화 |", "|---|---:|",
                      f"| 보유주식 평가액 | {_fmt(metrics['value'] - pm['value'], currency)} |",
                      f"| 미실현 손익 | {_fmt(metrics['unrealized_pnl'] - pm['unrealized_pnl'], currency)} |"]
        else:
            lines.append("첫 스냅샷이라 전일 비교가 없습니다. 다음 거래일부터 계산됩니다.")
        lines += ["", "## 자동 선정 종목", "", "| 종목 | 거래소 | 최대 투자금 |", "|---|---|---:|"]
        for x in current["selected"]:
            lines.append(f"| {x['symbol']} | {x['exchange'] if x['market'] == 'us' else 'KRX'} | {_fmt(x['max_position'], currency)} |")
        if not current["selected"]:
            lines.append("| - | - | - |")
        lines += ["", "## 보유 종목 상세", "", "| 종목 | 수량 | 평균가 | 현재가 | 평가액 | 손익 | 수익률 |", "|---|---:|---:|---:|---:|---:|---:|"]
        for x in current["positions"]:
            lines.append(f"| {x['symbol']} {x['name']} | {x['quantity']:g} | {x['average_price']:,.2f} | {x['current_price']:,.2f} | {_fmt(x['value'], currency)} | {_fmt(x['unrealized_pnl'], currency)} | {x['return_percent']:.2f}% |")
        if not current["positions"]:
            lines.append("| 보유 종목 없음 | - | - | - | - | - | - |")
        lines += ["", "## 자동매매 이벤트", "", "| 시각(UTC) | 종목 | 행동 | 사유 | 가격 | 수익률 |", "|---|---|---|---|---:|---:|"]
        for x in events:
            lines.append(f"| {x.get('time', '')} | {x.get('symbol', '')} | {x.get('action', '')} | {x.get('reason', '')} | {x.get('price', '')} | {x.get('profit_percent', '')} |")
        if not events:
            lines.append("| 이벤트 없음 | - | - | - | - | - |")
        lines += ["", "> 전일 대비는 장 종료 스냅샷 간 변화입니다. 입출금·환전·수수료·세금이 있으면 순수 매매손익과 다를 수 있습니다.", ""]
        return "\n".join(lines)

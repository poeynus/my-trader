# 국내·미국주식 자동매매 운영 가이드

## 전체 흐름

```text
장 종료
  ↓
거래대금·시가총액 순위에서 후보 수집
  ↓
저가·저유동성·경고·과도한 변동 종목 제외
  ↓
자동 매매 대상 저장 (.auto-universe.json)
  ↓
이동평균 진입 신호 판단
  ↓
다음 장에서 지정가 매수
  ↓
WebSocket 매수 1호가 실시간 감시
  ↓
+2.5% 익절 / -1.5% 손절
  ↓
장 종료 스냅샷 및 Markdown 보고서 생성
```

## 설치와 설정

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```

`.env`에는 실전 API 키와 계좌를 입력합니다. 조회와 dry-run 동안에는 주문 잠금을 유지합니다.

```dotenv
KIS_ENABLE_REAL_TRADING=false
```

`strategy.json`에서 핵심 정책을 조정합니다.

```json
{
  "execution_mode": "live",
  "take_profit_percent": 2.5,
  "stop_loss_percent": 1.5,
  "auto_discover": true,
  "selected_per_market": 3,
  "default_position_krw": 500000,
  "default_position_usd": 500
}
```

## 종목 자동 선정

국내는 보통주 거래대금 상위 종목, 미국은 NASDAQ·NYSE·AMEX의 거래대금과 시가총액 상위 교집합에서 후보를 만듭니다.
종목당 예산으로 최소 1주를 살 수 없는 종목은 자동으로 제외합니다.

```bash
.venv/bin/python main.py auto discover
```

선정 결과는 `.auto-universe.json`에 저장됩니다. `auto_discover=true`이면 `strategy.json`의 고정 종목을 직접 관리할 필요가 없습니다.

하드 필터만 다시 확인하려면 다음 명령을 사용합니다.

```bash
.venv/bin/python main.py auto screen
```

## 진입 판단

종목 재선정과 진입 판단을 연속 실행합니다.

```bash
.venv/bin/python main.py auto cycle
```

현재 진입 조건은 5일 이동평균이 20일 이동평균을 상향 돌파하는 경우입니다. `dry_run`에서는 주문 대신 `.trader-state.json`에 가상 포지션을 저장합니다.

## 장중 실시간 감시

실제 보유 종목의 매수 1호가를 WebSocket으로 감시합니다.

```bash
.venv/bin/python main.py auto watch --confirm-live
```

실제 주문은 다음 세 조건을 모두 만족해야 합니다.

1. `strategy.json`의 `live_markets`에 실전 시장이 포함됨
2. `.env`의 `KIS_ENABLE_REAL_TRADING=true`
3. 실행 명령에 `--confirm-live`

긴급 정지는 프로젝트 루트에 `STOP_TRADING` 파일을 만들면 됩니다.

## 완전 자동 실행

장 종료 종목 선정·보고서, 개장 후 진입 판단, 주문 가능 금액 확인, 체결 추적, 실시간 익절·손절을 한 프로세스로 실행합니다.

```bash
.venv/bin/python main.py auto autopilot
```

`dry_run`에서는 실제 주문 없이 개장 후 진입 판단과 장 종료 작업만 수행합니다. 실전 모드에서는 다음 명령을 사용합니다.

```bash
.venv/bin/python main.py auto autopilot --confirm-live
```

국내는 09:00~15:20 KST, 미국은 09:35~15:50 ET에 기본 10초 간격으로 매매 판단을 반복합니다. 실제 API 응답과 종목 수에 따라 한 사이클은 더 길어질 수 있습니다. 장 종료 때 우량·유동성 필터를 통과한 최대 20개 후보 중 5일 이동평균이 20일 이동평균보다 높은 상승 추세 종목만 최대 5개 저장합니다. 조건을 충족하는 종목이 부족하면 숫자를 억지로 채우지 않습니다. 장중에는 15분마다 거래대금 순위를 다시 조회해 감시 대상을 최대 8개로 갱신하며, 기존 보유 종목은 청산까지 유지합니다. 보유 중에는 현재가로 익절·손절을 판단하고, 매도가 체결되면 재진입 대기시간 후 남은 동시 투자 한도 안에서 다시 매수할 수 있습니다. 주문 전 국내 미수 없는 매수 가능 수량과 미국 통합증거금 반영 가능 수량을 확인합니다.

국내는 15:00, 미국은 15:35 ET부터 신규 진입을 중단합니다. 국내 15:15, 미국 15:45 ET부터 남은 포지션을 전량 청산하고 각각 15:29, 15:59까지 체결과 재시도를 반복합니다. 일시적인 잔고·시세·후보 갱신 API 오류는 거래 로그에 남기고 다음 사이클에서 자동 복구합니다. 전 거래일 포지션이 예외적으로 남아 있으면 다음 정규장 시작 직후 우선 청산합니다.

`autopilot` 자체가 매수와 매도를 모두 수행하므로 별도 `auto watch` 프로세스를 동시에 실행하지 마세요. 동일 종목 중복 매도 방지를 위해 두 실행 방식을 함께 사용하지 않습니다.

미체결 주문은 `fill_timeout_seconds` 동안 3초 간격으로 체결을 확인한 뒤 잔량을 취소하고, 최신 가격으로 `max_retries` 횟수만큼 재주문합니다.

```json
{
  "max_active_investment_krw": 200000,
  "max_active_investment_usd": 140,
  "reentry_cooldown_seconds": 60,
  "max_round_trips_per_symbol": 5,
  "max_daily_round_trips_per_market": 8,
  "max_daily_loss_krw": 10000,
  "max_daily_loss_usd": 7,
  "time_stop_minutes": 15,
  "time_stop_loss_percent": 0.3,
  "trailing_stop_activation_percent": 1.5,
  "trailing_stop_giveback_percent": 0.5,
  "intraday_refresh_minutes": 15,
  "max_monitored_per_market": 8,
  "fill_timeout_seconds": 60,
  "max_retries": 1
}
```

`max_active_investment_*`는 하루 누적 매수액이 아니라 동시에 보유할 수 있는 원가 합계입니다. 매도 체결 후에는 한도가 복구됩니다. 현재 설정은 같은 종목 매도 후 1분 대기, 종목별 하루 왕복 5회, 시장 전체 하루 왕복 8회, 국내 일일 확정손실 1만 원·미국 7달러 도달 시 신규 진입 중단입니다. 손실 한도는 미실현 손익이 아니라 프로그램이 확인한 당일 매도 체결 손익을 기준으로 합니다.

현재 시장별 모드는 다음처럼 미국만 실전으로 분리할 수 있습니다.

```json
{
  "execution_mode": "dry_run",
  "live_markets": ["kr", "us"]
}
```

`live_markets`가 있으면 포함된 시장만 실제 주문하고 나머지는 `dry_run`으로 동작합니다.

`autopilot`은 파일 잠금을 사용하여 두 개가 동시에 실행되는 것을 차단합니다.

## 장 종료 처리와 보고서

장 종료 명령은 해당 시장의 종목을 다시 선정하고 계좌 스냅샷과 Markdown 보고서를 생성합니다.

```bash
.venv/bin/python main.py auto eod --market kr
.venv/bin/python main.py auto eod --market us
```

상시 스케줄러를 실행하면 평일 국내 15:40(KST), 미국 16:10(ET)에 해당 작업을 하루 한 번 자동 실행합니다.

```bash
.venv/bin/python main.py auto scheduler
```

주말은 건너뜁니다. 거래소 임시 휴장일에는 순위 API 결과가 갱신되지 않을 수 있으므로 생성 보고서를 확인해야 합니다.

종목을 재선정하지 않고 보고서만 다시 만들 수도 있습니다.

```bash
.venv/bin/python main.py auto report --market kr
.venv/bin/python main.py auto report --market us
```

보고서는 `reports/YYYY-MM-DD-kr.md`와 `reports/YYYY-MM-DD-us.md`에 생성됩니다.

보고서에는 다음 정보가 포함됩니다.

- 자동 선정 종목과 종목별 투자 한도
- 보유 종목 수, 매입금액, 평가금액
- 종목별 평균가, 현재가, 평가손익, 수익률
- 전일 장 종료 스냅샷 대비 평가액 및 미실현 손익 변화
- 당일 자동매매 판단·주문·오류 이벤트

첫 실행일에는 전일 자료가 없어 기준 스냅샷만 저장합니다. 두 번째 거래일부터 전일 대비가 표시됩니다.

> 전일 대비 수치는 장 종료 스냅샷 간 변화입니다. 입출금, 환전, 수수료와 세금이 있으면 순수 매매손익과 다를 수 있습니다.

## 권장 일일 운영 순서

```text
한국 장 종료 → auto eod --market kr
미국 장 종료 → auto eod --market us
개장 전       → 선정 종목 확인 및 auto cycle
장중          → auto watch --confirm-live
```

`auto scheduler`가 장 종료 작업을 자동화합니다. 장중 `watch`는 별도 프로세스로 실행합니다.

## 생성 파일

| 파일 | 용도 |
|---|---|
| `.auto-universe.json` | 자동 선정 종목 |
| `.trader-state.json` | 가상 포지션과 중복 주문 상태 |
| `.execution-state.json` | 시장별 일일 신규투자 사용액 |
| `.autopilot-state.json` | 시장별 마지막 진입 실행일 |
| `trades.jsonl` | 자동매매 이벤트 원본 |
| `reports/YYYY-MM-DD-*.md` | 일간 Markdown 보고서 |
| `reports/snapshots-*.json` | 전일 비교용 계좌 스냅샷 |
| `.kis-token.json` | API 접근 토큰 캐시 |

## 점검 명령

```bash
.venv/bin/python main.py config-check
.venv/bin/python -m unittest discover -s tests -v
```

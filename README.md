# my-trader

한국투자증권 실전 Open API를 이용한 국내주식·미국주식 거래 CLI입니다. 키를 코드나 Git에 저장하지 않습니다.

전체 자동매매 흐름과 운영 방법은 [자동매매 운영 가이드](docs/AUTOMATION.md)를 참고하세요.

완전 자동 실행은 다음 명령을 사용합니다. 기본 `dry_run`에서는 실제 주문이 발생하지 않습니다.

```bash
.venv/bin/python main.py auto autopilot
```

## 지원 기능

- 국내(KRX), 미국(NASDAQ/NYSE/AMEX) 현재가 조회
- 국내·미국 계좌 잔고 조회
- 국내 시장가/지정가 및 미국 지정가 매수·매도
- 국내·미국 미체결 주문 전량 취소
- 주문 확인 플래그, 주문금액 한도, 실전 주문 이중 잠금
- 접근 토큰 로컬 캐시(파일 권한 `0600`)
- 이동평균 진입과 익절·손절 자동매매(dry-run 기본)

미국주식 주문은 우선 지정가만 지원합니다.

## 설정

Python 3.9 이상만 있으면 외부 패키지 없이 실행됩니다.

```bash
cp .env.example .env
```

발급받은 실전투자 앱키, 앱시크릿과 계좌번호를 `.env`에 입력합니다. `.env`와 토큰 캐시는 Git에서 제외됩니다. 조회만 할 때는 `KIS_ENABLE_REAL_TRADING=false`를 유지하세요.

```bash
python3 main.py config-check
```

## 사용법

```bash
# 현재가
python3 main.py quote kr 005930
python3 main.py quote us AAPL --exchange NASDAQ

# 잔고
python3 main.py balance kr
python3 main.py balance us

# 주문: --confirm 없이는 API에 전송되지 않습니다.
python3 main.py order kr buy 005930 1 --price 70000 --confirm
python3 main.py order kr sell 005930 1 --price 0 --confirm
python3 main.py order us buy AAPL 1 --price 150.25 --exchange NASDAQ --confirm

# 주문 응답 또는 미체결 조회에서 받은 번호로 취소
python3 main.py cancel kr 1234567890 1 --organization-number 91234 --confirm
python3 main.py cancel us 1234567890 1 --symbol AAPL --exchange NASDAQ --confirm
```

주문 응답 전체를 JSON으로 출력하므로 `ODNO`(주문번호), 국내의 경우 `KRX_FWDG_ORD_ORGNO`도 보관하세요.

## 실전 주문 잠금

주문과 취소는 `.env`에서 잠금을 풀고 각 명령에 `--confirm`을 넣어야 합니다.

```dotenv
KIS_ENABLE_REAL_TRADING=true
```

처음에는 작은 주문 한도로 시작하세요. 주문 한도는 `KIS_MAX_ORDER_KRW`, `KIS_MAX_ORDER_USD`로 조정할 수 있습니다.

## 자동매매

```bash
python3 main.py auto discover # 전체 시장 순위에서 종목 자동 선정
python3 main.py auto screen   # 선정 종목 하드 필터 결과
python3 main.py auto cycle    # 종목 재선정 후 진입 신호 판단
python3 main.py auto loop --interval 300
```

기본 `dry_run`은 실제 주문 없이 `.trader-state.json`에 가상 보유 상태를 저장하고 `trades.jsonl`에 판단을 기록합니다.
API 요청은 호출 제한을 피하기 위해 기본 1초 간격으로 실행되며 `.env`의 `KIS_MIN_REQUEST_INTERVAL`로 조정할 수 있습니다.

- `fast_period`, `slow_period`: 진입·청산 이동평균
- `take_profit_percent`: 평균 매수가 대비 익절률(기본 2.5%)
- `stop_loss_percent`: 평균 매수가 대비 손절률(기본 1.5%)
- `max_position`: 종목당 최대 매수금액(국내 KRW, 미국 USD)
- `selected_per_market`: 국내·미국에서 각각 자동 선정할 종목 수
- `default_position_krw`, `default_position_usd`: 자동 선정 종목의 최대 매수금액
- `max_active_investment_krw`, `max_active_investment_usd`: 시장별 동시 보유 원가 한도(매도 후 복구)
- `reentry_cooldown_seconds`: 매도 후 같은 종목 재진입 대기시간
- `max_round_trips_per_symbol`: 종목별 하루 최대 왕복매매 횟수
- `max_daily_loss_krw`, `max_daily_loss_usd`: 확정손실 도달 시 당일 신규 진입 중단 한도

긴급 중지는 프로젝트 루트에 `STOP_TRADING` 파일을 만들면 됩니다. 실전 자동주문은 전략의 `execution_mode`을 `live`, `.env`의 `KIS_ENABLE_REAL_TRADING`을 `true`로 바꾸고 실행 시 `--confirm-live`까지 지정해야 합니다.

보유 후 익절·손절은 REST 폴링 대신 WebSocket 실시간 체결가의 매수 1호가를 사용합니다.

```bash
python3 -m pip install -r requirements.txt
python3 main.py auto watch --confirm-live
```

`watch`는 등록된 실제 보유 종목만 구독하며, `+2.5%` 또는 `-1.5%` 도달 시 한 번만 지정가 매도합니다.

`auto_discover=true`이면 종목을 직접 지정할 필요가 없습니다. 국내는 보통주 거래대금 상위, 미국은 NASDAQ·NYSE·AMEX의 거래대금과 시가총액 상위 교집합을 만든 뒤 저가·저유동성·경고·과도한 변동 종목을 제거합니다. 결과는 `.auto-universe.json`에 저장됩니다.

장 종료 후 종목 재선정과 일간 Markdown 보고서를 생성합니다.

```bash
.venv/bin/python main.py auto eod --market kr
.venv/bin/python main.py auto eod --market us
.venv/bin/python main.py auto scheduler
```

## 테스트

```bash
python3 -m unittest discover -s tests -v
```

API 경로와 TR ID는 [한국투자증권 공식 Open Trading API 예제](https://github.com/koreainvestment/open-trading-api)를 기준으로 작성했습니다.

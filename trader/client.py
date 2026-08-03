from __future__ import annotations

import json
import os
import threading
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .config import Settings
from .http import APIError, HTTPTransport


US_QUOTE_EXCHANGES = {"NASDAQ": "NAS", "NYSE": "NYS", "AMEX": "AMS"}
US_ORDER_EXCHANGES = {"NASDAQ": "NASD", "NYSE": "NYSE", "AMEX": "AMEX"}


class SafetyError(ValueError):
    pass


class KISClient:
    def __init__(self, settings: Settings, transport: Optional[HTTPTransport] = None):
        self.settings = settings
        self.transport = transport or HTTPTransport()
        self._access_token: Optional[str] = None
        self._last_request_at = 0.0
        self._request_lock = threading.Lock()

    def _load_cached_token(self) -> Optional[str]:
        path = self.settings.token_cache_path
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if (
                data.get("base_url") == self.settings.base_url
                and data.get("app_key") == self.settings.app_key
                and float(data.get("expires_at", 0)) > time.time() + 60
            ):
                return str(data["access_token"])
        except (FileNotFoundError, ValueError, KeyError, OSError):
            return None
        return None

    def _save_token(self, token: str, expires_in: int) -> None:
        path = self.settings.token_cache_path
        path.write_text(
            json.dumps({"access_token": token, "expires_at": time.time() + expires_in, "base_url": self.settings.base_url, "app_key": self.settings.app_key}),
            encoding="utf-8",
        )
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def authenticate(self) -> str:
        if self._access_token:
            return self._access_token
        cached = self._load_cached_token()
        if cached:
            self._access_token = cached
            return cached
        response = self.transport.request(
            "POST",
            self.settings.base_url + "/oauth2/tokenP",
            {"content-type": "application/json; charset=utf-8"},
            body={"grant_type": "client_credentials", "appkey": self.settings.app_key, "appsecret": self.settings.app_secret},
            timeout=self.settings.timeout_seconds,
        )
        token = response.data.get("access_token")
        if not token:
            raise APIError("접근 토큰 응답에 access_token이 없습니다.")
        self._access_token = str(token)
        self._save_token(self._access_token, int(response.data.get("expires_in", 86400)))
        return self._access_token

    def websocket_approval_key(self) -> str:
        response = self.transport.request(
            "POST", self.settings.base_url + "/oauth2/Approval",
            {"content-type": "application/json; charset=utf-8"},
            body={"grant_type": "client_credentials", "appkey": self.settings.app_key,
                  "secretkey": self.settings.app_secret}, timeout=self.settings.timeout_seconds,
        )
        key = response.data.get("approval_key")
        if not key:
            raise APIError("웹소켓 접속키 응답에 approval_key가 없습니다.")
        return str(key)

    def _headers(self, tr_id: str) -> Dict[str, str]:
        return {
            "content-type": "application/json; charset=utf-8",
            "authorization": "Bearer " + self.authenticate(),
            "appkey": self.settings.app_key,
            "appsecret": self.settings.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }

    def _call(self, method: str, path: str, tr_id: str, *, params: Optional[Mapping[str, Any]] = None, body: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        attempts = 3 if method.upper() == "GET" else 1
        for attempt in range(attempts):
            try:
                with self._request_lock:
                    wait = self.settings.min_request_interval - (time.monotonic() - self._last_request_at)
                    if wait > 0:
                        time.sleep(wait)
                    self._last_request_at = time.monotonic()
                    response = self.transport.request(
                        method, self.settings.base_url + path, self._headers(tr_id), params=params, body=body,
                        timeout=self.settings.timeout_seconds,
                    )
                data = response.data
                if str(data.get("rt_cd", "0")) != "0":
                    raise APIError(str(data.get("msg1", "KIS API 요청 실패")), str(data.get("msg_cd", "")))
                return data
            except APIError as exc:
                message = str(exc).lower()
                retriable = ("초당 거래건수" in message or "timed out" in message or "연결 실패" in message
                             or exc.status == 429 or exc.status >= 500)
                if attempt + 1 >= attempts or not retriable:
                    raise
                time.sleep(1.5 * (attempt + 1))
        raise APIError("KIS API 재시도에 실패했습니다.")

    def domestic_quote(self, symbol: str) -> Dict[str, Any]:
        self._validate_domestic_symbol(symbol)
        data = self._call("GET", "/uapi/domestic-stock/v1/quotations/inquire-price", "FHKST01010100", params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol})
        return dict(data.get("output") or {})

    def us_quote(self, symbol: str, exchange: str) -> Dict[str, Any]:
        exchange = self._us_exchange(exchange, quote=True)
        data = self._call("GET", "/uapi/overseas-price/v1/quotations/price", "HHDFS00000300", params={"AUTH": "", "EXCD": exchange, "SYMB": symbol.upper()})
        return dict(data.get("output") or {})

    def domestic_daily_prices(self, symbol: str, days: int = 60) -> List[Dict[str, Any]]:
        self._validate_domestic_symbol(symbol)
        end = date.today()
        start = end - timedelta(days=max(days * 2, 120))
        data = self._call(
            "GET", "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice", "FHKST03010100",
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol,
                    "FID_INPUT_DATE_1": start.strftime("%Y%m%d"), "FID_INPUT_DATE_2": end.strftime("%Y%m%d"),
                    "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0"},
        )
        return list(data.get("output2") or [])[:days]

    def us_daily_prices(self, symbol: str, exchange: str, days: int = 60) -> List[Dict[str, Any]]:
        data = self._call(
            "GET", "/uapi/overseas-price/v1/quotations/dailyprice", "HHDFS76240000",
            params={"AUTH": "", "EXCD": self._us_exchange(exchange, quote=True), "SYMB": symbol.upper(),
                    "GUBN": "0", "BYMD": "", "MODP": "0"},
        )
        return list(data.get("output2") or [])[:days]

    def domestic_turnover_rank(self) -> List[Dict[str, Any]]:
        data = self._call("GET", "/uapi/domestic-stock/v1/quotations/volume-rank", "FHPST01710000", params={
            "FID_COND_MRKT_DIV_CODE": "J", "FID_COND_SCR_DIV_CODE": "20171", "FID_INPUT_ISCD": "0000",
            "FID_DIV_CLS_CODE": "1", "FID_BLNG_CLS_CODE": "3", "FID_TRGT_CLS_CODE": "111111111",
            "FID_TRGT_EXLS_CLS_CODE": "1111111111", "FID_INPUT_PRICE_1": "0", "FID_INPUT_PRICE_2": "0",
            "FID_VOL_CNT": "0", "FID_INPUT_DATE_1": "0",
        })
        return list(data.get("output") or [])

    def us_turnover_rank(self, exchange: str) -> List[Dict[str, Any]]:
        data = self._call("GET", "/uapi/overseas-stock/v1/ranking/trade-pbmn", "HHDFS76320010", params={
            "EXCD": self._us_exchange(exchange, quote=True), "NDAY": "0", "VOL_RANG": "0", "AUTH": "",
            "KEYB": "", "PRC1": "", "PRC2": "",
        })
        return list(data.get("output2") or [])

    def us_market_cap_rank(self, exchange: str) -> List[Dict[str, Any]]:
        data = self._call("GET", "/uapi/overseas-stock/v1/ranking/market-cap", "HHDFS76350100", params={
            "EXCD": self._us_exchange(exchange, quote=True), "VOL_RANG": "0", "KEYB": "", "AUTH": "", "CURR_GB": "0",
        })
        return list(data.get("output2") or [])

    def domestic_buying_power(self, symbol: str, price: float) -> Dict[str, Any]:
        data = self._call("GET", "/uapi/domestic-stock/v1/trading/inquire-psbl-order", "TTTC8908R", params={
            "CANO": self.settings.account_number, "ACNT_PRDT_CD": self.settings.account_product_code,
            "PDNO": symbol, "ORD_UNPR": self._price(price), "ORD_DVSN": "01",
            "CMA_EVLU_AMT_ICLD_YN": "N", "OVRS_ICLD_YN": "N",
        })
        return dict(data.get("output") or {})

    def us_buying_power(self, symbol: str, exchange: str, price: float) -> Dict[str, Any]:
        data = self._call("GET", "/uapi/overseas-stock/v1/trading/inquire-psamount", "TTTS3007R", params={
            "CANO": self.settings.account_number, "ACNT_PRDT_CD": self.settings.account_product_code,
            "OVRS_EXCG_CD": self._us_exchange(exchange, quote=False), "OVRS_ORD_UNPR": self._price(price),
            "ITEM_CD": symbol.upper(),
        })
        return dict(data.get("output") or {})

    def domestic_open_orders(self) -> List[Dict[str, Any]]:
        data = self._call("GET", "/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl", "TTTC0084R", params={
            "CANO": self.settings.account_number, "ACNT_PRDT_CD": self.settings.account_product_code,
            "INQR_DVSN_1": "0", "INQR_DVSN_2": "0", "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
        })
        return list(data.get("output") or [])

    def us_open_orders(self) -> List[Dict[str, Any]]:
        data = self._call("GET", "/uapi/overseas-stock/v1/trading/inquire-nccs", "TTTS3018R", params={
            "CANO": self.settings.account_number, "ACNT_PRDT_CD": self.settings.account_product_code,
            "OVRS_EXCG_CD": "NASD", "SORT_SQN": "DS", "CTX_AREA_FK200": "", "CTX_AREA_NK200": "",
        })
        return list(data.get("output") or [])

    def domestic_today_orders(self, symbol: str = "") -> List[Dict[str, Any]]:
        today = date.today().strftime("%Y%m%d")
        data = self._call("GET", "/uapi/domestic-stock/v1/trading/inquire-daily-ccld", "TTTC0081R", params={
            "CANO": self.settings.account_number, "ACNT_PRDT_CD": self.settings.account_product_code,
            "INQR_STRT_DT": today, "INQR_END_DT": today, "SLL_BUY_DVSN_CD": "00", "PDNO": symbol,
            "CCLD_DVSN": "00", "INQR_DVSN": "00", "INQR_DVSN_3": "00", "ORD_GNO_BRNO": "",
            "ODNO": "", "INQR_DVSN_1": "", "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
            "EXCG_ID_DVSN_CD": "KRX",
        })
        return list(data.get("output1") or [])

    def us_today_orders(self, symbol: str = "%") -> List[Dict[str, Any]]:
        today = date.today().strftime("%Y%m%d")
        data = self._call("GET", "/uapi/overseas-stock/v1/trading/inquire-ccnl", "TTTS3035R", params={
            "CANO": self.settings.account_number, "ACNT_PRDT_CD": self.settings.account_product_code,
            "PDNO": symbol, "ORD_STRT_DT": today, "ORD_END_DT": today, "SLL_BUY_DVSN": "00",
            "CCLD_NCCS_DVSN": "00", "OVRS_EXCG_CD": "%", "SORT_SQN": "DS", "ORD_DT": "",
            "ORD_GNO_BRNO": "", "ODNO": "", "CTX_AREA_NK200": "", "CTX_AREA_FK200": "",
        })
        return list(data.get("output") or [])

    def domestic_balance(self) -> Dict[str, Any]:
        data = self._call("GET", "/uapi/domestic-stock/v1/trading/inquire-balance", "TTTC8434R", params={
            "CANO": self.settings.account_number, "ACNT_PRDT_CD": self.settings.account_product_code,
            "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02", "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00",
            "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
        })
        return {"positions": data.get("output1") or [], "summary": data.get("output2") or []}

    def us_balance(self) -> Dict[str, Any]:
        data = self._call("GET", "/uapi/overseas-stock/v1/trading/inquire-balance", "TTTS3012R", params={
            "CANO": self.settings.account_number, "ACNT_PRDT_CD": self.settings.account_product_code,
            "OVRS_EXCG_CD": "NASD", "TR_CRCY_CD": "USD", "CTX_AREA_FK200": "", "CTX_AREA_NK200": "",
        })
        return {"positions": data.get("output1") or [], "summary": data.get("output2") or []}

    def domestic_order(self, side: str, symbol: str, quantity: int, price: float, confirmed: bool) -> Dict[str, Any]:
        self._validate_domestic_symbol(symbol)
        self._guard_order(side, quantity, price, self.settings.max_order_krw, confirmed, check_limit=price > 0)
        if price == 0:
            quote = self.domestic_quote(symbol)
            try:
                estimated_price = float(quote["stck_prpr"])
            except (KeyError, TypeError, ValueError) as exc:
                raise SafetyError("시장가 주문의 예상금액을 계산할 현재가가 없습니다.") from exc
            if quantity * estimated_price > self.settings.max_order_krw:
                raise SafetyError(
                    f"예상 주문금액 {quantity * estimated_price:,.0f}원이 설정 한도 "
                    f"{self.settings.max_order_krw:,.0f}원을 초과합니다."
                )
        tr_id = "TTTC0012U" if side == "buy" else "TTTC0011U"
        return self._call("POST", "/uapi/domestic-stock/v1/trading/order-cash", tr_id, body={
            "CANO": self.settings.account_number, "ACNT_PRDT_CD": self.settings.account_product_code,
            "PDNO": symbol, "ORD_DVSN": "01" if price == 0 else "00", "ORD_QTY": str(quantity),
            "ORD_UNPR": self._price(price), "EXCG_ID_DVSN_CD": "KRX", "SLL_TYPE": "01" if side == "sell" else "", "CNDT_PRIC": "",
        })

    def us_order(self, side: str, symbol: str, exchange: str, quantity: int, price: float, confirmed: bool) -> Dict[str, Any]:
        if price <= 0:
            raise SafetyError("미국주식은 현재 지정가 주문만 지원합니다. 0보다 큰 가격을 입력하세요.")
        self._guard_order(side, quantity, price, self.settings.max_order_usd, confirmed)
        order_exchange = self._us_exchange(exchange, quote=False)
        tr_id = "TTTT1002U" if side == "buy" else "TTTT1006U"
        return self._call("POST", "/uapi/overseas-stock/v1/trading/order", tr_id, body={
            "CANO": self.settings.account_number, "ACNT_PRDT_CD": self.settings.account_product_code,
            "OVRS_EXCG_CD": order_exchange, "PDNO": symbol.upper(), "ORD_QTY": str(quantity),
            "OVRS_ORD_UNPR": self._price(price), "CTAC_TLNO": "", "MGCO_APTM_ODNO": "",
            "SLL_TYPE": "00" if side == "sell" else "", "ORD_SVR_DVSN_CD": "0", "ORD_DVSN": "00",
        })

    def domestic_cancel(self, order_number: str, organization_number: str, quantity: int, confirmed: bool) -> Dict[str, Any]:
        self._guard_action(confirmed)
        if quantity <= 0:
            raise SafetyError("취소 수량은 1 이상이어야 합니다.")
        return self._call("POST", "/uapi/domestic-stock/v1/trading/order-rvsecncl", "TTTC0013U", body={
            "CANO": self.settings.account_number, "ACNT_PRDT_CD": self.settings.account_product_code,
            "KRX_FWDG_ORD_ORGNO": organization_number, "ORGN_ODNO": order_number, "ORD_DVSN": "00",
            "RVSE_CNCL_DVSN_CD": "02", "ORD_QTY": str(quantity), "ORD_UNPR": "0", "QTY_ALL_ORD_YN": "Y",
            "EXCG_ID_DVSN_CD": "KRX",
        })

    def us_cancel(self, order_number: str, symbol: str, exchange: str, quantity: int, confirmed: bool) -> Dict[str, Any]:
        self._guard_action(confirmed)
        if quantity <= 0:
            raise SafetyError("취소 수량은 1 이상이어야 합니다.")
        return self._call("POST", "/uapi/overseas-stock/v1/trading/order-rvsecncl", "TTTT1004U", body={
            "CANO": self.settings.account_number, "ACNT_PRDT_CD": self.settings.account_product_code,
            "OVRS_EXCG_CD": self._us_exchange(exchange, quote=False), "PDNO": symbol.upper(), "ORGN_ODNO": order_number,
            "RVSE_CNCL_DVSN_CD": "02", "ORD_QTY": str(quantity), "OVRS_ORD_UNPR": "0",
            "MGCO_APTM_ODNO": "", "ORD_SVR_DVSN_CD": "0",
        })

    def _guard_action(self, confirmed: bool) -> None:
        if not confirmed:
            raise SafetyError("주문 전송에는 --confirm 옵션이 필요합니다.")
        if not self.settings.enable_real_trading:
            raise SafetyError("실전 주문이 잠겨 있습니다. KIS_ENABLE_REAL_TRADING=true가 필요합니다.")

    def _guard_order(self, side: str, quantity: int, price: float, limit: float, confirmed: bool, check_limit: bool = True) -> None:
        if side not in {"buy", "sell"}:
            raise SafetyError("side는 buy 또는 sell이어야 합니다.")
        if quantity <= 0 or price < 0:
            raise SafetyError("수량은 1 이상, 가격은 0 이상이어야 합니다.")
        self._guard_action(confirmed)
        if check_limit and price > 0 and quantity * price > limit:
            raise SafetyError(f"주문금액 {quantity * price:,.2f}이 설정 한도 {limit:,.2f}을 초과합니다.")

    @staticmethod
    def _validate_domestic_symbol(symbol: str) -> None:
        if not (symbol.isdigit() and len(symbol) in {6, 7}):
            raise ValueError("국내 종목코드는 숫자 6자리(ETN은 7자리)여야 합니다.")

    @staticmethod
    def _us_exchange(exchange: str, quote: bool) -> str:
        mapping = US_QUOTE_EXCHANGES if quote else US_ORDER_EXCHANGES
        key = exchange.upper()
        if key not in mapping:
            raise ValueError("미국 거래소는 NASDAQ, NYSE, AMEX 중 하나여야 합니다.")
        return mapping[key]

    @staticmethod
    def _price(value: float) -> str:
        return (f"{value:.4f}").rstrip("0").rstrip(".") or "0"

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional


class ConfigurationError(ValueError):
    pass


def _read_dotenv(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class Settings:
    app_key: str
    app_secret: str
    account_number: str
    account_product_code: str = "01"
    enable_real_trading: bool = False
    max_order_krw: int = 1_000_000
    max_order_usd: float = 1_000.0
    timeout_seconds: float = 10.0
    min_request_interval: float = 1.0
    token_cache_path: Path = Path(".kis-token.json")

    @property
    def base_url(self) -> str:
        return "https://openapi.koreainvestment.com:9443"

    @classmethod
    def from_env(cls, env_path: Optional[Path] = None) -> "Settings":
        path = env_path or Path(".env")
        values = _read_dotenv(path)

        def get(name: str, default: str = "") -> str:
            return os.environ.get(name, values.get(name, default)).strip()

        settings = cls(
            app_key=get("KIS_APP_KEY"),
            app_secret=get("KIS_APP_SECRET"),
            account_number=get("KIS_ACCOUNT_NUMBER"),
            account_product_code=get("KIS_ACCOUNT_PRODUCT_CODE", "01"),
            enable_real_trading=_bool(get("KIS_ENABLE_REAL_TRADING", "false")),
            max_order_krw=int(get("KIS_MAX_ORDER_KRW", "1000000")),
            max_order_usd=float(get("KIS_MAX_ORDER_USD", "1000")),
            timeout_seconds=float(get("KIS_TIMEOUT_SECONDS", "10")),
            min_request_interval=float(get("KIS_MIN_REQUEST_INTERVAL", "1.0")),
            token_cache_path=Path(get("KIS_TOKEN_CACHE", ".kis-token.json")),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        missing = [
            name
            for name, value in (
                ("KIS_APP_KEY", self.app_key),
                ("KIS_APP_SECRET", self.app_secret),
                ("KIS_ACCOUNT_NUMBER", self.account_number),
            )
            if not value
        ]
        if missing:
            raise ConfigurationError("필수 설정이 없습니다: " + ", ".join(missing))
        if not (self.account_number.isdigit() and len(self.account_number) == 8):
            raise ConfigurationError("KIS_ACCOUNT_NUMBER는 계좌번호 앞 8자리여야 합니다.")
        if not (self.account_product_code.isdigit() and len(self.account_product_code) == 2):
            raise ConfigurationError("KIS_ACCOUNT_PRODUCT_CODE는 계좌번호 뒤 2자리여야 합니다.")
        if self.max_order_krw <= 0 or self.max_order_usd <= 0:
            raise ConfigurationError("최대 주문 금액은 0보다 커야 합니다.")
        if self.min_request_interval < 0:
            raise ConfigurationError("KIS_MIN_REQUEST_INTERVAL은 0 이상이어야 합니다.")

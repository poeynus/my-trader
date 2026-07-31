from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional


class APIError(RuntimeError):
    def __init__(self, message: str, code: str = "", status: int = 0):
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass
class Response:
    data: Dict[str, Any]
    headers: Mapping[str, str]


class HTTPTransport:
    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        params: Optional[Mapping[str, Any]] = None,
        body: Optional[Mapping[str, Any]] = None,
        timeout: float = 10.0,
    ) -> Response:
        if params:
            url += "?" + urllib.parse.urlencode(params)
        encoded = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(url, data=encoded, headers=dict(headers), method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
                return Response(json.loads(raw), dict(response.headers.items()))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {}
            message = payload.get("msg1") or payload.get("error_description") or raw or str(exc)
            raise APIError(message, str(payload.get("msg_cd", "")), exc.code) from exc
        except urllib.error.URLError as exc:
            raise APIError(f"KIS API 연결 실패: {exc.reason}") from exc

# etl/common/adapters/cardano/koios.py
from __future__ import annotations
from typing import Any, Dict, Optional
from etl.utils.http import HttpClient

DEFAULT_BASE = "https://api.koios.rest/api/v1"

class KoiosClient:
    def __init__(self, http: HttpClient, base_url: Optional[str] = None):
        self.http = http
        self.base = (base_url or DEFAULT_BASE).rstrip("/")

    def _url(self, endpoint: str) -> str:
        endpoint = endpoint.lstrip("/")
        return f"{self.base}/{endpoint}"

    def post(self,
             endpoint: str,
             payload: Dict[str, Any],
             *,
             ctx: Optional[Dict[str, Any]] = None
        ) -> Any:
        url = self._url(endpoint)
        try:
            return self.http.post_json(url, payload=payload)
        except Exception as e:
            # Direct lower-level logging in KoiosClient
            if hasattr(self.http, "log") and self.http.log:
                self.http.log.exception(
                    "koios_post_error",
                    extra={
                        "job":"etl-adapter",
                        "step":"koios_post",
                        "endpoint":endpoint,
                        **(ctx or {})
                    }
                )
            raise

    def get(self,
            endpoint: str,
            params: Optional[Dict[str, Any]] = None,
            *,
            ctx: Optional[Dict[str, Any]] = None
        ) -> Any:
        url = self._url(endpoint)
        try:
            return self.http.get_json(url, params=params)
        except Exception as e:
            if hasattr(self.http, "log") and self.http.log:
                self.http.log.exception(
                    "koios_get_error",
                    extra={
                        "job":"etl-adapter",
                        "step":"koios_get",
                        "endpoint":endpoint,
                        **(ctx or {})
                    }
                )
            raise

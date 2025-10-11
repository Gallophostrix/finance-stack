# etl/utils/http.py
from __future__ import annotations
import time
import random
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Optional, Tuple
import requests

# HTTPS status codes for which we retry
_RETRY_STATUSES = {429, 500, 502, 503, 504}

class HttpError(RuntimeError):
    def __init__(self, method: str, url: str, status: int, body: str):
        super().__init__(f"{method} {url} -> {status}: {body[:200]}")
        self.method, self.url, self.status, self.body = method, url, status, body

class HttpClient:
    """
    Common HTTP client:
    - Keep-alive session
    - Default timeouts
    - Retries (exponential backoff + jitter) on network errors and 5xx/429
    - Stable User-Agent
    - Optional logging (pass a logger; otherwise silent)
    """
    def __init__(
        self,
        timeout: float = 15.0,
        retries: int = 3,
        backoff: float = 1.6,
        ua: str = "finance-etl/1.0 (+https://localhost)",
        logger: Optional[Any] = None,
        max_rps: Optional[float] = None,
    ):
        self._s = requests.Session()
        self._s.headers.update({"User-Agent": ua})
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.ua = ua
        self.log = logger
        # Rate limiting
        self._min_interval = (1.0 / max_rps) if (max_rps and max_rps > 0) else None
        self._last_request_ts: Optional[float] = None

    def close(self):
        self._s.close()

    # ---------- Helpers ----------
    def _sleep(self, attempt: int):
        # Exponential backoff with jitter (to avoid thundering herd)
        base = (self.backoff ** attempt)
        jitter = 0.25 + random.random() * 0.5  # 0.25–0.75
        time.sleep(base * jitter)

    def _respect_rate_limit(self):
        if self._min_interval is None:
            return
        now = time.perf_counter()
        if self._last_request_ts is None:
            self._last_request_ts = now
            return
        elapsed = now - self._last_request_ts
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request_ts = time.perf_counter()

    def _retry_after_delay(self, resp: requests.Response) -> Optional[float]:
        ra = resp.headers.get("Retry-After")
        if not ra:
            return None
        # 1st Format: seconds
        try:
            return float(ra)
        except ValueError:
            pass
        # 2nd Format: HTTP-date
        try:
            dt = parsedate_to_datetime(ra)
            # parsedate_to_datetime returns an aware dt in UTC (often)
            delay = (dt - parsedate_to_datetime(time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime()))).total_seconds()
            return max(0.0, delay)
        except Exception:
            return None

    def _do_request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
        allow_statuses: Tuple[int, ...] = (200,),
    ) -> requests.Response:
        hdrs = {}
        if headers:
            hdrs.update(headers)

        last_exc = None
        for attempt in range(self.retries + 1):
            self._respect_rate_limit()
            t0 = time.perf_counter()
            try:
                resp = self._s.request(
                    method=method.upper(),
                    url=url,
                    params=params,
                    json=json,
                    headers=hdrs if hdrs else None,
                    timeout=timeout or self.timeout,
                )
                dur_ms = int((time.perf_counter() - t0) * 1000)

                if self.log:
                    extra = {"step": "http_request", "method": method.upper(),
                             "url": url, "status": resp.status_code, "duration_ms": dur_ms, "attempt": attempt}
                    self.log.info("http", extra=extra)

                # OK status check
                if resp.status_code in allow_statuses:
                    return resp

                # Retryable status
                if resp.status_code in _RETRY_STATUSES and attempt < self.retries:
                    # Uses Retry-After if present
                    ra = self._retry_after_delay(resp)
                    if ra is not None:
                        time.sleep(ra)
                    else:
                        self._sleep(attempt)
                    continue

                # Else: error
                raise HttpError(method, url, resp.status_code, resp.text)

            except (requests.RequestException, HttpError) as e:
                last_exc = e
                if self.log:
                    self.log.warning("http_error", extra={"step":"http_error","method":method.upper(),
                                                          "url":url,"attempt":attempt})
                if attempt < self.retries:
                    self._sleep(attempt)
                    continue
                # plus de retries
                if isinstance(e, HttpError):
                    raise
                raise RuntimeError(f"{method} {url} failed: {e}") from e

        # Should not reach here
        raise RuntimeError(f"{method} {url} failed: {last_exc}")

    # ---------- Public API ----------
    def get_json(
        self, url: str, *,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
        allow_statuses: Tuple[int, ...] = (200,)
    ) -> Dict[str, Any]:
        resp = self._do_request("GET", url, params=params, headers=headers,
                                timeout=timeout, allow_statuses=allow_statuses)
        try:
            return resp.json()
        except ValueError as e:
            raise HttpError("GET", url, resp.status_code, resp.text) from e

    def post_json(
        self, url: str, *,
        payload: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
        allow_statuses: Tuple[int, ...] = (200,)
    ) -> Dict[str, Any]:
        hdrs = {"Content-Type": "application/json"}
        if headers:
            hdrs.update(headers)
        resp = self._do_request("POST", url, json=payload, headers=hdrs,
                                timeout=timeout, allow_statuses=allow_statuses)
        try:
            return resp.json()
        except ValueError as e:
            raise HttpError("POST", url, resp.status_code, resp.text) from e

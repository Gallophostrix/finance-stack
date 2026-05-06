"""
HTTP client with rate limiting, retry, and structured logging.
"""

import logging
import time
from typing import Any, Optional

import httpx

log = logging.getLogger("root")


class HttpError(Exception):
    def __init__(self, method: str, url: str, status: int, body: str):
        self.method = method
        self.url = url
        self.status = status
        self.body = body
        super().__init__(f"{method} {url} -> {status}: {body[:200]}")


class RateLimiter:
    """Token bucket — enforces max N requests per second."""

    def __init__(self, max_rps: float):
        self.min_interval = 1.0 / max_rps
        self._last: float = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        wait = self.min_interval - (now - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()


class HttpClient:
    """
    Thin wrapper around httpx with:
    - Rate limiting (max_rps)
    - Automatic retry with exponential backoff on 429 / 5xx
    - Structured logging per request
    """

    def __init__(
        self,
        max_rps: float = 5.0,
        max_retries: int = 4,
        timeout: float = 30.0,
        headers: Optional[dict] = None,
    ):
        self._limiter = RateLimiter(max_rps)
        self._max_retries = max_retries
        self._client = httpx.Client(
            timeout=timeout,
            headers=headers or {},
            follow_redirects=True,
        )

    def get(
        self,
        url: str,
        params: Optional[dict] = None,
        headers: Optional[dict] = None,
    ) -> httpx.Response:
        """
        GET with rate limiting and retry.
        Raises HttpError on non-2xx after all retries.
        """
        last_err: Optional[HttpError] = None

        for attempt in range(1, self._max_retries + 1):
            self._limiter.wait()
            t0 = time.monotonic()
            try:
                resp = self._client.get(url, params=params, headers=headers)
                duration_ms = int((time.monotonic() - t0) * 1000)

                log.info(
                    "http_request",
                    extra={
                        "method": "GET",
                        "url": url,
                        "status": resp.status_code,
                        "duration_ms": duration_ms,
                        "attempt": attempt,
                    },
                )

                if resp.status_code < 300:
                    return resp

                # Retryable
                if resp.status_code in {429, 500, 502, 503, 504}:
                    last_err = HttpError("GET", url, resp.status_code, resp.text)
                    wait = 2**attempt
                    log.warning(
                        "http_retrying",
                        extra={
                            "status": resp.status_code,
                            "attempt": attempt,
                            "wait_s": wait,
                        },
                    )
                    time.sleep(wait)
                    continue

                # Non-retryable
                raise HttpError("GET", url, resp.status_code, resp.text)

            except httpx.RequestError as e:
                last_err = HttpError("GET", url, 0, str(e))
                log.warning(
                    "http_network_error",
                    extra={"attempt": attempt, "error": str(e)},
                )
                time.sleep(2**attempt)

        raise last_err or HttpError("GET", url, 0, "unknown error")

    def get_json(
        self,
        url: str,
        params: Optional[dict] = None,
        headers: Optional[dict] = None,
    ) -> Any:
        resp = self.get(url, params=params, headers=headers)
        return resp.json()

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def post_json(
        self,
        url: str,
        body: dict,
        headers: Optional[dict] = None,
    ) -> Any:
        """POST JSON with rate limiting and retry."""
        last_err: Optional[HttpError] = None

        for attempt in range(1, self._max_retries + 1):
            self._limiter.wait()
            t0 = time.monotonic()
            try:
                resp = self._client.post(url, json=body, headers=headers)
                duration_ms = int((time.monotonic() - t0) * 1000)

                log.info(
                    "http_request",
                    extra={
                        "method": "POST",
                        "url": url,
                        "status": resp.status_code,
                        "duration_ms": duration_ms,
                        "attempt": attempt,
                    },
                )

                if resp.status_code < 300:
                    return resp.json()

                if resp.status_code in {429, 500, 502, 503, 504}:
                    last_err = HttpError("POST", url, resp.status_code, resp.text)
                    wait = 2**attempt
                    log.warning(
                        "http_retrying",
                        extra={
                            "status": resp.status_code,
                            "attempt": attempt,
                            "wait_s": wait,
                        },
                    )
                    time.sleep(wait)
                    continue

                raise HttpError("POST", url, resp.status_code, resp.text)

            except httpx.RequestError as e:
                last_err = HttpError("POST", url, 0, str(e))
                log.warning(
                    "http_network_error", extra={"attempt": attempt, "error": str(e)}
                )
                time.sleep(2**attempt)

        raise last_err or HttpError("POST", url, 0, "unknown error")

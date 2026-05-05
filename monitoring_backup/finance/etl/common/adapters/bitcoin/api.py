# etl/common/adapters/bitcoin/api.py
from __future__ import annotations
from typing import Any, Dict, Optional
from etl.utils.http import HttpClient

ASSET_CODE = "BTC"

def addr_payload(
        http: HttpClient,
        base_url: str,
        addr: str,
        token: Optional[str],
        min_confs: Optional[int]
) -> Dict[str, Any]:
    """
    Query the BlockCypher API for a given Bitcoin address.

    Args:
        http:        Shared HttpClient (handles timeout, retries, rate-limit, logging)
        base_url:    Base API URL (e.g. "https://api.blockcypher.com/v1/btc/main")
        addr:        Bitcoin address (bech32 or legacy)
        token:       Optional BlockCypher API token
        min_confs:   Optional minimum number of confirmations to include

    Returns:
        Decoded JSON payload (Python dict) from the API response.

    Raises:
        ValueError: If the address is empty.
        HttpError:  If the HTTP request fails or returns a non-200 status code.
    """
    addr = (addr or "").strip()
    if not addr:
        if hasattr(http, "log") and http.log:
            http.log.error(
                "empty_address",
                extra={
                    "job": "etl-adapter",
                    "step": "addr_payload",
                    "asset": ASSET_CODE
                }
            )
        raise ValueError("BTC address is empty")
    
    params: Dict[str, Any] = {}
    if token:
        params["token"] = token
    if min_confs is not None:
        params["confirmations"] = int(min_confs)
    
    url = f"{base_url.rstrip('/')}/addrs/{addr}"

    try:
        return http.get_json(url, params=params)
    except Exception:
        if hasattr(http, "log") and http.log:
            http.log.exception(
                "btc_addr_payload_error",
                extra={
                    "job":"etl-adapter",
                    "step":"addr_payload",
                    "asset":ASSET_CODE,
                    "addr_tail": addr[-6:]
                }
            )
        raise
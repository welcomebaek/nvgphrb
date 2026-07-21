"""Fetch the live current price of Samsung Electronics (005930) via the KIS Open API.

Uses the official KIS (Korea Investment & Securities) Open API only:
  - OAuth access-token issuance: POST /oauth2/tokenP
  - Domestic stock current price:  GET /uapi/domestic-stock/v1/quotations/inquire-price
    (tr_id: FHKST01010100)

Credentials are read from .env (KIS_APP_KEY / KIS_APP_SECRET / KIS_URL_REST -
the real-trading key pair, used here only for a read-only public market-data
lookup). The issued OAuth token is cached in .kis_token_cache.json next to
this script so repeated runs don't re-issue a token and hit KIS's throttling.

Shared primitives (secret sanitization, credential loading, token cache,
token issuance) live in kis_common.py so the same code also serves the
paper-trading flow in order_buy_005930.py.

Run with: uv run kis_price.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from kis_common import KisApiError, REQUEST_TIMEOUT_SECONDS, get_access_token, load_credentials, sanitize

STOCK_CODE = "005930"
MARKET_DIV_CODE = "J"  # KRX
PRICE_URL_PATH = "/uapi/domestic-stock/v1/quotations/inquire-price"
TR_ID_INQUIRE_PRICE = "FHKST01010100"

SCRIPT_DIR = Path(__file__).resolve().parent
TOKEN_CACHE_PATH = SCRIPT_DIR / ".kis_token_cache.json"


def get_current_price(
    access_token: str,
    app_key: str,
    app_secret: str,
    base_url: str,
    stock_code: str = STOCK_CODE,
) -> dict[str, Any]:
    """Call the KIS domestic-stock current-price endpoint and return `output`."""
    url = f"{base_url}{PRICE_URL_PATH}"
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/plain",
        "charset": "UTF-8",
        "authorization": f"Bearer {access_token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "tr_id": TR_ID_INQUIRE_PRICE,
        "custtype": "P",
        "tr_cont": "",
    }
    params = {
        "FID_COND_MRKT_DIV_CODE": MARKET_DIV_CODE,
        "FID_INPUT_ISCD": stock_code,
    }

    secrets = [access_token, app_key, app_secret]

    try:
        resp = httpx.get(
            url, headers=headers, params=params, timeout=REQUEST_TIMEOUT_SECONDS
        )
    except httpx.RequestError as e:
        raise KisApiError(
            f"Network error while requesting current price: {sanitize(str(e), secrets)}"
        ) from None

    if resp.status_code != 200:
        raise KisApiError(
            "Current price lookup failed with HTTP "
            f"{resp.status_code}: {sanitize(resp.text, secrets)[:300]}"
        )

    try:
        payload = resp.json()
    except json.JSONDecodeError:
        raise KisApiError("Current price lookup returned non-JSON response") from None

    if payload.get("rt_cd") != "0":
        msg = payload.get("msg1", "unknown error")
        raise KisApiError(
            f"Current price lookup rejected by KIS: {sanitize(str(msg), secrets)}"
        )

    output = payload.get("output")
    if not isinstance(output, dict) or "stck_prpr" not in output:
        raise KisApiError("Current price lookup response missing expected 'output.stck_prpr'")

    return output


def format_price_output(output: dict[str, Any]) -> str:
    price = int(output["stck_prpr"])
    diff = int(output.get("prdy_vrss", 0))
    rate = output.get("prdy_ctrt", "0")
    sign_code = output.get("prdy_vrss_sign", "3")  # 3 = flat, 5 = down, 2 = up (KIS convention)
    sign = "+" if sign_code in ("1", "2") else ("-" if sign_code in ("4", "5") else "")
    queried_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return (
        f"삼성전자(005930) 현재가: {price:,}원 "
        f"(전일대비 {sign}{abs(diff):,}원, {rate}%) "
        f"- 조회시각 {queried_at}"
    )


def main() -> int:
    try:
        creds = load_credentials(["KIS_APP_KEY", "KIS_APP_SECRET", "KIS_URL_REST"])
        base_url = creds["KIS_URL_REST"]
        token = get_access_token(
            creds["KIS_APP_KEY"], creds["KIS_APP_SECRET"], base_url, TOKEN_CACHE_PATH
        )
        output = get_current_price(
            token, creds["KIS_APP_KEY"], creds["KIS_APP_SECRET"], base_url
        )
        print(format_price_output(output))
        return 0
    except KisApiError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - top-level safety net, sanitized
        print(f"Unexpected error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

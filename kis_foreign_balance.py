"""Fetch the real-account foreign-currency deposit balance via the KIS Open API.

Uses the official KIS (Korea Investment & Securities) Open API only:
  - OAuth access-token issuance: POST /oauth2/tokenP
  - 해외증거금 통화별조회 (foreign margin/deposit by currency): GET
    /uapi/overseas-stock/v1/trading/foreign-margin  (tr_id: TTTC2101R, real
    trading only - confirmed via kis-code-assistant-mcp against the official
    koreainvestment/open-trading-api examples_llm/overseas_stock/foreign_margin
    sample, which calls this tr_id unconditionally with no demo/paper
    variant). This endpoint was chosen over 해외주식 체결기준현재잔고
    (inquire-present-balance) because it needs only CANO/ACNT_PRDT_CD (no
    market/country filters) and its response's primary field
    (frcr_dncl_amt1 = 외화예수금액) is exactly the foreign-currency deposit
    balance requested - the simplest correct endpoint for "show me my
    foreign currency deposit balance". Note: KIS returns `output` as one
    row per (country, currency) market pair the account can trade in, so a
    single USD balance is repeated once per USD-denominated market (~10
    rows), plus a batch of blank padding rows filling out a fixed-size
    array; summarize_by_currency() collapses this down to one line per
    distinct currency code (see its docstring for detail).

Credentials are read from .env (KIS_APP_KEY / KIS_APP_SECRET / KIS_URL_REST /
KIS_ACCT_STOCK - the real-trading key pair and account number). This is a
pure read-only account inquiry with no side effects. The issued OAuth token
is cached in .kis_token_cache.json next to this script, reusing the same
cache kis_price.py already uses for this same credential set.

Shared primitives (secret sanitization, credential loading, token cache,
token issuance) live in kis_common.py.

Run with: uv run kis_foreign_balance.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from kis_common import KisApiError, REQUEST_TIMEOUT_SECONDS, get_access_token, load_credentials, sanitize

FOREIGN_MARGIN_URL_PATH = "/uapi/overseas-stock/v1/trading/foreign-margin"
TR_ID_FOREIGN_MARGIN = "TTTC2101R"
ACNT_PRDT_CD = "01"  # fixed product code, per project convention (see order_buy_005930_real.py)

SCRIPT_DIR = Path(__file__).resolve().parent
TOKEN_CACHE_PATH = SCRIPT_DIR / ".kis_token_cache.json"


def get_foreign_margin(
    access_token: str,
    app_key: str,
    app_secret: str,
    base_url: str,
    cano: str,
    acnt_prdt_cd: str = ACNT_PRDT_CD,
) -> list[dict[str, Any]]:
    """Call the KIS 해외증거금 통화별조회 endpoint and return `output` as a list."""
    url = f"{base_url}{FOREIGN_MARGIN_URL_PATH}"
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/plain",
        "charset": "UTF-8",
        "authorization": f"Bearer {access_token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "tr_id": TR_ID_FOREIGN_MARGIN,
        "custtype": "P",
        "tr_cont": "",
    }
    params = {
        "CANO": cano,
        "ACNT_PRDT_CD": acnt_prdt_cd,
    }

    secrets = [access_token, app_key, app_secret]

    try:
        resp = httpx.get(
            url, headers=headers, params=params, timeout=REQUEST_TIMEOUT_SECONDS
        )
    except httpx.RequestError as e:
        raise KisApiError(
            f"Network error while requesting foreign-currency balance: {sanitize(str(e), secrets)}"
        ) from None

    if resp.status_code != 200:
        raise KisApiError(
            "Foreign-currency balance lookup failed with HTTP "
            f"{resp.status_code}: {sanitize(resp.text, secrets)[:300]}"
        )

    try:
        payload = resp.json()
    except json.JSONDecodeError:
        raise KisApiError(
            "Foreign-currency balance lookup returned non-JSON response"
        ) from None

    if payload.get("rt_cd") != "0":
        msg = payload.get("msg1", "unknown error")
        raise KisApiError(
            f"Foreign-currency balance lookup rejected by KIS: {sanitize(str(msg), secrets)}"
        )

    output = payload.get("output")
    if output is None:
        raise KisApiError("Foreign-currency balance lookup response missing 'output'")

    if isinstance(output, list):
        return output
    if isinstance(output, dict):
        return [output]
    raise KisApiError("Foreign-currency balance lookup returned unexpected 'output' shape")


def summarize_by_currency(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse the raw per-market rows down to one entry per currency code.

    KIS returns this endpoint's `output` as one row per (country, currency)
    market pair the account can trade in - e.g. a single USD cash balance
    is repeated once for each of the ~10 USD-denominated markets (US,
    China, UK, Germany, ...). The tail of the fixed-size response array is
    also padded with rows that have every field blank. For a "what's my
    foreign currency deposit balance" summary, only the distinct currency
    codes matter, so this dedupes on crcy_cd (first occurrence wins - the
    deposit amount for a given currency is identical across its repeated
    market rows) and drops the blank padding rows.
    """
    seen: dict[str, dict[str, Any]] = {}
    market_counts: dict[str, int] = {}
    for row in rows:
        crcy_cd = (row.get("crcy_cd") or "").strip()
        if not crcy_cd:
            continue  # blank padding row
        market_counts[crcy_cd] = market_counts.get(crcy_cd, 0) + 1
        if crcy_cd not in seen:
            seen[crcy_cd] = row

    result = []
    for crcy_cd, row in seen.items():
        result.append(
            {
                "crcy_cd": crcy_cd,
                "frcr_dncl_amt1": row.get("frcr_dncl_amt1"),
                "market_count": market_counts[crcy_cd],
            }
        )
    return result


def format_balance_output(rows: list[dict[str, Any]]) -> str:
    queried_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header = f"외화 예수금 현황 - 조회시각 {queried_at}"

    currencies = summarize_by_currency(rows)
    if not currencies:
        return header + "\n  보유 외화 예수금 없음 (0)"

    lines = [header]
    for entry in currencies:
        amount_raw = entry["frcr_dncl_amt1"]
        try:
            amount = f"{float(amount_raw):,.2f}"
        except (TypeError, ValueError):
            amount = str(amount_raw)
        note = f" (거래가능 시장 {entry['market_count']}곳)" if entry["market_count"] > 1 else ""
        lines.append(f"  {entry['crcy_cd']}: {amount}{note}")

    return "\n".join(lines)


def main() -> int:
    try:
        creds = load_credentials(
            ["KIS_APP_KEY", "KIS_APP_SECRET", "KIS_URL_REST", "KIS_ACCT_STOCK"]
        )
        base_url = creds["KIS_URL_REST"]
        token = get_access_token(
            creds["KIS_APP_KEY"], creds["KIS_APP_SECRET"], base_url, TOKEN_CACHE_PATH
        )
        rows = get_foreign_margin(
            token,
            creds["KIS_APP_KEY"],
            creds["KIS_APP_SECRET"],
            base_url,
            creds["KIS_ACCT_STOCK"],
        )
        print(format_balance_output(rows))
        return 0
    except KisApiError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - top-level safety net, sanitized
        print(f"Unexpected error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

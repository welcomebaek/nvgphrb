"""Place a market BUY order for 1 share of Samsung Electronics (005930) via
the KIS REAL/실전투자 trading environment.

*** THIS PLACES A REAL ORDER AGAINST REAL MONEY WHEN RUN WITH --live. ***

SAFETY (read this before running):
  - Default behavior (no flags) is a DRY RUN: this script builds the exact
    HTTP request (URL, tr_id, sanitized headers, body) that WOULD be sent
    and logs/prints it, but makes NO network call to the order-cash
    endpoint at all.
  - Only `uv run order_buy_005930_real.py --live` actually obtains a token
    and submits a real (REAL-MONEY) order. Never pass --live casually.

This script is REAL-TRADING-ONLY. It reads exclusively:
  KIS_APP_KEY / KIS_APP_SECRET / KIS_URL_REST / KIS_ACCT_STOCK
from .env (the same real-trading credential pair kis_price.py already uses
successfully for read-only price lookups). It must never reference the
paper/모의투자 credential env vars or the paper-environment order tr_id (see
kis_common.py's module docstring and order_buy_005930.py for those names) -
only real-trading symbols may appear in this file. This is intentionally a
separate file from order_buy_005930.py (the paper-account version, kept
as-is and unmodified) so there is zero ambiguity about which environment a
given script targets.

Token cache: this script shares kis_price.py's .kis_token_cache.json
(rather than using a dedicated cache file). Both scripts authenticate with
the exact same KIS_APP_KEY/KIS_APP_SECRET/KIS_URL_REST triple against the
same OAuth endpoint, so a token issued for one is valid for the other -
sharing avoids redundant token issuance (and KIS throttles token issuance).
The cache format written/read is get_access_token's own
(access_token/issued_at/expires_in), so there is no format mismatch risk.

--- KIS API details, confirmed via the kis-code-assistant-mcp MCP server ---
(search_domestic_stock_api / read_source_code against
 koreainvestment/open-trading-api, examples_llm/domestic_stock/*)

Endpoint (order_cash.py, "주식주문(현금)"):
  POST /uapi/domestic-stock/v1/trading/order-cash
  tr_id: env_dv=="real" and ord_dv=="buy" -> "TTTC0012U" (used here)
         Read directly from order_cash.py's own tr_id-selection branch:
             if env_dv == "real":
                 if ord_dv == "sell": tr_id = "TTTC0011U"
                 elif ord_dv == "buy": tr_id = "TTTC0012U"
         (the letter-swap pattern guessed from the paper script's buy tr_id
         was verified, not assumed - order_cash.py's env_dv=="demo" branch,
         read in the same source fetch, confirms the paper-environment buy
         tr_id used by order_buy_005930.py is indeed this function's demo
         counterpart, giving high confidence these are a matched real/demo
         pair defined side-by-side in one function rather than coincidence.)
         (the env_dv=="real" sell tr_id, TTTC0011U, is a different value and
         is intentionally not used or named anywhere else in this file.)

ORD_DVSN ("01" = 시장가/market order): confirmed as real-trading-applicable
(NOT paper-specific) via TWO official docstrings, one of which explicitly
demonstrates env_dv="real" in its own worked example:
  - order_resv.py: "ord_dvsn_cd ... (00 : 지정가, 01 : 시장가, 02 : 조건부지정가, 05 : 장전 시간외)"
  - inquire_psbl_order.py: "ord_dvsn (str): [필수] 주문구분 (ex. 01 : 시장가)",
    and its own Example line calls inquire_psbl_order(env_dv="real", ...,
    ord_dvsn="01", ...) - i.e. the official sample explicitly pairs
    env_dv="real" with ord_dvsn="01".

ORD_UNPR for a market order = "0": order_resv.py docstring - "ord_unpr ...
(1주당 가격, 시장가/장전 시간외는 0 입력)". order_cash.py's own docstring
also notes an order with no ORD_UNPR is priced at the upper limit until
filled, consistent with using "0" for market orders.

EXCG_ID_DVSN_CD: order_cash's own official TEST example (chk_order_cash.py)
calls order_cash(env_dv="real", ord_dv="sell", ..., excg_id_dvsn_cd="SOR"),
i.e. the officially-published real-trading test case uses "SOR" - this is
direct confirmation for the REAL environment specifically, not an
extrapolation from the paper script. order_rvsecncl.py/order_credit.py
document the same enumerated domain: "KRX: 한국거래소, NXT:대체거래소/
넥스트레이드, SOR:SOR". "KRX" is a documented alternative if SOR is ever
rejected for this account (see report for risk).

ACNT_PRDT_CD="01": every order-related docstring in this source tree notes
the CANO/ACNT_PRDT_CD split as "계좌번호 체계(8-2)의 앞 8자리"/"...뒤 2자리"
and gives the literal example '"CANO": "12345678", "ACNT_PRDT_CD": "01"'.
KIS_ACCT_STOCK in .env is only the 8-digit CANO; ACNT_PRDT_CD is this fixed
"01" convention, not derived from .env.

Balance/account-inquiry tr_id (inquire_balance.py, used only for the
pre-flight linkage check documented in the accompanying report, NOT called
by this script): env_dv=="real" -> "TTTC8434R", read directly from
inquire_balance.py's own tr_id-selection branch (mirrors paper's
VTTC8434R, defined in the same if/elif).

Retry scope: KIS's exact error code for "market not open yet" could not be
located via the MCP tool's source samples (only example/happy-path code is
indexed, not the full error-code catalogue). The retry check below is a
best-effort Korean-keyword heuristic on msg1 - see report for this risk.
Identical heuristic to the paper script, carried over unchanged.

Run with: uv run order_buy_005930_real.py           (dry run, safe, default)
          uv run order_buy_005930_real.py --live     (submits a REAL order,
                                                        real money moves)
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from kis_common import KisApiError, REQUEST_TIMEOUT_SECONDS, get_access_token, load_credentials, sanitize

SCRIPT_DIR = Path(__file__).resolve().parent
# Shared with kis_price.py deliberately - see module docstring "Token cache".
REAL_TOKEN_CACHE_PATH = SCRIPT_DIR / ".kis_token_cache.json"
ORDER_LOG_PATH = SCRIPT_DIR / "order_log_real.jsonl"

REQUIRED_REAL_ENV_VARS = [
    "KIS_APP_KEY",
    "KIS_APP_SECRET",
    "KIS_URL_REST",
    "KIS_ACCT_STOCK",
]

ORDER_CASH_PATH = "/uapi/domestic-stock/v1/trading/order-cash"
TR_ID_REAL_BUY = "TTTC0012U"  # 실전투자(real) + 매수(buy), confirmed via MCP source read

STOCK_CODE = "005930"
ACNT_PRDT_CD = "01"
ORD_DVSN_MARKET = "01"  # 시장가 (market order)
ORD_UNPR_MARKET = "0"  # required "0" for market orders
EXCG_ID_DVSN_CD = "SOR"  # Smart Order Routing; matches order_cash's own official real-env test example
ORD_QTY = "1"

MAX_ORDER_ATTEMPTS = 4
RETRY_INTERVAL_SECONDS = 4.0

# Best-effort heuristic for a "market not open yet" style rejection. KIS's
# authoritative error-code table was not available via the MCP tool's
# source-sample index, so this matches on Korean phrasing commonly used for
# order-timing rejections. See the accompanying report for this risk.
MARKET_NOT_OPEN_MARKERS = [
    "주문시간",
    "주문 가능한 시간",
    "장운영시간",
    "장 운영시간",
    "장종료",
    "영업시간이 아니",
    "거래시간이 아니",
    "개장 전",
]


def mask_account(cano: str) -> str:
    """Partially mask an account number for logs (defense in depth)."""
    if len(cano) <= 4:
        return "*" * len(cano)
    return cano[:2] + "*" * (len(cano) - 4) + cano[-2:]


def build_order_body(cano: str) -> dict[str, str]:
    return {
        "CANO": cano,
        "ACNT_PRDT_CD": ACNT_PRDT_CD,
        "PDNO": STOCK_CODE,
        "ORD_DVSN": ORD_DVSN_MARKET,
        "ORD_QTY": ORD_QTY,
        "ORD_UNPR": ORD_UNPR_MARKET,
        "EXCG_ID_DVSN_CD": EXCG_ID_DVSN_CD,
        "SLL_TYPE": "",
        "CNDT_PRIC": "",
    }


def build_headers(access_token: str, app_key: str, app_secret: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Accept": "text/plain",
        "charset": "UTF-8",
        "authorization": f"Bearer {access_token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "tr_id": TR_ID_REAL_BUY,
        "custtype": "P",
        "tr_cont": "",
    }


def sanitized_headers(headers: dict[str, str], secrets: list[str]) -> dict[str, str]:
    return {k: sanitize(v, secrets) for k, v in headers.items()}


def is_market_not_open_rejection(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict) or payload.get("rt_cd") == "0":
        return False
    msg1 = str(payload.get("msg1", ""))
    return any(marker in msg1 for marker in MARKET_NOT_OPEN_MARKERS)


def extract_order_number(payload: dict[str, Any]) -> str | None:
    output = payload.get("output")
    if not isinstance(output, dict):
        return None
    # Official example (chk_order_cash.py) maps 'ODNO' (uppercase), but every
    # other KIS example in this source tree uses lowercase JSON keys - check
    # both defensively rather than guess.
    for key in ("odno", "ODNO"):
        val = output.get(key)
        if val:
            return str(val)
    return None


def append_log(record: dict[str, Any]) -> None:
    line = json.dumps(record, ensure_ascii=False)
    with open(ORDER_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def run_live(
    app_key: str, app_secret: str, base_url: str, cano: str, secrets_accum: list[str]
) -> tuple[dict[str, Any], int]:
    """Attempt the live REAL order with narrowly-scoped retry.

    Returns (result_fields_to_merge_into_log_entry, exit_code).
    Stops immediately on the first successful order acknowledgment - never
    places a second order within this call (at-most-one-order guarantee).
    """
    try:
        token = get_access_token(app_key, app_secret, base_url, REAL_TOKEN_CACHE_PATH)
    except KisApiError as e:
        return (
            {"status": "error", "reason": "auth_failure", "error": str(e), "order_number": None},
            1,
        )

    secrets = secrets_accum + [token]
    url = f"{base_url}{ORDER_CASH_PATH}"
    body = build_order_body(cano)
    attempts_log: list[dict[str, Any]] = []

    for attempt in range(1, MAX_ORDER_ATTEMPTS + 1):
        headers = build_headers(token, app_key, app_secret)

        try:
            resp = httpx.post(url, headers=headers, json=body, timeout=REQUEST_TIMEOUT_SECONDS)
        except httpx.RequestError as e:
            attempts_log.append({"attempt": attempt, "network_error": sanitize(str(e), secrets)})
            # Do NOT retry network errors: whether the order landed on KIS's
            # side is unknown, and blindly retrying risks a duplicate order.
            return (
                {
                    "status": "error",
                    "reason": "network_error",
                    "error": sanitize(str(e), secrets),
                    "attempts": attempts_log,
                    "order_number": None,
                },
                1,
            )

        try:
            payload = resp.json()
        except json.JSONDecodeError:
            attempts_log.append({"attempt": attempt, "http_status": resp.status_code, "non_json_response": True})
            return (
                {
                    "status": "error",
                    "reason": "non_json_response",
                    "http_status": resp.status_code,
                    "attempts": attempts_log,
                    "order_number": None,
                },
                1,
            )

        rt_cd = payload.get("rt_cd")
        msg_cd = payload.get("msg_cd")
        msg1 = sanitize(str(payload.get("msg1", "")), secrets)
        odno = extract_order_number(payload)

        attempts_log.append(
            {"attempt": attempt, "http_status": resp.status_code, "rt_cd": rt_cd, "msg_cd": msg_cd, "msg1": msg1}
        )

        if rt_cd == "0" and odno:
            # Success acknowledgment received - stop immediately, no further
            # attempts, no matter how many retries remain.
            return (
                {
                    "status": "placed",
                    "order_number": odno,
                    "attempts": attempts_log,
                },
                0,
            )

        if rt_cd == "0" and not odno:
            # Anomalous: KIS said success but we can't find an order number.
            # Do not retry (we cannot rule out the order having gone through)
            # - surface for manual review instead.
            return (
                {
                    "status": "error",
                    "reason": "ack_missing_order_number",
                    "attempts": attempts_log,
                    "order_number": None,
                },
                1,
            )

        if is_market_not_open_rejection(payload) and attempt < MAX_ORDER_ATTEMPTS:
            time.sleep(RETRY_INTERVAL_SECONDS)
            continue

        # Definitive rejection (or market-not-open retries exhausted).
        retries_exhausted = is_market_not_open_rejection(payload)
        return (
            {
                "status": "rejected",
                "reason": "market_not_open_retries_exhausted" if retries_exhausted else "rejected_by_kis",
                "msg1": msg1,
                "msg_cd": msg_cd,
                "attempts": attempts_log,
                "order_number": None,
            },
            # Judgment call (documented in report): a market-not-open
            # rejection, after exhausting the narrow retry budget, is a
            # *definitive, understood* outcome - exit 0 per the exit-code
            # contract so a scheduler wrapper can tell "ran to completion"
            # apart from "crashed". Any other KIS rejection (bad params,
            # insufficient balance, already-filled, ...) is NOT retried and
            # exits non-zero so it gets human attention.
            0 if retries_exhausted else 1,
        )

    # Should be unreachable (loop always returns), but keep a safety net.
    return (
        {"status": "error", "reason": "retry_loop_exhausted_unexpectedly", "attempts": attempts_log, "order_number": None},
        1,
    )


def main() -> int:
    live = "--live" in sys.argv[1:]
    timestamp = datetime.now().astimezone().isoformat()
    log_entry: dict[str, Any] = {"timestamp": timestamp, "mode": "live" if live else "dry-run"}

    try:
        creds = load_credentials(REQUIRED_REAL_ENV_VARS)
        app_key = creds["KIS_APP_KEY"]
        app_secret = creds["KIS_APP_SECRET"]
        base_url = creds["KIS_URL_REST"]
        cano = creds["KIS_ACCT_STOCK"]

        secrets = [app_key, app_secret]
        url = f"{base_url}{ORDER_CASH_PATH}"
        body = build_order_body(cano)
        preview_headers = sanitized_headers(
            build_headers("[TOKEN-NOT-ISSUED-IN-DRY-RUN]", app_key, app_secret), secrets
        )
        request_summary = {
            "method": "POST",
            "url": url,
            "tr_id": TR_ID_REAL_BUY,
            "headers": preview_headers,
            "body": {**body, "CANO": mask_account(cano)},
        }
        log_entry["request"] = request_summary

        if not live:
            log_entry["status"] = "dry-run"
            log_entry["order_number"] = None
            append_log(log_entry)
            print("[DRY RUN] order_buy_005930_real.py - no order-cash HTTP call was made.")
            print(json.dumps(request_summary, indent=2, ensure_ascii=False))
            print("Re-run with --live to submit this as a REAL order (real money will move).")
            return 0

        result, exit_code = run_live(app_key, app_secret, base_url, cano, secrets)
        log_entry.update(result)
        append_log(log_entry)
        return exit_code

    except KisApiError as e:
        log_entry["status"] = "error"
        log_entry["reason"] = "setup_failure"
        log_entry["error"] = str(e)  # KisApiError messages are sanitized by construction
        try:
            append_log(log_entry)
        except OSError:
            print("CRITICAL: failed to write order_log_real.jsonl", file=sys.stderr)
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - top-level safety net for genuine crashes
        print(f"Unexpected error: {type(e).__name__}: {e}", file=sys.stderr)
        try:
            log_entry["status"] = "crashed"
            log_entry["error"] = f"{type(e).__name__}: {e}"
            append_log(log_entry)
        except OSError:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())

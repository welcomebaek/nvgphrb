"""Place a market BUY order for 1 share of Samsung Electronics (005930) via
the KIS PAPER/모의투자 trading environment.

SAFETY (read this before running):
  - Default behavior (no flags) is a DRY RUN: this script builds the exact
    HTTP request (URL, tr_id, sanitized headers, body) that WOULD be sent
    and logs/prints it, but makes NO network call to the order-cash
    endpoint at all.
  - Only `uv run order_buy_005930.py --live` actually obtains a token and
    submits a real (paper-environment) order. Never pass --live casually.

This script is PAPER-ONLY. It reads exclusively:
  KIS_PAPER_APP_KEY / KIS_PAPER_APP_SECRET / KIS_URL_REST_PAPER / KIS_PAPER_STOCK
from .env, and uses a token cache (.kis_paper_token_cache.json) separate from
kis_price.py's real-trading cache. It must never reference the real-trading
credential env vars or tr_id that kis_price.py uses (see kis_common.py /
kis_price.py for those) - only paper-env symbols may appear in this file.

--- KIS API details, confirmed via the kis-code-assistant-mcp MCP server ---
(search_domestic_stock_api / read_source_code against
 koreainvestment/open-trading-api, examples_llm/domestic_stock/*)

Endpoint (order_cash.py, "주식주문(현금)"):
  POST /uapi/domestic-stock/v1/trading/order-cash
  tr_id: env_dv=="demo" (모의투자) and ord_dv=="buy" -> "VTTC0012U" (used here)
         (the env_dv=="real" counterpart tr_id is a different value and is
         intentionally not used or named in this file)

ORD_DVSN ("01" = 시장가/market order): confirmed independently in TWO other
official docstrings for the same order-parameter domain:
  - order_resv.py: "ord_dvsn_cd ... (00 : 지정가, 01 : 시장가, 02 : 조건부지정가, 05 : 장전 시간외)"
  - inquire_psbl_order.py: "ord_dvsn (str): [필수] 주문구분 (ex. 01 : 시장가)"

ORD_UNPR for a market order = "0": order_resv.py docstring - "ord_unpr ...
(1주당 가격, 시장가/장전 시간외는 0 입력)". order_cash.py's own docstring
also notes an order with no ORD_UNPR is priced at the upper limit until
filled, consistent with using "0" for market orders.

EXCG_ID_DVSN_CD: order_rvsecncl.py and order_credit.py both document the
same enumerated domain: "KRX: 한국거래소, NXT:대체거래소/넥스트레이드, SOR:SOR".
order_cash.py's own official test example (chk_order_cash.py) uses
excg_id_dvsn_cd="SOR", so this script uses "SOR" (Smart Order Routing across
KRX/NXT) to match the endpoint's own sample usage. "KRX" is a documented
alternative if SOR is ever rejected for this account (see report for risk).

ACNT_PRDT_CD="01": every order-related docstring in this source tree notes
the CANO/ACNT_PRDT_CD split as "계좌번호 체계(8-2)의 앞 8자리"/"...뒤 2자리"
and gives the literal example '"CANO": "12345678", "ACNT_PRDT_CD": "01"'.
KIS_PAPER_STOCK in .env is only the 8-digit CANO; ACNT_PRDT_CD is this fixed
"01" convention, not derived from .env.

Retry scope: KIS's exact error code for "market not open yet" could not be
located via the MCP tool's source samples (only example/happy-path code is
indexed, not the full error-code catalogue). The retry check below is a
best-effort Korean-keyword heuristic on msg1 - see report for this risk.

Run with: uv run order_buy_005930.py           (dry run, safe, default)
          uv run order_buy_005930.py --live     (submits a REAL paper order)
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from typing import Any

import httpx

from etf_arb import paths
from kis_common import KisApiError, REQUEST_TIMEOUT_SECONDS, get_access_token, load_credentials, sanitize

PAPER_TOKEN_CACHE_PATH = paths.PAPER_TOKEN_CACHE_PATH
ORDER_LOG_PATH = paths.ORDER_LOG_PAPER_PATH

REQUIRED_PAPER_ENV_VARS = [
    "KIS_PAPER_APP_KEY",
    "KIS_PAPER_APP_SECRET",
    "KIS_URL_REST_PAPER",
    "KIS_PAPER_STOCK",
]

ORDER_CASH_PATH = "/uapi/domestic-stock/v1/trading/order-cash"
TR_ID_PAPER_BUY = "VTTC0012U"  # 모의투자(demo) + 매수(buy), confirmed via MCP source read

STOCK_CODE = "005930"
ACNT_PRDT_CD = "01"
ORD_DVSN_MARKET = "01"  # 시장가 (market order)
ORD_UNPR_MARKET = "0"  # required "0" for market orders
EXCG_ID_DVSN_CD = "SOR"  # Smart Order Routing; matches order_cash's own official example
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
        "tr_id": TR_ID_PAPER_BUY,
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
    ORDER_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(ORDER_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def run_live(
    app_key: str, app_secret: str, base_url: str, cano: str, secrets_accum: list[str]
) -> tuple[dict[str, Any], int]:
    """Attempt the live paper order with narrowly-scoped retry.

    Returns (result_fields_to_merge_into_log_entry, exit_code).
    Stops immediately on the first successful order acknowledgment - never
    places a second order within this call (at-most-one-order guarantee).
    """
    try:
        token = get_access_token(app_key, app_secret, base_url, PAPER_TOKEN_CACHE_PATH)
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
            # contract so a cron wrapper can tell "ran to completion" apart
            # from "crashed". Any other KIS rejection (bad params,
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
        creds = load_credentials(REQUIRED_PAPER_ENV_VARS)
        app_key = creds["KIS_PAPER_APP_KEY"]
        app_secret = creds["KIS_PAPER_APP_SECRET"]
        base_url = creds["KIS_URL_REST_PAPER"]
        cano = creds["KIS_PAPER_STOCK"]

        secrets = [app_key, app_secret]
        url = f"{base_url}{ORDER_CASH_PATH}"
        body = build_order_body(cano)
        preview_headers = sanitized_headers(
            build_headers("[TOKEN-NOT-ISSUED-IN-DRY-RUN]", app_key, app_secret), secrets
        )
        request_summary = {
            "method": "POST",
            "url": url,
            "tr_id": TR_ID_PAPER_BUY,
            "headers": preview_headers,
            "body": {**body, "CANO": mask_account(cano)},
        }
        log_entry["request"] = request_summary

        if not live:
            log_entry["status"] = "dry-run"
            log_entry["order_number"] = None
            append_log(log_entry)
            print("[DRY RUN] order_buy_005930.py - no order-cash HTTP call was made.")
            print(json.dumps(request_summary, indent=2, ensure_ascii=False))
            print("Re-run with --live to submit this as a real PAPER-environment order.")
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
            print("CRITICAL: failed to write order_log.jsonl", file=sys.stderr)
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

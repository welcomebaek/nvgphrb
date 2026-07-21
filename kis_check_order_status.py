"""Check whether a specific REAL-account cash order has been filled (체결)
today, via the KIS Open API.

Uses the official KIS (Korea Investment & Securities) Open API only:
  - OAuth access-token issuance: POST /oauth2/tokenP
  - 주식일별주문체결조회 (daily order/execution inquiry):
    GET /uapi/domestic-stock/v1/trading/inquire-daily-ccld
    tr_id: TTTC0081R (real-trading, "3개월 이내"/within-3-months variant)

--- KIS API details, confirmed via the kis-code-assistant-mcp MCP server ---
(search_domestic_stock_api(function_name="inquire_daily_ccld") / read_source_code
 against koreainvestment/open-trading-api,
 examples_llm/domestic_stock/inquire_daily_ccld/{inquire_daily_ccld,chk_inquire_daily_ccld}.py)

tr_id selection, read directly from inquire_daily_ccld.py's own branch:
    if env_dv == "real":
        if pd_dv == "before": tr_id = "CTSC9215R"   # 3개월 이전
        elif pd_dv == "inner": tr_id = "TTTC0081R"   # 3개월 이내  <- used here
    elif env_dv == "demo":
        if pd_dv == "before": tr_id = "VTSC9215R"
        elif pd_dv == "inner": tr_id = "VTTC0081R"
This project only ever uses the real/실전 credential pair (see kis_common.py's
module docstring), and "오늘" (today) is always within the 3-month window, so
tr_id is hardcoded to TTTC0081R - NOT "TTTC8001R" (that pattern was an
unverified guess; the real value confirmed straight from source is TTTC0081R).

Required query params (GET, per the source's `params` dict - this is a
읽기 전용 조회 endpoint, unlike order-cash which is POST):
  CANO, ACNT_PRDT_CD, INQR_STRT_DT, INQR_END_DT (both = today, YYYYMMDD KST),
  SLL_BUY_DVSN_CD ("00" 전체/01 매도/02 매수 - "00" used here so the script
  works for a sell order too), PDNO (optional product filter, left blank),
  CCLD_DVSN ("00" 전체/01 체결/02 미체결 - "00" used so unfilled/rejected
  orders are visible too, not just filled ones), INQR_DVSN ("00" 역순/01
  정순), INQR_DVSN_3 ("00" 전체/01 현금/... - "00" covers cash orders),
  ORD_GNO_BRNO (optional, blank), ODNO (order number filter - set to the
  requested order number, narrows the result server-side), INQR_DVSN_1
  (optional, blank), CTX_AREA_FK100/NK100 (pagination continuation, blank
  for a first/only call), EXCG_ID_DVSN_CD - IMPORTANT DEVIATION FROM THE
  SOURCE'S OWN DEFAULT: inquire_daily_ccld.py's signature defaults this to
  "KRX", but that default was verified LIVE against this exact order and
  found to silently return zero rows ("조회할 내용이 없습니다") for an order
  actually placed with EXCG_ID_DVSN_CD="SOR" (order_buy_005930_real.py uses
  SOR/Smart-Order-Routing) - KRX only returns orders explicitly routed to
  the KRX exchange leg. "SOR" and "ALL" were both confirmed live to return
  the order correctly; "ALL" is used here since it is robust to whatever
  routing a future order used, not just today's SOR order.

Response shape: output1 is an array of order rows (one row per order/leg);
output2 is a single summary object (not needed here). Field names, read
directly from chk_inquire_daily_ccld.py's own COLUMN_MAPPING (official
컬럼명 -> 한글 mapping, so these are not guessed):
  odno            = 주문번호 (order number)
  pdno            = 상품번호 (product/stock code)
  prdt_name       = 상품명 (product/stock name)
  sll_buy_dvsn_cd_name = 매도매수구분코드명 (BUY/SELL as Korean text)
  ord_dvsn_name   = 주문구분명 (order type, e.g. 시장가)
  ord_qty         = 주문수량 (ordered quantity)
  ord_unpr        = 주문단가 (order unit price; 0 for a market order)
  tot_ccld_qty    = 총체결수량 (total FILLED quantity)          <- key field
  avg_prvs        = 평균가 (average FILL price)                 <- key field
  tot_ccld_amt    = 총체결금액 (total filled amount)
  rmn_qty         = 잔여수량 (remaining UNFILLED quantity)       <- key field
  rjct_qty        = 거부수량 (rejected quantity)
  cncl_yn         = 취소여부 (cancelled Y/N)
  ord_tmd         = 주문시각 (order time)
Overall fill status is derived client-side from tot_ccld_qty/ord_qty/rmn_qty/
rjct_qty/cncl_yn - KIS does not return a single "status" enum field for this
endpoint.

Credentials are read from .env (KIS_APP_KEY / KIS_APP_SECRET / KIS_URL_REST /
KIS_ACCT_STOCK - the same real-trading pair used by kis_price.py /
order_buy_005930_real.py). This is a pure read-only account inquiry with no
side effects. The issued OAuth token is cached in .kis_token_cache.json next
to this script, reusing the same cache the other real-trading scripts here
already use (same app key/secret/base URL -> same token is valid for all).

Shared primitives (secret sanitization, credential loading, token cache,
token issuance) live in kis_common.py.

Run with: uv run kis_check_order_status.py                # checks today's
                                                            # known order
                                                            # 0004538200
          uv run kis_check_order_status.py <order_number>  # checks any
                                                            # other order
                                                            # placed today
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from kis_common import KisApiError, REQUEST_TIMEOUT_SECONDS, get_access_token, load_credentials, sanitize

DEFAULT_ORDER_NUMBER = "0004538200"

DAILY_CCLD_URL_PATH = "/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
TR_ID_REAL_INNER = "TTTC0081R"  # 실전(real) + 3개월 이내(inner), confirmed via MCP source read
ACNT_PRDT_CD = "01"  # fixed product code, per project convention (see order_buy_005930_real.py)
EXCG_ID_DVSN_CD = "ALL"  # NOT the source's documented default ("KRX") - see
# module docstring: "KRX" was live-verified to silently return zero rows for
# an order placed with EXCG_ID_DVSN_CD="SOR" (this project's own buy script
# uses SOR). "ALL" was live-verified to return it correctly, and is robust
# to any exchange routing a future order might use.

KST = ZoneInfo("Asia/Seoul")

SCRIPT_DIR = Path(__file__).resolve().parent
TOKEN_CACHE_PATH = SCRIPT_DIR / ".kis_token_cache.json"


def get_daily_ccld(
    access_token: str,
    app_key: str,
    app_secret: str,
    base_url: str,
    cano: str,
    inqr_dt: str,
    odno: str,
    acnt_prdt_cd: str = ACNT_PRDT_CD,
) -> list[dict[str, Any]]:
    """Call the KIS 주식일별주문체결조회 endpoint and return `output1` as a list.

    inqr_dt is used as both INQR_STRT_DT and INQR_END_DT (today only, per
    this script's scope). odno filters server-side to the requested order.
    """
    url = f"{base_url}{DAILY_CCLD_URL_PATH}"
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/plain",
        "charset": "UTF-8",
        "authorization": f"Bearer {access_token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "tr_id": TR_ID_REAL_INNER,
        "custtype": "P",
        "tr_cont": "",
    }
    params = {
        "CANO": cano,
        "ACNT_PRDT_CD": acnt_prdt_cd,
        "INQR_STRT_DT": inqr_dt,
        "INQR_END_DT": inqr_dt,
        "SLL_BUY_DVSN_CD": "00",  # 전체 (all - works for a buy or sell order)
        "PDNO": "",
        "CCLD_DVSN": "00",  # 전체 (all - so unfilled/rejected orders are visible too)
        "INQR_DVSN": "00",  # 역순
        "INQR_DVSN_3": "00",  # 전체 (현금 포함)
        "ORD_GNO_BRNO": "",
        "ODNO": odno,
        "INQR_DVSN_1": "",
        "CTX_AREA_FK100": "",
        "CTX_AREA_NK100": "",
        "EXCG_ID_DVSN_CD": EXCG_ID_DVSN_CD,
    }

    secrets = [access_token, app_key, app_secret]

    try:
        resp = httpx.get(
            url, headers=headers, params=params, timeout=REQUEST_TIMEOUT_SECONDS
        )
    except httpx.RequestError as e:
        raise KisApiError(
            f"Network error while requesting order/execution history: {sanitize(str(e), secrets)}"
        ) from None

    if resp.status_code != 200:
        raise KisApiError(
            "Order/execution history lookup failed with HTTP "
            f"{resp.status_code}: {sanitize(resp.text, secrets)[:300]}"
        )

    try:
        payload = resp.json()
    except json.JSONDecodeError:
        raise KisApiError(
            "Order/execution history lookup returned non-JSON response"
        ) from None

    if payload.get("rt_cd") != "0":
        msg = payload.get("msg1", "unknown error")
        raise KisApiError(
            f"Order/execution history lookup rejected by KIS: {sanitize(str(msg), secrets)}"
        )

    output1 = payload.get("output1")
    if output1 is None:
        raise KisApiError("Order/execution history lookup response missing 'output1'")
    if isinstance(output1, list):
        return output1
    if isinstance(output1, dict):
        return [output1]
    raise KisApiError("Order/execution history lookup returned unexpected 'output1' shape")


def find_order(rows: list[dict[str, Any]], order_number: str) -> dict[str, Any] | None:
    """Find the row matching order_number, defensively (server-side ODNO
    filter should already narrow to this, but don't assume)."""
    for row in rows:
        if str(row.get("odno", "")).strip() == order_number.strip():
            return row
    return None


def _int_field(row: dict[str, Any], key: str) -> int:
    try:
        return int(str(row.get(key, "0")).strip() or "0")
    except ValueError:
        return 0


def format_order_status(order_number: str, row: dict[str, Any] | None, inqr_dt: str) -> str:
    queried_at = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S %Z")
    header = f"주문체결 조회 - 주문번호 {order_number} (조회일자 {inqr_dt}) - 조회시각 {queried_at}"

    if row is None:
        return (
            header
            + f"\n  결과 없음 (not found): 오늘자 주문/체결 내역에서 주문번호 {order_number}"
              "를 찾지 못했습니다. (실전/모의 계좌 혼동, 조회일자 오류, 혹은 아직 시스템에 "
              "반영되지 않았을 가능성을 확인하세요.)"
        )

    pdno = row.get("pdno", "")
    prdt_name = row.get("prdt_name", "")
    sll_buy_name = row.get("sll_buy_dvsn_cd_name", "")
    ord_dvsn_name = row.get("ord_dvsn_name", "")
    ord_tmd = row.get("ord_tmd", "")

    ord_qty = _int_field(row, "ord_qty")
    tot_ccld_qty = _int_field(row, "tot_ccld_qty")
    rmn_qty = _int_field(row, "rmn_qty")
    rjct_qty = _int_field(row, "rjct_qty")
    cncl_yn = row.get("cncl_yn", "")
    avg_prvs_raw = row.get("avg_prvs", "0")
    tot_ccld_amt = row.get("tot_ccld_amt", "0")

    try:
        avg_prvs = f"{float(avg_prvs_raw):,.2f}"
    except (TypeError, ValueError):
        avg_prvs = str(avg_prvs_raw)

    if cncl_yn == "Y":
        status = "취소됨 (cancelled)"
    elif rjct_qty > 0 and tot_ccld_qty == 0:
        status = "거부됨 (rejected)"
    elif tot_ccld_qty >= ord_qty and ord_qty > 0:
        status = "전량체결 (fully filled)"
    elif tot_ccld_qty > 0:
        status = "부분체결 (partially filled)"
    else:
        status = "미체결 (unfilled)"

    lines = [
        header,
        f"  종목: {pdno} {prdt_name}".rstrip(),
        f"  구분: {sll_buy_name} / {ord_dvsn_name} (주문시각 {ord_tmd})",
        f"  주문수량: {ord_qty:,}주",
        f"  체결수량: {tot_ccld_qty:,}주",
        f"  체결평균가: {avg_prvs}원 (총체결금액 {tot_ccld_amt}원)",
        f"  미체결(잔여)수량: {rmn_qty:,}주",
        f"  상태: {status}",
    ]
    return "\n".join(lines)


def main() -> int:
    order_number = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ORDER_NUMBER

    try:
        creds = load_credentials(
            ["KIS_APP_KEY", "KIS_APP_SECRET", "KIS_URL_REST", "KIS_ACCT_STOCK"]
        )
        base_url = creds["KIS_URL_REST"]
        token = get_access_token(
            creds["KIS_APP_KEY"], creds["KIS_APP_SECRET"], base_url, TOKEN_CACHE_PATH
        )
        inqr_dt = datetime.now(KST).strftime("%Y%m%d")
        rows = get_daily_ccld(
            token,
            creds["KIS_APP_KEY"],
            creds["KIS_APP_SECRET"],
            base_url,
            creds["KIS_ACCT_STOCK"],
            inqr_dt,
            order_number,
        )
        row = find_order(rows, order_number)
        print(format_order_status(order_number, row, inqr_dt))
        return 0
    except KisApiError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - top-level safety net, sanitized
        print(f"Unexpected error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

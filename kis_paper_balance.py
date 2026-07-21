"""Fetch the paper-trading (모의투자) account stock balance via the KIS Open API.

Checks whether the 모의투자 account is now actually usable - earlier this
project found it rejected with "모의투자 처리계좌의 ID와 사용자정보가 상이"
(account/app-key mismatch), which is why the ETF NAV-disparity system trades
via a local simulator instead of real KIS paper orders. The user has since
(re)activated the paper account and wants this re-verified.

Uses the official KIS Open API only:
  - OAuth access-token issuance: POST /oauth2/tokenP
  - 주식잔고조회 (stock balance inquiry): GET
    /uapi/domestic-stock/v1/trading/inquire-balance
    tr_id: VTTC8434R (demo/모의, confirmed via kis-code-assistant-mcp against
    the official koreainvestment/open-trading-api inquire_balance.py sample -
    env_dv="demo" branch. Real-trading counterpart is TTTC8434R, already used
    elsewhere in this project e.g. order status checks).

Credentials are read from .env (KIS_PAPER_APP_KEY / KIS_PAPER_APP_SECRET /
KIS_URL_REST_PAPER / KIS_PAPER_STOCK - the paper-trading key pair and CANO).
ACNT_PRDT_CD follows this project's established convention ("01", see
order_buy_005930_real.py). This is a pure read-only account inquiry with no
side effects - no order can be placed by this script.

Token is cached separately from the real-trading token (different base URL
and credentials), in .kis_paper_token_cache.json next to this script.

Shared primitives (secret sanitization, credential loading, token cache,
token issuance) live in kis_common.py.

Run with: uv run kis_paper_balance.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from kis_common import KisApiError, REQUEST_TIMEOUT_SECONDS, get_access_token, load_credentials, sanitize

BALANCE_URL_PATH = "/uapi/domestic-stock/v1/trading/inquire-balance"
TR_ID_BALANCE_DEMO = "VTTC8434R"
ACNT_PRDT_CD = "01"  # fixed product code, per project convention

SCRIPT_DIR = Path(__file__).resolve().parent
TOKEN_CACHE_PATH = SCRIPT_DIR / ".kis_paper_token_cache.json"


def get_paper_balance(
    access_token: str,
    app_key: str,
    app_secret: str,
    base_url: str,
    cano: str,
    acnt_prdt_cd: str = ACNT_PRDT_CD,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Call the KIS 주식잔고조회 endpoint (demo) and return (holdings, summary)."""
    url = f"{base_url}{BALANCE_URL_PATH}"
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/plain",
        "charset": "UTF-8",
        "authorization": f"Bearer {access_token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "tr_id": TR_ID_BALANCE_DEMO,
        "custtype": "P",
        "tr_cont": "",
    }
    params = {
        "CANO": cano,
        "ACNT_PRDT_CD": acnt_prdt_cd,
        "AFHR_FLPR_YN": "N",
        "OFL_YN": "",
        "INQR_DVSN": "02",  # 종목별
        "UNPR_DVSN": "01",
        "FUND_STTL_ICLD_YN": "N",
        "FNCG_AMT_AUTO_RDPT_YN": "N",
        "PRCS_DVSN": "00",  # 전일매매포함
        "CTX_AREA_FK100": "",
        "CTX_AREA_NK100": "",
    }

    secrets = [access_token, app_key, app_secret]

    try:
        resp = httpx.get(
            url, headers=headers, params=params, timeout=REQUEST_TIMEOUT_SECONDS
        )
    except httpx.RequestError as e:
        raise KisApiError(
            f"Network error while requesting paper balance: {sanitize(str(e), secrets)}"
        ) from None

    if resp.status_code != 200:
        raise KisApiError(
            "Paper balance lookup failed with HTTP "
            f"{resp.status_code}: {sanitize(resp.text, secrets)[:300]}"
        )

    try:
        payload = resp.json()
    except json.JSONDecodeError:
        raise KisApiError("Paper balance lookup returned non-JSON response") from None

    if payload.get("rt_cd") != "0":
        msg = payload.get("msg1", "unknown error")
        msg_cd = payload.get("msg_cd", "")
        raise KisApiError(
            f"Paper balance lookup rejected by KIS (msg_cd={msg_cd}): "
            f"{sanitize(str(msg), secrets)}"
        )

    output1 = payload.get("output1") or []
    output2 = payload.get("output2") or [{}]
    if isinstance(output2, list):
        output2 = output2[0] if output2 else {}

    return output1, output2


def format_balance_output(holdings: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    queried_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"모의투자 계좌 잔고 - 조회시각 {queried_at}"]

    real_holdings = [h for h in holdings if str(h.get("hldg_qty", "0")).strip() not in ("", "0")]
    if not real_holdings:
        lines.append("  보유 종목 없음")
    else:
        for h in real_holdings:
            name = h.get("prdt_name", "")
            code = h.get("pdno", "")
            qty = h.get("hldg_qty", "0")
            avg = h.get("pchs_avg_pric", "0")
            cur = h.get("prpr", "0")
            pl = h.get("evlu_pfls_amt", "0")
            lines.append(f"  {code} {name}: {qty}주, 평균단가 {avg}, 현재가 {cur}, 평가손익 {pl}")

    dnca = summary.get("dnca_tot_amt", "0")
    tot_evlu = summary.get("tot_evlu_amt", "0")
    lines.append(f"  예수금총금액: {dnca}")
    lines.append(f"  총평가금액: {tot_evlu}")

    return "\n".join(lines)


def main() -> int:
    try:
        creds = load_credentials(
            ["KIS_PAPER_APP_KEY", "KIS_PAPER_APP_SECRET", "KIS_URL_REST_PAPER", "KIS_PAPER_STOCK"]
        )
        base_url = creds["KIS_URL_REST_PAPER"]
        token = get_access_token(
            creds["KIS_PAPER_APP_KEY"], creds["KIS_PAPER_APP_SECRET"], base_url, TOKEN_CACHE_PATH
        )
        holdings, summary = get_paper_balance(
            token,
            creds["KIS_PAPER_APP_KEY"],
            creds["KIS_PAPER_APP_SECRET"],
            base_url,
            creds["KIS_PAPER_STOCK"],
        )
        print(format_balance_output(holdings, summary))
        return 0
    except KisApiError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - top-level safety net, sanitized
        print(f"Unexpected error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

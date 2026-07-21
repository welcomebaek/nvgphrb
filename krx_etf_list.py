"""Fetch the full list of domestically-listed ETFs via the KRX Open API.

CLAUDE.md exception: the KIS Open API has no endpoint that lists ALL
domestically-listed ETFs/ETNs (the etfetn category's inquire_price requires
an already-known ticker; there is no listing/master endpoint). For this one
narrow purpose - ETF/ETN master-list data - CLAUDE.md permits using the
official KRX (한국거래소) Open API instead. Every other lookup in this
project must still go through the KIS Open API as usual.

Uses the official KRX Open API only:
  - ETF 일별매매정보: GET https://data-dbg.krx.co.kr/svc/apis/etp/etf_bydd_trd
    Auth: HTTP header `AUTH_KEY: <발급받은 키>` (not a query param, not
    Bearer-style). Required query param: `basDd` (기준일자, YYYYMMDD).
    Querying by basDd alone (no ticker) returns every ETF traded/listed on
    that date - this is the mechanism used here to get the "full ETF list".
    Note: the http:// (no TLS) form of this URL 302-redirects to this same
    https:// URL - always call https:// directly.

Response shape (KRX's standard "일별매매정보" envelope): a top-level JSON
object with key `OutBlock_1` whose value is a list of per-instrument row
dicts. Verified live on 2026-07-20 against basDd=20260716 (1,146 ETF rows).
Confirmed field names: `BAS_DD` (기준일자), `ISU_CD` (종목코드), `ISU_NM`
(종목명), `TDD_CLSPRC` (종가), `CMPPREVDD_PRC` (전일대비), `FLUC_RT`
(등락률), `NAV`, `TDD_OPNPRC` (시가), `TDD_HGPRC` (고가), `TDD_LWPRC`
(저가), `ACC_TRDVOL` (누적거래량), `ACC_TRDVAL` (누적거래대금), `MKTCAP`
(시가총액), `INVSTASST_NETASST_TOTAMT` (순자산총액), `LIST_SHRS`
(상장주식수), `IDX_IND_NM` (기초지수명), `OBJ_STKPRC_IDX`/`CMPPREVDD_IDX`/
`FLUC_RT_IDX` (기초지수 종가/전일대비/등락률).

Credentials: KRX registration/auth is completely separate from KIS
credentials. Reads KRX_AUTH_KEY from .env (a single credential for one
external API - not routed through kis_common.py, which is KIS-specific).

Run with: uv run krx_etf_list.py [YYYYMMDD]
(defaults to today's date if omitted; KRX has no data for non-trading days)
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

KRX_ETF_URL = "https://data-dbg.krx.co.kr/svc/apis/etp/etf_bydd_trd"
REQUEST_TIMEOUT_SECONDS = 10.0

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_ENV_PATH = SCRIPT_DIR / ".env"


class KrxApiError(Exception):
    """Raised for KRX API failures. Message must never contain AUTH_KEY."""


class KrxAuthPendingError(KrxApiError):
    """Raised specifically for HTTP 401 - AUTH_KEY not yet approved/active."""


def sanitize(text: str, secrets: list[str]) -> str:
    """Strip any known secret values out of a string before it is surfaced."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


def load_krx_auth_key(env_path: Path | None = None) -> str:
    """Load KRX_AUTH_KEY from .env. Returns "" if absent/blank (caller decides)."""
    load_dotenv(env_path or DEFAULT_ENV_PATH)
    return os.environ.get("KRX_AUTH_KEY", "").strip()


def fetch_etf_list(auth_key: str, bas_dd: str) -> list[dict[str, Any]]:
    """Call the KRX ETF 일별매매정보 endpoint and return `OutBlock_1` as a list."""
    headers = {"AUTH_KEY": auth_key}
    params = {"basDd": bas_dd}
    secrets = [auth_key]

    try:
        resp = httpx.get(
            KRX_ETF_URL, headers=headers, params=params, timeout=REQUEST_TIMEOUT_SECONDS
        )
    except httpx.RequestError as e:
        raise KrxApiError(
            f"Network error while requesting ETF list: {sanitize(str(e), secrets)}"
        ) from None

    if resp.status_code == 401:
        raise KrxAuthPendingError(
            "KRX API 키가 아직 승인 대기 중입니다 (HTTP 401 Unauthorized).\n"
            "  openapi.krx.co.kr 에서 신청한 키가 '승인완료' 상태로 바뀐 뒤 다시 시도하세요."
        )

    if resp.status_code != 200:
        raise KrxApiError(
            "ETF 목록 조회 실패 - HTTP "
            f"{resp.status_code}: {sanitize(resp.text, secrets)[:500]}"
        )

    try:
        payload = resp.json()
    except json.JSONDecodeError:
        raise KrxApiError("ETF 목록 조회 응답이 JSON 형식이 아닙니다") from None

    if not isinstance(payload, dict):
        raise KrxApiError("ETF 목록 조회 응답이 예상한 JSON 객체 형태가 아닙니다")

    rows = payload.get("OutBlock_1")
    if rows is None:
        raise KrxApiError(
            "ETF 목록 조회 응답에 'OutBlock_1' 키가 없습니다. "
            f"응답 키 목록: {list(payload.keys())}"
        )
    if not isinstance(rows, list):
        raise KrxApiError("'OutBlock_1' 필드가 예상한 리스트 형태가 아닙니다")

    return rows


def print_etf_list(rows: list[dict[str, Any]], bas_dd: str) -> None:
    print(f"{bas_dd} 기준 국내 상장 ETF: 총 {len(rows)}건\n")

    header = (
        f"{'종목코드(ISU_CD)':<10} {'종목명(ISU_NM)':<26} "
        f"{'종가(TDD_CLSPRC)':>14} {'NAV':>14}"
    )
    print(header)
    print("-" * len(header))

    def _fmt_num(raw: Any) -> str:
        try:
            return f"{int(str(raw).replace(',', '')):,}"
        except (TypeError, ValueError):
            try:
                return f"{float(str(raw).replace(',', '')):,.2f}"
            except (TypeError, ValueError):
                return str(raw)

    for row in rows:
        isu_cd = str(row.get("ISU_CD", "")).strip()
        isu_nm = str(row.get("ISU_NM", "")).strip()
        clsprc = _fmt_num(row.get("TDD_CLSPRC", ""))
        nav = _fmt_num(row.get("NAV", ""))
        print(f"{isu_cd:<10} {isu_nm:<26} {clsprc:>14} {nav:>14}")

    first_row_keys = list(rows[0].keys())
    print(f"\n실제 응답에서 확인된 전체 필드명: {first_row_keys}")


def main() -> int:
    if len(sys.argv) > 1:
        bas_dd = sys.argv[1].strip()
    else:
        bas_dd = datetime.now().strftime("%Y%m%d")

    if len(bas_dd) != 8 or not bas_dd.isdigit():
        print(
            f"Error: 날짜 형식이 올바르지 않습니다. YYYYMMDD 8자리 숫자로 입력하세요 (입력값: {bas_dd!r})",
            file=sys.stderr,
        )
        return 1

    auth_key = load_krx_auth_key()
    if not auth_key:
        print(
            "Error: KRX_AUTH_KEY가 .env에 설정되어 있지 않습니다.\n"
            "  openapi.krx.co.kr 에서 신청한 KRX Open API 키가 '승인완료' 상태가 되면, "
            ".env 파일의 KRX_AUTH_KEY= 뒤에 발급받은 키 값을 입력한 뒤 다시 실행하세요.",
            file=sys.stderr,
        )
        return 1

    try:
        rows = fetch_etf_list(auth_key, bas_dd)
    except KrxAuthPendingError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except KrxApiError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - top-level safety net, sanitized
        print(
            f"Unexpected error: {type(e).__name__}: {sanitize(str(e), [auth_key])}",
            file=sys.stderr,
        )
        return 1

    if not rows:
        print(
            f"{bas_dd} 기준으로 조회된 ETF가 없습니다.\n"
            "  주말/공휴일 등 휴장일이면 KRX에 데이터가 없을 수 있습니다 - "
            "가장 최근 거래일 날짜로 다시 시도해보세요.\n"
            "  예: uv run krx_etf_list.py 20260717"
        )
        return 1

    print_etf_list(rows, bas_dd)
    return 0


if __name__ == "__main__":
    sys.exit(main())

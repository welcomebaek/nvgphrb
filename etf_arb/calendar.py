"""Trading-day calendar backed by KIS 국내휴장일조회 (CTCA0903R), cached locally.

KIS's own docstring warns this endpoint touches 원장서비스 and asks for at
most ~1 call per day - so results are cached to data/holiday_cache.json and
the API is only hit when the cache doesn't cover the dates we need.

Endpoint (verified via kis-code-assistant-mcp against official sample):
  GET /uapi/domestic-stock/v1/quotations/chk-holiday, tr_id CTCA0903R
  params: BASS_DT (YYYYMMDD), CTX_AREA_FK, CTX_AREA_NK (paging)
  output rows: bass_dt, opnd_yn (개장일여부 - the field to use for trading),
  plus tr_day_yn 등. Continuation: header tr_cont in ("M","F") -> re-call with
  tr_cont="N" and returned ctx_area_fk/nk.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime, timedelta

import httpx

from etf_arb import paths
from kis_common import KisApiError, REQUEST_TIMEOUT_SECONDS, sanitize

HOLIDAY_URL_PATH = "/uapi/domestic-stock/v1/quotations/chk-holiday"
TR_ID_CHK_HOLIDAY = "CTCA0903R"

CACHE_PATH = paths.HOLIDAY_CACHE_PATH

# 캐시가 오늘 이후 최소 이만큼의 달력을 커버해야 API 호출을 생략한다.
MIN_COVER_DAYS = 30


class CalendarError(Exception):
    pass


def _read_cache() -> dict[str, bool]:
    """{ 'YYYYMMDD': is_open } mapping."""
    try:
        raw = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        return {str(k): bool(v) for k, v in raw.items()}
    except (FileNotFoundError, json.JSONDecodeError, AttributeError):
        return {}


def _write_cache(cache: dict[str, bool]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=CACHE_PATH.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, CACHE_PATH)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _fetch_calendar(
    access_token: str, app_key: str, app_secret: str, base_url: str, bass_dt: str
) -> dict[str, bool]:
    """Call CTCA0903R once (with continuation paging) starting at bass_dt."""
    headers = {
        "Content-Type": "application/json",
        "authorization": f"Bearer {access_token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "tr_id": TR_ID_CHK_HOLIDAY,
        "custtype": "P",
    }
    secrets = [access_token, app_key, app_secret]
    result: dict[str, bool] = {}

    fk, nk, tr_cont = "", "", ""
    for _page in range(10):  # 공식 샘플과 동일한 최대 재귀 한도
        params = {"BASS_DT": bass_dt, "CTX_AREA_FK": fk, "CTX_AREA_NK": nk}
        req_headers = {**headers, "tr_cont": tr_cont}
        try:
            resp = httpx.get(
                f"{base_url}{HOLIDAY_URL_PATH}",
                headers=req_headers,
                params=params,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except httpx.RequestError as e:
            raise CalendarError(
                f"휴장일 조회 네트워크 오류: {sanitize(str(e), secrets)}"
            ) from None

        if resp.status_code != 200:
            raise CalendarError(
                f"휴장일 조회 실패 HTTP {resp.status_code}: "
                f"{sanitize(resp.text, secrets)[:300]}"
            )

        payload = resp.json()
        if payload.get("rt_cd") != "0":
            raise CalendarError(
                f"휴장일 조회 거부: {sanitize(str(payload.get('msg1', '')), secrets)}"
            )

        output = payload.get("output") or []
        if isinstance(output, dict):
            output = [output]
        for row in output:
            d = str(row.get("bass_dt", "")).strip()
            if len(d) == 8 and d.isdigit():
                result[d] = str(row.get("opnd_yn", "")).strip() == "Y"

        tr_cont_resp = resp.headers.get("tr_cont", "")
        if tr_cont_resp in ("M", "F"):
            fk = str(payload.get("ctx_area_fk", "")).strip()
            nk = str(payload.get("ctx_area_nk", "")).strip()
            tr_cont = "N"
            continue
        break

    if not result:
        raise CalendarError("휴장일 조회 응답에 유효한 날짜가 없습니다")
    return result


class TradingCalendar:
    """Trading-day arithmetic over the cached open/closed calendar."""

    def __init__(self, days: dict[str, bool]):
        self._days = days

    @classmethod
    def load(
        cls,
        access_token: str | None = None,
        app_key: str | None = None,
        app_secret: str | None = None,
        base_url: str | None = None,
        today: date | None = None,
    ) -> "TradingCalendar":
        """Load from cache; top up from KIS only if coverage is insufficient.

        Pass the KIS credentials only when a network top-up is acceptable;
        with credentials omitted, raises CalendarError if the cache can't
        cover today+MIN_COVER_DAYS.
        """
        today = today or date.today()
        cache = _read_cache()
        need_until = today + timedelta(days=MIN_COVER_DAYS)

        def covered() -> bool:
            d = today
            while d <= need_until:
                if d.strftime("%Y%m%d") not in cache:
                    return False
                d += timedelta(days=1)
            return True

        if not covered():
            if not all([access_token, app_key, app_secret, base_url]):
                raise CalendarError(
                    "휴장일 캐시가 부족한데 KIS 자격증명이 전달되지 않아 갱신할 수 없습니다"
                )
            fetched = _fetch_calendar(
                access_token, app_key, app_secret, base_url,
                today.strftime("%Y%m%d"),
            )
            cache.update(fetched)
            _write_cache(cache)
            if not covered():
                # CTCA0903R 한 호출이 커버하는 범위가 부족한 경우 마지막 날짜에서 이어서 1회 추가
                last = max(cache.keys())
                more = _fetch_calendar(
                    access_token, app_key, app_secret, base_url, last
                )
                cache.update(more)
                _write_cache(cache)

        return cls(cache)

    def is_trading_day(self, d: date) -> bool:
        key = d.strftime("%Y%m%d")
        if key not in self._days:
            raise CalendarError(f"캘린더 캐시 범위 밖의 날짜입니다: {key}")
        return self._days[key]

    def next_trading_day(self, d: date) -> date:
        cur = d + timedelta(days=1)
        for _ in range(60):
            if self.is_trading_day(cur):
                return cur
            cur += timedelta(days=1)
        raise CalendarError("60일 내에 다음 거래일을 찾지 못했습니다")

    def add_trading_days(self, d: date, n: int) -> date:
        """d로부터 n 거래일 뒤의 날짜 (n>=1). d 자신은 세지 않는다."""
        if n < 1:
            raise ValueError("n은 1 이상이어야 합니다")
        cur = d
        remaining = n
        while remaining > 0:
            cur = self.next_trading_day(cur)
            remaining -= 1
        return cur

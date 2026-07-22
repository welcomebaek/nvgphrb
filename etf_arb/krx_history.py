"""KRX 일별 ETF 데이터 백필 + 로컬 캐시 (Phase 1 유니버스 선정용).

krx_etf_list.py의 수집 메커니즘을 그대로 재사용한다:
  GET https://data-dbg.krx.co.kr/svc/apis/etp/etf_bydd_trd
  헤더 AUTH_KEY (.env의 KRX_AUTH_KEY), 파라미터 basDd=YYYYMMDD,
  응답 envelope의 OutBlock_1 리스트가 그 날짜의 전체 ETF 행.

주말/공휴일은 OutBlock_1이 빈 리스트로 온다. 빈 응답도 `[]`로 캐시해서
재실행 시 다시 조회하지 않되, 목표 거래일 수에는 세지 않는다.

캐시 위치: data/krx_daily/YYYYMMDD.json (OutBlock_1 리스트 원본 그대로).
증분 동작: 이미 캐시된 날짜는 API를 호출하지 않으므로, 재실행은 빠르게
빠진 날짜만 채운다.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from etf_arb import paths
from krx_etf_list import KrxApiError, fetch_etf_list, load_krx_auth_key

CACHE_DIR = paths.KRX_CACHE_DIR

FETCH_SLEEP_SECONDS = 0.35  # KRX 호출 간 대기 (예의상 스로틀)
PROGRESS_EVERY_FETCHES = 10
FETCH_RETRIES = 3           # 일시적 네트워크 오류(타임아웃 등) 재시도 횟수
RETRY_BACKOFF_SECONDS = 2.0
# 거래일 1일 확보에 달력일 ~1.5일 소요 + 연휴 여유. 이 이상 걸어도 못 채우면 중단.
MAX_WALKBACK_FACTOR = 2.0
MAX_WALKBACK_EXTRA_DAYS = 30


def _cache_path(bas_dd: str) -> Path:
    return CACHE_DIR / f"{bas_dd}.json"


def _read_cached_day(bas_dd: str) -> list[dict[str, Any]] | None:
    """캐시된 하루치 데이터. 파일이 없거나 손상이면 None (재조회 대상)."""
    path = _cache_path(bas_dd)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, list):
        return None
    return data


def _write_cached_day(bas_dd: str, rows: list[dict[str, Any]]) -> None:
    """하루치 데이터를 원자적으로 캐시에 기록한다."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=CACHE_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False)
        os.replace(tmp, _cache_path(bas_dd))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _is_trading_day_rows(rows: list[dict[str, Any]]) -> bool:
    """실제 거래가 있었던 날인지 판별.

    KRX는 주말엔 OutBlock_1을 빈 리스트로 주지만, 일부 공휴일에는
    가격 필드가 전부 빈 문자열인 행 스켈레톤을 반환한다 (예: 20260101).
    종가(TDD_CLSPRC)가 하나라도 채워져 있어야 거래일로 센다.
    """
    return any(str(r.get("TDD_CLSPRC", "")).strip() for r in rows)


def _fetch_with_retry(auth_key: str, bas_dd: str) -> list[dict[str, Any]]:
    """일시적 네트워크 오류에 한해 짧은 백오프로 재시도한다.

    ~170회 호출 루프 전체가 타임아웃 한 번에 죽지 않도록 하는 안전장치.
    인증 오류(KrxAuthPendingError 등 명확한 거부)는 재시도하지 않고 그대로 올린다.
    """
    last_error: KrxApiError | None = None
    for attempt in range(1, FETCH_RETRIES + 1):
        try:
            return fetch_etf_list(auth_key, bas_dd)
        except KrxApiError as e:
            msg = str(e)
            transient = "Network error" in msg or "timed out" in msg
            if not transient or attempt == FETCH_RETRIES:
                raise
            last_error = e
            print(f"  {bas_dd} 조회 일시 오류, {RETRY_BACKOFF_SECONDS}s 후 재시도 ({attempt}/{FETCH_RETRIES})")
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    raise last_error  # pragma: no cover - 위 루프에서 항상 raise/return


def ensure_history(
    days: int, today: date | None = None
) -> dict[str, list[dict[str, Any]]]:
    """어제부터 달력일을 거슬러 올라가며 비어있지 않은(=거래일) 응답을
    `days`일치 확보해 {YYYYMMDD: rows}로 반환한다.

    오늘 날짜는 장 마감 후에야 데이터가 생기므로 어제부터 시작한다.
    이미 캐시된 날짜(빈 날짜 포함)는 API를 호출하지 않는다.
    """
    if days < 1:
        raise ValueError("days는 1 이상이어야 합니다")

    today = today or date.today()
    auth_key = load_krx_auth_key()

    collected: dict[str, list[dict[str, Any]]] = {}
    fetched = 0
    reused = 0
    max_walkback = int(days * MAX_WALKBACK_FACTOR) + MAX_WALKBACK_EXTRA_DAYS

    print(f"KRX 일별 ETF 데이터 확보 시작: 목표 거래일 {days}일 (캐시: {CACHE_DIR})")

    d = today - timedelta(days=1)
    for _step in range(max_walkback):
        if len(collected) >= days:
            break
        bas_dd = d.strftime("%Y%m%d")
        rows = _read_cached_day(bas_dd)
        if rows is None:
            if not auth_key:
                raise KrxApiError(
                    "KRX_AUTH_KEY가 .env에 설정되어 있지 않아 백필을 진행할 수 없습니다"
                )
            rows = _fetch_with_retry(auth_key, bas_dd)
            _write_cached_day(bas_dd, rows)
            fetched += 1
            if fetched % PROGRESS_EVERY_FETCHES == 0:
                print(
                    f"  진행: API {fetched}건 조회, 거래일 "
                    f"{len(collected)}/{days}일 확보 (현재 {bas_dd})"
                )
            time.sleep(FETCH_SLEEP_SECONDS)
        else:
            reused += 1
        if rows and _is_trading_day_rows(rows):
            collected[bas_dd] = rows
        d -= timedelta(days=1)

    if len(collected) < days:
        raise KrxApiError(
            f"달력일 {max_walkback}일을 거슬러도 거래일 {days}일을 채우지 못했습니다 "
            f"(확보 {len(collected)}일). KRX 응답 상태를 확인하세요."
        )

    dates = sorted(collected)
    print(
        f"KRX 데이터 확보 완료: 거래일 {len(collected)}일 "
        f"({dates[0]} ~ {dates[-1]}), 신규 조회 {fetched}건, 캐시 재사용 {reused}건"
    )
    return collected

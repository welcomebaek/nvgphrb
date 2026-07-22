"""Phase 1: ETF NAV 괴리율 트레이딩 유니버스 선정 CLI -> etf_watchlist.json

흐름:
  1. etf_arb_config.json 로드 (etf_arb.config.load_config)
  2. KRX 일별 ETF 데이터 lookback_days 거래일치 확보 (etf_arb.krx_history,
     data/krx_daily/ 증분 캐시)
  3. 종목별 괴리율 시계열/에피소드 통계 계산 (etf_arb.universe, 순수함수)
  4. 하드 필터 + 스코어 랭킹
  5. 상위 5종목을 KIS NAV비교추이(일)로 교차검증 (KRX 계산 괴리율 vs KIS dprt)
  6. 장중이면 실시간 호가로 스프레드 검사, 초과 종목 제외
  7. etf_watchlist.json 기록 + 요약 테이블 출력

사용 KIS API (kis-code-assistant-mcp로 스펙 확인 완료):
  - NAV 비교추이(일): GET /uapi/etfetn/v1/quotations/nav-comparison-daily-trend
    tr_id FHPST02440200, params FID_COND_MRKT_DIV_CODE=J / FID_INPUT_ISCD /
    FID_INPUT_DATE_1 / FID_INPUT_DATE_2 (최대 100행), output[].dprt = 괴리율
  - 주식현재가 호가/예상체결: GET /uapi/domestic-stock/v1/quotations/
    inquire-asking-price-exp-ccn, tr_id FHKST01010200, output1.askp1/bidp1
    (ETF 스냅샷 inquire-price(FHPST02400000)에는 호가 필드가 없어 이 엔드포인트 사용)

주문/매매 없음 - 순수 조회 전용. 실행: uv run etf_universe_select.py
"""

from __future__ import annotations

import json
import sys
import time
from datetime import date, datetime, timedelta
from typing import Any

import httpx

from etf_arb.calendar import CalendarError, TradingCalendar
from etf_arb.config import ConfigError, load_config
from etf_arb.krx_history import ensure_history
from etf_arb import paths, universe
from kis_common import (
    KisApiError,
    REQUEST_TIMEOUT_SECONDS,
    get_access_token,
    load_credentials,
    sanitize,
)
from krx_etf_list import KrxApiError

TOKEN_CACHE_PATH = paths.TOKEN_CACHE_PATH
WATCHLIST_PATH = paths.WATCHLIST_PATH

NAV_DAILY_URL_PATH = "/uapi/etfetn/v1/quotations/nav-comparison-daily-trend"
TR_ID_NAV_DAILY = "FHPST02440200"
ASKING_URL_PATH = "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn"
TR_ID_ASKING = "FHKST01010200"

CROSS_CHECK_TOP_N = 5
CROSS_CHECK_LOOKBACK_CAL_DAYS = 90  # 100행 한도 내에서 1콜로 끝나는 범위
CROSS_CHECK_WARN_PCTPT = 0.05
KIS_THROTTLE_SECONDS = 0.25  # <= 5 req/s


def _kis_get(
    token: str,
    app_key: str,
    app_secret: str,
    base_url: str,
    url_path: str,
    tr_id: str,
    params: dict[str, str],
) -> dict[str, Any]:
    """KIS REST GET 공통 래퍼. rt_cd=0 확인 후 전체 payload 반환."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/plain",
        "charset": "UTF-8",
        "authorization": f"Bearer {token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "tr_id": tr_id,
        "custtype": "P",
        "tr_cont": "",
    }
    secrets = [token, app_key, app_secret]
    try:
        resp = httpx.get(
            f"{base_url}{url_path}",
            headers=headers,
            params=params,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except httpx.RequestError as e:
        raise KisApiError(
            f"KIS 요청 네트워크 오류({tr_id}): {sanitize(str(e), secrets)}"
        ) from None

    if resp.status_code != 200:
        raise KisApiError(
            f"KIS 조회 실패({tr_id}) HTTP {resp.status_code}: "
            f"{sanitize(resp.text, secrets)[:300]}"
        )

    try:
        payload = resp.json()
    except json.JSONDecodeError:
        raise KisApiError(f"KIS 응답이 JSON이 아닙니다({tr_id})") from None

    if payload.get("rt_cd") != "0":
        raise KisApiError(
            f"KIS 조회 거부({tr_id}): "
            f"{sanitize(str(payload.get('msg1', 'unknown')), secrets)}"
        )
    return payload


def kis_cross_check(
    candidates: list[dict[str, Any]],
    token: str,
    app_key: str,
    app_secret: str,
    base_url: str,
    today: date,
) -> None:
    """상위 후보의 KRX 계산 괴리율을 KIS dprt와 날짜별로 대조한다."""
    d1 = (today - timedelta(days=CROSS_CHECK_LOOKBACK_CAL_DAYS)).strftime("%Y%m%d")
    d2 = today.strftime("%Y%m%d")

    print(f"\n[KIS 교차검증] 상위 {len(candidates)}종목, 기간 {d1}~{d2}")
    for cand in candidates:
        code = cand["code"]
        try:
            payload = _kis_get(
                token, app_key, app_secret, base_url,
                NAV_DAILY_URL_PATH, TR_ID_NAV_DAILY,
                {
                    "FID_COND_MRKT_DIV_CODE": "J",
                    "FID_INPUT_ISCD": code,
                    "FID_INPUT_DATE_1": d1,
                    "FID_INPUT_DATE_2": d2,
                },
            )
        except KisApiError as e:
            print(f"  {code} {cand['name']}: 조회 실패 - {e}")
            time.sleep(KIS_THROTTLE_SECONDS)
            continue

        rows = payload.get("output") or []
        if isinstance(rows, dict):
            rows = [rows]
        kis_dprt: dict[str, float] = {}
        for row in rows:
            dt = str(row.get("stck_bsop_date", "")).strip()
            val = universe.parse_number(row.get("dprt"))
            if len(dt) == 8 and val is not None:
                kis_dprt[dt] = val

        ours = {dt: d * 100.0 for dt, d in cand["disparity"]}
        overlap = sorted(set(kis_dprt) & set(ours))
        if not overlap:
            print(f"  {code} {cand['name']}: 겹치는 날짜 없음 (KIS {len(kis_dprt)}일)")
            time.sleep(KIS_THROTTLE_SECONDS)
            continue

        diffs = [abs(kis_dprt[dt] - ours[dt]) for dt in overlap]
        mad = sum(diffs) / len(diffs)
        flag = "  <- 경고: 0.05%p 초과" if mad > CROSS_CHECK_WARN_PCTPT else ""
        print(
            f"  {code} {cand['name']}: {len(overlap)}일 대조, "
            f"평균 절대차 {mad:.4f}%p (최대 {max(diffs):.4f}%p){flag}"
        )
        time.sleep(KIS_THROTTLE_SECONDS)


def fetch_spread_pct(
    code: str, token: str, app_key: str, app_secret: str, base_url: str
) -> float | None:
    """실시간 매도1/매수1호가로 스프레드%를 계산. 호가 없으면 None."""
    payload = _kis_get(
        token, app_key, app_secret, base_url,
        ASKING_URL_PATH, TR_ID_ASKING,
        {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code},
    )
    out1 = payload.get("output1") or {}
    ask1 = universe.parse_number(out1.get("askp1"))
    bid1 = universe.parse_number(out1.get("bidp1"))
    if not ask1 or not bid1 or ask1 <= 0 or bid1 <= 0:
        return None
    mid = (ask1 + bid1) / 2.0
    return (ask1 - bid1) / mid * 100.0


def is_market_hours_now(cal: TradingCalendar, now: datetime) -> bool:
    try:
        if not cal.is_trading_day(now.date()):
            return False
    except CalendarError:
        return False
    hm = now.strftime("%H%M")
    return "0900" <= hm <= "1530"


def print_summary_table(watchlist: list[dict[str, Any]]) -> None:
    print(f"\n=== 최종 워치리스트 ({len(watchlist)}종목) ===")
    header = (
        f"{'#':>2} {'코드':<7} {'종목명':<24} {'스코어':>8} {'횟수/100d':>9} "
        f"{'해소율':>6} {'순엣지%':>8} {'중앙대금(억)':>10} {'스프레드%':>9}"
    )
    print(header)
    print("-" * 100)
    for i, t in enumerate(watchlist, 1):
        name = t["name"][:22]
        edge = t["mean_net_edge_pct"]
        spread = t["median_spread_pct"]
        print(
            f"{i:>2} {t['code']:<7} {name:<24} {t['score']:>8.3f} "
            f"{t['episodes_per_100d']:>9.2f} {t['p_resolve_within_n']:>6.2f} "
            f"{(edge if edge is not None else float('nan')):>8.3f} "
            f"{t['median_trdval'] / 1e8:>10.1f} "
            f"{(f'{spread:.3f}' if spread is not None else '-'):>9}"
        )


def main() -> int:
    cfg = load_config()
    ucfg = cfg.universe

    # 1) KRX 일별 데이터 확보 (증분 캐시)
    history = ensure_history(ucfg.lookback_days)
    dates = sorted(history)

    # 2) 종목별 집계 + 하드 필터
    aggregates, n_days = universe.build_etf_aggregates(history)
    survivors, funnel = universe.apply_filters(
        aggregates,
        n_days,
        ucfg.min_daily_value_krw,
        ucfg.max_price_krw,
        ucfg.exclude_foreign_underlying,
    )
    print(
        f"\n[필터 퍼널] 전체 {funnel['total']} -> 거래대금 {funnel['after_trdval']} "
        f"-> 가격 {funnel['after_price']} -> 상장기간 {funnel['after_presence']} "
        f"-> 해외기초 제외 {funnel['after_foreign']}"
    )

    # 3) 에피소드 통계 + 스코어 (공유 랭킹 함수 - etf_intraday_sampler.py와 동일 로직)
    score_threshold = universe.closest_threshold(
        ucfg.scan_entry_thresholds_pct, cfg.signals.entry_threshold_pct
    )
    ranked = universe.rank_survivors(
        survivors,
        ucfg.scan_entry_thresholds_pct,
        ucfg.scan_exit_threshold_pct,
        cfg.signals.force_exit_days,
        cfg.fees.commission_rate_pct,
        score_threshold,
    )
    n_positive = sum(1 for a in ranked if a["score"] > 0)
    print(
        f"[랭킹] 후보 {len(ranked)}종목 중 스코어 양수 {n_positive}종목 "
        f"(스코어 기준 임계값 {score_threshold:g}%)"
    )

    # 4) KIS 인증 (실전 조회용 키, 토큰 캐시 재사용)
    creds = load_credentials(["KIS_APP_KEY", "KIS_APP_SECRET", "KIS_URL_REST"])
    base_url = creds["KIS_URL_REST"]
    token = get_access_token(
        creds["KIS_APP_KEY"], creds["KIS_APP_SECRET"], base_url, TOKEN_CACHE_PATH
    )

    # 5) 상위 5종목 KRX vs KIS 괴리율 교차검증
    kis_cross_check(
        ranked[:CROSS_CHECK_TOP_N],
        token, creds["KIS_APP_KEY"], creds["KIS_APP_SECRET"], base_url,
        date.today(),
    )

    # 6) 장중이면 실시간 스프레드 검사
    now = datetime.now()
    try:
        cal = TradingCalendar.load(
            access_token=token,
            app_key=creds["KIS_APP_KEY"],
            app_secret=creds["KIS_APP_SECRET"],
            base_url=base_url,
        )
        market_open = is_market_hours_now(cal, now)
    except CalendarError as e:
        print(f"\n캘린더 로드 실패로 스프레드 검사를 건너뜁니다: {e}")
        market_open = False

    watchlist: list[dict[str, Any]] = []
    if market_open:
        print(
            f"\n[스프레드 검사] 장중({now.strftime('%H:%M')}) - "
            f"실시간 호가 기준, 상한 {ucfg.max_spread_pct}%"
        )
        for cand in ranked:
            if len(watchlist) >= ucfg.max_watchlist_size:
                break
            try:
                spread = fetch_spread_pct(
                    cand["code"], token,
                    creds["KIS_APP_KEY"], creds["KIS_APP_SECRET"], base_url,
                )
            except KisApiError as e:
                print(f"  {cand['code']} {cand['name']}: 호가 조회 실패 - {e} -> 제외")
                time.sleep(KIS_THROTTLE_SECONDS)
                continue
            time.sleep(KIS_THROTTLE_SECONDS)
            if spread is None:
                print(f"  {cand['code']} {cand['name']}: 유효 호가 없음 -> 제외")
                continue
            if spread > ucfg.max_spread_pct:
                print(
                    f"  {cand['code']} {cand['name']}: 스프레드 {spread:.3f}% > "
                    f"{ucfg.max_spread_pct}% -> 제외"
                )
                continue
            cand["median_spread_pct"] = round(spread, 4)
            watchlist.append(cand)
    else:
        print("\n[스프레드 검사] 장외 시간이므로 건너뜀 (median_spread_pct=null)")
        for cand in ranked[: ucfg.max_watchlist_size]:
            cand["median_spread_pct"] = None
            watchlist.append(cand)

    # 7) etf_watchlist.json 기록
    main_key = f"{score_threshold:g}"
    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "lookback": {
            "trading_days": n_days,
            "start": dates[0],
            "end": dates[-1],
        },
        "thresholds": {
            "scan_entry_thresholds_pct": list(ucfg.scan_entry_thresholds_pct),
            "scan_exit_threshold_pct": ucfg.scan_exit_threshold_pct,
            "signal_entry_threshold_pct": cfg.signals.entry_threshold_pct,
            "score_threshold_pct": score_threshold,
            "force_exit_days": cfg.signals.force_exit_days,
        },
        "spread_checked_live": market_open,
        "tickers": [],
    }
    for cand in watchlist:
        main_stats = cand["episodes"][main_key]
        out["tickers"].append(
            {
                "code": cand["code"],
                "name": cand["name"],
                "idx_ind_nm": cand["idx_ind_nm"],
                "foreign_underlying": cand["foreign_underlying"],
                "median_trdval": int(cand["median_trdval"]),
                "median_spread_pct": cand["median_spread_pct"],
                "episodes": cand["episodes"],
                "p_resolve_within_n": main_stats["p_resolve_within_n"],
                "mean_net_edge_pct": main_stats["mean_net_edge_pct"],
                "score": cand["score"],
            }
        )
    WATCHLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    WATCHLIST_PATH.write_text(
        json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"\n워치리스트 저장 완료: {WATCHLIST_PATH} ({len(watchlist)}종목)")

    # 요약 테이블 (mean_net_edge/p_resolve/episodes_per_100d는 기준 임계값 것)
    for cand in watchlist:
        s = cand["episodes"][main_key]
        cand["episodes_per_100d"] = s["episodes_per_100d"]
        cand["p_resolve_within_n"] = s["p_resolve_within_n"]
        cand["mean_net_edge_pct"] = s["mean_net_edge_pct"]
    print_summary_table(watchlist)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ConfigError as e:
        print(f"설정 오류: {e}", file=sys.stderr)
        sys.exit(1)
    except (KrxApiError, KisApiError, CalendarError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n중단됨 (캐시는 보존되어 재실행 시 이어서 진행)", file=sys.stderr)
        sys.exit(130)
    except Exception as e:  # noqa: BLE001 - top-level safety net
        print(f"Unexpected error: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)

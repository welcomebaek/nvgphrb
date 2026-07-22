"""장중 광역 ETF 괴리율 샘플러 (Phase 1.5).

일별 종가 데이터의 구조적 맹점 - "하루 안에 열리고 닫힌 괴리 에피소드는
관측 불가" - 을 메우기 위해, 넓은 국내 ETF 후보군(~100종목)의 현재가/NAV/
괴리율을 장중 1분 주기로 REST 폴링해 JSONL로 축적한다. 이 데이터가 쌓이면
워치리스트 리프레셔(Phase 2.5)가 분 단위 에피소드 통계로 재선정에 활용한다.

데이터 소스:
  - KIS ETF/ETN 현재가 (FHPST02400000, GET /uapi/etfetn/v1/quotations/
    inquire-price) - 응답 output의 stck_prpr(현재가) / nav / dprt(괴리율)
    필드 사용 (공식 샘플로 확인됨). 이게 주(primary) 조회 - 실패 시 종목 스킵.
  - 주식현재가 호가 (FHKST01010200, GET /uapi/domestic-stock/v1/quotations/
    inquire-asking-price-exp-ccn) - output1.askp1/bidp1로 매도1/매수1호가
    스프레드%를 종목당 한 건 더 조회해 축적한다. 장이 닫힌 08:15 장전
    리프레셔가 실시간 호가를 못 보므로, 이렇게 며칠 쌓인 스프레드의 N일
    이동평균으로 구조적 고스프레드 종목(레버리지/인버스 등)을 걸러내기 위함.
    스프레드 조회는 실패해도 해당 필드만 null로 남기고 스윕은 계속한다.

후보군: KRX 일별 캐시에서 유니버스 하드필터(거래대금/가격/상장기간/해외기초
제외)를 통과한 국내 ETF를 스코어순으로 최대 SAMPLE_POOL_SIZE종목.
스프레드 필터는 적용하지 않는다 - 샘플러의 목적이 바로 "넓게 보기"다.

레이트리밋: 종목당 현재가+호가 2콜 x 0.1s 간격(약 20req/s 피크, KIS 한도
근처지만 콜 사이 0.1s로 평활) → 100종목 스윕에 ~20초, 1분 주기 내 여유.

실행: uv run etf_intraday_sampler.py   (장중이 아니면 개장 대기/종료 안내)
중단: Ctrl-C (그때까지 수집분은 저널에 남음)
"""

from __future__ import annotations

import json
import signal
import sys
import time
from datetime import date, datetime, time as dtime
from pathlib import Path

import httpx

from etf_arb.calendar import TradingCalendar
from etf_arb.config import load_config
from etf_arb.krx_history import ensure_history
from etf_arb import universe
from kis_common import (
    KisApiError,
    REQUEST_TIMEOUT_SECONDS,
    get_access_token,
    load_credentials,
    sanitize,
)
from kis_price import TOKEN_CACHE_PATH

ETF_PRICE_URL_PATH = "/uapi/etfetn/v1/quotations/inquire-price"
TR_ID_ETF_PRICE = "FHPST02400000"
# 호가 스프레드 조회 (etf_universe_select.fetch_spread_pct와 동일 엔드포인트/파라미터).
# 순환 임포트를 피하려고 상수만 여기 두고 소형 로컬 헬퍼로 미러링한다.
ASKING_URL_PATH = "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn"
TR_ID_ASKING = "FHKST01010200"

SAMPLE_POOL_SIZE = 100
SWEEP_INTERVAL_SECONDS = 60.0
PER_REQUEST_GAP_SECONDS = 0.1

MARKET_OPEN = dtime(9, 0)
MARKET_CLOSE = dtime(15, 30)

PROJECT_ROOT = Path(__file__).resolve().parent
JOURNAL_PATH = PROJECT_ROOT / "logs" / "intraday_samples.jsonl"

_shutdown = False


def _handle_signal(signum, frame):  # noqa: ARG001
    global _shutdown
    _shutdown = True


def _self_compute_sample_pool(cfg) -> list[dict]:
    """KRX 캐시에서 하드필터 통과 국내 ETF를 스코어순 상위 N종목 선정 (폴백 경로)."""
    ucfg = cfg.universe
    history = ensure_history(ucfg.lookback_days)
    aggregates, n_days = universe.build_etf_aggregates(history)
    survivors, funnel = universe.apply_filters(
        aggregates,
        n_days,
        ucfg.min_daily_value_krw,
        ucfg.max_price_krw,
        ucfg.exclude_foreign_underlying,
    )
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
    pool = ranked[:SAMPLE_POOL_SIZE]
    print(
        f"[자체계산] 샘플 풀 구성: 필터 통과 {len(survivors)}종목 중 스코어순 {len(pool)}종목 "
        f"(퍼널 {funnel})"
    )
    return pool


def build_sample_pool(cfg) -> list[dict]:
    """오늘자 `etf_candidates_ranked.json`(있고 신선하면)을 우선 사용, 아니면
    자체 계산 폴백 (KRX 캐시 + 하드필터 + 공유 랭킹 함수).

    독립 실행성 유지가 목적: 워치리스트 리프레셔가 아직 안 돌았거나 실패해도
    샘플러 단독으로 계속 동작해야 한다.
    """
    ranked_path = PROJECT_ROOT / "etf_candidates_ranked.json"
    if ranked_path.exists():
        try:
            raw = json.loads(ranked_path.read_text(encoding="utf-8"))
            generated_at = str(raw.get("generated_at", ""))
            candidates = raw.get("candidates") or []
            if generated_at[:10] == date.today().isoformat() and candidates:
                pool = candidates[:SAMPLE_POOL_SIZE]
                print(
                    f"[랭킹 파일에서 로드] {ranked_path.name} (생성 {generated_at}) - "
                    f"{len(pool)}종목"
                )
                return pool
            print(
                f"{ranked_path.name}이 오늘자가 아니거나 비어 있어 자체 계산으로 폴백합니다 "
                f"(generated_at={generated_at!r})"
            )
        except (json.JSONDecodeError, OSError) as e:
            print(f"{ranked_path.name} 읽기 실패, 자체 계산으로 폴백합니다: {e}")
    else:
        print(f"{ranked_path.name}이 없어 자체 계산으로 폴백합니다")

    return _self_compute_sample_pool(cfg)


def fetch_etf_snapshot(
    client: httpx.Client,
    base_url: str,
    access_token: str,
    app_key: str,
    app_secret: str,
    code: str,
) -> dict | None:
    """FHPST02400000 한 건 조회. 실패 시 None (스윕은 계속)."""
    headers = {
        "Content-Type": "application/json",
        "authorization": f"Bearer {access_token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "tr_id": TR_ID_ETF_PRICE,
        "custtype": "P",
    }
    params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}
    try:
        resp = client.get(
            f"{base_url}{ETF_PRICE_URL_PATH}",
            headers=headers,
            params=params,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except httpx.RequestError:
        return None
    if resp.status_code != 200:
        return None
    try:
        payload = resp.json()
    except json.JSONDecodeError:
        return None
    if payload.get("rt_cd") != "0":
        return None
    output = payload.get("output")
    return output if isinstance(output, dict) else None


def fetch_spread(
    client: httpx.Client,
    base_url: str,
    access_token: str,
    app_key: str,
    app_secret: str,
    code: str,
) -> tuple[float | None, float | None, float | None]:
    """FHKST01010200 호가 한 건 조회 -> (askp1, bidp1, spread_pct).

    etf_universe_select.fetch_spread_pct와 동일한 엔드포인트/파라미터/필드
    파싱 패턴을 샘플러의 httpx.Client로 미러링한다(순환 임포트 회피). 실패
    또는 유효호가 없음이면 (None, None, None)을 반환하고 스윕은 계속한다.
    """
    headers = {
        "Content-Type": "application/json",
        "authorization": f"Bearer {access_token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "tr_id": TR_ID_ASKING,
        "custtype": "P",
    }
    params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}
    try:
        resp = client.get(
            f"{base_url}{ASKING_URL_PATH}",
            headers=headers,
            params=params,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except httpx.RequestError:
        return None, None, None
    if resp.status_code != 200:
        return None, None, None
    try:
        payload = resp.json()
    except json.JSONDecodeError:
        return None, None, None
    if payload.get("rt_cd") != "0":
        return None, None, None
    out1 = payload.get("output1") or {}
    ask1 = universe.parse_number(out1.get("askp1"))
    bid1 = universe.parse_number(out1.get("bidp1"))
    if not ask1 or not bid1 or ask1 <= 0 or bid1 <= 0:
        return None, None, None
    spread_pct = (ask1 - bid1) / ((ask1 + bid1) / 2.0) * 100.0
    return ask1, bid1, spread_pct


def main() -> int:
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    cfg = load_config()
    creds = load_credentials(["KIS_APP_KEY", "KIS_APP_SECRET", "KIS_URL_REST"])
    base_url = creds["KIS_URL_REST"]
    app_key, app_secret = creds["KIS_APP_KEY"], creds["KIS_APP_SECRET"]

    token = get_access_token(app_key, app_secret, base_url, TOKEN_CACHE_PATH)
    cal = TradingCalendar.load(token, app_key, app_secret, base_url)

    today = date.today()
    if not cal.is_trading_day(today):
        print(f"{today}는 휴장일입니다. 샘플러를 실행하지 않습니다.")
        return 0

    now = datetime.now()
    if now.time() >= MARKET_CLOSE:
        print(f"장 마감({MARKET_CLOSE}) 이후입니다. 다음 거래일에 실행하세요.")
        return 0
    if now.time() < MARKET_OPEN:
        wait_s = (
            datetime.combine(today, MARKET_OPEN) - now
        ).total_seconds()
        print(f"개장 전입니다. {int(wait_s)}초 후 개장 시각까지 대기합니다.")
        time.sleep(max(0.0, wait_s))

    pool = build_sample_pool(cfg)
    codes = [(a["code"], a["name"]) for a in pool]
    JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)

    print(
        f"장중 샘플링 시작: {len(codes)}종목, {SWEEP_INTERVAL_SECONDS:.0f}s 주기, "
        f"저널 {JOURNAL_PATH}"
    )

    sweeps = 0
    with httpx.Client() as client, JOURNAL_PATH.open("a", encoding="utf-8") as journal:
        while not _shutdown and datetime.now().time() < MARKET_CLOSE:
            sweep_start = time.monotonic()
            ts = datetime.now().isoformat(timespec="seconds")
            ok = 0
            for code, _name in codes:
                if _shutdown:
                    break
                output = fetch_etf_snapshot(
                    client, base_url, token, app_key, app_secret, code
                )
                if output is not None:
                    # 현재가(primary) 성공 시에만 호가 스프레드를 추가 조회.
                    # 스프레드 콜 실패는 해당 필드만 null로 남기고 스윕 계속.
                    time.sleep(PER_REQUEST_GAP_SECONDS)  # 현재가/호가 콜 사이 간격
                    ask1, bid1, spread_pct = fetch_spread(
                        client, base_url, token, app_key, app_secret, code
                    )
                    record = {
                        "ts": ts,
                        "code": code,
                        "prpr": output.get("stck_prpr"),
                        "nav": output.get("nav"),
                        "dprt": output.get("dprt"),
                        "askp1": ask1,
                        "bidp1": bid1,
                        "spread_pct": (
                            round(spread_pct, 4) if spread_pct is not None else None
                        ),
                    }
                    journal.write(json.dumps(record, ensure_ascii=False) + "\n")
                    ok += 1
                time.sleep(PER_REQUEST_GAP_SECONDS)
            journal.flush()
            sweeps += 1
            if sweeps % 10 == 1:
                print(f"  스윕 #{sweeps} ({ts}): {ok}/{len(codes)}종목 수집")

            elapsed = time.monotonic() - sweep_start
            remaining = SWEEP_INTERVAL_SECONDS - elapsed
            # 다음 스윕까지 대기 (중단 신호에 1초 단위로 반응)
            while remaining > 0 and not _shutdown:
                step = min(1.0, remaining)
                time.sleep(step)
                remaining -= step

    print(f"샘플링 종료: 총 {sweeps}회 스윕. 저널: {JOURNAL_PATH}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KisApiError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:  # noqa: BLE001 - 최상위 안전망
        print(f"Unexpected error: {type(e).__name__}: {sanitize(str(e), [])}", file=sys.stderr)
        sys.exit(1)

"""Phase 2.5: 일일 워치리스트 리프레셔 CLI -> etf_candidates_ranked.json + etf_watchlist.json

Phase 1(`etf_universe_select.py`)은 120거래일 종가 기준 통계만으로 1회성
워치리스트를 만들었다. 이 스크립트는 매일 자동으로(launchd 08:15) 재계산해,
Phase 1.5 샘플러(`etf_intraday_sampler.py`)가 쌓은 장중 분 단위 괴리 데이터를
반영한 합성 스코어로 워치리스트를 새로 고친다.

흐름 (설계 문서 Phase 2.5 "파이프라인" 8단계):
  1. 휴장일이면 즉시 종료 (파일 변경 없음)
  2. Portfolio.load()로 보유종목(held_codes) 확인
  3. KRX 일별 데이터 확보 -> 종목별 집계(필터 전 원본 보관) -> 하드 필터
  4. universe.rank_survivors()로 일별 스코어 계산
  5. logs/intraday_samples.jsonl을 세션별로 파싱해 종목별 장중 에피소드 통계
  6. universe.blend_scores()로 일별/장중 스코어를 백분위 정규화 후 합성
  7. etf_candidates_ranked.json 저장 (상위 SAMPLE_POOL_SIZE - 샘플러 풀)
  8. 보유종목 히스테리시스: 보유종목 무조건 전원 포함(폴백체인: 필터통과
     랭킹 -> 필터 전 원본집계 -> 이전 워치리스트 -> 스텁) + 나머지 슬롯을
     combined_score 내림차순 신규 후보로 채움(장중이면 실시간 스프레드 검사,
     보유종목은 면제) -> etf_watchlist.json 원자적 저장

주문/매매 없음 - 순수 조회 전용. 실행: uv run etf_watchlist_refresh.py [--force-spread-check] [--portfolio-path PATH] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any

from etf_arb.calendar import CalendarError, TradingCalendar
from etf_arb.config import ConfigError, load_config
from etf_arb.intraday_history import load_intraday_sessions
from etf_arb.krx_history import ensure_history
from etf_arb.portfolio import DEFAULT_STATE_PATH, Portfolio, PortfolioError
from etf_arb.spread_history import (
    exclude_for_spread,
    load_daily_spread_medians,
    nday_ma_spread,
)
from etf_arb import universe
from etf_intraday_sampler import SAMPLE_POOL_SIZE
from etf_universe_select import (
    KIS_THROTTLE_SECONDS,
    fetch_spread_pct,
    is_market_hours_now,
)
from kis_common import (
    KisApiError,
    get_access_token,
    load_credentials,
)
from krx_etf_list import KrxApiError

SCRIPT_DIR = Path(__file__).resolve().parent
TOKEN_CACHE_PATH = SCRIPT_DIR / ".kis_token_cache.json"
WATCHLIST_PATH = SCRIPT_DIR / "etf_watchlist.json"
CANDIDATES_PATH = SCRIPT_DIR / "etf_candidates_ranked.json"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """temp 파일 + os.replace 원자적 쓰기 (krx_history/portfolio와 동일 패턴)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_previous_watchlist_by_code(path: Path) -> dict[str, dict[str, Any]]:
    """어제자 etf_watchlist.json을 코드별 dict로. 없거나 손상이면 빈 dict."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        tickers = raw.get("tickers") or []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return {str(t["code"]): t for t in tickers if t.get("code")}


def _entry_from_candidate(
    cand: dict[str, Any],
    main_key: str,
    pinned: bool,
    spread: float | None,
    nday_ma_spread_pct: float | None = None,
    spread_days: int = 0,
) -> dict[str, Any]:
    """rank_survivors/blend_scores를 거친(필터 통과) 후보 -> 워치리스트 항목."""
    main_stats = cand["episodes"][main_key]
    return {
        "code": cand["code"],
        "name": cand["name"],
        "idx_ind_nm": cand["idx_ind_nm"],
        "foreign_underlying": cand["foreign_underlying"],
        "median_trdval": int(cand["median_trdval"]),
        "median_spread_pct": spread,
        "nday_ma_spread_pct": nday_ma_spread_pct,
        "spread_days": spread_days,
        "episodes": cand["episodes"],
        "p_resolve_within_n": main_stats["p_resolve_within_n"],
        "mean_net_edge_pct": main_stats["mean_net_edge_pct"],
        "score": cand["score"],
        "pinned_held_position": pinned,
        "daily_score": cand["score"],
        "intraday_score": cand.get("intraday_score"),
        "combined_score": cand.get("combined_score"),
        "expected_disparity": _expected_disparity_block(cand),
        "data_source": "ranked",
    }


def _expected_disparity_block(cand: dict[str, Any]) -> dict[str, Any] | None:
    """종목별 기대괴리 분포(장중 분위수)를 워치리스트에 실어준다.

    2단계(동적 진입 임계값)가 이 값을 소비할 예정이나, 표본이 신뢰성 있으려면
    며칠 축적이 필요하므로 지금은 '기록만' 한다. n_samples를 함께 실어 소비측이
    신뢰도(min_samples)를 판단할 수 있게 한다. 장중 통계가 없으면 None.
    """
    stats = cand.get("intraday_stats")
    if not stats:
        return None
    return {
        "n_samples": stats.get("n_samples", 0),
        "p5": stats.get("disparity_p5"),
        "p10": stats.get("disparity_p10"),
        "p25": stats.get("disparity_p25"),
        "median": stats.get("disparity_median"),
    }


def _entry_from_aggregate(
    agg: dict[str, Any],
    score_threshold_pct: float,
    scan_exit_threshold_pct: float,
    force_exit_days: int,
    commission_rate_pct: float,
) -> dict[str, Any]:
    """하드필터 탈락(=survivors/ranked에 없음)한 보유종목 폴백: 필터 전
    원본 집계에서 단건으로 에피소드 통계를 계산해 채운다."""
    series = [d for _, d in agg["disparity"]]
    stats = universe.episode_stats(
        series, score_threshold_pct, scan_exit_threshold_pct,
        force_exit_days, commission_rate_pct,
    )
    score = universe.compute_score(stats)
    return {
        "code": agg["code"],
        "name": agg["name"],
        "idx_ind_nm": agg["idx_ind_nm"],
        "foreign_underlying": agg["foreign_underlying"],
        "median_trdval": int(agg["median_trdval"]),
        "median_spread_pct": None,
        "nday_ma_spread_pct": None,
        "spread_days": 0,
        "episodes": {f"{score_threshold_pct:g}": stats},
        "p_resolve_within_n": stats["p_resolve_within_n"],
        "mean_net_edge_pct": stats["mean_net_edge_pct"],
        "score": score,
        "pinned_held_position": True,
        "daily_score": score,
        "intraday_score": None,
        "combined_score": None,
        "expected_disparity": None,
        "data_source": "aggregates_fallback",
    }


def _entry_from_previous(prev: dict[str, Any]) -> dict[str, Any]:
    """어제자 etf_watchlist.json에는 있었지만 오늘 원본 집계에도 없는(상장폐지/
    거래정지 등 극단 케이스) 보유종목 폴백."""
    return {
        "code": str(prev["code"]),
        "name": prev.get("name", ""),
        "idx_ind_nm": prev.get("idx_ind_nm", ""),
        "foreign_underlying": prev.get("foreign_underlying"),
        "median_trdval": int(prev.get("median_trdval") or 0),
        "median_spread_pct": None,
        "nday_ma_spread_pct": prev.get("nday_ma_spread_pct"),
        "spread_days": int(prev.get("spread_days") or 0),
        "episodes": prev.get("episodes", {}),
        "p_resolve_within_n": prev.get("p_resolve_within_n"),
        "mean_net_edge_pct": prev.get("mean_net_edge_pct"),
        "score": prev.get("score"),
        "pinned_held_position": True,
        "daily_score": prev.get("score"),
        "intraday_score": None,
        "combined_score": None,
        "expected_disparity": prev.get("expected_disparity"),
        "data_source": "previous_watchlist_fallback",
    }


def _bare_stub(code: str) -> dict[str, Any]:
    """어디에서도 정보를 찾지 못한 보유종목 - 이름 모름 스텁 + stderr 경고 (호출측 담당)."""
    return {
        "code": code,
        "name": "",
        "idx_ind_nm": "",
        "foreign_underlying": None,
        "median_trdval": 0,
        "median_spread_pct": None,
        "nday_ma_spread_pct": None,
        "spread_days": 0,
        "episodes": {},
        "p_resolve_within_n": None,
        "mean_net_edge_pct": None,
        "score": None,
        "pinned_held_position": True,
        "daily_score": None,
        "intraday_score": None,
        "combined_score": None,
        "expected_disparity": None,
        "data_source": "stub_unknown",
    }


def print_final_table(tickers: list[dict[str, Any]]) -> None:
    print(f"\n=== 최종 워치리스트 ({len(tickers)}종목) ===")
    header = (
        f"{'#':>2} {'코드':<7} {'종목명':<20} {'보유':>4} {'일별':>8} {'장중':>8} "
        f"{'합성':>7} {'중앙대금(억)':>10} {'스프레드%':>9} {'출처':<24}"
    )
    print(header)
    print("-" * 112)
    for i, t in enumerate(tickers, 1):
        name = (t["name"] or "(이름미상)")[:18]
        daily = t["daily_score"]
        intraday = t["intraday_score"]
        combined = t["combined_score"]
        spread = t["median_spread_pct"]
        pinned = "Y" if t["pinned_held_position"] else ""
        print(
            f"{i:>2} {t['code']:<7} {name:<20} {pinned:>4} "
            f"{(f'{daily:.3f}' if daily is not None else '-'):>8} "
            f"{(f'{intraday:.3f}' if intraday is not None else '-'):>8} "
            f"{(f'{combined:.3f}' if combined is not None else '-'):>7} "
            f"{t['median_trdval'] / 1e8:>10.1f} "
            f"{(f'{spread:.3f}' if spread is not None else '-'):>9} "
            f"{t['data_source']:<24}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="일일 워치리스트 리프레셔 (Phase 2.5) - "
        "일별+장중 합성 스코어로 etf_watchlist.json 재계산"
    )
    parser.add_argument(
        "--force-spread-check", action="store_true",
        help="장외 시간에도 실시간 스프레드 검사를 강제 실행",
    )
    parser.add_argument(
        "--portfolio-path", type=Path, default=None,
        help="포트폴리오 상태 파일 경로 (기본: state/portfolio_sim.json)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="etf_watchlist.json을 실제로 쓰지 않고 결과만 출력",
    )
    args = parser.parse_args()

    cfg = load_config()
    ucfg = cfg.universe

    creds = load_credentials(["KIS_APP_KEY", "KIS_APP_SECRET", "KIS_URL_REST"])
    base_url = creds["KIS_URL_REST"]
    token = get_access_token(
        creds["KIS_APP_KEY"], creds["KIS_APP_SECRET"], base_url, TOKEN_CACHE_PATH
    )

    # 1) 휴장일 self-guard
    cal = TradingCalendar.load(
        access_token=token,
        app_key=creds["KIS_APP_KEY"],
        app_secret=creds["KIS_APP_SECRET"],
        base_url=base_url,
    )
    today = date.today()
    if not cal.is_trading_day(today):
        print(f"[워치리스트 리프레시] {today}는 휴장일입니다. 실행하지 않습니다 (파일 변경 없음).")
        return 0

    # 2) 보유종목 확인
    portfolio_path = args.portfolio_path or DEFAULT_STATE_PATH
    held = Portfolio.load(path=portfolio_path, initial_cash=cfg.risk.virtual_capital_krw)
    held_codes = set(held.positions.keys())
    print(
        f"[보유종목] {portfolio_path}: {len(held_codes)}종목 "
        f"{sorted(held_codes) if held_codes else '(없음)'}"
    )

    # 3) KRX 일별 데이터 + 집계(원본 보관) + 하드필터
    history = ensure_history(ucfg.lookback_days)
    dates = sorted(history)
    aggregates, n_days = universe.build_etf_aggregates(history)
    survivors, funnel = universe.apply_filters(
        aggregates, n_days, ucfg.min_daily_value_krw, ucfg.max_price_krw,
        ucfg.exclude_foreign_underlying,
    )
    print(
        f"\n[필터 퍼널] 전체 {funnel['total']} -> 거래대금 {funnel['after_trdval']} "
        f"-> 가격 {funnel['after_price']} -> 상장기간 {funnel['after_presence']} "
        f"-> 해외기초 제외 {funnel['after_foreign']}"
    )

    # 4) 일별 스코어 랭킹 (공유 함수)
    score_threshold = universe.closest_threshold(
        ucfg.scan_entry_thresholds_pct, cfg.signals.entry_threshold_pct
    )
    main_key = f"{score_threshold:g}"
    ranked = universe.rank_survivors(
        survivors, ucfg.scan_entry_thresholds_pct, ucfg.scan_exit_threshold_pct,
        cfg.signals.force_exit_days, cfg.fees.commission_rate_pct, score_threshold,
    )
    n_positive = sum(1 for a in ranked if a["score"] > 0)
    print(
        f"[랭킹] 후보 {len(ranked)}종목 중 일별 스코어 양수 {n_positive}종목 "
        f"(기준 임계값 {score_threshold:g}%)"
    )

    # 5) 장중 에피소드 통계 (저널에서 세션 로드 -> 후보와 매칭되는 것만)
    intraday_sessions = load_intraday_sessions(
        lookback_days=ucfg.intraday_lookback_days, today=today
    )
    n_matched = 0
    for cand in ranked:
        sessions = intraday_sessions.get(cand["code"])
        if not sessions:
            continue
        stats = universe.intraday_episode_stats(
            sessions, score_threshold, ucfg.scan_exit_threshold_pct,
            ucfg.intraday_deadline_minutes, cfg.fees.commission_rate_pct,
        )
        cand["intraday_stats"] = stats
        cand["intraday_score"] = universe.compute_intraday_score(stats)
        cand["n_intraday_samples"] = stats["n_samples"]
        n_matched += 1
    n_eligible = sum(
        1 for a in ranked if a.get("n_intraday_samples", 0) >= ucfg.intraday_min_samples
    )
    print(
        f"[장중 데이터] 저널(최근 {ucfg.intraday_lookback_days}일) 종목 "
        f"{len(intraday_sessions)}개 중 후보 매칭 {n_matched}종목, "
        f"최소샘플({ucfg.intraday_min_samples}) 충족 {n_eligible}종목"
    )

    # 5b) 장중 스프레드 이력 -> 종목별 N일 이동평균 (장전 스프레드 필터의 재료).
    # 08:15 장전엔 실시간 호가를 못 보므로, 샘플러가 며칠간 축적한 일별 스프레드
    # 중앙값의 spread_lookback_days일 평균으로 구조적 고스프레드 종목을 걸러낸다.
    spread_medians = load_daily_spread_medians(
        lookback_days=ucfg.spread_lookback_days, today=today
    )

    def _spread_ma_for(code: str) -> tuple[float | None, int]:
        series = spread_medians.get(code)
        result = nday_ma_spread(series, ucfg.spread_lookback_days) if series else None
        if result is None:
            return None, 0
        return result

    def _spread_fields(code: str) -> dict[str, Any]:
        ma, n_days = _spread_ma_for(code)
        return {
            "nday_ma_spread_pct": round(ma, 4) if ma is not None else None,
            "spread_days": n_days,
        }

    n_spread_codes = len(spread_medians)
    print(
        f"[스프레드 이력] 저널(최근 {ucfg.spread_lookback_days}일) 스프레드 보유 "
        f"{n_spread_codes}종목 (일별 중앙값 -> {ucfg.spread_lookback_days}일 이동평균, "
        f"상한 {ucfg.max_spread_pct}%, 최소 {ucfg.spread_min_days}일 필요)"
    )

    # 6) 점수 합성 (백분위 정규화 blend)
    blended = universe.blend_scores(ranked, ucfg.intraday_min_samples, ucfg.intraday_weight)
    blended_by_code = {c["code"]: c for c in blended}

    # 7) etf_candidates_ranked.json 저장 (상위 SAMPLE_POOL_SIZE = 샘플러 풀, --dry-run과 무관하게 기록)
    candidates_out = blended[:SAMPLE_POOL_SIZE]
    ranked_payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "lookback": {"trading_days": n_days, "start": dates[0], "end": dates[-1]},
        "intraday_lookback_days": ucfg.intraday_lookback_days,
        "score_threshold_pct": score_threshold,
        "candidates": [
            {
                "code": c["code"],
                "name": c["name"],
                "idx_ind_nm": c["idx_ind_nm"],
                "foreign_underlying": c["foreign_underlying"],
                "median_trdval": int(c["median_trdval"]),
                "daily_score": c["score"],
                "daily_pctile": c["daily_pctile"],
                "n_intraday_samples": c.get("n_intraday_samples", 0),
                "intraday_score": c.get("intraday_score"),
                "intraday_pctile": c["intraday_pctile"],
                "combined_score": c["combined_score"],
                "expected_disparity": _expected_disparity_block(c),
                **_spread_fields(c["code"]),
            }
            for c in candidates_out
        ],
    }
    _atomic_write_json(CANDIDATES_PATH, ranked_payload)
    print(f"\n[저장] {CANDIDATES_PATH.name} 기록 완료 ({len(candidates_out)}종목, 샘플러 풀용)")

    # 8) 보유종목 히스테리시스 + 최종 워치리스트
    prev_by_code = _load_previous_watchlist_by_code(WATCHLIST_PATH)

    print(f"\n[보유종목 히스테리시스] 보유 {len(held_codes)}종목 강제 포함 처리:")
    held_entries: list[dict[str, Any]] = []
    source_counts: Counter[str] = Counter()
    for code in sorted(held_codes):
        if code in blended_by_code:
            _ma, _n_days = _spread_ma_for(code)
            entry = _entry_from_candidate(
                blended_by_code[code], main_key, pinned=True, spread=None,
                nday_ma_spread_pct=round(_ma, 4) if _ma is not None else None,
                spread_days=_n_days,
            )
        elif code in aggregates:
            entry = _entry_from_aggregate(
                aggregates[code], score_threshold, ucfg.scan_exit_threshold_pct,
                cfg.signals.force_exit_days, cfg.fees.commission_rate_pct,
            )
        elif code in prev_by_code:
            entry = _entry_from_previous(prev_by_code[code])
        else:
            entry = _bare_stub(code)
            print(
                f"경고: 보유종목 {code}에 대한 정보를 필터통과목록/원본집계/"
                "이전워치리스트 어디에서도 찾지 못했습니다 - 이름 모름 스텁으로 "
                "포함합니다. 즉시 데이터 확인이 필요합니다!",
                file=sys.stderr,
            )
        held_entries.append(entry)
        source_counts[entry["data_source"]] += 1
        print(f"  {code} ({entry['name'] or '이름미상'}) <- {entry['data_source']}")

    if len(held_codes) > ucfg.max_watchlist_size:
        print(
            f"경고: 보유종목 수({len(held_codes)})가 max_watchlist_size"
            f"({ucfg.max_watchlist_size})를 초과합니다. 오펀 방지를 위해 보유종목은 "
            "전량 포함하며 이번 워치리스트는 한도를 넘어 커집니다.",
            file=sys.stderr,
        )
    print(
        f"  집계: 랭킹매칭 {source_counts['ranked']} / 집계폴백 "
        f"{source_counts['aggregates_fallback']} / 이전워치리스트폴백 "
        f"{source_counts['previous_watchlist_fallback']} / 스텁 "
        f"{source_counts['stub_unknown']}"
    )

    remaining_slots = max(0, ucfg.max_watchlist_size - len(held_entries))
    fresh_pool = [c for c in blended if c["code"] not in held_codes]

    now = datetime.now()
    try:
        market_open = is_market_hours_now(cal, now)
    except CalendarError:
        market_open = False
    do_spread_check = market_open or args.force_spread_check

    # N일 이동평균 스프레드 필터: 장전(08:15)엔 실시간 호가를 못 보므로 이게
    # 스프레드 배제의 기본 메커니즘이다. 신규 후보에만 적용(보유종목 면제 -
    # 오펀 방지). 이력이 spread_min_days 미만이면 데이터 부족으로 제외하지
    # 않는다(intraday_min_samples와 동일한 graceful gating).
    # 아래 do_spread_check(실시간 호가 게이트)는 수동 장중 실행/--force용으로
    # 그대로 유지 - 이동평균 게이트를 먼저 통과한 후보만 실시간 호가까지 본다.
    fresh_entries: list[dict[str, Any]] = []
    n_spread_excluded = 0      # 이동평균 스프레드 초과로 제외한 신규 후보 수
    n_spread_insufficient = 0  # 선정됐지만 이력 부족이라 이동평균 필터가 미적용된 수

    def _ma_spread_gate(cand: dict[str, Any]) -> tuple[bool, float | None, int]:
        """(제외여부, ma, n_days). 제외 시 사유를 출력하고 카운터를 올린다."""
        nonlocal n_spread_excluded
        ma, n_days = _spread_ma_for(cand["code"])
        if exclude_for_spread(ma, n_days, ucfg.spread_min_days, ucfg.max_spread_pct):
            n_spread_excluded += 1
            print(
                f"  {cand['code']} {cand['name']}: N일({n_days}) 이동평균 스프레드 "
                f"{ma:.3f}% > 상한 {ucfg.max_spread_pct}% -> 제외"
            )
            return True, ma, n_days
        return False, ma, n_days

    if do_spread_check:
        reason = "장중" if market_open else "강제(--force-spread-check)"
        print(
            f"\n[스프레드 검사] {reason} ({now.strftime('%H:%M')}) - "
            f"신규 후보만 대상(보유종목 면제), 상한 {ucfg.max_spread_pct}%, "
            f"필요 슬롯 {remaining_slots} (N일 이동평균 사전필터 + 실시간 호가 게이트)"
        )
        for cand in fresh_pool:
            if len(fresh_entries) >= remaining_slots:
                break
            excluded, ma, n_days = _ma_spread_gate(cand)
            if excluded:
                continue
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
            if n_days < ucfg.spread_min_days:
                n_spread_insufficient += 1
            fresh_entries.append(
                _entry_from_candidate(
                    cand, main_key, pinned=False, spread=round(spread, 4),
                    nday_ma_spread_pct=round(ma, 4) if ma is not None else None,
                    spread_days=n_days,
                )
            )
    else:
        print(
            f"\n[스프레드 검사] 장외 - 실시간 호가 건너뜀(median_spread_pct=null), "
            f"N일 이동평균 스프레드 필터로 선정(상한 {ucfg.max_spread_pct}%), "
            f"필요 슬롯 {remaining_slots}"
        )
        for cand in fresh_pool:
            if len(fresh_entries) >= remaining_slots:
                break
            excluded, ma, n_days = _ma_spread_gate(cand)
            if excluded:
                continue
            if n_days < ucfg.spread_min_days:
                n_spread_insufficient += 1
            fresh_entries.append(
                _entry_from_candidate(
                    cand, main_key, pinned=False, spread=None,
                    nday_ma_spread_pct=round(ma, 4) if ma is not None else None,
                    spread_days=n_days,
                )
            )

    print(
        f"[스프레드 필터 요약] N일({ucfg.spread_lookback_days}) 이동평균 > "
        f"{ucfg.max_spread_pct}% 초과로 신규 후보 {n_spread_excluded}종목 제외, "
        f"스프레드 이력 부족(< {ucfg.spread_min_days}일)으로 미적용 {n_spread_insufficient}종목"
    )

    final_tickers = held_entries + fresh_entries
    print(
        f"\n[최종 구성] 보유 {len(held_entries)}종목(강제) + 신규 {len(fresh_entries)}종목 "
        f"= 총 {len(final_tickers)}종목 (한도 {ucfg.max_watchlist_size})"
    )

    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "lookback": {"trading_days": n_days, "start": dates[0], "end": dates[-1]},
        "thresholds": {
            "scan_entry_thresholds_pct": list(ucfg.scan_entry_thresholds_pct),
            "scan_exit_threshold_pct": ucfg.scan_exit_threshold_pct,
            "signal_entry_threshold_pct": cfg.signals.entry_threshold_pct,
            "score_threshold_pct": score_threshold,
            "force_exit_days": cfg.signals.force_exit_days,
        },
        "spread_checked_live": do_spread_check,
        "held_positions_pinned": sorted(held_codes),
        "intraday_data_used": n_eligible > 0,
        "tickers": final_tickers,
    }

    print_final_table(final_tickers)

    if args.dry_run:
        print(f"\n[dry-run] {WATCHLIST_PATH.name} 저장 생략 (검증 목적, 파일 변경 없음)")
    else:
        _atomic_write_json(WATCHLIST_PATH, out)
        print(f"\n워치리스트 저장 완료: {WATCHLIST_PATH} ({len(final_tickers)}종목)")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ConfigError as e:
        print(f"설정 오류: {e}", file=sys.stderr)
        sys.exit(1)
    except PortfolioError as e:
        print(f"포트폴리오 로드 오류: {e}", file=sys.stderr)
        sys.exit(1)
    except (KrxApiError, KisApiError, CalendarError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n중단됨 (KRX 캐시는 보존되어 재실행 시 이어서 진행)", file=sys.stderr)
        sys.exit(130)
    except Exception as e:  # noqa: BLE001 - top-level safety net
        print(f"Unexpected error: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)

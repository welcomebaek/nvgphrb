"""캐시된 KRX 일별 데이터 위에서 동작하는 순수 통계/스코어링 (I/O 없음).

일별 괴리율 시계열 d_t = (종가 - NAV) / NAV 를 종목별로 구성하고,
하드 필터(거래대금/가격/상장기간/해외기초자산) 및 괴리 에피소드 통계
(발생빈도, 해소 소요, N거래일 내 해소율, 평균 포획 엣지)를 계산한다.

주의: 여기서 차감하는 왕복비용은 수수료 x 2 뿐이다. 스프레드는 과거
데이터가 없어 여기서 반영하지 못하며, 유니버스 선정 마지막 단계의
실시간 호가 스프레드 검사에서 별도로 걸러진다.
"""

from __future__ import annotations

import bisect
import math
from typing import Any

# 해외 기초자산 판별 키워드 (ETF명 ISU_NM + 기초지수명 IDX_IND_NM 양쪽 대상,
# 대소문자 무시). KRX의 IDX_IND_NM은 영문("Solactive US ...", "Hang Seng TECH
# Index", "PHLX Semiconductor ...")인 경우가 많아 한글 지역 키워드만으로는
# 놓친다 - 반드시 ETF명(한글)과 함께 검사해야 한다.
# 환헤지 "(H)" 접미사가 붙어도 이름에 지역 키워드가 남으므로 별도 처리 불요.
FOREIGN_UNDERLYING_KEYWORDS: tuple[str, ...] = (
    # 한글 지역/자산 키워드 (주로 ETF명에서 매칭)
    "나스닥", "미국", "차이나", "중국", "항셍", "아시아", "일본", "니케이",
    "인도", "대만", "베트남", "글로벌", "국제", "해외", "유로", "유럽",
    "독일", "선진국", "신흥국", "달러", "위안", "엔선물", "엔화",
    "원유", "천연가스", "골드", "국제금", "은선물", "구리", "원자재",
    "빅테크", "테슬라", "엔비디아", "애플", "팔란티어", "월드", "버크셔",
    # TRF/TDF: 글로벌주식+국내채권 혼합 자산배분 펀드 - NAV에 해외 성분 포함
    "TRF", "TDF",
    # 영문 지수명 키워드 (주로 IDX_IND_NM에서 매칭)
    "NASDAQ", "S&P", "PHLX", "NYSE", "HANG SENG", "HSCEI", "HSTECH",
    "CSI", "STAR50", "CHINEXT", "CHINA", "NIKKEI", "TOPIX", "NIFTY",
    "STOXX", "MSCI", "DOW JONES", "RUSSELL", "FACTSET", "MVIS",
    " US ", "U.S.", "AMERICA", "GLOBAL", "ASIA", "JAPAN", "INDIA",
    "VIETNAM", "EUROPE", "WORLD", "TAIWAN",
)

# 상장기간 필터: 조회기간 거래일의 이 비율 이상 유효 데이터가 있어야 함
MIN_PRESENCE_RATIO = 0.9


def parse_number(raw: Any) -> float | None:
    """KRX 숫자 필드(콤마 포함 문자열, 빈 값 가능)를 float로. 실패 시 None."""
    if raw is None:
        return None
    s = str(raw).replace(",", "").strip()
    if not s or s == "-":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def is_foreign_underlying(idx_ind_nm: str, isu_nm: str = "") -> bool:
    """ETF명+기초지수명 키워드 휴리스틱으로 해외 기초자산 여부를 판별한다.

    " US "처럼 공백을 포함한 키워드가 문자열 경계에서도 매칭되도록
    검사 대상 문자열 양끝에 공백을 덧붙인 뒤 검사한다.
    """
    haystack = f" {idx_ind_nm} {isu_nm} ".upper()
    return any(kw.upper() in haystack for kw in FOREIGN_UNDERLYING_KEYWORDS)


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def _percentile(xs: list[float], pct: float) -> float | None:
    """nearest-rank 방식 백분위수."""
    if not xs:
        return None
    s = sorted(xs)
    k = max(0, math.ceil(pct / 100.0 * len(s)) - 1)
    return s[k]


def build_etf_aggregates(
    history: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, dict[str, Any]], int]:
    """{YYYYMMDD: rows} 캐시에서 종목별 집계를 만든다.

    반환: (종목코드 -> 집계 dict, 조회기간 거래일 수)
    집계 dict 필드:
      code, name, idx_ind_nm, foreign_underlying,
      disparity: [(YYYYMMDD, d)] 시간순 (종가/NAV 유효한 날만),
      median_trdval, last_close, n_days_present, n_valid_days
    """
    dates = sorted(history)
    per: dict[str, dict[str, Any]] = {}

    for bas_dd in dates:
        for row in history[bas_dd]:
            code = str(row.get("ISU_CD", "")).strip()
            if not code:
                continue
            agg = per.setdefault(
                code,
                {
                    "code": code,
                    "name": "",
                    "idx_ind_nm": "",
                    "disparity": [],
                    "_trdvals": [],
                    "last_close": None,
                    "n_days_present": 0,
                },
            )
            name = str(row.get("ISU_NM", "")).strip()
            if name:
                agg["name"] = name
            idx_nm = str(row.get("IDX_IND_NM", "")).strip()
            if idx_nm:
                agg["idx_ind_nm"] = idx_nm

            agg["n_days_present"] += 1
            trdval = parse_number(row.get("ACC_TRDVAL"))
            if trdval is not None:
                agg["_trdvals"].append(trdval)

            close = parse_number(row.get("TDD_CLSPRC"))
            nav = parse_number(row.get("NAV"))
            if close and nav:  # None/0 모두 제외
                agg["disparity"].append((bas_dd, (close - nav) / nav))
                agg["last_close"] = close  # 날짜 오름차순이라 마지막 유효 종가

    for agg in per.values():
        agg["median_trdval"] = _median(agg.pop("_trdvals")) or 0.0
        agg["n_valid_days"] = len(agg["disparity"])
        agg["foreign_underlying"] = is_foreign_underlying(
            agg["idx_ind_nm"], agg["name"]
        )

    return per, len(dates)


def apply_filters(
    aggregates: dict[str, dict[str, Any]],
    n_trading_days: int,
    min_daily_value_krw: int,
    max_price_krw: int,
    exclude_foreign_underlying: bool,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """하드 필터를 순서대로 적용하고 (생존 목록, 퍼널 카운트)를 반환한다."""
    funnel: dict[str, int] = {"total": len(aggregates)}

    step = [
        a for a in aggregates.values() if a["median_trdval"] >= min_daily_value_krw
    ]
    funnel["after_trdval"] = len(step)

    step = [
        a
        for a in step
        if a["last_close"] is not None and a["last_close"] <= max_price_krw
    ]
    funnel["after_price"] = len(step)

    min_days = MIN_PRESENCE_RATIO * n_trading_days
    step = [a for a in step if a["n_valid_days"] >= min_days]
    funnel["after_presence"] = len(step)

    if exclude_foreign_underlying:
        step = [a for a in step if not a["foreign_underlying"]]
    funnel["after_foreign"] = len(step)

    return step, funnel


def episode_stats(
    disparity: list[float],
    entry_threshold_pct: float,
    exit_threshold_pct: float,
    force_exit_days: int,
    commission_rate_pct: float,
) -> dict[str, Any]:
    """단일 진입 임계값에 대한 괴리 에피소드 통계.

    에피소드: d_t <= -entry% 인 날 t에 시작, 이후 최초로 d_u >= -exit% 인
    날 u에 해소 (duration = u - t 거래일). 데이터 끝까지 미해소면 미해소
    에피소드로 남기고 (보수적으로 'N일 내 해소'에 불포함) 종료.

    mean_net_edge_pct = 해소 에피소드들의 mean(d_end - d_start)에서
    왕복 수수료(2 x commission)만 차감한 값(%). 스프레드는 실시간 호가
    검사 단계에서 별도 적용.
    """
    entry = -entry_threshold_pct / 100.0
    exit_ = -exit_threshold_pct / 100.0
    n = len(disparity)

    durations: list[int] = []
    edges: list[float] = []
    n_episodes = 0

    i = 0
    while i < n:
        if disparity[i] <= entry:
            n_episodes += 1
            d_start = disparity[i]
            resolved = False
            for u in range(i + 1, n):
                if disparity[u] >= exit_:
                    durations.append(u - i)
                    edges.append(disparity[u] - d_start)
                    i = u + 1
                    resolved = True
                    break
            if not resolved:
                break  # 미해소 에피소드는 데이터 끝까지 이어짐
        else:
            i += 1

    n_resolved = len(durations)
    n_within = sum(1 for du in durations if du <= force_exit_days)
    round_trip_cost = 2.0 * commission_rate_pct / 100.0  # fraction

    mean_net_edge_pct: float | None = None
    if edges:
        mean_net_edge_pct = (sum(edges) / len(edges) - round_trip_cost) * 100.0

    return {
        "n_episodes": n_episodes,
        "n_resolved": n_resolved,
        "episodes_per_100d": (n_episodes / n * 100.0) if n else 0.0,
        "median_duration_days": _median([float(x) for x in durations]),
        "p90_duration_days": _percentile([float(x) for x in durations], 90.0),
        "p_resolve_within_n": (n_within / n_episodes) if n_episodes else 0.0,
        "mean_net_edge_pct": mean_net_edge_pct,
    }


def closest_threshold(scan_thresholds: tuple[float, ...], target: float) -> float:
    """스캔 임계값 중 시그널 진입 임계값에 가장 가까운 값."""
    return min(scan_thresholds, key=lambda t: abs(t - target))


def compute_score(stats: dict[str, Any]) -> float:
    """스코어 = 발생빈도(per 100d) x N일내 해소율 x 평균 순엣지(%).

    해소 에피소드가 없으면 0 (관찰 가치 없음으로 최하위 랭크).
    """
    edge = stats.get("mean_net_edge_pct")
    if edge is None:
        return 0.0
    return stats["episodes_per_100d"] * stats["p_resolve_within_n"] * edge


def rank_survivors(
    survivors: list[dict[str, Any]],
    scan_entry_thresholds_pct: tuple[float, ...],
    scan_exit_threshold_pct: float,
    force_exit_days: int,
    commission_rate_pct: float,
    score_threshold_pct: float,
) -> list[dict[str, Any]]:
    """생존 종목(apply_filters 통과분)에 일별 에피소드 통계+스코어를 매긴다.

    `etf_universe_select.py`(Phase 1)의 원래 인라인 랭킹 루프를 그대로
    옮긴 것 - 각 survivor dict를 제자리에서 변형(mutate)해 "episodes"
    (스캔 임계값별 통계 dict)와 "score"(score_threshold_pct 기준 통계로
    계산한 compute_score 값) 필드를 채운 뒤, (score, median_trdval)
    내림차순으로 정렬한 새 리스트를 반환한다. score_threshold_pct는
    호출측이 closest_threshold()로 미리 골라 전달하는 스캔 임계값이다.
    """
    for agg in survivors:
        series = [d for _, d in agg["disparity"]]
        agg["episodes"] = {
            f"{th:g}": episode_stats(
                series,
                th,
                scan_exit_threshold_pct,
                force_exit_days,
                commission_rate_pct,
            )
            for th in scan_entry_thresholds_pct
        }
        main_stats = agg["episodes"][f"{score_threshold_pct:g}"]
        agg["score"] = compute_score(main_stats)

    return sorted(
        survivors, key=lambda a: (a["score"], a["median_trdval"]), reverse=True
    )


def intraday_episode_stats(
    sessions: list[tuple[list[float], list[float]]],
    entry_threshold_pct: float,
    exit_threshold_pct: float,
    deadline_minutes: float,
    commission_rate_pct: float,
) -> dict[str, Any]:
    """장중 세션별 괴리 에피소드 통계 (해소 소요 단위 = 분).

    sessions: [(timestamps_epoch_sorted, disparity_pct_sorted), ...] - 캘린더
    날짜(세션) 하나당 튜플 하나. 에피소드는 세션 경계를 절대 넘지 않는다
    (각 세션 내부에서 독립적으로 탐지 - 전날 장마감 시점 미해소 에피소드가
    다음날 개장과 이어붙는 일은 없다).

    단위 주의: disparity_pct는 이미 "%" 단위 그대로다 (예: -0.37은 -0.37%),
    KIS 원본 dprt 필드와 동일한 관례. `episode_stats()`의 disparity 인자
    (예: -0.005 같은 소수 fraction, (종가-NAV)/NAV)와는 단위가 다르므로
    절대 혼용하지 말 것 - entry_threshold_pct/exit_threshold_pct도 마찬가지로
    이미 % 단위 값(예: 0.5는 0.5%)을 그대로 받는다.

    duration은 거래일이 아니라 분: (timestamps[u] - timestamps[i]) / 60.
    세션 끝까지 미해소인 에피소드는 (episode_stats()와 동일하게) 보수적으로
    미해소 처리하고 그 세션 탐지를 종료한다 (다음 세션으로 안 이어짐).
    """
    entry = -entry_threshold_pct
    exit_ = -exit_threshold_pct

    durations: list[float] = []
    edges: list[float] = []
    n_episodes = 0
    n_samples = 0
    total_minutes_covered = 0.0
    all_disparity: list[float] = []  # 세션 전체 풀 (기대괴리 분위수용)

    for timestamps, disparity in sessions:
        n = len(disparity)
        n_samples += n
        all_disparity.extend(disparity)
        if n >= 2:
            total_minutes_covered += (timestamps[-1] - timestamps[0]) / 60.0

        i = 0
        while i < n:
            if disparity[i] <= entry:
                n_episodes += 1
                d_start = disparity[i]
                resolved = False
                for u in range(i + 1, n):
                    if disparity[u] >= exit_:
                        durations.append((timestamps[u] - timestamps[i]) / 60.0)
                        edges.append(disparity[u] - d_start)
                        i = u + 1
                        resolved = True
                        break
                if not resolved:
                    break  # 세션 끝까지 미해소 - 이 세션의 탐지는 여기서 종료
            else:
                i += 1

    n_resolved = len(durations)
    n_within = sum(1 for du in durations if du <= deadline_minutes)
    round_trip_cost_pct = 2.0 * commission_rate_pct  # disparity_pct와 동일하게 이미 % 단위

    mean_net_edge_pct: float | None = None
    if edges:
        mean_net_edge_pct = sum(edges) / len(edges) - round_trip_cost_pct

    # 기대괴리 분포 (음수일수록 깊은 저평가). 동적 진입 임계값 재료로 축적한다.
    # 오름차순 정렬 기준: p5는 가장 깊은 꼬리 근처, p50(중앙)은 평상시 수준.
    # 표본이 적으면(None) 신뢰 못 하므로 호출측이 min_samples로 게이팅해야 한다.
    disparity_percentiles = {
        "disparity_p5": _percentile(all_disparity, 5.0),
        "disparity_p10": _percentile(all_disparity, 10.0),
        "disparity_p25": _percentile(all_disparity, 25.0),
        "disparity_median": _median(all_disparity),
    }

    return {
        "n_sessions": len(sessions),
        "n_samples": n_samples,
        "n_episodes": n_episodes,
        "n_resolved": n_resolved,
        "episodes_per_100min": (
            (n_episodes / total_minutes_covered * 100.0)
            if total_minutes_covered
            else 0.0
        ),
        "median_duration_minutes": _median(durations),
        "p90_duration_minutes": _percentile(durations, 90.0),
        "p_resolve_within_deadline": (n_within / n_episodes) if n_episodes else 0.0,
        "mean_net_edge_pct": mean_net_edge_pct,
        **disparity_percentiles,
    }


def compute_intraday_score(stats: dict[str, Any]) -> float:
    """장중 스코어 = 발생빈도(per 100min) x 기한내 해소율 x 평균 순엣지(%).

    compute_score()를 미러링한다: 해소 에피소드가 없으면 0.
    """
    edge = stats.get("mean_net_edge_pct")
    if edge is None:
        return 0.0
    return stats["episodes_per_100min"] * stats["p_resolve_within_deadline"] * edge


def _fractional_rank(target: float, sorted_values: list[float]) -> float:
    """sorted_values 내 target의 서수(ordinal)/동점 인지 백분위 순위, [0,1].

    n<=1이면 순위가 정의되지 않으므로 관례적으로 1.0(최상위)을 반환한다.
    동점 구간은 중간 순위(mid-rank)로 처리해 공정하게 나눈다.
    """
    n = len(sorted_values)
    if n <= 1:
        return 1.0
    lo = bisect.bisect_left(sorted_values, target)
    hi = bisect.bisect_right(sorted_values, target)
    mid_rank = (lo + hi - 1) / 2.0
    return mid_rank / (n - 1)


def blend_scores(
    survivors: list[dict[str, Any]],
    intraday_min_samples: int,
    intraday_weight: float,
) -> list[dict[str, Any]]:
    """일별/장중 스코어를 백분위 순위로 정규화한 뒤 blend한다.

    survivors의 각 dict는 이미 "score"(일별, rank_survivors가 채움)를 갖고
    있어야 하며, 선택적으로 "intraday_score"+"n_intraday_samples"를 가질 수
    있다 (장중 샘플이 있는 종목만). daily_pctile은 전체 후보 중에서 계산하고,
    intraday_pctile은 n_intraday_samples >= intraday_min_samples인 부분집합
    안에서만 계산한다 (콜드스타트 종목은 장중 데이터 없이도 daily_pctile만으로
    랭크 가능).

    combined_score = intraday_weight*intraday_pctile + (1-intraday_weight)*
    daily_pctile (장중 미달이면 daily_pctile 그대로). 각 dict를 제자리에서
    변형해 daily_pctile/intraday_pctile(미계산 시 None)/combined_score
    필드를 채우고, (combined_score, median_trdval) 내림차순으로 재정렬한
    새 리스트를 반환한다.
    """
    daily_sorted = sorted(a["score"] for a in survivors)
    for a in survivors:
        a["daily_pctile"] = _fractional_rank(a["score"], daily_sorted)
        a["intraday_pctile"] = None

    eligible = [
        a
        for a in survivors
        if a.get("n_intraday_samples", 0) >= intraday_min_samples
    ]
    intraday_sorted = sorted(a["intraday_score"] for a in eligible)
    for a in eligible:
        a["intraday_pctile"] = _fractional_rank(a["intraday_score"], intraday_sorted)

    for a in survivors:
        if a["intraday_pctile"] is not None:
            a["combined_score"] = (
                intraday_weight * a["intraday_pctile"]
                + (1.0 - intraday_weight) * a["daily_pctile"]
            )
        else:
            a["combined_score"] = a["daily_pctile"]

    return sorted(
        survivors,
        key=lambda a: (a["combined_score"], a["median_trdval"]),
        reverse=True,
    )

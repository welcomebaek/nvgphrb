"""Pure watchlist-selection logic for etf_watchlist_refresh.py.

The refresher script owns I/O (credentials, KRX/KIS fetches, files, printing
the final table); everything that decides WHICH tickers end up on the
watchlist lives here so it can be unit-tested without a network:

- CodeHistory     per-code pre-market history (N-day MA spread, resolution)
- held_entry      held-position hysteresis: always pinned, fallback chain
                  ranked -> unfiltered aggregate -> previous watchlist -> stub
- select_fresh_entries
                  fill the remaining slots from the blended ranking through
                  the nonresolution -> MA-spread -> (optional) live-spread
                  gates
- entry_* builders
                  the watchlist record shapes for each data source

Gate functions print a one-line reason for every exclusion, matching the
refresher's console log (the refresher's stdout is its run log).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from etf_arb import universe
from etf_arb.config import UniverseConfig
from etf_arb.resolution_history import exclude_for_nonresolution
from etf_arb.spread_history import exclude_for_spread, nday_ma_spread
from kis_common import KisApiError

# code -> spread % (or None when the book has no valid quote). May raise
# KisApiError; throttling between calls is the caller's business.
LiveSpreadFn = Callable[[str], "float | None"]


# ---------------------------------------------------------------- history

@dataclass(frozen=True)
class CodeHistory:
    """Per-code pre-market history the gates read (both loaded by the
    refresher from the sampler journal)."""

    spread_medians: Mapping[str, list[tuple[date, float]]]
    resolution_stats: Mapping[str, tuple[int, int]]
    spread_lookback_days: int

    def spread_ma(self, code: str) -> tuple[float | None, int]:
        """(N-day moving-average spread, days used); (None, 0) without data."""
        series = self.spread_medians.get(code)
        result = nday_ma_spread(series, self.spread_lookback_days) if series else None
        if result is None:
            return None, 0
        return result

    def resolution(self, code: str) -> tuple[int, int]:
        """(episodes, resolved) over the resolution lookback; (0, 0) if none."""
        return self.resolution_stats.get(code, (0, 0))

    def record_fields(self, code: str) -> dict[str, Any]:
        """History fields recorded on every candidate/watchlist record."""
        ma, n_days = self.spread_ma(code)
        n_ep, n_res = self.resolution(code)
        return {
            "nday_ma_spread_pct": round(ma, 4) if ma is not None else None,
            "spread_days": n_days,
            "resolution_episodes": n_ep,
            "resolution_resolved": n_res,
            "resolution_rate": (n_res / n_ep) if n_ep else None,
        }


@dataclass
class GateStats:
    spread_excluded: int = 0      # 이동평균 스프레드 초과로 제외한 신규 후보 수
    spread_insufficient: int = 0  # 선정됐지만 이력 부족이라 이동평균 필터가 미적용된 수
    res_excluded: int = 0         # 구조적 비해소로 제외한 신규 후보 수
    res_insufficient: int = 0     # 해소율 게이트를 통과했지만 에피소드 부족으로 미적용된 수


# ---------------------------------------------------------------- record shapes

def entry_from_candidate(
    cand: dict[str, Any],
    main_key: str,
    pinned: bool,
    spread: float | None,
    entry_eligible: bool,
    nday_ma_spread_pct: float | None = None,
    spread_days: int = 0,
    resolution_episodes: int = 0,
    resolution_resolved: int = 0,
) -> dict[str, Any]:
    """rank_survivors/blend_scores를 거친(필터 통과) 후보 -> 워치리스트 항목.

    entry_eligible: 신규 진입을 허용해도 되는지. 신규후보(pinned=False)는 이
    함수 호출 전 이미 스프레드/해소율 게이트를 통과했으므로 항상 True. 보유종목
    히스테리시스로 핀된 경우(pinned=True)는 하드필터는 통과했지만(그래서 이
    함수로 옴) 스프레드 이동평균 초과 또는 구조적 비해소일 수 있어 호출측이
    별도로 계산해 전달한다 - 핀은 "청산 신호는 계속 받게" 하려는 것이지 "신규
    진입해도 된다"는 뜻이 아니다."""
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
        "resolution_episodes": resolution_episodes,
        "resolution_resolved": resolution_resolved,
        "resolution_rate": (
            resolution_resolved / resolution_episodes
            if resolution_episodes
            else None
        ),
        "episodes": cand["episodes"],
        "p_resolve_within_n": main_stats["p_resolve_within_n"],
        "mean_net_edge_pct": main_stats["mean_net_edge_pct"],
        "score": cand["score"],
        "pinned_held_position": pinned,
        "entry_eligible": entry_eligible,
        "daily_score": cand["score"],
        "intraday_score": cand.get("intraday_score"),
        "combined_score": cand.get("combined_score"),
        "expected_disparity": expected_disparity_block(cand),
        "data_source": "ranked",
    }


def expected_disparity_block(cand: dict[str, Any]) -> dict[str, Any] | None:
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


def entry_from_aggregate(
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
        "resolution_episodes": 0,
        "resolution_resolved": 0,
        "resolution_rate": None,
        "episodes": {f"{score_threshold_pct:g}": stats},
        "p_resolve_within_n": stats["p_resolve_within_n"],
        "mean_net_edge_pct": stats["mean_net_edge_pct"],
        "score": score,
        "pinned_held_position": True,
        "entry_eligible": False,
        "daily_score": score,
        "intraday_score": None,
        "combined_score": None,
        "expected_disparity": None,
        "data_source": "aggregates_fallback",
    }


def entry_from_previous(prev: dict[str, Any]) -> dict[str, Any]:
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
        "resolution_episodes": int(prev.get("resolution_episodes") or 0),
        "resolution_resolved": int(prev.get("resolution_resolved") or 0),
        "resolution_rate": prev.get("resolution_rate"),
        "episodes": prev.get("episodes", {}),
        "p_resolve_within_n": prev.get("p_resolve_within_n"),
        "mean_net_edge_pct": prev.get("mean_net_edge_pct"),
        "score": prev.get("score"),
        "pinned_held_position": True,
        "entry_eligible": False,
        "daily_score": prev.get("score"),
        "intraday_score": None,
        "combined_score": None,
        "expected_disparity": prev.get("expected_disparity"),
        "data_source": "previous_watchlist_fallback",
    }


def bare_stub(code: str) -> dict[str, Any]:
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
        "resolution_episodes": 0,
        "resolution_resolved": 0,
        "resolution_rate": None,
        "episodes": {},
        "p_resolve_within_n": None,
        "mean_net_edge_pct": None,
        "score": None,
        "pinned_held_position": True,
        "entry_eligible": False,
        "daily_score": None,
        "intraday_score": None,
        "combined_score": None,
        "expected_disparity": None,
        "data_source": "stub_unknown",
    }



# ---------------------------------------------------------------- held positions

def held_entry(
    code: str,
    *,
    blended_by_code: Mapping[str, dict[str, Any]],
    aggregates: Mapping[str, dict[str, Any]],
    prev_by_code: Mapping[str, dict[str, Any]],
    hist: CodeHistory,
    ucfg: UniverseConfig,
    main_key: str,
    score_threshold: float,
    force_exit_days: int,
    commission_rate_pct: float,
) -> dict[str, Any]:
    """Watchlist record for a HELD position - always pinned (orphan
    prevention), built from the best available source.

    A held ticker that is still in the blended ranking already passed the hard
    filters, so only spread/resolution remain: they are judged with the same
    gates as fresh candidates, but they only decide entry_eligible - a pin is
    "keep receiving exit signals", not "new entries are fine". Every fallback
    source is entry-ineligible. The caller warns on a stub (data_source ==
    "stub_unknown")."""
    if code in blended_by_code:
        ma, n_days = hist.spread_ma(code)
        n_ep, n_res = hist.resolution(code)
        spread_excluded = exclude_for_spread(
            ma, n_days, ucfg.spread_min_days, ucfg.max_spread_pct
        )
        res_excluded = exclude_for_nonresolution(
            n_ep, n_res, ucfg.resolution_min_episodes, ucfg.min_resolution_rate
        )
        return entry_from_candidate(
            blended_by_code[code], main_key, pinned=True, spread=None,
            entry_eligible=not (spread_excluded or res_excluded),
            nday_ma_spread_pct=round(ma, 4) if ma is not None else None,
            spread_days=n_days,
            resolution_episodes=n_ep,
            resolution_resolved=n_res,
        )
    if code in aggregates:
        return entry_from_aggregate(
            aggregates[code], score_threshold, ucfg.scan_exit_threshold_pct,
            force_exit_days, commission_rate_pct,
        )
    if code in prev_by_code:
        return entry_from_previous(prev_by_code[code])
    return bare_stub(code)


# ---------------------------------------------------------------- fresh candidates

def nonresolution_gate(
    cand: dict[str, Any], hist: CodeHistory, ucfg: UniverseConfig, stats: GateStats
) -> tuple[bool, int, int]:
    """(excluded, n_ep, n_res). Runs BEFORE the spread gate - a ticker whose
    discounts don't close intraday is no use however tight its spread."""
    n_ep, n_res = hist.resolution(cand["code"])
    if exclude_for_nonresolution(
        n_ep, n_res, ucfg.resolution_min_episodes, ucfg.min_resolution_rate
    ):
        stats.res_excluded += 1
        print(
            f"  {cand['code']} {cand['name']}: 당일 해소율 "
            f"{n_res}/{n_ep}={n_res / n_ep:.0%} < 하한 "
            f"{ucfg.min_resolution_rate:.0%} -> 제외(구조적 비해소)"
        )
        return True, n_ep, n_res
    if n_ep < ucfg.resolution_min_episodes:
        stats.res_insufficient += 1
    return False, n_ep, n_res


def ma_spread_gate(
    cand: dict[str, Any], hist: CodeHistory, ucfg: UniverseConfig, stats: GateStats
) -> tuple[bool, float | None, int]:
    """(excluded, ma, n_days). Pre-market (08:15) the live book is closed, so
    the N-day MA of the sampler's daily median spreads is the primary spread
    filter. Fewer than spread_min_days of history -> not excluded."""
    ma, n_days = hist.spread_ma(cand["code"])
    if exclude_for_spread(ma, n_days, ucfg.spread_min_days, ucfg.max_spread_pct):
        stats.spread_excluded += 1
        print(
            f"  {cand['code']} {cand['name']}: N일({n_days}) 이동평균 스프레드 "
            f"{ma:.3f}% > 상한 {ucfg.max_spread_pct}% -> 제외"
        )
        return True, ma, n_days
    return False, ma, n_days


def _live_spread_gate(
    cand: dict[str, Any], ucfg: UniverseConfig, live_spread: LiveSpreadFn
) -> tuple[bool, float | None]:
    """(excluded, spread) from the live book (market hours / --force only)."""
    try:
        spread = live_spread(cand["code"])
    except KisApiError as e:
        print(f"  {cand['code']} {cand['name']}: 호가 조회 실패 - {e} -> 제외")
        return True, None
    if spread is None:
        print(f"  {cand['code']} {cand['name']}: 유효 호가 없음 -> 제외")
        return True, None
    if spread > ucfg.max_spread_pct:
        print(
            f"  {cand['code']} {cand['name']}: 스프레드 {spread:.3f}% > "
            f"{ucfg.max_spread_pct}% -> 제외"
        )
        return True, spread
    return False, spread


def select_fresh_entries(
    fresh_pool: list[dict[str, Any]],
    remaining_slots: int,
    hist: CodeHistory,
    ucfg: UniverseConfig,
    main_key: str,
    live_spread: LiveSpreadFn | None = None,
) -> tuple[list[dict[str, Any]], GateStats]:
    """Fill remaining_slots from fresh_pool (blended order, held tickers
    already removed) through nonresolution -> MA spread -> live spread.

    live_spread=None is the pre-market path (median_spread_pct=null). With it,
    the live book is consulted only for candidates that already passed both
    history gates, and only until the slots are full."""
    stats = GateStats()
    entries: list[dict[str, Any]] = []
    for cand in fresh_pool:
        if len(entries) >= remaining_slots:
            break
        res_excl, n_ep, n_res = nonresolution_gate(cand, hist, ucfg, stats)
        if res_excl:
            continue
        excluded, ma, n_days = ma_spread_gate(cand, hist, ucfg, stats)
        if excluded:
            continue
        spread: float | None = None
        if live_spread is not None:
            live_excl, spread = _live_spread_gate(cand, ucfg, live_spread)
            if live_excl:
                continue
        if n_days < ucfg.spread_min_days:
            stats.spread_insufficient += 1
        entries.append(
            entry_from_candidate(
                cand, main_key, pinned=False,
                spread=round(spread, 4) if spread is not None else None,
                entry_eligible=True,
                nday_ma_spread_pct=round(ma, 4) if ma is not None else None,
                spread_days=n_days,
                resolution_episodes=n_ep,
                resolution_resolved=n_res,
            )
        )
    return entries, stats

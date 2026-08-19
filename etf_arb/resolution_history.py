"""장중 샘플러 저널(`logs/intraday_samples.jsonl`)의 당일 해소율 재구성 (Phase 2.5).

`etf_intraday_sampler.py`가 스윕마다 남기는 스냅샷의 `askp1`/`bidp1`/`nav`로
종목×세션(캘린더 날짜)별 괴리 에피소드를 재구성해, 종목마다 최근 며칠간
"진입한 괴리가 당일 장중에 해소됐는가"의 (n_episodes, n_resolved)를 집계한다.
장전 리프레셔가 "구조적으로 당일 해소가 안 되는" 종목을 선정 단계에서 제외하는
데 쓴다.

핵심 — ask/bid 비대칭: 실제 거래는 진입=매도1호가(ask), 청산=매수1호가(bid)로
비대칭이다. `universe.intraday_episode_stats`는 체결가(prpr) 하나로 진입·청산을
모두 판정해 거의 모든 종목이 해소율 90~100%로 나오고 판별력이 없다. 이 모듈은
진입은 ask, 청산은 bid로 판정해 "실제로 사서 되팔 수 있었는가"를 잰다 - 같은
종목이 prpr 기준 65%인데 ask/bid 기준 0%로 드러나는 이유(2026-08 `465350`).

I/O 실패(손상/잘림)에 관대하다: 킬된 샘플러가 파일 끝을 어중간하게 잘라먹을 수
있고 예전 레코드(askp1/bidp1 없음)도 섞여 있으므로, 파싱 실패한 줄과 필드가
없는/무효한 줄은 조용히 건너뛰고 절대 죽지 않는다.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any

from etf_arb import paths, universe

JOURNAL_PATH = paths.INTRADAY_SAMPLES_PATH


def _hhmm(value: str) -> dtime:
    h, m = value.split(":")
    return dtime(int(h), int(m))


def _session_episodes(
    series: list[tuple[float, float]],
    entry_neg: float,
    exit_neg: float,
    maxdisp_neg: float,
) -> tuple[int, int]:
    """한 세션(날짜)의 (ask_disp, bid_disp) 시계열에서 (n_episodes, n_resolved).

    `universe.intraday_episode_stats`와 동일한 스캔 관례: 진입 조건이 만족되면
    에피소드 시작, 이후 청산 조건 만족 시점에 해소로 카운트하고 그 다음부터 다시
    스캔. 세션 끝까지 미해소인 에피소드가 나오면 그 세션 탐지를 종료한다(다음
    세션으로 안 이어짐 - 보수적).

    진입: maxdisp_neg < ask_disp <= entry_neg (예: -3.0 < ad <= -0.5).
      -3%보다 깊은 값은 NAV 이상 아티팩트라 진입 게이트(disparity_implausible)와
      동일하게 에피소드로 치지 않는다.
    청산: bid_disp >= exit_neg (예: bd >= -0.1). 진입 다음 샘플부터 탐색(동일 틱
      매수-매도 불가).
    """
    n = len(series)
    n_ep = n_res = 0
    i = 0
    while i < n:
        ad = series[i][0]
        if maxdisp_neg < ad <= entry_neg:
            n_ep += 1
            resolved = False
            for u in range(i + 1, n):
                if series[u][1] >= exit_neg:
                    n_res += 1
                    i = u + 1
                    resolved = True
                    break
            if not resolved:
                break
        else:
            i += 1
    return n_ep, n_res


def load_resolution_stats(
    path: Path = JOURNAL_PATH,
    lookback_days: int = 20,
    today: date | None = None,
    window: tuple[str, str] | None = None,
    entry_threshold_pct: float = 0.5,
    exit_threshold_pct: float = 0.1,
    max_entry_disparity_pct: float = 3.0,
) -> dict[str, tuple[int, int]]:
    """저널을 파싱해 {code: (n_episodes, n_resolved)}로 집계한다.

    lookback_days는 오늘을 포함해 최근 며칠(오늘 - (lookback_days-1) 이상)의
    표본만 반영한다. 종목×날짜별로 에피소드를 재구성해 최근 며칠치를 합산한다.
    파일이 없으면 빈 dict.

    window=("HH:MM","HH:MM")를 주면 그 시각 창 [start, end) 안의 샘플만 반영한다
    (진입 시그널 창과 일치, 동시호가 시간대 배제). None이면 시각 필터 없음.

    다음 줄은 조용히 건너뛴다: JSON 파싱 실패, ts/code 누락 또는 형식 오류,
    askp1/bidp1/nav 부재(레거시 레코드) 또는 parse_number로 파싱되지 않거나
    0 이하인 값 - 잘린 마지막 줄이나 예전 포맷 때문에 전체 로드가 죽지 않도록.
    """
    path = Path(path)
    if not path.exists():
        return {}

    today = today or date.today()
    cutoff = today - timedelta(days=lookback_days - 1)
    win = (_hhmm(window[0]), _hhmm(window[1])) if window is not None else None

    entry_neg = -entry_threshold_pct
    exit_neg = -exit_threshold_pct
    maxdisp_neg = -max_entry_disparity_pct

    # code -> {날짜문자열: [(ts_dt, ask_disp, bid_disp), ...]}
    by_code_day: dict[str, dict[str, list[tuple[datetime, float, float]]]] = {}

    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError:
                continue

            code = row.get("code")
            ts_raw = row.get("ts")
            if not code or not ts_raw:
                continue
            code = str(code)

            nav = universe.parse_number(row.get("nav"))
            ask = universe.parse_number(row.get("askp1"))
            bid = universe.parse_number(row.get("bidp1"))
            if nav is None or ask is None or bid is None:
                continue
            if nav <= 0 or ask <= 0 or bid <= 0:
                continue

            try:
                ts_dt = datetime.fromisoformat(str(ts_raw))
            except ValueError:
                continue

            ts_date = ts_dt.date()
            if not (cutoff <= ts_date <= today):
                continue
            if win is not None and not (win[0] <= ts_dt.time() < win[1]):
                continue

            ask_disp = (ask - nav) / nav * 100.0
            bid_disp = (bid - nav) / nav * 100.0
            day_map = by_code_day.setdefault(code, {})
            day_map.setdefault(ts_date.isoformat(), []).append(
                (ts_dt, ask_disp, bid_disp)
            )

    result: dict[str, tuple[int, int]] = {}
    for code, day_map in by_code_day.items():
        tot_ep = tot_res = 0
        for day_key, rows in day_map.items():
            rows.sort(key=lambda r: r[0])
            series = [(ad, bd) for _, ad, bd in rows]
            ep, res = _session_episodes(series, entry_neg, exit_neg, maxdisp_neg)
            tot_ep += ep
            tot_res += res
        if tot_ep > 0:
            result[code] = (tot_ep, tot_res)

    return result


def exclude_for_nonresolution(
    n_episodes: int,
    n_resolved: int,
    min_episodes: int,
    min_resolution_rate: float,
) -> bool:
    """당일 해소율로 신규 후보를 제외할지 결정하는 순수 함수.

    - 에피소드가 min_episodes 미만이면 데이터 부족으로 제외하지 않는다(False):
      spread_history.exclude_for_spread와 동일한 graceful-gating.
    - min_episodes 이상이면서 해소율(n_resolved/n_episodes)이 min_resolution_rate
      **미만**이면 제외(True) - 구조적으로 당일 수렴하지 않는 종목.
    - 그 외에는 유지(False).
    """
    if n_episodes < min_episodes:
        return False
    return (n_resolved / n_episodes) < min_resolution_rate

"""장중 샘플러 저널(`logs/intraday_samples.jsonl`)의 스프레드 이력 파싱 (Phase 2.5).

`etf_intraday_sampler.py`가 스윕마다 남기는 스냅샷에 추가된 `spread_pct`
(매도1/매수1호가 스프레드%)를 종목별/세션(캘린더 날짜)별로 묶어, 종목마다
일별 스프레드 중앙값 시계열을 만든다. 이 위에서 최근 N일 이동평균을 계산해
장전 리프레셔가 "구조적 고스프레드" 종목을 선정 단계에서 제외하는 데 쓴다.

장이 닫힌 08:15에는 실시간 호가를 못 보므로, 지난 며칠간 축적된 스프레드
표본으로 대체하는 것이 이 모듈의 존재 이유다.

I/O 실패(손상/잘림)에 관대하다: 킬된 샘플러 프로세스가 파일 끝을 어중간하게
잘라먹을 수 있고, 예전 레코드(spread_pct 필드 없음)도 섞여 있으므로, 파싱
실패한 줄과 spread_pct가 없는/유효하지 않은 줄은 조용히 건너뛰고 절대 죽지 않는다.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from etf_arb import universe

PROJECT_ROOT = Path(__file__).resolve().parent.parent
JOURNAL_PATH = PROJECT_ROOT / "logs" / "intraday_samples.jsonl"


def load_daily_spread_medians(
    path: Path = JOURNAL_PATH,
    lookback_days: int = 5,
    today: date | None = None,
) -> dict[str, list[tuple[date, float]]]:
    """저널을 파싱해 {code: [(day, median_spread_pct), ...] 날짜 오름차순}로 묶는다.

    lookback_days는 오늘을 포함해 최근 며칠(오늘 - (lookback_days-1) 이상)의
    표본만 반영한다. 각 (종목, 날짜)에 대해 그날의 유효 spread_pct 표본들의
    중앙값을 계산한다. 파일이 없으면 빈 dict.

    다음 줄은 조용히 건너뛴다: JSON 파싱 실패, ts/code 누락 또는 형식 오류,
    spread_pct 필드 부재(레거시 레코드) 또는 universe.parse_number로 파싱되지
    않는 값 - 잘린 마지막 줄 하나나 예전 포맷 때문에 전체 로드가 죽지 않도록.
    """
    path = Path(path)
    if not path.exists():
        return {}

    today = today or date.today()
    cutoff = today - timedelta(days=lookback_days - 1)

    # code -> {날짜문자열: [spread_pct, ...]}
    by_code_day: dict[str, dict[str, list[float]]] = {}

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

            # 레거시 레코드(spread_pct 키 없음)는 조용히 건너뛴다.
            if "spread_pct" not in row:
                continue
            spread = universe.parse_number(row.get("spread_pct"))
            if spread is None:
                continue

            try:
                ts_dt = datetime.fromisoformat(str(ts_raw))
            except ValueError:
                continue

            ts_date = ts_dt.date()
            if not (cutoff <= ts_date <= today):
                continue

            day_key = ts_date.isoformat()
            day_map = by_code_day.setdefault(code, {})
            day_map.setdefault(day_key, []).append(spread)

    result: dict[str, list[tuple[date, float]]] = {}
    for code, day_map in by_code_day.items():
        series: list[tuple[date, float]] = []
        for day_key in sorted(day_map):
            median = universe._median(day_map[day_key])
            if median is None:
                continue
            series.append((date.fromisoformat(day_key), median))
        if series:
            result[code] = series

    return result


def nday_ma_spread(
    daily_medians_for_code: list[tuple[date, float]],
    lookback_days: int,
) -> tuple[float, int] | None:
    """한 종목의 [(day, median), ...]에서 최근 lookback_days 서로 다른 날의
    일별 중앙값 평균("N일 이동평균")과 사용한 날 수를 반환한다.

    날짜 오름차순 정렬 여부와 무관하게 마지막 N개 날짜를 취한다. 데이터가
    하루도 없으면 None.
    """
    if not daily_medians_for_code:
        return None
    ordered = sorted(daily_medians_for_code, key=lambda p: p[0])
    window = ordered[-lookback_days:]
    values = [v for _, v in window]
    return sum(values) / len(values), len(values)


def exclude_for_spread(
    nday_ma: float | None,
    n_days: int,
    min_days: int,
    max_spread: float,
) -> bool:
    """N일 이동평균 스프레드로 신규 후보를 제외할지 결정하는 순수 함수.

    - 이력이 min_days 미만이면 데이터 부족으로 제외하지 않는다(False):
      intraday_min_samples와 동일한 graceful-gating.
    - min_days 이상이면서 이동평균이 max_spread를 초과하면 제외(True).
    - 그 외에는 유지(False).
    """
    if nday_ma is None or n_days < min_days:
        return False
    return nday_ma > max_spread

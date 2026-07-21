"""장중 광역 샘플러 저널(`logs/intraday_samples.jsonl`) 파싱 (Phase 2.5).

`etf_intraday_sampler.py`가 1분 주기로 남기는 JSONL 한 줄당 한 스냅샷
({"ts", "code", "prpr", "nav", "dprt"})을 종목별/세션(캘린더 날짜)별로
묶어 `universe.intraday_episode_stats()`가 바로 받을 수 있는 형태로 반환한다.

I/O 실패(손상/잘림)에 관대하다: 킬된 샘플러 프로세스가 파일 끝을 어중간하게
잘라먹을 수 있으므로, 파싱 실패한 줄은 조용히 건너뛰고 절대 죽지 않는다.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from etf_arb import universe

PROJECT_ROOT = Path(__file__).resolve().parent.parent
JOURNAL_PATH = PROJECT_ROOT / "logs" / "intraday_samples.jsonl"


def load_intraday_sessions(
    path: Path = JOURNAL_PATH,
    lookback_days: int = 10,
    today: date | None = None,
) -> dict[str, list[tuple[list[float], list[float]]]]:
    """저널을 파싱해 {code: [(timestamps_epoch, disparity_pct), ...]}로 묶는다.

    반환값의 리스트 원소 하나가 세션(=캘린더 날짜) 하나. 각 세션 내부는
    ts 오름차순으로 정렬돼 있다. lookback_days는 오늘을 포함해 최근 며칠의
    ts만 반영한다 (오늘 - (lookback_days-1) 이상). 파일이 없으면 빈 dict.

    다음 줄은 조용히 건너뛴다: JSON 파싱 실패, ts/code 누락 또는 형식 오류,
    dprt가 universe.parse_number로 파싱되지 않는 값 - 킬된 샘플러가 남긴
    잘린 마지막 줄 하나 때문에 전체 로드가 죽지 않도록.
    """
    path = Path(path)
    if not path.exists():
        return {}

    today = today or date.today()
    cutoff = today - timedelta(days=lookback_days - 1)

    # code -> {날짜문자열: [(epoch, disparity_pct), ...]}
    by_code_day: dict[str, dict[str, list[tuple[float, float]]]] = {}

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

            try:
                ts_dt = datetime.fromisoformat(str(ts_raw))
            except ValueError:
                continue

            ts_date = ts_dt.date()
            if not (cutoff <= ts_date <= today):
                continue

            dprt = universe.parse_number(row.get("dprt"))
            if dprt is None:
                continue

            day_key = ts_date.isoformat()
            day_map = by_code_day.setdefault(code, {})
            day_map.setdefault(day_key, []).append((ts_dt.timestamp(), dprt))

    result: dict[str, list[tuple[list[float], list[float]]]] = {}
    for code, day_map in by_code_day.items():
        sessions: list[tuple[list[float], list[float]]] = []
        for day_key in sorted(day_map):
            pairs = sorted(day_map[day_key], key=lambda p: p[0])
            timestamps = [p[0] for p in pairs]
            disparities = [p[1] for p in pairs]
            sessions.append((timestamps, disparities))
        result[code] = sessions

    return result

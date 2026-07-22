"""Tests for the intraday-session loader's time-window filter.

`etf_arb.intraday_history.load_intraday_sessions` optionally restricts samples
to a time-of-day window (aligned with the entry-signal window) so opening/closing
call-auction artifacts (NAV/price desync, e.g. -10% garbage right after open)
never pollute the disparity distribution stats.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from etf_arb.intraday_history import load_intraday_sessions

TODAY = date(2026, 7, 22)


def _write(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )


def _rows() -> list[dict]:
    # 같은 종목, 같은 날, 시각만 다르게: 09:01(장초반 허수), 09:05(창 시작),
    # 12:00(정상), 15:00(창 끝-경계, 제외돼야), 15:25(마감 동시호가)
    return [
        {"ts": "2026-07-22T09:01:00", "code": "AAA", "dprt": "-10.2"},
        {"ts": "2026-07-22T09:05:00", "code": "AAA", "dprt": "-0.5"},
        {"ts": "2026-07-22T12:00:00", "code": "AAA", "dprt": "-0.3"},
        {"ts": "2026-07-22T15:00:00", "code": "AAA", "dprt": "-0.4"},
        {"ts": "2026-07-22T15:25:00", "code": "AAA", "dprt": "-9.9"},
    ]


def test_no_window_keeps_all(tmp_path: Path):
    p = tmp_path / "s.jsonl"
    _write(p, _rows())
    sessions = load_intraday_sessions(path=p, lookback_days=5, today=TODAY)
    ts_list, dprt_list = sessions["AAA"][0]
    assert len(dprt_list) == 5  # 필터 없으면 전부


def test_window_excludes_auction_samples(tmp_path: Path):
    p = tmp_path / "s.jsonl"
    _write(p, _rows())
    sessions = load_intraday_sessions(
        path=p, lookback_days=5, today=TODAY, window=("09:05", "15:00")
    )
    _ts, dprt_list = sessions["AAA"][0]
    # 09:05(포함) ~ 15:00(미포함, 경계 [start,end)) -> 09:05, 12:00 만 남음
    assert dprt_list == [-0.5, -0.3]
    # 개장 허수(-10.2)와 마감 동시호가(-9.9)와 경계값 15:00(-0.4) 제외
    assert -10.2 not in dprt_list
    assert -9.9 not in dprt_list
    assert -0.4 not in dprt_list


def test_window_all_excluded_drops_code(tmp_path: Path):
    p = tmp_path / "s.jsonl"
    _write(p, [{"ts": "2026-07-22T09:01:00", "code": "BBB", "dprt": "-10.0"}])
    sessions = load_intraday_sessions(
        path=p, lookback_days=5, today=TODAY, window=("09:05", "15:00")
    )
    assert "BBB" not in sessions  # 창 밖 샘플만 있으면 종목 자체가 안 잡힘

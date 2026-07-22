"""Tests for the N-day-MA spread filter data layer (Phase 2.5 spread gate).

`etf_arb.spread_history` reads the intraday sampler journal's per-sample
`spread_pct`, computes a per-ticker per-day MEDIAN spread, then an N-day moving
average of those daily medians, and exposes a pure exclusion-decision function.
These tests pin the median/MA math and the tolerance for legacy/malformed rows
so the pre-market spread gate behaves predictably from day one.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from etf_arb.spread_history import (
    exclude_for_spread,
    load_daily_spread_medians,
    nday_ma_spread,
)

TODAY = date(2026, 7, 22)


def _write_jsonl(path: Path, rows: list) -> None:
    """rows: list of either dict (JSON-encoded) or raw str (written verbatim)."""
    lines = []
    for r in rows:
        lines.append(r if isinstance(r, str) else json.dumps(r, ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _rec(ts: str, code: str, spread_pct, **extra) -> dict:
    rec = {"ts": ts, "code": code, "prpr": "1000", "nav": "1000", "dprt": "0.0"}
    if spread_pct is not None:
        rec["spread_pct"] = spread_pct
    rec.update(extra)
    return rec


# --- daily median grouping -------------------------------------------------

def test_daily_median_single_day_multiple_samples(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(p, [
        _rec("2026-07-22T09:00:00", "AAA", 0.1),
        _rec("2026-07-22T09:01:00", "AAA", 0.2),
        _rec("2026-07-22T09:02:00", "AAA", 0.3),
    ])
    out = load_daily_spread_medians(p, lookback_days=5, today=TODAY)
    assert out["AAA"] == [(date(2026, 7, 22), 0.2)]


def test_daily_median_even_count_is_average_of_middle_two(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(p, [
        _rec("2026-07-22T09:00:00", "AAA", 0.10),
        _rec("2026-07-22T09:01:00", "AAA", 0.20),
        _rec("2026-07-22T09:02:00", "AAA", 0.30),
        _rec("2026-07-22T09:03:00", "AAA", 0.40),
    ])
    out = load_daily_spread_medians(p, lookback_days=5, today=TODAY)
    assert out["AAA"] == [(date(2026, 7, 22), 0.25)]


def test_daily_median_multiple_days_sorted(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(p, [
        # day 2 written first to prove sorting by day
        _rec("2026-07-21T09:00:00", "AAA", 0.40),
        _rec("2026-07-21T09:01:00", "AAA", 0.60),
        _rec("2026-07-20T09:00:00", "AAA", 0.10),
        _rec("2026-07-20T09:01:00", "AAA", 0.30),
    ])
    out = load_daily_spread_medians(p, lookback_days=5, today=TODAY)
    assert out["AAA"] == [
        (date(2026, 7, 20), 0.20),
        (date(2026, 7, 21), 0.50),
    ]


def test_lookback_window_excludes_old_rows(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(p, [
        _rec("2026-07-22T09:00:00", "AAA", 0.5),   # in window
        _rec("2026-07-10T09:00:00", "AAA", 9.9),   # far outside lookback=5
    ])
    out = load_daily_spread_medians(p, lookback_days=5, today=TODAY)
    assert out["AAA"] == [(date(2026, 7, 22), 0.5)]


def test_missing_file_returns_empty(tmp_path):
    assert load_daily_spread_medians(tmp_path / "nope.jsonl", 5, TODAY) == {}


# --- legacy / malformed tolerance ------------------------------------------

def test_legacy_rows_without_spread_pct_are_skipped(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(p, [
        # legacy record shape: no spread_pct key at all
        {"ts": "2026-07-22T09:00:00", "code": "AAA", "prpr": "1000",
         "nav": "1000", "dprt": "0.0"},
        _rec("2026-07-22T09:01:00", "AAA", 0.4),
    ])
    out = load_daily_spread_medians(p, lookback_days=5, today=TODAY)
    # only the one valid row contributes
    assert out["AAA"] == [(date(2026, 7, 22), 0.4)]


def test_null_and_nonnumeric_spread_skipped(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(p, [
        _rec("2026-07-22T09:00:00", "AAA", None),      # spread_pct absent
        _rec("2026-07-22T09:01:00", "AAA", "-"),       # non-numeric
        {"ts": "2026-07-22T09:02:00", "code": "AAA", "spread_pct": None},  # explicit null
        _rec("2026-07-22T09:03:00", "AAA", 0.8),
    ])
    out = load_daily_spread_medians(p, lookback_days=5, today=TODAY)
    assert out["AAA"] == [(date(2026, 7, 22), 0.8)]


def test_malformed_lines_tolerated(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(p, [
        "this is not json",
        _rec("2026-07-22T09:00:00", "AAA", 0.2),
        '{"ts": "2026-07-22T09:01:00", "code": "AAA", "spread_pct": 0.4',  # truncated
        _rec("2026-07-22T09:02:00", "AAA", 0.6),
        "",  # blank line
    ])
    out = load_daily_spread_medians(p, lookback_days=5, today=TODAY)
    # two well-formed samples -> median of [0.2, 0.6] = 0.4
    assert out["AAA"] == [(date(2026, 7, 22), 0.4)]


def test_string_numeric_spread_parsed(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(p, [
        _rec("2026-07-22T09:00:00", "AAA", "0.30"),
        _rec("2026-07-22T09:01:00", "AAA", "0.50"),
    ])
    out = load_daily_spread_medians(p, lookback_days=5, today=TODAY)
    assert out["AAA"] == [(date(2026, 7, 22), 0.40)]


# --- N-day moving average --------------------------------------------------

def test_nday_ma_mean_of_last_n_days():
    series = [
        (date(2026, 7, 20), 0.2),
        (date(2026, 7, 21), 0.5),
        (date(2026, 7, 22), 0.8),
    ]
    ma, n = nday_ma_spread(series, lookback_days=2)
    assert n == 2
    assert ma == (0.5 + 0.8) / 2


def test_nday_ma_fewer_days_than_lookback_uses_all():
    series = [
        (date(2026, 7, 20), 0.2),
        (date(2026, 7, 22), 0.8),
    ]
    ma, n = nday_ma_spread(series, lookback_days=5)
    assert n == 2
    assert ma == (0.2 + 0.8) / 2


def test_nday_ma_unsorted_input_takes_latest_days():
    # deliberately out of order; nday_ma must sort by date first
    series = [
        (date(2026, 7, 22), 0.8),
        (date(2026, 7, 20), 0.2),
        (date(2026, 7, 21), 0.5),
    ]
    ma, n = nday_ma_spread(series, lookback_days=1)
    assert n == 1
    assert ma == 0.8  # latest day only


def test_nday_ma_zero_days_returns_none():
    assert nday_ma_spread([], lookback_days=5) is None


# --- pure exclusion decision ----------------------------------------------

def test_exclude_sufficient_days_over_threshold():
    assert exclude_for_spread(0.9, n_days=3, min_days=2, max_spread=0.15) is True


def test_keep_sufficient_days_under_threshold():
    assert exclude_for_spread(0.05, n_days=3, min_days=2, max_spread=0.15) is False


def test_keep_insufficient_days_even_if_over_threshold():
    # graceful gating: not enough history -> never excluded, regardless of value
    assert exclude_for_spread(0.9, n_days=1, min_days=2, max_spread=0.15) is False


def test_keep_none_ma():
    assert exclude_for_spread(None, n_days=0, min_days=2, max_spread=0.15) is False


def test_boundary_equal_threshold_is_kept():
    # strictly greater excludes; exactly at threshold is kept
    assert exclude_for_spread(0.15, n_days=5, min_days=2, max_spread=0.15) is False

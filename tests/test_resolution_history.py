"""Tests for the same-day resolution data layer (Phase 2.5 non-resolution gate).

`etf_arb.resolution_history` reconstructs disparity episodes from the intraday
sampler's per-sample askp1/bidp1/nav using the ask/bid asymmetry (entry on ask,
exit on bid), aggregates per-ticker (n_episodes, n_resolved) over a lookback
window, and exposes a pure exclusion-decision function. These tests pin the
episode-reconstruction semantics (open/resolve/unresolved, session boundaries,
the implausible-depth cap, the time window) and the tolerance for
legacy/malformed rows so the pre-market non-resolution gate behaves predictably.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from etf_arb.resolution_history import (
    exclude_for_nonresolution,
    load_resolution_stats,
)

TODAY = date(2026, 8, 19)

# entry -0.5%, exit -0.1%, implausible cap -3% (config defaults)
ENTRY, EXIT, MAXDISP = 0.5, 0.1, 3.0


def _write(path: Path, rows: list) -> None:
    """rows: dict (JSON-encoded) or raw str (written verbatim)."""
    lines = [r if isinstance(r, str) else json.dumps(r, ensure_ascii=False) for r in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _rec(ts: str, code: str, ask, bid, nav) -> dict:
    return {"ts": ts, "code": code, "askp1": ask, "bidp1": bid, "nav": nav}


def _load(path: Path, **kw):
    return load_resolution_stats(
        path=path, lookback_days=30, today=TODAY,
        entry_threshold_pct=ENTRY, exit_threshold_pct=EXIT,
        max_entry_disparity_pct=MAXDISP, **kw,
    )


# --- episode reconstruction ------------------------------------------------

def test_resolved_episode(tmp_path):
    # nav 1000: ask 993 -> ask_disp -0.7% (<=-0.5 opens); later bid 1000 ->
    # bid_disp 0% (>=-0.1 resolves).
    p = tmp_path / "s.jsonl"
    _write(p, [
        _rec("2026-08-19T09:10:00", "AAA", 993, 990, 1000),   # open
        _rec("2026-08-19T09:20:00", "AAA", 1000, 1000, 1000),  # resolve
    ])
    assert _load(p)["AAA"] == (1, 1)


def test_unresolved_episode_counts_only(tmp_path):
    # opens and never gets bid_disp back to -0.1% before session end.
    p = tmp_path / "s.jsonl"
    _write(p, [
        _rec("2026-08-19T09:10:00", "AAA", 993, 990, 1000),   # ask -0.7 open
        _rec("2026-08-19T09:20:00", "AAA", 994, 992, 1000),   # bid -0.8, no
        _rec("2026-08-19T09:30:00", "AAA", 995, 993, 1000),   # bid -0.7, no
    ])
    assert _load(p)["AAA"] == (1, 0)


def test_no_episode_when_never_deep_enough(tmp_path):
    # ask_disp never reaches -0.5% -> no episode, code absent from result.
    p = tmp_path / "s.jsonl"
    _write(p, [
        _rec("2026-08-19T09:10:00", "AAA", 998, 997, 1000),   # ask -0.2
        _rec("2026-08-19T09:20:00", "AAA", 999, 998, 1000),
    ])
    assert "AAA" not in _load(p)


def test_implausible_depth_not_counted_as_episode(tmp_path):
    # ask_disp -6% (< -3% cap) is a NAV artifact, not a real episode.
    p = tmp_path / "s.jsonl"
    _write(p, [
        _rec("2026-08-19T09:10:00", "AAA", 940, 935, 1000),   # ask -6.0
        _rec("2026-08-19T09:20:00", "AAA", 945, 940, 1000),   # ask -5.5
    ])
    assert "AAA" not in _load(p)


def test_episodes_do_not_cross_session_boundary(tmp_path):
    # An episode that opens on day 1 and never resolves does not carry into
    # day 2; day 2 opens its own (resolved) episode.
    p = tmp_path / "s.jsonl"
    _write(p, [
        _rec("2026-08-18T09:10:00", "AAA", 993, 990, 1000),   # d1 open
        _rec("2026-08-18T14:00:00", "AAA", 994, 992, 1000),   # d1 unresolved
        _rec("2026-08-19T09:10:00", "AAA", 993, 990, 1000),   # d2 open
        _rec("2026-08-19T09:20:00", "AAA", 1000, 1000, 1000),  # d2 resolve
    ])
    # d1: (1, 0), d2: (1, 1) -> summed (2, 1)
    assert _load(p)["AAA"] == (2, 1)


def test_multiple_episodes_same_day(tmp_path):
    p = tmp_path / "s.jsonl"
    _write(p, [
        _rec("2026-08-19T09:10:00", "AAA", 993, 990, 1000),   # open #1
        _rec("2026-08-19T09:20:00", "AAA", 1000, 1000, 1000),  # resolve #1
        _rec("2026-08-19T10:10:00", "AAA", 992, 989, 1000),   # open #2
        _rec("2026-08-19T10:20:00", "AAA", 1001, 1000, 1000),  # resolve #2
    ])
    assert _load(p)["AAA"] == (2, 2)


def test_window_excludes_out_of_window_samples(tmp_path):
    # A resolving sample outside the entry window must not close the episode.
    p = tmp_path / "s.jsonl"
    _write(p, [
        _rec("2026-08-19T09:10:00", "AAA", 993, 990, 1000),   # in-window open
        _rec("2026-08-19T15:10:00", "AAA", 1000, 1000, 1000),  # out of window
    ])
    out = load_resolution_stats(
        path=p, lookback_days=30, today=TODAY, window=("09:05", "15:00"),
        entry_threshold_pct=ENTRY, exit_threshold_pct=EXIT,
        max_entry_disparity_pct=MAXDISP,
    )
    assert out["AAA"] == (1, 0)   # opened but resolve sample filtered out


# --- legacy / malformed tolerance ------------------------------------------

def test_missing_file_returns_empty(tmp_path):
    assert load_resolution_stats(path=tmp_path / "nope.jsonl", today=TODAY) == {}


def test_legacy_and_malformed_rows_skipped(tmp_path):
    p = tmp_path / "s.jsonl"
    _write(p, [
        "not json at all",
        {"ts": "2026-08-19T09:10:00", "code": "AAA", "nav": 1000},  # no ask/bid
        _rec("2026-08-19T09:11:00", "AAA", 993, 990, 1000),   # open
        _rec("2026-08-19T09:12:00", "AAA", 0, 0, 1000),        # zero prices -> skip
        _rec("2026-08-19T09:20:00", "AAA", 1000, 1000, 1000),  # resolve
        '{"ts": "2026-08-19T09:21:00", "code": "AAA"',        # truncated
        "",
    ])
    assert _load(p)["AAA"] == (1, 1)


def test_string_numeric_prices_parsed(tmp_path):
    p = tmp_path / "s.jsonl"
    _write(p, [
        _rec("2026-08-19T09:10:00", "AAA", "993", "990", "1000"),
        _rec("2026-08-19T09:20:00", "AAA", "1000", "1000", "1000"),
    ])
    assert _load(p)["AAA"] == (1, 1)


def test_lookback_window_excludes_old_days(tmp_path):
    p = tmp_path / "s.jsonl"
    _write(p, [
        _rec("2026-08-01T09:10:00", "AAA", 993, 990, 1000),   # far outside
        _rec("2026-08-01T09:20:00", "AAA", 1000, 1000, 1000),
        _rec("2026-08-19T09:10:00", "AAA", 993, 990, 1000),   # in window
        _rec("2026-08-19T09:20:00", "AAA", 1000, 1000, 1000),
    ])
    out = load_resolution_stats(
        path=p, lookback_days=5, today=TODAY,
        entry_threshold_pct=ENTRY, exit_threshold_pct=EXIT,
        max_entry_disparity_pct=MAXDISP,
    )
    assert out["AAA"] == (1, 1)   # only the recent day


# --- pure exclusion decision -----------------------------------------------

def test_exclude_structural_nonresolver():
    # 0/15 resolution, enough episodes, below floor -> exclude.
    assert exclude_for_nonresolution(15, 0, min_episodes=10, min_resolution_rate=0.15) is True


def test_keep_above_floor():
    # 3/10 = 30% >= 15% floor -> keep.
    assert exclude_for_nonresolution(10, 3, min_episodes=10, min_resolution_rate=0.15) is False


def test_keep_insufficient_episodes_even_if_zero_resolution():
    # graceful gating: not enough episodes -> never excluded.
    assert exclude_for_nonresolution(5, 0, min_episodes=10, min_resolution_rate=0.15) is False


def test_boundary_rate_equal_floor_is_kept():
    # strictly below excludes; exactly at floor is kept.
    assert exclude_for_nonresolution(20, 3, min_episodes=10, min_resolution_rate=0.15) is False


def test_zero_episodes_never_excluded():
    assert exclude_for_nonresolution(0, 0, min_episodes=10, min_resolution_rate=0.15) is False

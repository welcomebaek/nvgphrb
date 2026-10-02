"""Unit tests for the pure watchlist-selection logic extracted from
etf_watchlist_refresh.main(): per-code history lookups, the held-position
hysteresis fallback chain, and fresh-candidate gating/slot filling.

No network, no files, no clock - the live-spread lookup is injected.
"""

from __future__ import annotations

from datetime import date

import pytest

from etf_arb.config import UniverseConfig
from etf_arb.watchlist_select import (
    CodeHistory,
    GateStats,
    held_entry,
    select_fresh_entries,
)
from kis_common import KisApiError

MAIN_KEY = "0.5"


def make_ucfg(**overrides) -> UniverseConfig:
    kw = dict(
        lookback_days=120,
        min_daily_value_krw=500_000_000,
        max_price_krw=500_000,
        exclude_foreign_underlying=True,
        max_spread_pct=0.15,
        scan_entry_thresholds_pct=(0.3, 0.5, 0.8),
        scan_exit_threshold_pct=0.1,
        max_watchlist_size=20,
        intraday_min_samples=60,
        intraday_weight=0.4,
        intraday_lookback_days=10,
        intraday_deadline_minutes=60.0,
        spread_lookback_days=5,
        spread_min_days=2,
        resolution_lookback_days=20,
        resolution_min_episodes=10,
        min_resolution_rate=0.30,
        resolution_entry_threshold_pct=0.3,
    )
    kw.update(overrides)
    return UniverseConfig(**kw)


def cand(code: str, score: float = 1.0) -> dict:
    return {
        "code": code,
        "name": f"ETF{code}",
        "idx_ind_nm": "IDX",
        "foreign_underlying": False,
        "median_trdval": 1_000_000_000.0,
        "episodes": {MAIN_KEY: {"p_resolve_within_n": 0.8, "mean_net_edge_pct": 0.2}},
        "score": score,
        "intraday_score": None,
        "combined_score": score,
    }


def days(*values: float) -> list[tuple[date, float]]:
    return [(date(2026, 9, i + 1), v) for i, v in enumerate(values)]


def hist(spreads=None, resolution=None) -> CodeHistory:
    return CodeHistory(
        spread_medians=spreads or {},
        resolution_stats=resolution or {},
        spread_lookback_days=5,
    )


# ---------------------------------------------------------------- CodeHistory

class TestCodeHistory:
    def test_missing_code_has_no_history(self):
        h = hist()
        assert h.spread_ma("X") == (None, 0)
        assert h.resolution("X") == (0, 0)

    def test_spread_ma_uses_last_n_days(self):
        h = hist(spreads={"X": days(0.9, 0.1, 0.1, 0.1, 0.1, 0.1)})
        ma, n = h.spread_ma("X")
        assert n == 5
        assert ma == pytest.approx(0.1)

    def test_record_fields(self):
        h = hist(spreads={"X": days(0.12345, 0.12345)},
                 resolution={"X": (10, 4)})
        assert h.record_fields("X") == {
            "nday_ma_spread_pct": 0.1235,
            "spread_days": 2,
            "resolution_episodes": 10,
            "resolution_resolved": 4,
            "resolution_rate": 0.4,
        }

    def test_record_fields_without_history(self):
        assert hist().record_fields("X") == {
            "nday_ma_spread_pct": None,
            "spread_days": 0,
            "resolution_episodes": 0,
            "resolution_resolved": 0,
            "resolution_rate": None,
        }


# ---------------------------------------------------------------- held_entry

def _held(code, h=None, blended=None, aggregates=None, prev=None, ucfg=None):
    return held_entry(
        code,
        blended_by_code=blended or {},
        aggregates=aggregates or {},
        prev_by_code=prev or {},
        hist=h or hist(),
        ucfg=ucfg or make_ucfg(),
        main_key=MAIN_KEY,
        score_threshold=0.5,
        force_exit_days=5,
        commission_rate_pct=0.015,
    )


class TestHeldEntry:
    def test_ranked_held_position_stays_entry_eligible(self):
        e = _held("A", blended={"A": cand("A")})
        assert e["data_source"] == "ranked"
        assert e["pinned_held_position"] is True
        assert e["entry_eligible"] is True
        assert e["median_spread_pct"] is None  # never live-checked when pinned

    def test_ranked_but_structurally_nonresolving_blocks_new_entries(self):
        h = hist(resolution={"A": (20, 2)})  # 10% < 30% floor, >= 10 episodes
        e = _held("A", h=h, blended={"A": cand("A")})
        assert e["pinned_held_position"] is True
        assert e["entry_eligible"] is False
        assert e["resolution_rate"] == pytest.approx(0.1)

    def test_ranked_but_wide_ma_spread_blocks_new_entries(self):
        h = hist(spreads={"A": days(0.5, 0.5)})
        e = _held("A", h=h, blended={"A": cand("A")})
        assert e["entry_eligible"] is False
        assert e["nday_ma_spread_pct"] == 0.5

    def test_falls_back_to_unfiltered_aggregate(self):
        agg = {"code": "B", "name": "B", "idx_ind_nm": "", "foreign_underlying": False,
               "median_trdval": 5.0, "disparity": [("d", -0.6), ("d", 0.0)]}
        e = _held("B", aggregates={"B": agg})
        assert e["data_source"] == "aggregates_fallback"
        assert e["entry_eligible"] is False

    def test_falls_back_to_previous_watchlist(self):
        e = _held("C", prev={"C": {"code": "C", "name": "old", "score": 2.0}})
        assert e["data_source"] == "previous_watchlist_fallback"
        assert e["name"] == "old"
        assert e["entry_eligible"] is False

    def test_unknown_code_becomes_stub(self):
        e = _held("D")
        assert e["data_source"] == "stub_unknown"
        assert e["entry_eligible"] is False

    def test_ranked_wins_over_every_fallback(self):
        agg = {"code": "A", "name": "agg", "idx_ind_nm": "", "foreign_underlying": False,
               "median_trdval": 5.0, "disparity": []}
        e = _held("A", blended={"A": cand("A")}, aggregates={"A": agg},
                  prev={"A": {"code": "A"}})
        assert e["data_source"] == "ranked"


# ---------------------------------------------------------------- fresh selection

class TestSelectFreshPremarket:
    def test_fills_slots_in_pool_order(self):
        pool = [cand(c) for c in "ABCD"]
        entries, stats = select_fresh_entries(pool, 2, hist(), make_ucfg(), MAIN_KEY)
        assert [e["code"] for e in entries] == ["A", "B"]
        assert all(e["entry_eligible"] and not e["pinned_held_position"]
                   for e in entries)
        assert all(e["median_spread_pct"] is None for e in entries)

    def test_no_slots_selects_nothing(self):
        entries, stats = select_fresh_entries([cand("A")], 0, hist(), make_ucfg(), MAIN_KEY)
        assert entries == []
        assert stats == GateStats()

    def test_rejected_candidates_do_not_consume_slots(self):
        h = hist(spreads={"A": days(0.5, 0.5)}, resolution={"B": (20, 1)})
        pool = [cand(c) for c in "ABCD"]
        entries, stats = select_fresh_entries(pool, 2, h, make_ucfg(), MAIN_KEY)
        assert [e["code"] for e in entries] == ["C", "D"]
        assert stats.spread_excluded == 1
        assert stats.res_excluded == 1

    def test_nonresolution_gate_runs_before_spread_gate(self):
        # Both gates would reject A; it must be counted once, as nonresolution.
        h = hist(spreads={"A": days(0.5, 0.5)}, resolution={"A": (20, 1)})
        _, stats = select_fresh_entries([cand("A")], 1, h, make_ucfg(), MAIN_KEY)
        assert stats.res_excluded == 1
        assert stats.spread_excluded == 0

    def test_insufficient_history_passes_and_is_counted(self):
        h = hist(spreads={"A": days(0.5)}, resolution={"A": (3, 0)})
        entries, stats = select_fresh_entries([cand("A")], 1, h, make_ucfg(), MAIN_KEY)
        assert [e["code"] for e in entries] == ["A"]
        assert stats.spread_insufficient == 1
        assert stats.res_insufficient == 1


class TestSelectFreshLive:
    def test_live_spread_recorded_rounded(self):
        entries, _ = select_fresh_entries(
            [cand("A")], 1, hist(), make_ucfg(), MAIN_KEY,
            live_spread=lambda code: 0.123456,
        )
        assert entries[0]["median_spread_pct"] == 0.1235

    def test_live_failures_and_wide_spreads_are_skipped(self):
        def live(code):
            if code == "A":
                raise KisApiError("boom")
            return {"B": None, "C": 0.30, "D": 0.05}[code]

        entries, _ = select_fresh_entries(
            [cand(c) for c in "ABCD"], 4, hist(), make_ucfg(), MAIN_KEY,
            live_spread=live,
        )
        assert [e["code"] for e in entries] == ["D"]

    def test_live_lookup_skipped_for_history_rejects_and_after_slots_fill(self):
        calls = []

        def live(code):
            calls.append(code)
            return 0.05

        h = hist(resolution={"A": (20, 1)})
        select_fresh_entries([cand(c) for c in "ABCD"], 2, h, make_ucfg(), MAIN_KEY,
                             live_spread=live)
        assert calls == ["B", "C"]

    def test_insufficient_spread_counted_only_for_selected(self):
        # A has 1 day of spread history but fails live -> not counted.
        h = hist(spreads={"A": days(0.1), "B": days(0.1)})
        _, stats = select_fresh_entries(
            [cand("A"), cand("B")], 2, h, make_ucfg(), MAIN_KEY,
            live_spread=lambda code: None if code == "A" else 0.05,
        )
        assert stats.spread_insufficient == 1

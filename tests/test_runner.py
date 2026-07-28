"""Unit tests for pure helpers extracted from the runner (no I/O).

`find_orphan_risk` is a tiny set-difference extracted from
`run_session()`'s startup safety check: it flags held positions that would
lose websocket coverage (and therefore never receive an exit signal) because
today's watchlist doesn't include them.

`entry_ineligible_codes` is extracted from `Runner.__init__`: held positions
pinned into the watchlist by hysteresis (orphan prevention) may not have
actually passed hard filters/spread - the refresher marks those with
`entry_eligible: false`, and this function turns that into the code set the
runner should block from fresh entries (exit evaluation stays unaffected).
"""

from __future__ import annotations

from etf_arb.runner import entry_ineligible_codes, find_orphan_risk


def test_no_orphans_when_all_held_codes_are_watchlisted():
    held = {"233740", "091160"}
    watchlist = {"233740", "091160", "0064K0"}
    assert find_orphan_risk(held, watchlist) == set()


def test_flags_held_codes_missing_from_watchlist():
    held = {"233740", "091160", "0018C0"}
    watchlist = {"233740"}
    assert find_orphan_risk(held, watchlist) == {"091160", "0018C0"}


def test_empty_held_is_never_orphaned():
    assert find_orphan_risk(set(), {"233740"}) == set()


def test_empty_watchlist_orphans_all_held():
    held = {"233740", "091160"}
    assert find_orphan_risk(held, set()) == held


def test_entry_ineligible_flags_only_false():
    tickers = [
        {"code": "233740", "entry_eligible": True},
        {"code": "091160", "entry_eligible": False},
    ]
    assert entry_ineligible_codes(tickers) == {"091160"}


def test_entry_ineligible_missing_field_defaults_to_eligible():
    # 레거시 스키마(etf_universe_select.py 등)는 entry_eligible 키가 아예 없다 -
    # 기존 동작(제한 없음)을 유지해야 한다.
    tickers = [{"code": "233740"}]
    assert entry_ineligible_codes(tickers) == set()


def test_entry_ineligible_empty_watchlist():
    assert entry_ineligible_codes([]) == set()


def test_entry_ineligible_all_eligible_returns_empty():
    tickers = [
        {"code": "233740", "entry_eligible": True},
        {"code": "091160", "entry_eligible": True},
    ]
    assert entry_ineligible_codes(tickers) == set()

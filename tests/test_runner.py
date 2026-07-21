"""Unit test for the orphan-position risk check (pure function, no I/O).

`find_orphan_risk` is a tiny set-difference extracted from
`run_session()`'s startup safety check: it flags held positions that would
lose websocket coverage (and therefore never receive an exit signal) because
today's watchlist doesn't include them.
"""

from __future__ import annotations

from etf_arb.runner import find_orphan_risk


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

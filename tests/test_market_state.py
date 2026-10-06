"""Unit tests for the pure order-book ladder-walk helpers on TickerState.

These are deterministic, no-I/O primitives: ask_levels() (usable ladder with
single-level fallback), effective_buy_fill() (threshold-aware level-boundary
walk used by the entry double-check) and effective_buy_vwap() (VWAP of an
intended fill size, for journaling). MarketState.update_quote ladder plumbing
is covered too.
"""

from __future__ import annotations

import pytest

from etf_arb.market_state import MarketState, TickerState


def mk(
    ask_ladder: list[tuple[int, int]] | None = None,
    ask1: int | None = None,
    ask1_qty: int | None = None,
    nav: float | None = 10_000.0,
    bid_ladder: list[tuple[int, int]] | None = None,
    bid1: int | None = None,
    bid1_qty: int | None = None,
) -> TickerState:
    return TickerState(
        code="X",
        nav=nav,
        ask1=ask1,
        ask1_qty=ask1_qty,
        bid1=bid1,
        bid1_qty=bid1_qty,
        ask_ladder=ask_ladder or [],
        bid_ladder=bid_ladder or [],
    )


class TestAskLevels:
    def test_uses_ladder_when_present(self):
        st = mk(ask_ladder=[(100, 10), (102, 20)])
        assert st.ask_levels() == [(100, 10), (102, 20)]

    def test_falls_back_to_ask1_when_ladder_empty(self):
        st = mk(ask1=100, ask1_qty=40)
        assert st.ask_levels() == [(100, 40)]

    def test_empty_when_no_liquidity(self):
        assert mk().ask_levels() == []
        assert mk(ask1=100).ask_levels() == []  # qty missing

    def test_stops_at_first_hole(self):
        assert mk(ask_ladder=[(100, 10), (0, 5), (105, 10)]).ask_levels() == [
            (100, 10)
        ]
        assert mk(ask_ladder=[(100, 10), (102, 0), (105, 10)]).ask_levels() == [
            (100, 10)
        ]


class TestEffectiveBuyVwap:
    def test_single_level(self):
        assert mk(ask_ladder=[(100, 50)]).effective_buy_vwap(30) == (30, 100.0)

    def test_capped_by_depth(self):
        assert mk(ask_ladder=[(100, 50)]).effective_buy_vwap(80) == (50, 100.0)

    def test_multi_level_rising_prices(self):
        st = mk(ask_ladder=[(100, 10), (102, 10), (105, 10)])
        filled, vwap = st.effective_buy_vwap(25)
        # 10@100 + 10@102 + 5@105 = 2545; / 25 = 101.8
        assert filled == 25
        assert vwap == pytest.approx(101.8)

    def test_exact_boundary_fill(self):
        st = mk(ask_ladder=[(100, 10), (102, 10)])
        filled, vwap = st.effective_buy_vwap(20)  # 1000 + 1020 = 2020 / 20
        assert filled == 20
        assert vwap == pytest.approx(101.0)

    def test_fallback_single_level(self):
        assert mk(ask1=100, ask1_qty=40).effective_buy_vwap(30) == (30, 100.0)

    def test_none_when_no_liquidity(self):
        assert mk().effective_buy_vwap(10) is None

    def test_none_when_target_nonpositive(self):
        assert mk(ask_ladder=[(100, 10)]).effective_buy_vwap(0) is None


class TestEffectiveBuyFill:
    def test_stops_when_vwap_exceeds_limit(self):
        # level2 would push VWAP to (1000+2000)/20 = 150 > 149 -> stop at 10
        st = mk(ask_ladder=[(100, 10), (200, 10)])
        assert st.effective_buy_fill(20, 149.0) == (10, 100.0, 100)

    def test_boundary_equality_accepted(self):
        # VWAP exactly at the limit (150 <= 150) is accepted
        st = mk(ask_ladder=[(100, 10), (200, 10)])
        assert st.effective_buy_fill(20, 150.0) == (20, 150.0, 200)

    def test_returns_worst_price_of_accepted_levels(self):
        st = mk(ask_ladder=[(100, 10), (102, 10), (105, 10)])
        filled, vwap, worst = st.effective_buy_fill(25, 1e9)
        assert filled == 25
        assert vwap == pytest.approx(101.8)
        assert worst == 105  # deepest accepted level

    def test_target_cap_on_final_level(self):
        # target 15 < full depth: 10@100 + 5@102 = 1510 / 15
        st = mk(ask_ladder=[(100, 10), (102, 10)])
        filled, vwap, worst = st.effective_buy_fill(15, 1e9)
        assert filled == 15
        assert vwap == pytest.approx(1510 / 15)
        assert worst == 102

    def test_none_when_no_liquidity(self):
        assert mk().effective_buy_fill(10, 1e9) is None

    def test_fallback_single_level(self):
        assert mk(ask1=100, ask1_qty=40).effective_buy_fill(30, 1e9) == (
            30, 100.0, 100
        )


class TestBidLevels:
    def test_uses_ladder_when_present(self):
        st = mk(bid_ladder=[(99, 10), (98, 20)])
        assert st.bid_levels() == [(99, 10), (98, 20)]

    def test_falls_back_to_bid1_when_ladder_empty(self):
        assert mk(bid1=99, bid1_qty=40).bid_levels() == [(99, 40)]

    def test_empty_when_no_liquidity(self):
        assert mk().bid_levels() == []
        assert mk(bid1=99).bid_levels() == []  # qty missing

    def test_stops_at_first_hole(self):
        assert mk(bid_ladder=[(99, 10), (98, 0), (97, 10)]).bid_levels() == [
            (99, 10)
        ]


class TestEffectiveSellFill:
    """Mirror of effective_buy_fill on the bid side: deeper bid levels are
    priced LOWER, so cumulative VWAP is non-increasing and the walk stops once
    a level drags it below min_vwap."""

    def test_stops_when_vwap_falls_below_floor(self):
        # level2 would drag VWAP to (2000+1000)/20 = 150 < 151 -> stop at 10
        st = mk(bid_ladder=[(200, 10), (100, 10)])
        assert st.effective_sell_fill(20, 151.0) == (10, 200.0, 200)

    def test_boundary_equality_accepted(self):
        st = mk(bid_ladder=[(200, 10), (100, 10)])
        assert st.effective_sell_fill(20, 150.0) == (20, 150.0, 100)

    def test_vwap_based_not_level_based(self):
        # level2 price 98 is itself below the floor 99.5, but the cumulative
        # VWAP (100*100 + 98*10)/110 = 99.82 still clears it -> accepted.
        st = mk(bid_ladder=[(100, 100), (98, 10)])
        filled, vwap, worst = st.effective_sell_fill(110, 99.5)
        assert filled == 110
        assert vwap == pytest.approx(10_980 / 110)
        assert worst == 98

    def test_target_cap_on_final_level(self):
        st = mk(bid_ladder=[(100, 10), (99, 10)])
        filled, vwap, worst = st.effective_sell_fill(15, 0.0)
        assert filled == 15
        assert vwap == pytest.approx((1000 + 495) / 15)
        assert worst == 99

    def test_none_when_best_level_below_floor(self):
        assert mk(bid_ladder=[(99, 10)]).effective_sell_fill(5, 99.5) is None

    def test_none_when_no_liquidity_or_target(self):
        assert mk().effective_sell_fill(10, 0.0) is None
        assert mk(bid_ladder=[(99, 10)]).effective_sell_fill(0, 0.0) is None

    def test_fallback_single_level(self):
        assert mk(bid1=99, bid1_qty=40).effective_sell_fill(30, 0.0) == (
            30, 99.0, 99
        )


class TestUpdateQuoteLadder:
    def test_stores_ladders(self):
        m = MarketState()
        st = m.update_quote(
            "X", 100, 10, 99, 20, "0", 5.0,
            ask_ladder=[(100, 10), (101, 5)],
            bid_ladder=[(99, 20)],
        )
        assert st.ask_ladder == [(100, 10), (101, 5)]
        assert st.bid_ladder == [(99, 20)]
        assert st.ask1 == 100 and st.bid1 == 99

    def test_ladder_defaults_empty(self):
        m = MarketState()
        st = m.update_quote("X", 100, 10, 99, 20, "0", 5.0)
        assert st.ask_ladder == []
        assert st.bid_ladder == []

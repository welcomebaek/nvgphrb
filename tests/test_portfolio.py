"""Unit tests for portfolio persistence/arithmetic and the sim executor.

No network, no real clock: paths go to tmp_path, the executor clock and the
deadline function are injected.
"""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from etf_arb.executor_sim import SimExecutor
from etf_arb.market_state import TickerState
from etf_arb.portfolio import Portfolio, PortfolioError

NOW = datetime(2026, 7, 20, 13, 0, 0)
NOW_EPOCH = NOW.timestamp()
TODAY = NOW.date()
DEADLINE = date(2026, 7, 27)

COMMISSION_RATE_PCT = 0.015  # matches live config


def make_portfolio(tmp_path, cash: int = 8_500_000) -> Portfolio:
    return Portfolio.load(path=tmp_path / "portfolio_sim.json", initial_cash=cash)


def make_snap(
    code: str = "233740",
    nav: float = 10_000.0,
    ask1: int = 9_940,
    ask1_qty: int = 1_000,
    bid1: int = 9_990,
    bid1_qty: int = 1_000,
    ask_ladder: list[tuple[int, int]] | None = None,
    bid_ladder: list[tuple[int, int]] | None = None,
) -> TickerState:
    return TickerState(
        code=code,
        nav=nav,
        nav_ts=NOW_EPOCH - 1.0,
        ask1=ask1,
        ask1_qty=ask1_qty,
        bid1=bid1,
        bid1_qty=bid1_qty,
        quote_ts=NOW_EPOCH - 1.0,
        hour_cls_code="0",
        ask_ladder=ask_ladder or [],
        bid_ladder=bid_ladder or [],
    )


def make_executor(tmp_path, portfolio: Portfolio) -> SimExecutor:
    return SimExecutor(
        portfolio,
        commission_rate_pct=COMMISSION_RATE_PCT,
        deadline_fn=lambda d: DEADLINE,
        trades_path=tmp_path / "trades.jsonl",
        now_fn=lambda: NOW_EPOCH,
    )


def read_trades(tmp_path) -> list[dict]:
    path = tmp_path / "trades.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------- portfolio

class TestPortfolio:
    def test_fresh_portfolio_when_file_missing(self, tmp_path):
        p = make_portfolio(tmp_path)
        assert p.cash == 8_500_000
        assert p.positions == {}
        assert p.realized_pnl == 0
        assert p.entries_today(TODAY) == 0

    def test_buy_then_full_sell_math_and_invariant(self, tmp_path):
        p = make_portfolio(tmp_path)
        p.apply_buy(
            code="233740", qty=100, price=10_000, commission=150,
            ts=NOW_EPOCH, entry_date=TODAY, entry_disparity_pct=-0.6,
            deadline_date=DEADLINE,
        )
        assert p.cash == 8_500_000 - 1_000_000 - 150
        assert p.positions["233740"].qty == 100
        assert p.positions["233740"].buy_commission == 150

        realized = p.apply_sell(
            code="233740", qty=100, price=10_050, commission=150, ts=NOW_EPOCH + 60
        )
        # (10050-10000)*100 - 150(sell comm) - 150(buy comm) = 4700
        assert realized == 4_700
        assert p.realized_pnl == 4_700
        assert "233740" not in p.positions
        assert p.cooldowns["233740"] == NOW_EPOCH + 60
        # invariant: flat -> cash - initial == realized_pnl
        assert p.cash - 8_500_000 == p.realized_pnl

    def test_partial_sell_allocates_buy_commission_proportionally(self, tmp_path):
        p = make_portfolio(tmp_path)
        p.apply_buy(
            code="233740", qty=100, price=10_000, commission=150,
            ts=NOW_EPOCH, entry_date=TODAY, entry_disparity_pct=-0.6,
            deadline_date=DEADLINE,
        )
        r1 = p.apply_sell(
            code="233740", qty=40, price=10_050, commission=60, ts=NOW_EPOCH + 60
        )
        # 40*50 - 60 - floor(150*40/100)=60 -> 1880
        assert r1 == 2_000 - 60 - 60
        pos = p.positions["233740"]
        assert pos.qty == 60
        assert pos.buy_commission == 90  # 150 - 60

        r2 = p.apply_sell(
            code="233740", qty=60, price=10_050, commission=90, ts=NOW_EPOCH + 120
        )
        # remainder gets ALL remaining buy commission: 60*50 - 90 - 90
        assert r2 == 3_000 - 90 - 90
        assert "233740" not in p.positions
        # full liquidation accounted for every won of both commissions
        assert p.cash - 8_500_000 == p.realized_pnl

    def test_sell_guards(self, tmp_path):
        p = make_portfolio(tmp_path)
        with pytest.raises(PortfolioError):
            p.apply_sell(code="233740", qty=1, price=10_000, commission=1, ts=0.0)
        p.apply_buy(
            code="233740", qty=10, price=10_000, commission=15,
            ts=NOW_EPOCH, entry_date=TODAY, entry_disparity_pct=-0.6,
            deadline_date=DEADLINE,
        )
        with pytest.raises(PortfolioError):
            p.apply_sell(code="233740", qty=11, price=10_000, commission=15, ts=0.0)

    def test_buy_guards(self, tmp_path):
        p = make_portfolio(tmp_path, cash=100)
        with pytest.raises(PortfolioError):  # cash insufficient
            p.apply_buy(
                code="233740", qty=1, price=10_000, commission=1,
                ts=NOW_EPOCH, entry_date=TODAY, entry_disparity_pct=-0.6,
                deadline_date=DEADLINE,
            )

    def test_entries_today_rolls_over_by_date(self, tmp_path):
        p = make_portfolio(tmp_path)
        p.record_entry(TODAY)
        p.record_entry(TODAY)
        assert p.entries_today(TODAY) == 2
        tomorrow = date(2026, 7, 21)
        assert p.entries_today(tomorrow) == 0
        p.record_entry(tomorrow)
        assert p.entries_today(tomorrow) == 1

    def test_save_load_round_trip(self, tmp_path):
        p = make_portfolio(tmp_path)
        p.apply_buy(
            code="233740", qty=100, price=10_000, commission=150,
            ts=NOW_EPOCH, entry_date=TODAY, entry_disparity_pct=-0.612,
            deadline_date=DEADLINE,
        )
        p.record_entry(TODAY)
        p.cooldowns["091160"] = NOW_EPOCH - 100
        p.realized_pnl = 1_234
        p.save()

        q = Portfolio.load(path=p.path, initial_cash=0)
        assert q.cash == p.cash
        assert q.realized_pnl == 1_234
        assert q.entries_today(TODAY) == 1
        assert q.cooldowns == p.cooldowns
        pos = q.positions["233740"]
        assert pos.qty == 100
        assert pos.avg_price == 10_000.0
        assert pos.entry_date == TODAY
        assert pos.deadline_date == DEADLINE
        assert pos.entry_disparity_pct == -0.612
        assert pos.buy_commission == 150

    def test_load_never_resets_on_corrupt_file(self, tmp_path):
        path = tmp_path / "portfolio_sim.json"
        path.write_text("{broken", encoding="utf-8")
        with pytest.raises(PortfolioError):
            Portfolio.load(path=path, initial_cash=8_500_000)

    def test_view_reflects_state(self, tmp_path):
        p = make_portfolio(tmp_path)
        p.apply_buy(
            code="233740", qty=10, price=10_000, commission=15,
            ts=NOW_EPOCH, entry_date=TODAY, entry_disparity_pct=-0.6,
            deadline_date=DEADLINE,
        )
        p.record_entry(TODAY)
        p.cooldowns["091160"] = 123.0
        v = p.view(TODAY)
        assert v.open_codes == frozenset({"233740"})
        assert v.entries_today == 1
        assert v.cash == p.cash
        assert v.last_exit_ts["091160"] == 123.0


# ---------------------------------------------------------------- executor

class TestSimExecutor:
    def test_buy_full_fill_commission_floored(self, tmp_path):
        p = make_portfolio(tmp_path)
        ex = make_executor(tmp_path, p)
        fill = ex.place_order(
            "buy", "233740", 201, 9_940, make_snap(), reason="entry"
        )
        assert fill is not None
        assert fill.qty == 201
        assert fill.price == 9_940
        # 201*9940 = 1,997,940; *0.00015 = 299.691 -> floor 299
        assert fill.commission == 299
        assert p.cash == 8_500_000 - 1_997_940 - 299
        pos = p.positions["233740"]
        assert pos.deadline_date == DEADLINE
        assert pos.entry_date == TODAY
        assert pos.entry_disparity_pct == pytest.approx(-0.6)

        trades = read_trades(tmp_path)
        assert len(trades) == 1
        t = trades[0]
        assert t["side"] == "buy"
        assert t["qty"] == 201
        assert t["price"] == 9_940
        assert t["commission"] == 299
        assert t["nav_at_fill"] == 10_000.0
        assert t["disparity_at_fill"] == pytest.approx(-0.6)
        assert t["reason"] == "entry"
        assert t["deadline_date"] == DEADLINE.isoformat()

    def test_buy_partial_fill_limited_by_ask_qty(self, tmp_path):
        p = make_portfolio(tmp_path)
        ex = make_executor(tmp_path, p)
        fill = ex.place_order(
            "buy", "233740", 201, 9_940, make_snap(ask1_qty=50), reason="entry"
        )
        assert fill is not None
        assert fill.qty == 50
        assert p.positions["233740"].qty == 50

    def test_buy_no_fill_when_price_above_limit(self, tmp_path):
        p = make_portfolio(tmp_path)
        ex = make_executor(tmp_path, p)
        fill = ex.place_order(
            "buy", "233740", 100, 9_940, make_snap(ask1=9_945), reason="entry"
        )
        assert fill is None
        assert p.positions == {}
        assert read_trades(tmp_path) == []

    def test_buy_capped_so_commission_never_overdraws_cash(self, tmp_path):
        # cash exactly 100 shares' worth: commission must shave one share off
        p = make_portfolio(tmp_path, cash=1_000_000)
        ex = make_executor(tmp_path, p)
        fill = ex.place_order(
            "buy", "233740", 100, 10_000,
            make_snap(ask1=10_000, ask1_qty=1_000), reason="entry",
        )
        assert fill is not None
        assert fill.qty == 99
        # 99*10000 = 990,000; comm floor(148.5) = 148
        assert fill.commission == 148
        assert p.cash == 1_000_000 - 990_000 - 148
        assert p.cash >= 0

    def test_sell_partial_fill_remainder_stays(self, tmp_path):
        p = make_portfolio(tmp_path)
        ex = make_executor(tmp_path, p)
        ex.place_order("buy", "233740", 100, 9_940, make_snap(), reason="entry")
        fill = ex.place_order(
            "sell", "233740", 100, 9_990, make_snap(bid1_qty=40), reason="exit"
        )
        assert fill is not None
        assert fill.qty == 40
        assert p.positions["233740"].qty == 60  # unsold remainder stays

        trades = read_trades(tmp_path)
        assert trades[-1]["side"] == "sell"
        assert trades[-1]["qty"] == 40
        assert trades[-1]["reason"] == "exit"

    def test_sell_no_fill_when_bid_below_limit(self, tmp_path):
        p = make_portfolio(tmp_path)
        ex = make_executor(tmp_path, p)
        ex.place_order("buy", "233740", 100, 9_940, make_snap(), reason="entry")
        fill = ex.place_order(
            "sell", "233740", 100, 9_990, make_snap(bid1=9_985), reason="exit"
        )
        assert fill is None
        assert p.positions["233740"].qty == 100

    def test_sell_without_position_is_none(self, tmp_path):
        p = make_portfolio(tmp_path)
        ex = make_executor(tmp_path, p)
        assert ex.place_order("sell", "233740", 10, 9_990, make_snap()) is None

    def test_buy_walks_ask_ladder_realizes_vwap(self, tmp_path):
        # Fill 134 across two levels: 100@9940 + 34@9945.
        p = make_portfolio(tmp_path)
        ex = make_executor(tmp_path, p)
        snap = make_snap(ask_ladder=[(9_940, 100), (9_945, 1_000)])
        fill = ex.place_order("buy", "233740", 134, 9_945, snap, reason="entry")
        assert fill is not None
        assert fill.qty == 134
        # cost = 100*9940 + 34*9945 = 994000 + 338130 = 1,332,130
        cost = 100 * 9_940 + 34 * 9_945
        vwap = cost / 134
        assert fill.price == round(vwap)  # 9941
        # commission = floor(1,332,130 * 0.00015 + 1e-6) = 199
        assert fill.commission == 199
        assert p.cash == 8_500_000 - cost - 199
        pos = p.positions["233740"]
        assert pos.avg_price == pytest.approx(vwap)
        assert pos.qty == 134
        # integer-won invariant preserved despite fractional VWAP
        assert isinstance(p.cash, int)

        t = read_trades(tmp_path)[-1]
        assert t["qty"] == 134
        assert t["price"] == round(vwap)
        assert t["realized_vwap"] == pytest.approx(round(vwap, 4))
        assert t["worst_price"] == 9_945
        assert t["levels_used"] == 2

    def test_buy_ladder_limit_price_rejection_fills_partial(self, tmp_path):
        # ask2 (9960) is above the limit (9940) -> only ask1's depth fills.
        p = make_portfolio(tmp_path)
        ex = make_executor(tmp_path, p)
        snap = make_snap(ask_ladder=[(9_940, 50), (9_960, 1_000)])
        fill = ex.place_order("buy", "233740", 134, 9_940, snap, reason="entry")
        assert fill is not None
        assert fill.qty == 50  # deeper level rejected by limit price
        assert fill.price == 9_940
        assert p.positions["233740"].qty == 50

    def test_buy_empty_ladder_falls_back_to_single_level(self, tmp_path):
        # No ladder -> old ask1-only behavior; realized VWAP == ask1.
        p = make_portfolio(tmp_path)
        ex = make_executor(tmp_path, p)
        fill = ex.place_order("buy", "233740", 100, 9_940, make_snap(), reason="entry")
        assert fill is not None
        assert fill.qty == 100
        assert fill.price == 9_940
        t = read_trades(tmp_path)[-1]
        assert t["realized_vwap"] == pytest.approx(9_940.0)
        assert t["levels_used"] == 1

    def test_buy_ladder_cash_cap_shaves_deepest_shares(self, tmp_path):
        # Cash only covers part of a two-level fill; the shave must drop the
        # most-expensive (deepest) shares first and keep cash non-negative.
        p = make_portfolio(tmp_path, cash=1_000_000)
        ex = make_executor(tmp_path, p)
        snap = make_snap(ask_ladder=[(9_900, 100), (9_950, 100)])
        fill = ex.place_order("buy", "233740", 150, 9_950, snap, reason="entry")
        assert fill is not None
        # 100@9900 = 990,000; adding any 9950 share + commission overflows 1M.
        assert fill.qty == 100
        assert fill.price == 9_900
        assert p.cash >= 0

    def test_round_trip_updates_entry_counter_and_persists(self, tmp_path):
        p = make_portfolio(tmp_path)
        ex = make_executor(tmp_path, p)
        ex.place_order("buy", "233740", 100, 9_940, make_snap(), reason="entry")
        assert p.entries_today(TODAY) == 1
        ex.place_order("sell", "233740", 100, 9_990, make_snap(), reason="exit")

        # state file was saved atomically after each fill; reload and check
        q = Portfolio.load(path=p.path, initial_cash=0)
        assert q.positions == {}
        assert q.cash == p.cash
        assert q.realized_pnl == p.realized_pnl
        # buy comm floor(994000*.00015)=149, sell comm floor(999000*.00015)=149
        # realized = (9990-9940)*100 - 149 - 149 = 4702
        assert q.realized_pnl == 4_702
        assert q.cash - 8_500_000 == q.realized_pnl

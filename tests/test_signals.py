"""Unit tests for the pure signal engine (etf_arb/signals.py).

Everything runs on synthetic snapshots and a fixed wall clock - no network,
no real clock. Covers every entry gate (each distinct rejection reason),
debounce semantics, staleness rules for exit vs force-exit, sizing edge
cases, and force-exit deadline arithmetic across a weekend + holiday using a
locally constructed TradingCalendar.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from etf_arb.calendar import TradingCalendar
from etf_arb.config import (
    Config,
    ExecutionConfig,
    FeesConfig,
    RiskConfig,
    SignalsConfig,
    UniverseConfig,
)
from etf_arb.market_state import TickerState
from etf_arb.signals import (
    ConditionTracker,
    EnterSignal,
    ExitSignal,
    NoAction,
    PortfolioView,
    Position,
    check_disaster,
    evaluate_entry,
    evaluate_exit,
)

# 13:00 on a Monday, well inside the 09:05-15:00 entry window.
NOW = datetime(2026, 7, 20, 13, 0, 0)
NOW_EPOCH = NOW.timestamp()
TODAY = NOW.date()


def make_cfg(**signal_overrides) -> Config:
    sig = dict(
        entry_threshold_pct=0.5,
        exit_threshold_pct=0.1,
        entry_confirm_seconds=3.0,
        nav_max_age_seconds=30.0,
        quote_max_age_seconds=10.0,
        no_entry_before="09:05",
        no_entry_after="15:00",
        force_exit_days=5,
        force_exit_time="14:50",
        disaster_alert_pct=3.0,
        depth_scale_span_pct=0.3,
        min_fill_ratio=0.5,
    )
    sig.update(signal_overrides)
    risk_overrides = sig.pop("_risk", {})
    risk_kwargs = dict(
        virtual_capital_krw=8_500_000,
        alloc_per_position_krw=2_000_000,
        max_positions=4,
        max_entries_per_day=6,
        cooldown_minutes=60,
        min_alloc_per_position_krw=1_000_000,
        max_alloc_per_position_krw=2_000_000,
    )
    risk_kwargs.update(risk_overrides)
    return Config(
        universe=UniverseConfig(
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
        ),
        signals=SignalsConfig(**sig),
        risk=RiskConfig(**risk_kwargs),
        fees=FeesConfig(commission_rate_pct=0.015, min_margin_pct=0.15),
        execution=ExecutionConfig(mode="sim", live_enabled=False),
    )


def make_snap(
    code: str = "233740",
    nav: float | None = 10_000.0,
    nav_age: float = 1.0,
    ask1: int | None = 9_940,
    ask1_qty: int | None = 1_000,
    bid1: int | None = 9_990,
    bid1_qty: int | None = 1_000,
    quote_age: float = 1.0,
    hour_cls_code: str | None = "0",
    now_epoch: float = NOW_EPOCH,
    ask_ladder: list[tuple[int, int]] | None = None,
    bid_ladder: list[tuple[int, int]] | None = None,
) -> TickerState:
    """Default: entry disparity -0.60% (signal-worthy), fresh on both sides.

    ask_ladder/bid_ladder default to empty, exercising the single-level
    fallback in the ladder-walk helpers."""
    return TickerState(
        code=code,
        nav=nav,
        nav_ts=(now_epoch - nav_age) if nav is not None else None,
        ask1=ask1,
        ask1_qty=ask1_qty,
        bid1=bid1,
        bid1_qty=bid1_qty,
        quote_ts=(now_epoch - quote_age) if ask1 is not None else None,
        hour_cls_code=hour_cls_code,
        ask_ladder=ask_ladder or [],
        bid_ladder=bid_ladder or [],
    )


def make_view(
    cash: int = 8_500_000,
    open_codes: frozenset[str] = frozenset(),
    entries_today: int = 0,
    last_exit_ts: dict[str, float] | None = None,
) -> PortfolioView:
    return PortfolioView(
        cash=cash,
        open_codes=open_codes,
        entries_today=entries_today,
        last_exit_ts=last_exit_ts or {},
    )


def make_position(
    code: str = "233740",
    qty: int = 100,
    avg_price: float = 9_940.0,
    entry_date: date = TODAY,
    deadline_date: date = date(2026, 7, 27),
    entry_disparity_pct: float = -0.6,
) -> Position:
    return Position(
        code=code,
        qty=qty,
        avg_price=avg_price,
        entry_ts=NOW_EPOCH - 3600,
        entry_date=entry_date,
        entry_disparity_pct=entry_disparity_pct,
        deadline_date=deadline_date,
        buy_commission=149,
    )


def confirmed_tracker(code: str = "233740") -> ConditionTracker:
    """A tracker whose debounce is already satisfied for `code`."""
    tr = ConditionTracker()
    tr.observe(code, NOW_EPOCH - 5.0, NOW_EPOCH - 5.0)
    tr.observe(code, NOW_EPOCH - 2.0, NOW_EPOCH - 2.0)  # 2nd quote tick
    return tr


def entry(snap=None, cfg=None, now=NOW, view=None, tracker=None):
    return evaluate_entry(
        snap if snap is not None else make_snap(),
        cfg or make_cfg(),
        now,
        view if view is not None else make_view(),
        tracker if tracker is not None else confirmed_tracker(),
    )


# ---------------------------------------------------------------- entry gates

class TestEntryGates:
    def test_signal_fires_when_all_gates_pass(self):
        d = entry()
        assert isinstance(d, EnterSignal)
        assert d.code == "233740"
        assert d.limit_price == 9_940
        # depth-proportional sizing: disp -0.6%, threshold 0.5%, span 0.3%
        #   depth_frac = (0.6-0.5)/0.3 = 1/3
        #   target_alloc = 1_000_000 + 1_000_000*(1/3) = 1_333_333.33
        #   target_qty = floor(min(1_333_333.33, 8_500_000)/9940) = 134
        # empty ladder -> single-level fallback fills all 134 at ask1.
        assert d.qty == 134
        assert d.disparity_pct == pytest.approx(-0.6, abs=1e-6)
        assert d.effective_vwap == pytest.approx(9_940.0)
        assert d.effective_disparity_pct == pytest.approx(-0.6, abs=1e-6)
        assert d.reason == "entry"

    def test_already_in_position(self):
        d = entry(view=make_view(open_codes=frozenset({"233740"})))
        assert d == NoAction("already_in_position")

    def test_max_entries_today(self):
        d = entry(view=make_view(entries_today=6))
        assert d == NoAction("max_entries_today")

    def test_max_positions(self):
        codes = frozenset({"A", "B", "C", "D"})
        d = entry(view=make_view(open_codes=codes))
        assert d == NoAction("max_positions")

    def test_cooldown(self):
        # exited 30 min ago, cooldown is 60 min
        d = entry(view=make_view(last_exit_ts={"233740": NOW_EPOCH - 1800}))
        assert d == NoAction("cooldown")

    def test_cooldown_expired_allows_entry(self):
        d = entry(view=make_view(last_exit_ts={"233740": NOW_EPOCH - 3601}))
        assert isinstance(d, EnterSignal)

    def test_before_entry_window(self):
        early = datetime(2026, 7, 20, 9, 4, 59)
        d = entry(snap=make_snap(now_epoch=early.timestamp()), now=early)
        assert d == NoAction("before_entry_window")

    def test_after_entry_window(self):
        late = datetime(2026, 7, 20, 15, 0, 1)
        d = entry(snap=make_snap(now_epoch=late.timestamp()), now=late)
        assert d == NoAction("after_entry_window")

    def test_not_regular_session(self):
        d = entry(snap=make_snap(hour_cls_code="A"))
        assert d == NoAction("not_regular_session")

    def test_quote_stale(self):
        d = entry(snap=make_snap(quote_age=11.0))
        assert d == NoAction("quote_stale")

    def test_nav_stale(self):
        d = entry(snap=make_snap(nav_age=31.0))
        assert d == NoAction("nav_stale")

    def test_no_disparity_when_nav_zero(self):
        d = entry(snap=make_snap(nav=0.0))
        assert d == NoAction("no_disparity")

    def test_disparity_above_threshold(self):
        # -0.3% > -0.5% threshold -> no entry
        d = entry(snap=make_snap(ask1=9_970))
        assert d == NoAction("disparity_above_threshold")

    def test_disparity_exactly_at_threshold_passes(self):
        # -0.5% == -theta passes the <= comparison
        d = entry(snap=make_snap(ask1=9_950))
        assert isinstance(d, EnterSignal)

    def test_qty_zero(self):
        # price above the whole allocation -> qty 0
        snap = make_snap(nav=3_000_000.0, ask1=2_970_000, bid1=2_985_000)
        d = entry(snap=snap, tracker=confirmed_tracker())
        assert d == NoAction("qty_zero")

    def test_qty_capped_by_cash_not_alloc(self):
        # cash 1M < target_alloc 1.33M -> budget capped by cash
        d = entry(view=make_view(cash=1_000_000))
        assert isinstance(d, EnterSignal)
        assert d.qty == 100  # floor(1_000_000 / 9940)


# ------------------------------------------------- depth-proportional sizing

class TestDepthProportionalSizing:
    """nav=10000; ask1 sets the disparity. threshold 0.5%, span 0.3%,
    min_alloc 1M, max_alloc 2M. Default cash 8.5M so budget = target_alloc.
    Default ask1_qty 1000 (single-level fallback) fills the full target."""

    def test_shallow_disparity_uses_min_alloc(self):
        # disp exactly -0.5% -> depth_frac 0 -> target_alloc = min 1_000_000
        d = entry(snap=make_snap(ask1=9_950))
        assert isinstance(d, EnterSignal)
        assert d.qty == 100  # floor(1_000_000 / 9950)

    def test_deep_disparity_uses_max_alloc(self):
        # disp -0.8% -> depth_frac (0.8-0.5)/0.3 = 1.0 -> max 2_000_000
        d = entry(snap=make_snap(ask1=9_920))
        assert isinstance(d, EnterSignal)
        assert d.qty == 201  # floor(2_000_000 / 9920)

    def test_mid_disparity_interpolates(self):
        # disp -0.65% -> depth_frac 0.5 -> 1_500_000
        d = entry(snap=make_snap(ask1=9_935))
        assert isinstance(d, EnterSignal)
        assert d.qty == 150  # floor(1_500_000 / 9935)

    def test_depth_frac_clipped_at_one(self):
        # disp -1.0% -> (1.0-0.5)/0.3 = 1.667 -> clipped to 1.0 -> max 2_000_000
        d = entry(snap=make_snap(ask1=9_900))
        assert isinstance(d, EnterSignal)
        assert d.qty == 202  # floor(2_000_000 / 9900)

    def test_deeper_disparity_never_shrinks_allocation(self):
        shallow = entry(snap=make_snap(ask1=9_950))  # min alloc
        deep = entry(snap=make_snap(ask1=9_920))      # max alloc
        # more capital at 9920 despite the higher per-share price
        assert deep.qty * 9_920 > shallow.qty * 9_950


# ----------------------------------------- effective-disparity double-check

class TestEffectiveDisparityDoubleCheck:
    """Default snap: disp -0.6% -> target_alloc 1_333_333.33 -> target_qty 134,
    max_vwap = 10000*(1-0.5/100) = 9950. min_fill_ratio 0.5 -> floor 67."""

    def test_book_too_thin_effective_skips(self):
        # ask1 depth 40 (< 67); ask2 @9990 erodes VWAP past 9950 -> qty_ok
        # stuck at 40 < min_fill_ratio*target -> skip.
        snap = make_snap(ask_ladder=[(9_940, 40), (9_990, 1_000)])
        assert entry(snap=snap) == NoAction("book_too_thin_effective")

    def test_reduced_qty_when_deeper_level_erodes_edge(self):
        # ask1 depth 100 (>= 67); ask2 @9990 erodes edge at full size, so
        # qty_ok caps at 100 (< target 134) but clears min_fill_ratio -> enter.
        snap = make_snap(ask_ladder=[(9_940, 100), (9_990, 1_000)])
        d = entry(snap=snap)
        assert isinstance(d, EnterSignal)
        assert d.qty == 100
        assert d.limit_price == 9_940  # only ask1 level accepted
        assert d.effective_vwap == pytest.approx(9_940.0)
        assert d.effective_disparity_pct == pytest.approx(-0.6, abs=1e-6)

    def test_multi_level_accepted_when_vwap_still_clears(self):
        # ask2 only slightly higher (9945): full 134 fills, VWAP ~9941.3
        # still <= 9950 -> enter full size, limit at worst accepted level 9945.
        snap = make_snap(ask_ladder=[(9_940, 100), (9_945, 1_000)])
        d = entry(snap=snap)
        assert isinstance(d, EnterSignal)
        assert d.qty == 134
        assert d.limit_price == 9_945
        cost = 100 * 9_940 + 34 * 9_945
        assert d.effective_vwap == pytest.approx(cost / 134)
        assert d.effective_disparity_pct == pytest.approx(
            (cost / 134 - 10_000) / 10_000 * 100
        )

    def test_exact_boundary_vwap_accepted(self):
        # Single deep level priced exactly at max_vwap (9950 == boundary).
        snap = make_snap(ask1=9_950, ask_ladder=[(9_950, 1_000)])
        d = entry(snap=snap)
        assert isinstance(d, EnterSignal)  # equality clears the <= test
        assert d.qty == 100  # min alloc (disp -0.5), floor(1M/9950)

    def test_empty_ladder_single_level_fallback_enters(self):
        # No ladder at all -> single ask1 level -> old behavior preserved.
        d = entry(snap=make_snap(ask_ladder=[]))
        assert isinstance(d, EnterSignal)
        assert d.qty == 134
        assert d.limit_price == 9_940


# ---------------------------------------------------------------- debounce

class TestDebounce:
    def test_requires_duration_and_two_quote_ticks(self):
        tr = ConditionTracker()
        t0 = NOW_EPOCH

        # tick 1: condition true -> starts the clock, still confirming
        n1 = datetime.fromtimestamp(t0)
        d1 = evaluate_entry(
            make_snap(quote_age=0.5, now_epoch=t0), make_cfg(), n1,
            make_view(), tr,
        )
        assert d1 == NoAction("confirming")

        # 4s later but with the SAME quote_ts -> only 1 quote tick, still no
        t1 = t0 + 4.0
        snap_same_quote = make_snap(now_epoch=t0, quote_age=0.5)
        snap_same_quote.nav_ts = t1 - 1.0  # keep NAV fresh
        d2 = evaluate_entry(
            snap_same_quote, make_cfg(), datetime.fromtimestamp(t1),
            make_view(), tr,
        )
        assert d2 == NoAction("confirming")

        # a second, fresh quote tick after >= confirm_seconds -> fires
        t2 = t0 + 5.0
        d3 = evaluate_entry(
            make_snap(quote_age=0.1, now_epoch=t2), make_cfg(),
            datetime.fromtimestamp(t2), make_view(), tr,
        )
        assert isinstance(d3, EnterSignal)

    def test_two_ticks_but_too_fast_keeps_confirming(self):
        tr = ConditionTracker()
        t0 = NOW_EPOCH
        evaluate_entry(
            make_snap(quote_age=0.5, now_epoch=t0), make_cfg(),
            datetime.fromtimestamp(t0), make_view(), tr,
        )
        t1 = t0 + 1.0  # second quote tick, but only 1s elapsed (< 3s)
        d = evaluate_entry(
            make_snap(quote_age=0.1, now_epoch=t1), make_cfg(),
            datetime.fromtimestamp(t1), make_view(), tr,
        )
        assert d == NoAction("confirming")

    def test_condition_break_resets_clock(self):
        tr = ConditionTracker()
        t0 = NOW_EPOCH
        evaluate_entry(
            make_snap(quote_age=0.5, now_epoch=t0), make_cfg(),
            datetime.fromtimestamp(t0), make_view(), tr,
        )
        # 2s in, disparity recovers above threshold -> tracker resets
        t1 = t0 + 2.0
        d_break = evaluate_entry(
            make_snap(ask1=9_990, quote_age=0.1, now_epoch=t1), make_cfg(),
            datetime.fromtimestamp(t1), make_view(), tr,
        )
        assert d_break == NoAction("disparity_above_threshold")
        assert tr.peek("233740") is None

        # condition true again at t0+4 with 2 ticks by t0+5 -> still needs
        # 3s from the RESET, so t0+5 must not fire yet
        t2 = t0 + 4.0
        evaluate_entry(
            make_snap(quote_age=0.1, now_epoch=t2), make_cfg(),
            datetime.fromtimestamp(t2), make_view(), tr,
        )
        t3 = t0 + 5.0
        d = evaluate_entry(
            make_snap(quote_age=0.1, now_epoch=t3), make_cfg(),
            datetime.fromtimestamp(t3), make_view(), tr,
        )
        assert d == NoAction("confirming")

    def test_staleness_resets_debounce(self):
        tr = ConditionTracker()
        t0 = NOW_EPOCH
        evaluate_entry(
            make_snap(quote_age=0.5, now_epoch=t0), make_cfg(),
            datetime.fromtimestamp(t0), make_view(), tr,
        )
        t1 = t0 + 2.0
        d = evaluate_entry(
            make_snap(quote_age=11.0, now_epoch=t1), make_cfg(),
            datetime.fromtimestamp(t1), make_view(), tr,
        )
        assert d == NoAction("quote_stale")
        assert tr.peek("233740") is None

    def test_successful_entry_resets_tracker(self):
        tr = confirmed_tracker()
        d = entry(tracker=tr)
        assert isinstance(d, EnterSignal)
        assert tr.peek("233740") is None


# ---------------------------------------------------------------- exit

class TestExit:
    def test_normal_exit_fires_when_disparity_recovered(self):
        # bid 9990 vs nav 10000 -> -0.1% >= -0.1% threshold
        d = evaluate_exit(make_position(), make_snap(), make_cfg(), NOW)
        assert isinstance(d, ExitSignal)
        assert d.reason == "exit"
        assert d.qty == 100
        assert d.limit_price == 9_990

    def test_exit_blocked_below_threshold(self):
        d = evaluate_exit(
            make_position(), make_snap(bid1=9_985), make_cfg(), NOW
        )
        assert d == NoAction("exit_disparity_below")

    def test_nav_staleness_blocks_normal_exit(self):
        d = evaluate_exit(
            make_position(), make_snap(nav_age=31.0), make_cfg(), NOW
        )
        assert d == NoAction("exit_nav_stale")

    def test_quote_staleness_blocks_normal_exit(self):
        d = evaluate_exit(
            make_position(), make_snap(quote_age=11.0), make_cfg(), NOW
        )
        assert d == NoAction("exit_quote_stale")

    def test_closing_auction_blocks_normal_exit(self):
        d = evaluate_exit(
            make_position(), make_snap(hour_cls_code="A"), make_cfg(), NOW
        )
        assert d == NoAction("exit_not_regular_session")

    def test_force_exit_ignores_staleness(self):
        # deadline day, 14:50, everything stale -> still exits at bid
        at_1450 = datetime(2026, 7, 27, 14, 50, 0)
        snap = make_snap(
            nav_age=600.0, quote_age=600.0, hour_cls_code="A",
            now_epoch=at_1450.timestamp(),
        )
        pos = make_position(deadline_date=date(2026, 7, 27))
        d = evaluate_exit(pos, snap, make_cfg(), at_1450)
        assert isinstance(d, ExitSignal)
        assert d.reason == "force_exit"
        assert d.limit_price == 9_990

    def test_no_force_exit_before_cutoff_time_on_deadline_day(self):
        at_1449 = datetime(2026, 7, 27, 14, 49, 59)
        snap = make_snap(bid1=9_985, now_epoch=at_1449.timestamp())
        pos = make_position(deadline_date=date(2026, 7, 27))
        d = evaluate_exit(pos, snap, make_cfg(), at_1449)
        assert d == NoAction("exit_disparity_below")

    def test_force_exit_any_time_once_deadline_passed(self):
        # restarted the day AFTER the deadline: exit at first opportunity
        morning = datetime(2026, 7, 28, 9, 10, 0)
        snap = make_snap(bid1=9_985, now_epoch=morning.timestamp())
        pos = make_position(deadline_date=date(2026, 7, 27))
        d = evaluate_exit(pos, snap, make_cfg(), morning)
        assert isinstance(d, ExitSignal)
        assert d.reason == "force_exit"

    def test_force_exit_without_quote_reports_reason(self):
        at_1450 = datetime(2026, 7, 27, 14, 50, 0)
        snap = TickerState(code="233740")  # never received anything
        pos = make_position(deadline_date=date(2026, 7, 27))
        d = evaluate_exit(pos, snap, make_cfg(), at_1450)
        assert d == NoAction("force_exit_no_quote")


# ---------------------------------------------------------------- disaster

class TestDisaster:
    def test_alert_at_threshold(self):
        snap = make_snap(ask1=9_700)  # -3.0%
        assert check_disaster(snap, make_cfg()) == pytest.approx(-3.0)

    def test_no_alert_above_threshold(self):
        assert check_disaster(make_snap(), make_cfg()) is None  # -0.6%


# ------------------------------------------------- deadline arithmetic

class TestDeadlineArithmetic:
    """force_exit_days across a weekend + holiday, on a locally built
    TradingCalendar (no network)."""

    @staticmethod
    def cal() -> TradingCalendar:
        days: dict[str, bool] = {}
        d = date(2026, 7, 20)
        while d <= date(2026, 8, 14):
            days[d.strftime("%Y%m%d")] = d.weekday() < 5
            d += timedelta(days=1)
        days["20260727"] = False  # 가상 공휴일 (월요일)
        return TradingCalendar(days)

    def test_five_trading_days_across_weekend_and_holiday(self):
        cal = self.cal()
        # entry Fri 7/24; skips Sat 25, Sun 26, holiday Mon 27
        # -> 28, 29, 30, 31, then Mon 8/3 = 5th trading day
        assert cal.add_trading_days(date(2026, 7, 24), 5) == date(2026, 8, 3)

    def test_plain_week(self):
        cal = self.cal()
        # entry Mon 7/20 -> 21, 22, 23, 24, then (27 holiday, 25/26 weekend)
        # 5th trading day = Tue 7/28
        assert cal.add_trading_days(date(2026, 7, 20), 5) == date(2026, 7, 28)

    def test_one_day(self):
        cal = self.cal()
        # Fri 7/24 + 1 trading day skips weekend + holiday -> Tue 7/28
        assert cal.add_trading_days(date(2026, 7, 24), 1) == date(2026, 7, 28)

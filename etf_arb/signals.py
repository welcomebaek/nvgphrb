"""Pure signal engine for the ETF NAV-disparity strategy (Phase 3).

No I/O and no clock reads: every input (snapshot, wall-clock `now`, portfolio
view, debounce tracker) is passed in, so every function here is deterministic
and unit-testable without a network.

Sign conventions (from the plan):
  entry disparity = (ask1 - nav) / nav  [%]  - what we PAY relative to NAV
  exit  disparity = (bid1 - nav) / nav  [%]  - what we RECEIVE relative to NAV
Enter when entry disparity <= -entry_threshold_pct (ask abnormally cheap),
exit when exit disparity >= -exit_threshold_pct (discount mostly reverted).

Every rejection returns a NoAction with a DISTINCT reason code so the journal
can build a skip-reason histogram (the main tuning tool).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time as dtime
from typing import Mapping, Union

from etf_arb.config import Config
from etf_arb.market_state import TickerState

# H0STASP0 hour_cls_code: '0' = 장중(정규장). During the closing auction
# (15:20~15:30) the quote fields switch to expected-execution-price semantics,
# so anything other than '0' must not be traded on.
REGULAR_SESSION_CODE = "0"


# ---------------------------------------------------------------- data types

@dataclass(frozen=True)
class Position:
    code: str
    qty: int
    avg_price: float          # per-share fill price (won)
    entry_ts: float           # epoch seconds of the entry fill
    entry_date: date
    entry_disparity_pct: float
    deadline_date: date       # force-exit deadline (trading-day arithmetic)
    buy_commission: int = 0   # unallocated buy commission remaining (won)


@dataclass(frozen=True)
class EnterSignal:
    code: str
    qty: int
    limit_price: int
    reason: str               # "entry"
    disparity_pct: float      # ask1-vs-nav disparity (top of book)
    effective_vwap: float     # volume-weighted avg price over the accepted fill
    effective_disparity_pct: float  # (effective_vwap - nav)/nav*100


@dataclass(frozen=True)
class ExitSignal:
    code: str
    qty: int
    limit_price: int
    reason: str               # "exit" | "force_exit"
    disparity_pct: float | None


@dataclass(frozen=True)
class NoAction:
    reason_code: str


EntryDecision = Union[EnterSignal, NoAction]
ExitDecision = Union[ExitSignal, NoAction]


@dataclass(frozen=True)
class PortfolioView:
    """Read-only slice of portfolio state that entry gating needs."""

    cash: int
    open_codes: frozenset[str]
    entries_today: int
    last_exit_ts: Mapping[str, float]  # code -> epoch seconds of last exit


# ---------------------------------------------------------------- debounce

@dataclass
class ConfirmState:
    first_ok_ts: float          # epoch when the condition first became true
    last_quote_ts: float | None
    quote_ticks: int            # distinct quote timestamps seen while true


class ConditionTracker:
    """Per-code debounce state for the entry condition.

    The entry condition must hold continuously for entry_confirm_seconds AND
    be observed on >= 2 distinct quote ticks (distinct quote_ts values), so a
    single stray quote frame can never trigger an entry on its own. Any break
    of the condition resets the clock. Pure state - the caller owns it and
    passes it into evaluate_entry().
    """

    def __init__(self) -> None:
        self._by_code: dict[str, ConfirmState] = {}

    def observe(
        self, code: str, now_epoch: float, quote_ts: float | None
    ) -> ConfirmState:
        st = self._by_code.get(code)
        if st is None:
            st = ConfirmState(
                first_ok_ts=now_epoch,
                last_quote_ts=quote_ts,
                quote_ticks=1 if quote_ts is not None else 0,
            )
            self._by_code[code] = st
        elif quote_ts is not None and quote_ts != st.last_quote_ts:
            st.quote_ticks += 1
            st.last_quote_ts = quote_ts
        return st

    def reset(self, code: str) -> None:
        self._by_code.pop(code, None)

    def peek(self, code: str) -> ConfirmState | None:
        return self._by_code.get(code)


# ---------------------------------------------------------------- helpers

def _hhmm(value: str) -> dtime:
    h, m = value.split(":")
    return dtime(int(h), int(m))


# ---------------------------------------------------------------- entry

def evaluate_entry(
    snapshot: TickerState,
    cfg: Config,
    now: datetime,
    portfolio: PortfolioView,
    tracker: ConditionTracker,
) -> EntryDecision:
    """All entry gates from the plan, in order. Pure - no I/O, no clock.

    Gate order: risk gates first (these do NOT touch the debounce tracker,
    so a temporarily blocked but persisting opportunity keeps its clock),
    then time/session/freshness/disparity gates (failures RESET the tracker -
    the condition is no longer verifiably true), then debounce, then sizing.
    """
    s, r = cfg.signals, cfg.risk
    code = snapshot.code
    now_epoch = now.timestamp()

    # -- risk gates ---------------------------------------------------------
    if code in portfolio.open_codes:
        return NoAction("already_in_position")
    if portfolio.entries_today >= r.max_entries_per_day:
        return NoAction("max_entries_today")
    if len(portfolio.open_codes) >= r.max_positions:
        return NoAction("max_positions")
    last_exit = portfolio.last_exit_ts.get(code)
    if last_exit is not None and now_epoch - last_exit < r.cooldown_minutes * 60:
        return NoAction("cooldown")

    # -- time window --------------------------------------------------------
    t = now.time()
    if t < _hhmm(s.no_entry_before):
        tracker.reset(code)
        return NoAction("before_entry_window")
    if t > _hhmm(s.no_entry_after):
        tracker.reset(code)
        return NoAction("after_entry_window")

    # -- session / freshness ------------------------------------------------
    if snapshot.hour_cls_code != REGULAR_SESSION_CODE:
        tracker.reset(code)
        return NoAction("not_regular_session")
    if snapshot.quote_stale(now_epoch, s.quote_max_age_seconds):
        tracker.reset(code)
        return NoAction("quote_stale")
    if snapshot.nav_stale(now_epoch, s.nav_max_age_seconds):
        tracker.reset(code)
        return NoAction("nav_stale")

    # -- disparity condition ------------------------------------------------
    disp = snapshot.entry_disparity_pct()
    if disp is None:
        tracker.reset(code)
        return NoAction("no_disparity")
    if disp > -s.entry_threshold_pct:
        tracker.reset(code)
        return NoAction("disparity_above_threshold")

    # -- debounce: continuously true for confirm_seconds on >=2 quote ticks -
    confirm = tracker.observe(code, now_epoch, snapshot.quote_ts)
    if (
        now_epoch - confirm.first_ok_ts < s.entry_confirm_seconds
        or confirm.quote_ticks < 2
    ):
        return NoAction("confirming")

    # -- capital-capped, order-book-driven sizing ---------------------------
    # disp is not None => nav > 0 and ask1 truthy, so the divisions are safe.
    # Capital no longer determines the size directly - it only sets a
    # generous ceiling (cap_qty). The ladder walk below is what actually
    # decides how much we buy: real liquidity stacked in the profitable
    # price range IS the size, not a formula proportional to disparity depth.
    capital_cap = min(r.max_alloc_per_position_krw, portfolio.cash)
    cap_qty = int(capital_cap // snapshot.ask1)
    if cap_qty <= 0:
        return NoAction("qty_zero")

    # -- multi-level order-book walk: sizing AND effective-disparity check -
    # Don't trust ask1 alone: walk the ask ladder up to cap_qty shares,
    # stopping at the deepest level whose cumulative VWAP still clears the
    # entry threshold. max_vwap is the highest average price whose effective
    # disparity is still <= -entry_threshold_pct: (vwap-nav)/nav*100 <=
    # -theta  <=>  vwap <= nav*(1 - theta/100). qty_ok is the final size -
    # skip only when the resulting notional can't clear the minimum-viable-
    # trade floor (not worth the fixed costs of a round trip).
    max_vwap = snapshot.nav * (1.0 - s.entry_threshold_pct / 100.0)
    fill = snapshot.effective_buy_fill(cap_qty, max_vwap)
    if fill is None:
        return NoAction("book_too_thin_effective")
    qty_ok, eff_vwap, worst_price = fill
    if qty_ok * eff_vwap < r.min_alloc_per_position_krw:
        return NoAction("notional_too_small")
    eff_disp = (eff_vwap - snapshot.nav) / snapshot.nav * 100.0

    tracker.reset(code)  # a fresh confirmation is required for any re-entry
    return EnterSignal(
        code=code,
        qty=qty_ok,
        # worst accepted level price -> executor limit-reject still works and
        # it may fill every accepted level up to this price.
        limit_price=int(worst_price),
        reason="entry",
        disparity_pct=disp,
        effective_vwap=eff_vwap,
        effective_disparity_pct=eff_disp,
    )


# ---------------------------------------------------------------- exit

def evaluate_exit(
    position: Position,
    snapshot: TickerState,
    cfg: Config,
    now: datetime,
) -> ExitDecision:
    """Exit gates. Force exit ignores staleness (deadline discipline beats
    data quality); the normal exit requires fresh NAV+quote and a regular
    session so the disparity comparison is meaningful."""
    s = cfg.signals
    now_epoch = now.timestamp()
    today = now.date()
    disp = snapshot.exit_disparity_pct()

    # -- force exit: deadline reached -> unconditional bid-side sell --------
    # On the deadline day we wait until force_exit_time (14:50, before the
    # closing auction); once the deadline day has PASSED we exit at the first
    # opportunity regardless of time-of-day.
    overdue = today > position.deadline_date
    due_now = (
        today == position.deadline_date
        and now.time() >= _hhmm(s.force_exit_time)
    )
    if overdue or due_now:
        if not snapshot.bid1:
            return NoAction("force_exit_no_quote")
        return ExitSignal(
            code=position.code,
            qty=position.qty,
            limit_price=int(snapshot.bid1),
            reason="force_exit",
            disparity_pct=disp,
        )

    # -- normal exit --------------------------------------------------------
    if snapshot.hour_cls_code != REGULAR_SESSION_CODE:
        return NoAction("exit_not_regular_session")
    if snapshot.quote_stale(now_epoch, s.quote_max_age_seconds):
        return NoAction("exit_quote_stale")
    if snapshot.nav_stale(now_epoch, s.nav_max_age_seconds):
        return NoAction("exit_nav_stale")
    if disp is None:
        return NoAction("exit_no_disparity")
    if disp < -s.exit_threshold_pct:
        return NoAction("exit_disparity_below")
    return ExitSignal(
        code=position.code,
        qty=position.qty,
        limit_price=int(snapshot.bid1),
        reason="exit",
        disparity_pct=disp,
    )


# ---------------------------------------------------------------- disaster

def check_disaster(snapshot: TickerState, cfg: Config) -> float | None:
    """Journal-only alert: entry-side disparity <= -disaster_alert_pct.

    Returns the disparity when in the disaster zone, else None. Takes no
    action - the caller journals it (a widening discount on a held position
    is information, not an automatic trade)."""
    disp = snapshot.entry_disparity_pct()
    if disp is not None and disp <= -cfg.signals.disaster_alert_pct:
        return disp
    return None

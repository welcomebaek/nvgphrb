"""Per-ticker latest market snapshot store (pure, no I/O).

Merges the two asynchronous realtime streams (H0STNAV0 NAV ticks and
H0STASP0 order-book ticks) into one snapshot per ticker, each side carrying
its own receive timestamp so staleness can be judged independently against
the config limits (signals.nav_max_age_seconds / quote_max_age_seconds).

All timestamps are epoch seconds (time.time() at frame receipt). Every
method takes `now` explicitly so this module stays deterministic and
unit-testable; callers pass time.time().
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TickerState:
    code: str
    nav: float | None = None
    nav_ts: float | None = None
    ask1: int | None = None
    ask1_qty: int | None = None
    bid1: int | None = None
    bid1_qty: int | None = None
    quote_ts: float | None = None
    hour_cls_code: str | None = None
    nav_frames: int = 0
    quote_frames: int = 0
    # Full order-book ladders as [(price, qty), ...], best level first. Empty
    # when only top-of-book (ask1/bid1) is known; the ladder-walk helpers fall
    # back to the single top-of-book level in that case.
    ask_ladder: list[tuple[int, int]] = field(default_factory=list)
    bid_ladder: list[tuple[int, int]] = field(default_factory=list)

    def nav_age(self, now: float) -> float | None:
        """Seconds since last NAV tick, or None if never received."""
        if self.nav_ts is None:
            return None
        return now - self.nav_ts

    def quote_age(self, now: float) -> float | None:
        """Seconds since last quote tick, or None if never received."""
        if self.quote_ts is None:
            return None
        return now - self.quote_ts

    def nav_stale(self, now: float, max_age_seconds: float) -> bool:
        """True when NAV is missing or older than max_age_seconds."""
        age = self.nav_age(now)
        return age is None or age > max_age_seconds

    def quote_stale(self, now: float, max_age_seconds: float) -> bool:
        """True when the quote is missing or older than max_age_seconds."""
        age = self.quote_age(now)
        return age is None or age > max_age_seconds

    def entry_disparity_pct(self) -> float | None:
        """(ask1 - nav) / nav in percent - the buy-side (entry) disparity."""
        if self.nav is None or self.nav <= 0 or not self.ask1:
            return None
        return (self.ask1 - self.nav) / self.nav * 100.0

    def exit_disparity_pct(self) -> float | None:
        """(bid1 - nav) / nav in percent - the sell-side (exit) disparity."""
        if self.nav is None or self.nav <= 0 or not self.bid1:
            return None
        return (self.bid1 - self.nav) / self.nav * 100.0

    # -- order-book ladder walks (pure, deterministic, no I/O) --------------

    def ask_levels(self) -> list[tuple[int, int]]:
        """Usable ask-side ladder [(price, qty), ...], best price first.

        Falls back to the single ask1 level when no multi-level ladder is
        present (graceful degradation when only top-of-book is known). Stops
        at the first empty (0/None price or qty) level - a valid ladder ends
        at the first hole."""
        source = self.ask_ladder or (
            [(self.ask1, self.ask1_qty)]
            if self.ask1 and self.ask1_qty is not None
            else []
        )
        out: list[tuple[int, int]] = []
        for price, qty in source:
            if not price or qty is None or price <= 0 or qty <= 0:
                break
            out.append((int(price), int(qty)))
        return out

    def effective_buy_fill(
        self, target_qty: int, max_vwap: float
    ) -> tuple[int, float, int] | None:
        """Walk the ask ladder accumulating up to target_qty shares.

        Returns the deepest LEVEL-boundary cumulative fill
        (qty_ok, cumulative_vwap, worst_price) whose cumulative VWAP stays
        <= max_vwap. Because deeper ask levels are priced higher, cumulative
        VWAP is monotonically non-decreasing, so once a level pushes VWAP
        over max_vwap the walk stops. Returns None when there is no usable
        liquidity or even the best level already exceeds max_vwap.

        The qty is measured at level boundaries (or the target_qty cap on the
        final level), matching "cumulative VWAP at each level" - no
        partial-within-level refinement."""
        if target_qty <= 0:
            return None
        filled = 0
        cost = 0
        best: tuple[int, float, int] | None = None
        for price, qty in self.ask_levels():
            take = min(qty, target_qty - filled)
            if take <= 0:
                break
            new_filled = filled + take
            new_cost = cost + take * price
            new_vwap = new_cost / new_filled
            if new_vwap > max_vwap:
                break  # this level erodes the edge past the limit; stop
            filled, cost = new_filled, new_cost
            best = (filled, new_vwap, price)
            if filled >= target_qty:
                break
        return best

    def effective_buy_vwap(self, target_qty: int) -> tuple[int, float] | None:
        """(filled_qty, volume_weighted_avg_price) for filling up to
        target_qty by walking the ask ladder, ignoring any price ceiling.
        None when target_qty <= 0 or there is no usable liquidity. Useful for
        journaling the realized effective price of an intended fill size."""
        res = self.effective_buy_fill(target_qty, float("inf"))
        if res is None:
            return None
        filled, vwap, _worst = res
        return filled, vwap


@dataclass
class MarketState:
    """Snapshot store keyed by ticker code."""

    _states: dict[str, TickerState] = field(default_factory=dict)

    def get(self, code: str) -> TickerState:
        state = self._states.get(code)
        if state is None:
            state = TickerState(code=code)
            self._states[code] = state
        return state

    def codes(self) -> list[str]:
        return list(self._states.keys())

    def update_nav(self, code: str, nav: float, ts: float) -> TickerState:
        state = self.get(code)
        state.nav = nav
        state.nav_ts = ts
        state.nav_frames += 1
        return state

    def update_quote(
        self,
        code: str,
        ask1: int,
        ask1_qty: int,
        bid1: int,
        bid1_qty: int,
        hour_cls_code: str,
        ts: float,
        ask_ladder: list[tuple[int, int]] | None = None,
        bid_ladder: list[tuple[int, int]] | None = None,
    ) -> TickerState:
        state = self.get(code)
        state.ask1 = ask1
        state.ask1_qty = ask1_qty
        state.bid1 = bid1
        state.bid1_qty = bid1_qty
        state.hour_cls_code = hour_cls_code
        state.quote_ts = ts
        state.quote_frames += 1
        state.ask_ladder = list(ask_ladder) if ask_ladder else []
        state.bid_ladder = list(bid_ladder) if bid_ladder else []
        return state

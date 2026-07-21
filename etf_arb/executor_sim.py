"""Conservative simulated executor (Phase 3).

Fill model:
  buy : walks the ask ladder - fills up to `requested` shares across levels,
        each level's shares at that level's price, rejecting any level priced
        above limit_price; realized price is the volume-weighted average. When
        no multi-level ladder is present it falls back to the single ask1
        level (the old ask1-only behavior). filled_qty may be < requested.
  sell: fills at bid1, filled_qty = min(requested, bid1_qty, position qty).
        (A symmetric bid-ladder sell-side effective-disparity check is a
        possible future extension; intentionally not implemented here.)
Partial fills are allowed; the unsold remainder simply stays in the position.
Commission is charged on both sides, floored to whole won. A buy is
additionally capped so that qty*price + commission never exceeds cash.

Every fill is appended to logs/etf_arb_trades_sim.jsonl and the portfolio is
saved atomically right after the in-memory update, so a kill at any moment
leaves a consistent state file.

Determinism: the clock (now_fn) and the deadline arithmetic (deadline_fn,
normally calendar.add_trading_days) are injected, so unit tests run without
network or wall clock.
"""

from __future__ import annotations

import json
import math
import os
import time
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from etf_arb.executor import Fill
from etf_arb.market_state import TickerState
from etf_arb.portfolio import Portfolio

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TRADES_PATH = PROJECT_ROOT / "logs" / "etf_arb_trades_sim.jsonl"


class SimExecutor:
    def __init__(
        self,
        portfolio: Portfolio,
        commission_rate_pct: float,
        deadline_fn: Callable[[date], date],
        trades_path: Path = DEFAULT_TRADES_PATH,
        now_fn: Callable[[], float] = time.time,
    ):
        self.portfolio = portfolio
        self._rate = commission_rate_pct / 100.0
        self._deadline_fn = deadline_fn
        self.trades_path = Path(trades_path)
        self._now_fn = now_fn

    # -- public API (matches executor.Executor) -----------------------------

    def place_order(
        self,
        side: str,
        code: str,
        qty: int,
        limit_price: int,
        snapshot: TickerState,
        reason: str = "",
    ) -> Fill | None:
        if qty <= 0:
            return None
        if side == "buy":
            return self._buy(code, qty, limit_price, snapshot, reason)
        if side == "sell":
            return self._sell(code, qty, limit_price, snapshot, reason)
        raise ValueError(f"알 수 없는 주문 방향: {side!r}")

    # -- internals ----------------------------------------------------------

    def _commission(self, qty: int, price: int) -> int:
        # +1e-6 absorbs binary-float error so an exact boundary like
        # 2,000,000 * 0.00015 = 300.0 never floors to 299.
        return int(math.floor(qty * price * self._rate + 1e-6))

    def _commission_cost(self, cost: int) -> int:
        """Commission on a notional `cost` (won). Used for multi-level buys
        where notional is the sum of qty_i*price_i, not qty*price."""
        return int(math.floor(cost * self._rate + 1e-6))

    def _buy(
        self,
        code: str,
        qty: int,
        limit_price: int,
        snapshot: TickerState,
        reason: str,
    ) -> Fill | None:
        # Walk the ask ladder: accumulate up to `qty` shares across levels,
        # each level's shares at that level's price, rejecting any level
        # priced above limit_price (a book that moved up just fills less -
        # conservative). ask_levels() falls back to the single ask1 level
        # when no multi-level ladder is present.
        segs: list[tuple[int, int]] = []  # (price, shares) actually taken
        filled = 0
        for price, lvl_qty in snapshot.ask_levels():
            if price > limit_price:
                break  # this and all deeper levels are above our limit
            take = min(lvl_qty, qty - filled)
            if take <= 0:
                break
            segs.append((price, take))
            filled += take
            if filled >= qty:
                break
        if filled <= 0:
            return None

        def cost_of(n: int) -> int:
            c = 0
            rem = n
            for seg_price, seg_take in segs:
                t = seg_take if seg_take < rem else rem
                c += t * seg_price
                rem -= t
                if rem <= 0:
                    break
            return c

        # Cap so cost incl. commission never exceeds cash (sizing uses
        # budget//ask1, so commission can push the cost just over cash).
        # Shaving one share removes the deepest, most-expensive share first.
        while filled > 0:
            cost = cost_of(filled)
            if cost + self._commission_cost(cost) <= self.portfolio.cash:
                break
            filled -= 1
        if filled <= 0:
            return None

        cost = cost_of(filled)
        commission = self._commission_cost(cost)
        vwap = cost / filled            # exact volume-weighted avg (float)
        fill_price = round(vwap)        # integer per-share price for display
        # Deepest (highest) price among the shares actually kept: walk the
        # ascending-price segments until the cumulative reaches `filled`.
        worst_price = segs[0][0]
        cum = 0
        for seg_price, seg_take in segs:
            worst_price = seg_price
            cum += seg_take
            if cum >= filled:
                break

        ts = self._now_fn()
        entry_date = datetime.fromtimestamp(ts).date()
        deadline = self._deadline_fn(entry_date)
        disparity = snapshot.entry_disparity_pct()

        self.portfolio.apply_buy(
            code=code,
            qty=filled,
            price=vwap,  # exact VWAP -> position avg_price is exact
            commission=commission,
            ts=ts,
            entry_date=entry_date,
            entry_disparity_pct=disparity if disparity is not None else 0.0,
            deadline_date=deadline,
        )
        self.portfolio.record_entry(entry_date)
        self.portfolio.save()

        self._write_trade(
            ts=ts,
            code=code,
            side="buy",
            qty=filled,
            price=fill_price,
            commission=commission,
            nav_at_fill=snapshot.nav,
            disparity_at_fill=disparity,
            reason=reason,
            extra={
                "deadline_date": deadline.isoformat(),
                "qty_requested": qty,
                "realized_vwap": round(vwap, 4),
                "worst_price": worst_price,
                "levels_used": len(segs),
            },
        )
        return Fill(
            side="buy", code=code, qty=filled, price=fill_price,
            commission=commission, ts=ts,
        )

    def _sell(
        self,
        code: str,
        qty: int,
        limit_price: int,
        snapshot: TickerState,
        reason: str,
    ) -> Fill | None:
        pos = self.portfolio.positions.get(code)
        if pos is None:
            return None
        if not snapshot.bid1 or snapshot.bid1_qty is None:
            return None
        price = int(snapshot.bid1)
        if price < limit_price:  # book moved below our limit -> no fill
            return None

        fill_qty = min(qty, snapshot.bid1_qty, pos.qty)
        if fill_qty <= 0:
            return None

        ts = self._now_fn()
        commission = self._commission(fill_qty, price)
        realized = self.portfolio.apply_sell(
            code=code, qty=fill_qty, price=price, commission=commission, ts=ts
        )
        self.portfolio.save()

        self._write_trade(
            ts=ts,
            code=code,
            side="sell",
            qty=fill_qty,
            price=price,
            commission=commission,
            nav_at_fill=snapshot.nav,
            disparity_at_fill=snapshot.exit_disparity_pct(),
            reason=reason,
            extra={"realized_pnl": realized, "qty_requested": qty},
        )
        return Fill(
            side="sell", code=code, qty=fill_qty, price=price,
            commission=commission, ts=ts,
        )

    def _write_trade(
        self,
        ts: float,
        code: str,
        side: str,
        qty: int,
        price: int,
        commission: int,
        nav_at_fill: float | None,
        disparity_at_fill: float | None,
        reason: str,
        extra: dict | None = None,
    ) -> None:
        record = {
            "ts": datetime.fromtimestamp(ts).astimezone().isoformat(
                timespec="milliseconds"
            ),
            "epoch_ts": round(ts, 3),
            "code": code,
            "side": side,
            "qty": qty,
            "price": price,
            "commission": commission,
            "nav_at_fill": nav_at_fill,
            "disparity_at_fill": (
                round(disparity_at_fill, 4)
                if disparity_at_fill is not None
                else None
            ),
            "reason": reason,
        }
        if extra:
            record.update(extra)
        self.trades_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False)
        fd = os.open(
            self.trades_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644
        )
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
        finally:
            os.close(fd)

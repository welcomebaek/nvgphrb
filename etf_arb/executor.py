"""Order-execution interface shared by the sim and (Phase 4) live executors.

The runner talks only to this Protocol, so switching sim -> live later is a
construction-time decision, never a call-site change. Phase 3 ships only the
sim implementation (executor_sim.SimExecutor); no live order code exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from etf_arb.market_state import TickerState


@dataclass(frozen=True)
class Fill:
    side: str        # "buy" | "sell"
    code: str
    qty: int         # filled quantity - may be < requested (partial fill)
    price: int       # per-share fill price (won)
    commission: int  # won, floored
    ts: float        # epoch seconds


class Executor(Protocol):
    def place_order(
        self,
        side: str,
        code: str,
        qty: int,
        limit_price: int,
        snapshot: TickerState,
        reason: str = "",
    ) -> Fill | None:
        """Place a limit order sized qty at limit_price. Returns the fill
        (possibly partial) or None when nothing filled. `reason` is carried
        into the trade log ("entry" / "exit" / "force_exit")."""
        ...

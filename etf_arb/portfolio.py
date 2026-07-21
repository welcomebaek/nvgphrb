"""Portfolio state (positions + cash) with atomic JSON persistence.

State file: state/portfolio_sim.json, written via temp file + os.replace so a
crash mid-write can never corrupt it. Schema:

  {
    "cash": int,                        # won
    "positions": {code: {qty, avg_price, entry_ts, entry_date,
                         entry_disparity_pct, deadline_date,
                         buy_commission}},
    "realized_pnl": int,                # cumulative, commissions included
    "entries_today": {"date": "YYYY-MM-DD", "count": int},
    "cooldowns": {code: last_exit_ts},  # epoch seconds
    "last_updated": iso timestamp
  }

Restart-safe: load() only parses - it never recomputes or discards anything.
All money amounts are integer won. P&L bookkeeping keeps the invariant
`cash - initial_cash == realized_pnl` whenever no positions are open, because
the buy commission is carried on the position (buy_commission) and allocated
into realized P&L proportionally as the position is sold off.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

from etf_arb.signals import PortfolioView, Position

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATE_PATH = PROJECT_ROOT / "state" / "portfolio_sim.json"


class PortfolioError(Exception):
    pass


class Portfolio:
    def __init__(
        self,
        path: Path,
        cash: int,
        realized_pnl: int = 0,
        positions: dict[str, Position] | None = None,
        entries_date: str = "",
        entries_count: int = 0,
        cooldowns: dict[str, float] | None = None,
    ):
        self.path = Path(path)
        self.cash = int(cash)
        self.realized_pnl = int(realized_pnl)
        self.positions: dict[str, Position] = positions or {}
        self._entries_date = entries_date
        self._entries_count = int(entries_count)
        self.cooldowns: dict[str, float] = cooldowns or {}

    # -- persistence --------------------------------------------------------

    @classmethod
    def load(
        cls, path: Path = DEFAULT_STATE_PATH, initial_cash: int = 0
    ) -> "Portfolio":
        """Load from disk; a missing file means a fresh portfolio with
        initial_cash. A corrupt file raises (never silently resets money)."""
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls(path=path, cash=int(initial_cash))
        except json.JSONDecodeError as e:
            raise PortfolioError(
                f"포트폴리오 상태 파일 파싱 실패({path}): {e}"
            ) from None

        try:
            positions: dict[str, Position] = {}
            for code, p in (raw.get("positions") or {}).items():
                positions[str(code)] = Position(
                    code=str(code),
                    qty=int(p["qty"]),
                    avg_price=float(p["avg_price"]),
                    entry_ts=float(p["entry_ts"]),
                    entry_date=date.fromisoformat(p["entry_date"]),
                    entry_disparity_pct=float(p["entry_disparity_pct"]),
                    deadline_date=date.fromisoformat(p["deadline_date"]),
                    buy_commission=int(p.get("buy_commission", 0)),
                )
            et = raw.get("entries_today") or {}
            return cls(
                path=path,
                cash=int(raw["cash"]),
                realized_pnl=int(raw.get("realized_pnl", 0)),
                positions=positions,
                entries_date=str(et.get("date", "")),
                entries_count=int(et.get("count", 0)),
                cooldowns={
                    str(k): float(v)
                    for k, v in (raw.get("cooldowns") or {}).items()
                },
            )
        except (KeyError, TypeError, ValueError) as e:
            raise PortfolioError(
                f"포트폴리오 상태 필드 오류({path}): {e!r}"
            ) from None

    def save(self) -> None:
        """Atomic write: temp file in the same directory + os.replace."""
        payload = {
            "cash": self.cash,
            "positions": {
                code: {
                    "qty": p.qty,
                    "avg_price": p.avg_price,
                    "entry_ts": p.entry_ts,
                    "entry_date": p.entry_date.isoformat(),
                    "entry_disparity_pct": p.entry_disparity_pct,
                    "deadline_date": p.deadline_date.isoformat(),
                    "buy_commission": p.buy_commission,
                }
                for code, p in self.positions.items()
            },
            "realized_pnl": self.realized_pnl,
            "entries_today": {
                "date": self._entries_date,
                "count": self._entries_count,
            },
            "cooldowns": self.cooldowns,
            "last_updated": datetime.now().astimezone().isoformat(
                timespec="milliseconds"
            ),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # -- entries-per-day counter -------------------------------------------

    def entries_today(self, today: date) -> int:
        """Entry count for `today` (a stored count from another day reads 0)."""
        if self._entries_date != today.isoformat():
            return 0
        return self._entries_count

    def record_entry(self, today: date) -> None:
        key = today.isoformat()
        if self._entries_date != key:
            self._entries_date = key
            self._entries_count = 1
        else:
            self._entries_count += 1

    # -- views --------------------------------------------------------------

    def view(self, today: date) -> PortfolioView:
        return PortfolioView(
            cash=self.cash,
            open_codes=frozenset(self.positions.keys()),
            entries_today=self.entries_today(today),
            last_exit_ts=dict(self.cooldowns),
        )

    # -- fills (pure arithmetic; caller decides when to save()) -------------

    def apply_buy(
        self,
        code: str,
        qty: int,
        price: int,
        commission: int,
        ts: float,
        entry_date: date,
        entry_disparity_pct: float,
        deadline_date: date,
    ) -> Position:
        if qty <= 0:
            raise PortfolioError(f"매수 수량이 0 이하입니다: {qty}")
        if code in self.positions:
            raise PortfolioError(f"이미 보유 중인 종목입니다: {code}")
        # round() keeps cash integer-won even when price is a fractional VWAP
        # from a multi-level ladder fill; for integer prices this is a no-op.
        # round(qty*vwap) == round(total_cost) == total_cost exactly because
        # the true notional is an integer sum of qty_i*price_i.
        cost = round(qty * price) + commission
        if cost > self.cash:
            raise PortfolioError(
                f"현금 부족: 필요 {cost:,}원 > 보유 {self.cash:,}원 ({code})"
            )
        self.cash -= cost
        pos = Position(
            code=code,
            qty=qty,
            avg_price=float(price),
            entry_ts=ts,
            entry_date=entry_date,
            entry_disparity_pct=entry_disparity_pct,
            deadline_date=deadline_date,
            buy_commission=commission,
        )
        self.positions[code] = pos
        return pos

    def apply_sell(
        self, code: str, qty: int, price: int, commission: int, ts: float
    ) -> int:
        """Apply a (possibly partial) sell fill. Returns realized P&L (won)
        for this fill, with the buy commission allocated proportionally so
        that a full liquidation accounts for every won of commission."""
        pos = self.positions.get(code)
        if pos is None:
            raise PortfolioError(f"보유하지 않은 종목 매도: {code}")
        if qty <= 0 or qty > pos.qty:
            raise PortfolioError(
                f"매도 수량 오류: {qty} (보유 {pos.qty}) ({code})"
            )

        if qty == pos.qty:
            buy_comm_alloc = pos.buy_commission
        else:
            buy_comm_alloc = pos.buy_commission * qty // pos.qty

        proceeds = qty * price - commission
        realized = proceeds - round(qty * pos.avg_price) - buy_comm_alloc

        self.cash += proceeds
        self.realized_pnl += realized
        self.cooldowns[code] = ts

        if qty == pos.qty:
            del self.positions[code]
        else:
            self.positions[code] = replace(
                pos,
                qty=pos.qty - qty,
                buy_commission=pos.buy_commission - buy_comm_alloc,
            )
        return realized

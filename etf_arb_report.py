"""ETF NAV 괴리율 시뮬 성과 리포트 CLI.

logs/etf_arb_trades_sim.jsonl(체결)과 logs/etf_arb_journal.jsonl(저널)을 읽어
일별/누적 성과와 스킵사유 히스토그램을 출력한다. 실현손익은 포트폴리오 상태
파일을 신뢰하지 않고 체결 JSONL에서 FIFO 매칭으로 독립 재계산한다(대사 목적) -
마지막에 상태 파일의 realized_pnl과 비교해 차이를 보여준다.

실행: uv run etf_arb_report.py [--trades PATH] [--journal PATH] [--state PATH]
파일이 없거나 비어 있어도 정상 동작한다 (해당 섹션만 '없음' 표시).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_TRADES_PATH = PROJECT_ROOT / "logs" / "etf_arb_trades_sim.jsonl"
DEFAULT_JOURNAL_PATH = PROJECT_ROOT / "logs" / "etf_arb_journal.jsonl"
DEFAULT_STATE_PATH = PROJECT_ROOT / "state" / "portfolio_sim.json"


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Stream a JSONL file; missing file -> empty, bad lines are skipped."""
    try:
        f = path.open("r", encoding="utf-8")
    except FileNotFoundError:
        return
    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield obj


def _epoch(trade: dict[str, Any]) -> float:
    if isinstance(trade.get("epoch_ts"), (int, float)):
        return float(trade["epoch_ts"])
    try:
        return datetime.fromisoformat(str(trade.get("ts", ""))).timestamp()
    except ValueError:
        return 0.0


def _day(trade: dict[str, Any]) -> str:
    return str(trade.get("ts", ""))[:10] or "????-??-??"


# ---------------------------------------------------------------- trades

def analyze_trades(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """FIFO(종목당 단일 포지션) 매칭으로 왕복/손익을 체결에서 재계산한다."""
    trades = sorted(trades, key=_epoch)

    per_day: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"buys": 0, "sells": 0, "realized": 0.0, "force_exits": 0}
    )
    open_lots: dict[str, dict[str, Any]] = {}  # code -> aggregated open trip
    trips: list[dict[str, Any]] = []
    unmatched_sells = 0

    for t in trades:
        code = str(t.get("code", ""))
        side = str(t.get("side", ""))
        qty = int(t.get("qty", 0))
        price = int(t.get("price", 0))
        comm = int(t.get("commission", 0))
        disp = t.get("disparity_at_fill")
        day = _day(t)

        if side == "buy":
            per_day[day]["buys"] += 1
            lot = open_lots.setdefault(
                code,
                {
                    "qty": 0, "cost": 0, "comm_buy": 0, "sold": 0,
                    "proceeds": 0, "comm_sell": 0,
                    "buy_disp_w": 0.0, "buy_disp_q": 0,
                    "sell_disp_w": 0.0, "sell_disp_q": 0,
                    "entry_ts": _epoch(t), "exit_ts": None, "force": False,
                    "realized": 0.0,
                },
            )
            lot["qty"] += qty
            lot["cost"] += qty * price
            lot["comm_buy"] += comm
            if isinstance(disp, (int, float)):
                lot["buy_disp_w"] += float(disp) * qty
                lot["buy_disp_q"] += qty
        elif side == "sell":
            per_day[day]["sells"] += 1
            if str(t.get("reason", "")) == "force_exit":
                per_day[day]["force_exits"] += 1
            lot = open_lots.get(code)
            if lot is None or lot["qty"] - lot["sold"] < qty:
                unmatched_sells += 1
                continue
            avg_cost = lot["cost"] / lot["qty"]
            comm_buy_alloc = lot["comm_buy"] * qty / lot["qty"]
            realized = qty * price - comm - qty * avg_cost - comm_buy_alloc
            per_day[day]["realized"] += realized
            lot["realized"] += realized
            lot["sold"] += qty
            lot["proceeds"] += qty * price
            lot["comm_sell"] += comm
            lot["exit_ts"] = _epoch(t)
            lot["force"] = lot["force"] or (
                str(t.get("reason", "")) == "force_exit"
            )
            if isinstance(disp, (int, float)):
                lot["sell_disp_w"] += float(disp) * qty
                lot["sell_disp_q"] += qty
            if lot["sold"] >= lot["qty"]:  # 포지션 완전 청산 = 왕복 1회
                trips.append({"code": code, **lot})
                del open_lots[code]

    wins = sum(1 for tr in trips if tr["realized"] > 0)
    holding_s = [
        tr["exit_ts"] - tr["entry_ts"]
        for tr in trips
        if tr["exit_ts"] is not None
    ]
    captured = []
    for tr in trips:
        if tr["buy_disp_q"] > 0 and tr["sell_disp_q"] > 0:
            captured.append(
                tr["sell_disp_w"] / tr["sell_disp_q"]
                - tr["buy_disp_w"] / tr["buy_disp_q"]
            )

    return {
        "n_trades": len(trades),
        "per_day": dict(per_day),
        "trips": trips,
        "n_trips": len(trips),
        "wins": wins,
        "avg_holding_s": (sum(holding_s) / len(holding_s)) if holding_s else None,
        "avg_captured_pct": (sum(captured) / len(captured)) if captured else None,
        "total_realized": sum(tr["realized"] for tr in trips)
        + sum(
            lot["realized"] for lot in open_lots.values()
        ),  # 부분 청산분 포함
        "force_exit_trips": sum(1 for tr in trips if tr["force"]),
        "unmatched_sells": unmatched_sells,
        "still_open_lots": len(open_lots),
    }


# ---------------------------------------------------------------- journal

def analyze_journal(path: Path) -> dict[str, Any]:
    skip_total: Counter[str] = Counter()
    skip_per_day: dict[str, Counter[str]] = defaultdict(Counter)
    signals = Counter()
    disaster_alerts = 0
    for rec in iter_jsonl(path):
        ev = rec.get("event")
        day = str(rec.get("ts", ""))[:10]
        if ev in ("entry_skip", "exit_skip"):
            reason = str(rec.get("reason", "?"))
            skip_total[reason] += 1
            skip_per_day[day][reason] += 1
        elif ev in ("entry_signal", "exit_signal", "entry_fill", "exit_fill",
                    "entry_no_fill", "exit_no_fill"):
            signals[ev] += 1
        elif ev == "disaster_alert":
            disaster_alerts += 1
    return {
        "skip_total": skip_total,
        "skip_per_day": dict(skip_per_day),
        "signals": signals,
        "disaster_alerts": disaster_alerts,
    }


# ---------------------------------------------------------------- output

def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 3600:
        return f"{seconds / 60:.1f}분"
    return f"{seconds / 3600:.1f}시간"


def main() -> int:
    parser = argparse.ArgumentParser(description="ETF 괴리율 시뮬 성과 리포트")
    parser.add_argument("--trades", type=Path, default=DEFAULT_TRADES_PATH)
    parser.add_argument("--journal", type=Path, default=DEFAULT_JOURNAL_PATH)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH)
    args = parser.parse_args()

    trades = list(iter_jsonl(args.trades))
    ta = analyze_trades(trades)
    ja = analyze_journal(args.journal)

    print("=" * 64)
    print("ETF NAV 괴리율 시뮬 리포트")
    print("=" * 64)

    # -- 일별 ---------------------------------------------------------------
    print("\n[일별 체결]")
    if not ta["per_day"]:
        print("  체결 없음")
    else:
        for day in sorted(ta["per_day"]):
            d = ta["per_day"][day]
            print(
                f"  {day}: 매수 {d['buys']}건 / 매도 {d['sells']}건 "
                f"(강제청산 {d['force_exits']}건), "
                f"실현손익 {d['realized']:+,.0f}원"
            )

    # -- 누적 ---------------------------------------------------------------
    print("\n[누적 성과]")
    print(f"  체결 건수      : {ta['n_trades']}건")
    print(f"  왕복(완전청산) : {ta['n_trips']}회")
    if ta["n_trips"] > 0:
        print(
            f"  승률           : {ta['wins']}/{ta['n_trips']} "
            f"({ta['wins'] / ta['n_trips'] * 100:.0f}%)"
        )
        print(f"  평균 보유시간  : {_fmt_duration(ta['avg_holding_s'])}")
        if ta["avg_captured_pct"] is not None:
            print(f"  평균 포획괴리  : {ta['avg_captured_pct']:+.3f}%p")
        print(f"  강제청산 왕복  : {ta['force_exit_trips']}회")
    print(f"  실현손익(재계산): {ta['total_realized']:+,.0f}원")
    if ta["unmatched_sells"]:
        print(f"  경고: 매칭 안 된 매도 {ta['unmatched_sells']}건 (데이터 이상)")

    # -- 시그널/재난 ---------------------------------------------------------
    if ja["signals"] or ja["disaster_alerts"]:
        print("\n[시그널 카운트 (저널)]")
        for ev in sorted(ja["signals"]):
            print(f"  {ev:<15}: {ja['signals'][ev]}건")
        if ja["disaster_alerts"]:
            print(f"  disaster_alert : {ja['disaster_alerts']}건")

    # -- 스킵 히스토그램 -----------------------------------------------------
    print("\n[스킵사유 히스토그램 (누적)]")
    if not ja["skip_total"]:
        print("  스킵 기록 없음")
    else:
        total = sum(ja["skip_total"].values())
        for reason, n in ja["skip_total"].most_common():
            print(f"  {reason:<26}: {n:>8}회 ({n / total * 100:5.1f}%)")
        days = sorted(ja["skip_per_day"])
        if len(days) > 1:
            print("\n[스킵사유 상위 3 (일별)]")
            for day in days:
                top = ja["skip_per_day"][day].most_common(3)
                items = ", ".join(f"{r} {n}" for r, n in top)
                print(f"  {day}: {items}")

    # -- 보유 포지션 (상태 파일) ---------------------------------------------
    print("\n[보유 포지션 (state/portfolio_sim.json)]")
    state_realized: int | None = None
    try:
        state = json.loads(args.state.read_text(encoding="utf-8"))
        positions = state.get("positions") or {}
        state_realized = state.get("realized_pnl")
        if not positions:
            print("  없음")
        else:
            for code, p in positions.items():
                print(
                    f"  {code}: {p.get('qty')}주 @ {p.get('avg_price'):,.0f}원, "
                    f"진입 {p.get('entry_date')} "
                    f"(괴리 {p.get('entry_disparity_pct'):+.3f}%), "
                    f"강제청산 기한 {p.get('deadline_date')}"
                )
        print(
            f"  현금 {state.get('cash'):,}원 / "
            f"상태파일 실현손익 {state.get('realized_pnl'):+,}원 "
            f"(갱신 {state.get('last_updated')})"
        )
    except FileNotFoundError:
        print("  상태 파일 없음 (아직 시뮬 매매 세션이 실행되지 않음)")
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        print(f"  상태 파일 읽기 실패: {e}")

    # -- 대사 ---------------------------------------------------------------
    if state_realized is not None and (ta["n_trips"] > 0 or ta["n_trades"] > 0):
        diff = ta["total_realized"] - float(state_realized)
        verdict = "일치" if abs(diff) < 1.0 else f"차이 {diff:+,.0f}원"
        print(
            f"\n[대사] 체결 재계산 {ta['total_realized']:+,.0f}원 vs "
            f"상태파일 {state_realized:+,}원 -> {verdict}"
        )

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())

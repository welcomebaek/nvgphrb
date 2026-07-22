"""Orchestrator: one websocket session per trading day.

Startup sequence: load config + watchlist -> trading-day check via
TradingCalendar -> REST access token (shared cache) -> websocket approval key
-> connect + subscribe (NAV + 호가 x 20 tickers). The asyncio main loop
consumes websocket events into MarketState and journals a per-ticker
disparity summary every --summary-interval seconds. Exits cleanly at market
close (15:30 KST + grace) or on SIGINT/SIGTERM, writing an EOD summary line.

Phase 3: unless --observe-only is passed, a SimTradeEngine is attached to the
`Runner.on_snapshot` hook - every snapshot update is evaluated for exits (on
held tickers) or entries (with debounce), fills are simulated conservatively
via SimExecutor, and every signal AND every skip is journaled with a reason
code. A periodic force-exit sweep runs in the summary loop so positions can
force-exit even when their own ticker goes quiet. Only execution.mode="sim"
is supported here - no live order code exists in this module.
"""

from __future__ import annotations

import asyncio
import json
import signal
import sys
import time
from collections import Counter
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Any, Callable

from kis_common import KisApiError, get_access_token, load_credentials

from etf_arb import journal, paths
from etf_arb.calendar import CalendarError, TradingCalendar
from etf_arb.config import Config, ConfigError, load_config
from etf_arb.executor_sim import SimExecutor
from etf_arb.market_state import MarketState, TickerState
from etf_arb.portfolio import Portfolio, PortfolioError
from etf_arb.signals import (
    ConditionTracker,
    NoAction,
    check_disaster,
    evaluate_entry,
    evaluate_exit,
)
from etf_arb.ws_client import KisWsClient, get_approval_key

WATCHLIST_PATH = paths.WATCHLIST_PATH
TOKEN_CACHE_PATH = paths.TOKEN_CACHE_PATH

MARKET_CLOSE = dtime(15, 30, 30)  # 장 마감(15:30) + 30초 유예
DEFAULT_SUMMARY_INTERVAL_SECONDS = 10.0


class RunnerError(Exception):
    pass


def load_watchlist(path: Path = WATCHLIST_PATH) -> list[dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        tickers = raw["tickers"]
    except (FileNotFoundError, json.JSONDecodeError, KeyError) as e:
        raise RunnerError(f"워치리스트 로드 실패 ({path}): {e!r}") from None
    if not tickers:
        raise RunnerError("워치리스트가 비어 있습니다")
    return tickers


def find_orphan_risk(held_codes: set[str], watchlist_codes: set[str]) -> set[str]:
    """보유 중인데 오늘 워치리스트에는 없는 종목코드 집합 (순수함수, 유닛테스트 대상).

    이런 종목은 웹소켓 구독 대상이 아니게 되어 청산 시그널을 영영 못 받는
    "오펀 포지션" 위험이 있다 - Phase 2.5 워치리스트 리프레셔의 보유종목
    히스테리시스가 정상 작동한다면 항상 빈 집합이어야 한다."""
    return held_codes - watchlist_codes


class SimTradeEngine:
    """Phase 3 trading brain: snapshot updates -> pure signals -> sim fills.

    Owns the persistent Portfolio, the entry debounce tracker and the sim
    executor. All decision logic lives in etf_arb.signals (pure); this class
    only feeds it inputs, executes decisions and journals everything -
    every signal and every skip, each with a distinct reason code.
    """

    DISASTER_REALERT_SECONDS = 300.0  # per-code journal alert rate limit

    def __init__(self, config: Config, cal: TradingCalendar, market: MarketState):
        self.config = config
        self.market = market
        self.portfolio = Portfolio.load(
            initial_cash=config.risk.virtual_capital_krw
        )
        self.tracker = ConditionTracker()
        self.executor = SimExecutor(
            self.portfolio,
            commission_rate_pct=config.fees.commission_rate_pct,
            deadline_fn=lambda d: cal.add_trading_days(
                d, config.signals.force_exit_days
            ),
        )
        self.skip_counts: Counter[str] = Counter()
        self._disaster_last: dict[str, float] = {}
        # Persist immediately so the state file exists from session start and
        # a restart mid-session always has something to load.
        self.portfolio.save()
        journal.append(
            "portfolio_loaded",
            {
                "cash": self.portfolio.cash,
                "realized_pnl": self.portfolio.realized_pnl,
                "entries_today": self.portfolio.entries_today(date.today()),
                "positions": {
                    c: {
                        "qty": p.qty,
                        "avg_price": p.avg_price,
                        "entry_date": p.entry_date.isoformat(),
                        "deadline_date": p.deadline_date.isoformat(),
                    }
                    for c, p in self.portfolio.positions.items()
                },
            },
        )

    # -- snapshot hook ------------------------------------------------------

    def handle_snapshot(self, code: str, st: TickerState) -> None:
        now = datetime.now()
        if code in self.portfolio.positions:
            self._evaluate_exit(code, st, now)
        else:
            self._evaluate_entry(code, st, now)

    # -- entry path ---------------------------------------------------------

    def _evaluate_entry(self, code: str, st: TickerState, now: datetime) -> None:
        view = self.portfolio.view(now.date())
        decision = evaluate_entry(st, self.config, now, view, self.tracker)
        if isinstance(decision, NoAction):
            self.skip_counts[decision.reason_code] += 1
            journal.append(
                "entry_skip", {"code": code, "reason": decision.reason_code}
            )
            return

        journal.append(
            "entry_signal",
            {
                "code": code,
                "qty": decision.qty,
                "limit_price": decision.limit_price,
                "disp_pct": round(decision.disparity_pct, 4),
                "eff_vwap": round(decision.effective_vwap, 2),
                "eff_disp_pct": round(decision.effective_disparity_pct, 4),
            },
        )
        fill = self.executor.place_order(
            "buy", code, decision.qty, decision.limit_price, st,
            reason=decision.reason,
        )
        if fill is None:
            self.skip_counts["entry_no_fill"] += 1
            journal.append("entry_no_fill", {"code": code})
            return
        pos = self.portfolio.positions.get(code)
        journal.append(
            "entry_fill",
            {
                "code": code,
                "qty": fill.qty,
                "qty_requested": decision.qty,
                "price": fill.price,
                "commission": fill.commission,
                "cash": self.portfolio.cash,
                "deadline_date": (
                    pos.deadline_date.isoformat() if pos else None
                ),
            },
        )
        print(
            f"[etf_arb] 진입 체결(시뮬): {code} {fill.qty}주 @ {fill.price:,}원 "
            f"(괴리 {decision.disparity_pct:.3f}%)"
        )

    # -- exit path ----------------------------------------------------------

    def _evaluate_exit(self, code: str, st: TickerState, now: datetime) -> None:
        pos = self.portfolio.positions[code]

        disaster = check_disaster(st, self.config)
        if disaster is not None:
            last = self._disaster_last.get(code, 0.0)
            if now.timestamp() - last >= self.DISASTER_REALERT_SECONDS:
                self._disaster_last[code] = now.timestamp()
                journal.append(
                    "disaster_alert",
                    {
                        "code": code,
                        "entry_disp_pct": round(disaster, 4),
                        "threshold_pct": self.config.signals.disaster_alert_pct,
                    },
                )
                print(
                    f"[etf_arb] 재난 경보(저널 전용): {code} "
                    f"괴리 {disaster:.2f}% <= -{self.config.signals.disaster_alert_pct}%"
                )

        decision = evaluate_exit(pos, st, self.config, now)
        if isinstance(decision, NoAction):
            self.skip_counts[decision.reason_code] += 1
            journal.append(
                "exit_skip", {"code": code, "reason": decision.reason_code}
            )
            return

        journal.append(
            "exit_signal",
            {
                "code": code,
                "qty": decision.qty,
                "limit_price": decision.limit_price,
                "reason": decision.reason,
                "disp_pct": (
                    round(decision.disparity_pct, 4)
                    if decision.disparity_pct is not None
                    else None
                ),
            },
        )
        fill = self.executor.place_order(
            "sell", code, decision.qty, decision.limit_price, st,
            reason=decision.reason,
        )
        if fill is None:
            self.skip_counts["exit_no_fill"] += 1
            journal.append("exit_no_fill", {"code": code, "reason": decision.reason})
            return
        remaining = (
            self.portfolio.positions[code].qty
            if code in self.portfolio.positions
            else 0
        )
        journal.append(
            "exit_fill",
            {
                "code": code,
                "qty": fill.qty,
                "qty_requested": decision.qty,
                "price": fill.price,
                "commission": fill.commission,
                "reason": decision.reason,
                "remaining_qty": remaining,
                "cash": self.portfolio.cash,
                "realized_pnl": self.portfolio.realized_pnl,
            },
        )
        print(
            f"[etf_arb] 청산 체결(시뮬/{decision.reason}): {code} "
            f"{fill.qty}주 @ {fill.price:,}원 (잔여 {remaining}주)"
        )

    # -- periodic -----------------------------------------------------------

    def force_exit_sweep(self) -> None:
        """Safety net run from the periodic loop: positions get their exit
        (incl. force-exit past deadline) evaluated even when their own ticker
        stopped producing websocket events."""
        now = datetime.now()
        for code in list(self.portfolio.positions):
            self._evaluate_exit(code, self.market.get(code), now)

    def eod(self) -> None:
        self.portfolio.save()
        journal.append("skip_histogram", {"counts": dict(self.skip_counts)})
        journal.append(
            "portfolio_eod",
            {
                "cash": self.portfolio.cash,
                "realized_pnl": self.portfolio.realized_pnl,
                "open_positions": {
                    c: {
                        "qty": p.qty,
                        "avg_price": p.avg_price,
                        "deadline_date": p.deadline_date.isoformat(),
                    }
                    for c, p in self.portfolio.positions.items()
                },
            },
        )


class Runner:
    def __init__(
        self,
        config: Config,
        tickers: list[dict[str, Any]],
        summary_interval: float = DEFAULT_SUMMARY_INTERVAL_SECONDS,
        max_runtime_seconds: float | None = None,
        observe_only: bool = True,
        cal: TradingCalendar | None = None,
    ):
        self.config = config
        self.tickers = tickers
        self.codes = [str(t["code"]) for t in tickers]
        self.names = {str(t["code"]): str(t.get("name", "")) for t in tickers}
        self.summary_interval = summary_interval
        self.max_runtime_seconds = max_runtime_seconds
        self.observe_only = observe_only
        self.cal = cal

        self.state = MarketState()
        self.client: KisWsClient | None = None
        # Called as on_snapshot(code, ticker_state) after every state update.
        self.on_snapshot: Callable[[str, TickerState], None] | None = None
        self.engine: SimTradeEngine | None = None
        if not observe_only:
            if cal is None:
                raise RunnerError("시뮬 매매 모드에는 TradingCalendar가 필요합니다")
            self.engine = SimTradeEngine(config, cal, self.state)
            self.on_snapshot = self.engine.handle_snapshot

        self._stop_event = asyncio.Event()
        self._stop_reason = ""
        self._started_at = 0.0

    # -- lifecycle ----------------------------------------------------------

    def request_stop(self, reason: str) -> None:
        if not self._stop_event.is_set():
            self._stop_reason = reason
            self._stop_event.set()

    async def run(self) -> None:
        self._started_at = time.time()
        journal.append(
            "startup",
            {
                "mode": "observe-only" if self.observe_only else "sim",
                "codes": self.codes,
                "summary_interval_s": self.summary_interval,
                "nav_max_age_s": self.config.signals.nav_max_age_seconds,
                "quote_max_age_s": self.config.signals.quote_max_age_seconds,
            },
        )
        mode_label = "관찰 모드" if self.observe_only else "시뮬 매매 모드"
        print(
            f"[etf_arb] {mode_label} 시작: {len(self.codes)}종목, "
            f"요약 주기 {self.summary_interval:.0f}초 (마감 {MARKET_CLOSE} 자동 종료)"
        )
        if self.engine is not None:
            p = self.engine.portfolio
            print(
                f"[etf_arb] 포트폴리오: 현금 {p.cash:,}원, "
                f"보유 {len(p.positions)}종목, 누적손익 {p.realized_pnl:,}원"
            )

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(
                sig, self.request_stop, f"시그널 수신({sig.name})"
            )

        tasks = [
            asyncio.create_task(self._consume(), name="consume"),
            asyncio.create_task(self._summary_loop(), name="summary"),
            asyncio.create_task(self._clock_guard(), name="clock"),
        ]
        try:
            await self._stop_event.wait()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(sig)
            self._journal_eod()
        print(f"[etf_arb] 종료: {self._stop_reason}")

    # -- tasks --------------------------------------------------------------

    async def _consume(self) -> None:
        assert self.client is not None, "run_session()이 client를 설정해야 합니다"
        try:
            async for ev in self.client.events():
                if ev["type"] == "nav":
                    st = self.state.update_nav(ev["code"], ev["nav"], ev["ts"])
                elif ev["type"] == "quote":
                    st = self.state.update_quote(
                        ev["code"],
                        ev["ask1"],
                        ev["ask1_qty"],
                        ev["bid1"],
                        ev["bid1_qty"],
                        ev["hour_cls_code"],
                        ev["ts"],
                        ask_ladder=ev.get("ask_ladder"),
                        bid_ladder=ev.get("bid_ladder"),
                    )
                else:
                    continue
                if self.on_snapshot is not None:
                    self.on_snapshot(ev["code"], st)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - fatal: stop the session
            journal.append(
                "runner_error", {"where": "consume", "error": f"{type(e).__name__}: {e}"}
            )
            self.request_stop(f"수신 루프 오류: {type(e).__name__}")

    async def _summary_loop(self) -> None:
        s = self.config.signals
        while True:
            await asyncio.sleep(self.summary_interval)
            if self.engine is not None:
                # 강제청산 스윕: 조용해진 종목의 포지션도 기한 청산을 놓치지 않게.
                self.engine.force_exit_sweep()
            now = time.time()
            fresh_both = 0
            for code in self.codes:
                st = self.state.get(code)
                nav_stale = st.nav_stale(now, s.nav_max_age_seconds)
                quote_stale = st.quote_stale(now, s.quote_max_age_seconds)
                if not nav_stale and not quote_stale:
                    fresh_both += 1
                entry = st.entry_disparity_pct()
                exit_ = st.exit_disparity_pct()
                journal.append(
                    "disparity",
                    {
                        "code": code,
                        "nav": st.nav,
                        "ask1": st.ask1,
                        "bid1": st.bid1,
                        "ask1_qty": st.ask1_qty,
                        "bid1_qty": st.bid1_qty,
                        "entry_disp_pct": round(entry, 4) if entry is not None else None,
                        "exit_disp_pct": round(exit_, 4) if exit_ is not None else None,
                        "nav_age_s": round(st.nav_age(now), 1) if st.nav_age(now) is not None else None,
                        "quote_age_s": round(st.quote_age(now), 1) if st.quote_age(now) is not None else None,
                        "nav_stale": nav_stale,
                        "quote_stale": quote_stale,
                        "hour_cls_code": st.hour_cls_code,
                    },
                )
            total_nav = sum(self.state.get(c).nav_frames for c in self.codes)
            total_quote = sum(self.state.get(c).quote_frames for c in self.codes)
            engine_note = ""
            if self.engine is not None:
                engine_note = (
                    f", 보유 {len(self.engine.portfolio.positions)}종목"
                    f"/현금 {self.engine.portfolio.cash:,}원"
                )
            print(
                f"[etf_arb] {datetime.now().strftime('%H:%M:%S')} "
                f"신선(양쪽) {fresh_both}/{len(self.codes)}종목, "
                f"누적 NAV {total_nav} / 호가 {total_quote} 프레임{engine_note}"
            )

    async def _clock_guard(self) -> None:
        while True:
            now_dt = datetime.now()
            if now_dt.time() >= MARKET_CLOSE:
                self.request_stop("장 마감(15:30) 도달")
                return
            if (
                self.max_runtime_seconds is not None
                and time.time() - self._started_at >= self.max_runtime_seconds
            ):
                self.request_stop(f"최대 실행시간 {self.max_runtime_seconds:.0f}초 도달")
                return
            await asyncio.sleep(1.0)

    # -- EOD ----------------------------------------------------------------

    def _journal_eod(self) -> None:
        now = time.time()
        per_ticker = {}
        no_nav: list[str] = []
        no_quote: list[str] = []
        for code in self.codes:
            st = self.state.get(code)
            per_ticker[code] = {
                "nav_frames": st.nav_frames,
                "quote_frames": st.quote_frames,
            }
            if st.nav_frames == 0:
                no_nav.append(code)
            if st.quote_frames == 0:
                no_quote.append(code)
        if self.engine is not None:
            self.engine.eod()
        journal.append(
            "eod_summary",
            {
                "reason": self._stop_reason,
                "runtime_s": round(now - self._started_at, 1),
                "reconnects": self.client.reconnect_count if self.client else 0,
                "tickers_total": len(self.codes),
                "tickers_no_nav": no_nav,
                "tickers_no_quote": no_quote,
                "frames": per_ticker,
            },
        )


async def run_session(
    summary_interval: float = DEFAULT_SUMMARY_INTERVAL_SECONDS,
    max_runtime_seconds: float | None = None,
    observe_only: bool = True,
) -> int:
    """Full startup sequence + main loop. Returns process exit code."""
    try:
        config = load_config()
        tickers = load_watchlist()
    except (ConfigError, RunnerError) as e:
        print(f"[etf_arb] 시작 실패: {e}", file=sys.stderr)
        return 1

    # 오펀 포지션 안전점검: 보유종목이 오늘 워치리스트에 다 있는지 조기 확인.
    # (Phase 2.5 워치리스트 리프레셔의 보유종목 히스테리시스가 정상이라면
    # 항상 통과해야 하지만, 수동 워치리스트 편집 등 어떤 경로로든 어긋나면
    # 청산 시그널을 영영 못 받는 오펀이 생기므로 여기서 fail fast한다.)
    watchlist_codes = {str(t["code"]) for t in tickers}
    try:
        held = Portfolio.load(initial_cash=config.risk.virtual_capital_krw)
    except PortfolioError as e:
        print(f"[etf_arb] 포트폴리오 로드 실패(오펀 점검): {e}", file=sys.stderr)
        return 1
    missing = find_orphan_risk(set(held.positions), watchlist_codes)
    if missing:
        print(
            f"[etf_arb] 오펀 위험 감지: 보유종목 {sorted(missing)}이(가) 오늘 워치리스트에 "
            "없습니다. 청산 시그널을 받을 수 없으므로 시작을 중단합니다 "
            "(etf_watchlist_refresh.py로 워치리스트를 갱신한 뒤 재실행하세요).",
            file=sys.stderr,
        )
        journal.append("held_position_orphan_risk", {"missing_codes": sorted(missing)})
        return 1

    # 이 러너에는 실주문 코드가 없다: 매매는 execution.mode="sim"만 지원.
    if not observe_only and config.execution.mode != "sim":
        print(
            f"[etf_arb] execution.mode={config.execution.mode!r}는 지원하지 않습니다 "
            "(Phase 3는 'sim' 전용, 실전 실행기는 별도 승인 후 Phase 4).",
            file=sys.stderr,
        )
        return 1

    try:
        creds = load_credentials(
            ["KIS_APP_KEY", "KIS_APP_SECRET", "KIS_URL_REST", "KIS_URL_WS"]
        )
    except KisApiError as e:
        print(f"[etf_arb] 자격증명 로드 실패: {e}", file=sys.stderr)
        return 1

    app_key = creds["KIS_APP_KEY"]
    app_secret = creds["KIS_APP_SECRET"]
    journal.register_secrets([app_key, app_secret])

    # 거래일 확인 (캐시 우선, 부족할 때만 KIS 1콜)
    try:
        token = get_access_token(
            app_key, app_secret, creds["KIS_URL_REST"], TOKEN_CACHE_PATH
        )
        journal.register_secrets([token])
        cal = TradingCalendar.load(
            access_token=token,
            app_key=app_key,
            app_secret=app_secret,
            base_url=creds["KIS_URL_REST"],
        )
        today = date.today()
        if not cal.is_trading_day(today):
            msg = f"오늘({today})은 휴장일입니다. 종료합니다."
            print(f"[etf_arb] {msg}")
            journal.append("skip_non_trading_day", {"date": str(today)})
            return 0
    except (KisApiError, CalendarError) as e:
        print(f"[etf_arb] 거래일 확인 실패: {e}", file=sys.stderr)
        return 1

    # 웹소켓 접속키는 여기서 선발급해 실패를 조기에 드러낸다 (클라이언트는 캐시 재사용).
    try:
        approval_key = get_approval_key(
            app_key, app_secret, creds["KIS_URL_REST"]
        )
        journal.register_secrets([approval_key])
    except KisApiError as e:
        print(f"[etf_arb] 웹소켓 접속키 발급 실패: {e}", file=sys.stderr)
        return 1

    try:
        runner = Runner(
            config,
            tickers,
            summary_interval=summary_interval,
            max_runtime_seconds=max_runtime_seconds,
            observe_only=observe_only,
            cal=cal,
        )
    except (RunnerError, PortfolioError) as e:
        print(f"[etf_arb] 러너 초기화 실패: {e}", file=sys.stderr)
        return 1
    runner.client = KisWsClient(
        ws_url=creds["KIS_URL_WS"],
        app_key=app_key,
        app_secret=app_secret,
        rest_base_url=creds["KIS_URL_REST"],
        codes=runner.codes,
        journal_fn=journal.append,
    )
    await runner.run()
    return 0

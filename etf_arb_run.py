"""ETF NAV 괴리율 시스템 실행 CLI (Phase 3: 시그널 + 시뮬 매매).

장중에 실행하면 워치리스트 20종목의 실시간 NAV(H0STNAV0)와 호가(H0STASP0)를
웹소켓으로 구독하고, 스냅샷마다 시그널을 평가해 가상 체결(시뮬)로 매매한다.
- 기본 모드: 시뮬 매매 (etf_arb_config.json의 execution.mode="sim")
- --observe-only: 매매 없이 관찰만 (Phase 2 동작)
실주문 코드는 없다. 체결은 logs/etf_arb_trades_sim.jsonl, 상태는
state/portfolio_sim.json, 저널은 logs/etf_arb_journal.jsonl 에 기록된다.

실행: uv run etf_arb_run.py [--observe-only] [--summary-interval 10]
종료: Ctrl-C(SIGINT)/SIGTERM 시 상태 저장 후 종료, 15:30 장 마감 시 자동 종료.
리포트: uv run etf_arb_report.py
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from etf_arb.runner import DEFAULT_SUMMARY_INTERVAL_SECONDS, run_session


def main() -> int:
    parser = argparse.ArgumentParser(
        description="ETF NAV 괴리율 러너 (기본: 시뮬 매매, --observe-only로 관찰 전용)"
    )
    parser.add_argument(
        "--observe-only",
        action="store_true",
        default=False,
        help="시그널/매매 없이 관찰만 한다 (기본: 시뮬 매매)",
    )
    parser.add_argument(
        "--summary-interval",
        type=float,
        default=DEFAULT_SUMMARY_INTERVAL_SECONDS,
        metavar="초",
        help="종목별 괴리율 요약 저널 주기 (기본 10초)",
    )
    parser.add_argument(
        "--max-runtime-seconds",
        type=float,
        default=None,
        metavar="초",
        help="지정 시 해당 시간 경과 후 자동 종료 (테스트용)",
    )
    args = parser.parse_args()

    try:
        return asyncio.run(
            run_session(
                summary_interval=args.summary_interval,
                max_runtime_seconds=args.max_runtime_seconds,
                observe_only=args.observe_only,
            )
        )
    except KeyboardInterrupt:
        # 시그널 핸들러가 등록되기 전(스타트업 중)의 Ctrl-C
        print("[etf_arb] 사용자 중단(스타트업 중)")
        return 130


if __name__ == "__main__":
    sys.exit(main())

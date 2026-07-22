"""데이터 저장 위치를 소스 트리에서 분리 (디렉토리 꼬임으로 인한 데이터 사고 방지).

로그/상태/캐시/생성물은 소스 위치와 무관한 고정 DATA_ROOT에 둔다.
DATA_ROOT는 환경변수 KIS_ARB_DATA_DIR로 재정의 가능, 기본값 ~/kis_arb_data.
소스 디렉토리를 옮기거나 이름을 바꿔도 쌓이는 데이터는 영향받지 않는다.
설정(etf_arb_config.json)과 .env는 소스와 함께 유지(버전관리/관례).
"""
from __future__ import annotations

import os
from pathlib import Path

_DEFAULT_ROOT = Path.home() / "kis_arb_data"
DATA_ROOT = Path(os.environ.get("KIS_ARB_DATA_DIR") or _DEFAULT_ROOT).expanduser()

LOGS_DIR = DATA_ROOT / "logs"
STATE_DIR = DATA_ROOT / "state"
DATA_DIR = DATA_ROOT / "data"
KRX_CACHE_DIR = DATA_DIR / "krx_daily"
CACHE_DIR = DATA_ROOT / "cache"

JOURNAL_PATH = LOGS_DIR / "etf_arb_journal.jsonl"
TRADES_PATH = LOGS_DIR / "etf_arb_trades_sim.jsonl"
INTRADAY_SAMPLES_PATH = LOGS_DIR / "intraday_samples.jsonl"
PORTFOLIO_PATH = STATE_DIR / "portfolio_sim.json"
HOLIDAY_CACHE_PATH = DATA_DIR / "holiday_cache.json"
WATCHLIST_PATH = DATA_ROOT / "etf_watchlist.json"
CANDIDATES_PATH = DATA_ROOT / "etf_candidates_ranked.json"
TOKEN_CACHE_PATH = CACHE_DIR / ".kis_token_cache.json"
PAPER_TOKEN_CACHE_PATH = CACHE_DIR / ".kis_paper_token_cache.json"
WS_APPROVAL_CACHE_PATH = CACHE_DIR / ".kis_ws_approval_cache.json"
ORDER_LOG_REAL_PATH = LOGS_DIR / "order_log_real.jsonl"
ORDER_LOG_PAPER_PATH = LOGS_DIR / "order_log.jsonl"

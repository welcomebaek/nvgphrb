# 운영 / 런북

이 문서는 매매 시스템을 **운영·모니터링·복구**하는 방법을 다룹니다.

---

## 일일 자동 스케줄 (macOS launchd)

매 거래일 자동 기동. 각 스크립트는 휴장일이면 스스로 종료(파일 변경 없음).

| 시각(KST) | launchd 레이블 | 스크립트 | 역할 |
|---|---|---|---|
| 08:15 | `com.welcomebaek.etf-watchlist-refresh` | `etf_watchlist_refresh.py` | 워치리스트 재생성 |
| 08:50 | `com.welcomebaek.etf-arb-daily-start` | `etf_arb_run.py` | 시뮬 매매 러너(→15:30 자동 종료) |
| 08:56 | `com.welcomebaek.etf-intraday-sampler` | `etf_intraday_sampler.py` | 장중 샘플러(→15:30 종료) |

- 순서 의도: 리프레셔(08:15)가 워치리스트/후보를 만든 뒤 러너(08:50)와 샘플러(08:56)가 그걸
  소비. 08:50에 러너를 미리 켜는 이유는 09:00 개장 전 동시호가 데이터를 관측하고 웹소켓을
  워밍업하기 위함(진입은 09:05부터).
- plist는 `~/Library/LaunchAgents/com.welcomebaek.etf-*.plist`. `RunAtLoad=false`(로드 시 즉시
  실행 안 함, 예약 시각에만).
- 확인: `launchctl list | grep etf`
- 수동 로드/해제:
  ```bash
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.welcomebaek.etf-<name>.plist
  launchctl bootout   gui/$(id -u)/com.welcomebaek.etf-<name>
  ```
- ⚠️ **Mac이 08:15~15:30 동안 깨어 있어야** 실행됨(잠자기/종료 시 조용히 누락). cron이 아니라
  launchd를 쓰는 이유는 macOS TCC가 crontab 쓰기를 막기 때문.

## 수동 실행

```bash
uv run etf_universe_select.py         # 콜드스타트 워치리스트 생성(수동, 장중 스프레드 검사 포함)
uv run etf_watchlist_refresh.py --dry-run   # 리프레셔 시뮬(파일 안 씀)
uv run etf_arb_run.py                 # 러너(시뮬 매매)
uv run etf_arb_run.py --observe-only  # 시그널만 관찰, 매매 안 함
uv run etf_intraday_sampler.py        # 샘플러(장중에만)
uv run etf_arb_report.py              # 손익/통계 리포트
```

## 로그 & 상태 파일 (`logs/`, `state/` — gitignore)

| 파일 | 내용 |
|---|---|
| `logs/etf_arb_journal.jsonl` | 러너 이벤트 저널(아래 이벤트 타입) |
| `logs/etf_arb_trades_sim.jsonl` | 가상 체결 내역(ts/side/qty/price/수수료/괴리/VWAP) |
| `logs/intraday_samples.jsonl` | 샘플러 수집(괴리율/스프레드) |
| `logs/etf_*_launchd_stdout.log` / `stderr.log` | 각 launchd 잡의 콘솔 출력 |
| `state/portfolio_sim.json` | 가상 포트폴리오(현금/보유/실현손익/진입카운트/쿨다운) |
| `data/krx_daily/*.json`, `data/holiday_cache.json` | KRX 일별/휴장일 캐시 |

### 주요 저널 이벤트 타입

`startup`, `ws_connected`, `ws_subscribed_all`, `disparity`(주기적 종목 스냅샷),
`entry_signal`/`entry_fill`, `exit_signal`/`exit_fill`, `entry_skip`/`exit_skip`(사유 코드 포함),
`eod_summary`(장 마감 요약), `portfolio_eod`, `skip_histogram`.

## 모니터링

- **손익/통계**: `uv run etf_arb_report.py` — 왕복 손익, 승률, 평균 보유기간, 강제청산 수,
  스킵사유 히스토그램(전략 튜닝의 핵심 도구).
- **오늘 진입/청산 빠른 확인**:
  ```bash
  grep -E '"entry_signal"|"exit_signal"' logs/etf_arb_journal.jsonl | grep $(date +%Y-%m-%d)
  cat state/portfolio_sim.json
  ```
- **주요 스킵 사유**: `disparity_above_threshold`(그냥 임계값 미달, 정상), `before_entry_window`,
  `not_regular_session`(동시호가 차단), `quote_stale`/`nav_stale`(신선도), `cooldown`,
  `max_positions`, `book_too_thin_effective`(호가 얇아 실효괴리 미달), `exit_disparity_below`.

## 복구 / 재시작

- 러너는 **하루 1프로세스**(마감 후 종료). 연속성은 전부 `state/portfolio_sim.json`에 원자적
  저장 — 장중 kill/재시작해도 포지션/기한 보존(체결마다 즉시 저장).
- SIGINT/SIGTERM 시 상태 저장 후 정상 종료.
- 웹소켓 끊김은 자동 재접속+재구독(수동 개입 불필요).

## ⚠️ 모의투자 계좌 미연동 상황

KIS 모의투자(paper) 계좌로 실제 주문/조회를 시도하면 다음 에러가 납니다:

```
msg_cd=90070000  모의투자 처리계좌의 ID와 사용자정보가 상이하여 처리 불가능 합니다.
```

- 원인: `.env`의 `KIS_PAPER_STOCK`(모의 계좌번호)이 `KIS_PAPER_APP_KEY`에 연결된 계좌와
  불일치. 모의투자 "대회 참가" 등 별도 활성화가 필요한 것으로 추정.
- `kis_paper_balance.py`로 재확인 가능(현재 위 에러 반환).
- **그래서 이 프로젝트의 매매 시스템은 KIS 모의투자가 아니라 로컬 가상 체결(시뮬)로 동작.**
  모의투자 재연동은 [roadmap.md](roadmap.md) 참조.

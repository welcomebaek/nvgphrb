# kis_MCP_trading

한국투자증권(KIS) Open API를 이용한 국내 주식/ETF 조회·거래 도구 모음과,
ETF NAV 괴리율 평균회귀 전략을 시뮬레이션하는 자동매매 시스템.

> ⚠️ 이 프로젝트의 매매 시스템은 현재 **로컬 시뮬레이션 모드**로만 동작합니다.
> 실제 주문이 나가는 경로는 명시적 이중 게이트(`execution.mode="live"` +
> `live_enabled=true`)로 잠겨 있으며 기본값은 꺼져 있습니다. 단독 스크립트
> `order_buy_005930_real.py`만 예외적으로 실전 주문이 가능하니 사용에 주의하세요.

## 📖 상세 문서 (`docs/`)

이 README는 빠른 시작을 위한 랜딩 페이지입니다. 원리·규칙·방법론의 상세는 아래를 참고하세요.

- [docs/README.md](docs/README.md) — 기본 원리 & 파일/모듈 구조
- [docs/trading.md](docs/trading.md) — 매수/매도 원칙, 사이징, 비용 모델, 기술(웹소켓/KIS API), **config 레퍼런스**
- [docs/watchlist.md](docs/watchlist.md) — 데이터 수집 & 워치리스트 선정 원칙
- [docs/operations.md](docs/operations.md) — 운영/런북(스케줄·로그·모니터링·복구)
- [docs/roadmap.md](docs/roadmap.md) — 향후 작업

## 프로젝트 규칙 (CLAUDE.md)

1. **패키지 관리는 `uv`로만** — `pip`/`poetry`/`conda` 직접 호출 금지.
2. **주가/투자 데이터는 KIS Open API로만** — Yahoo Finance 등 범용 API 금지.
   단, 국내 상장 ETF/ETN **전체 목록**처럼 KIS가 제공하지 않는 데이터에 한해
   한국거래소(KRX) 공식 Open API 사용을 허용.

## 요구사항

- Python 3.10+
- [uv](https://docs.astral.sh/uv/)
- KIS Developers 계정 및 발급 앱키/시크릿, KRX Open API 인증키

## 설정

```bash
uv sync                     # 의존성 설치 (.venv 생성)
cp .env.example .env        # 자격증명 템플릿 복사 후 실제 값 입력
```

`.env`에 필요한 키 목록은 `.env.example` 참고. `.env`와 토큰 캐시 파일들은
`.gitignore`로 커밋에서 제외됩니다.

## 단독 조회 스크립트

| 스크립트 | 설명 |
|---|---|
| `kis_price.py` | 삼성전자(005930) 현재가 조회 (참고 구현) |
| `kis_foreign_balance.py` | 실전 계좌 외화 예수금 조회 |
| `kis_paper_balance.py` | 모의투자 계좌 잔고 조회 |
| `kis_check_order_status.py` | 주문번호로 체결 여부 조회 |
| `krx_etf_list.py` | KRX Open API로 국내 상장 ETF 전체 목록 조회 |
| `order_buy_005930_real.py` | ⚠️ 실전 계좌 005930 1주 시장가 매수 (`--live` 필요) |

```bash
uv run kis_price.py
```

## ETF NAV 괴리율 자동매매 시스템 (`etf_arb/` 패키지)

장중 ETF의 실시간 NAV와 매도1호가를 관찰해, 매도호가가 NAV보다 일정 % 이상
저평가되면(음의 괴리) 매수하고 괴리가 해소되면 매도하는 평균회귀 전략.
현재 로컬 가상 체결로 시뮬레이션 중.

### 일일 파이프라인

| 스크립트 | 역할 |
|---|---|
| `etf_watchlist_refresh.py` | KRX 전종목 + 장중 누적 데이터로 재스코어링해 감시 20종목 선정. 보유 포지션은 워치리스트에 강제 고정(오펀 방지) |
| `etf_intraday_sampler.py` | 후보 ~100종목의 현재가/NAV/괴리율을 1분 주기 REST 폴링해 축적 |
| `etf_arb_run.py` | 웹소켓으로 감시 20종목 실시간 관찰 → 시그널 평가 → 가상 체결 |
| `etf_arb_report.py` | 체결/저널 기반 손익·통계 리포트 |
| `etf_universe_select.py` | 콜드스타트용 초기 워치리스트 생성 (수동) |

정기 실행은 macOS `launchd`로 매 거래일 08:15(리프레셔)/08:56(샘플러)/08:50(러너)에
자동 기동하며, 각 스크립트가 휴장일이면 스스로 종료합니다.

### `etf_arb/` 패키지 구성

- `config.py` — 설정 로드 + 교차검증 (진입 임계값이 왕복비용을 넘는지 등)
- `calendar.py` — KIS 휴장일조회 기반 거래일 캘린더
- `krx_history.py` / `intraday_history.py` — KRX 일별·장중 데이터 수집/파싱
- `universe.py` — 괴리 에피소드 통계, 스코어링, 기대괴리 분위수
- `ws_client.py` — KIS 웹소켓(NAV+10단계 호가) 클라이언트, 재접속
- `market_state.py` — 종목별 실시간 스냅샷 + 호가 사다리
- `signals.py` — 순수 시그널 엔진(진입/청산/강제청산, 깊이비례 사이징, 실효괴리 더블체크)
- `portfolio.py` / `executor_sim.py` — 가상 포트폴리오 영속화 + 시뮬 체결
- `runner.py` — 오케스트레이터

### 실행

```bash
uv run etf_universe_select.py    # 최초 1회: 워치리스트 생성
uv run etf_arb_run.py            # 시뮬 매매 러너 (장중)
uv run etf_arb_run.py --observe-only   # 시그널만 관찰, 매매 안 함
uv run etf_arb_report.py         # 결과 리포트
```

### 설정 (`etf_arb_config.json`)

진입/청산 임계값, 신선도 가드, 포지션 사이징(min/max 배분, 깊이 스케일),
강제청산 기한, 수수료 모델, 실행 모드 등을 한 파일에서 관리. 모든 값은
로드 시 교차검증됩니다.

## 테스트

```bash
uv run pytest
```

순수 함수(시그널 게이트, 디바운스, 사이징, 호가 사다리 VWAP, 휴장일 낀 기한
연산, 괴리 분위수 등)를 중심으로 커버.

## 데이터/생성 파일

`data/`, `state/`, `logs/`, `etf_watchlist.json`, `etf_candidates_ranked.json`은
런타임에 생성되며 `.gitignore`에서 제외됩니다. 새 환경에서는 위 실행 절차대로
워치리스트를 먼저 생성하세요.

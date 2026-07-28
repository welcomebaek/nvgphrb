# 매매 원칙 & 기술적 사항

이 문서는 ETF NAV 괴리율 전략의 **매수/매도 규칙**, **포지션 사이징**, **비용 모델**,
**기술 스택(웹소켓/KIS API)**, 그리고 **모든 설정값의 레퍼런스**를 다룹니다.

> **모든 기준값은 `etf_arb_config.json`에서 수정 가능합니다.** `etf_arb/config.py`가 이 파일을
> 로드하고 프로즌 데이터클래스로 검증합니다(잘못된 값이면 로드 시 에러). JSON은 주석을 달 수
> 없으므로 각 필드의 의미/근거는 이 문서 마지막의 [config 레퍼런스](#config-레퍼런스)를
> 참고하세요. 값을 바꾸면 다음 프로세스 기동 시 반영됩니다(실행 중 프로세스는 메모리에 이미
> 로드된 값을 사용).

관련 코드: `etf_arb/signals.py`(순수 시그널 엔진), `etf_arb/executor_sim.py`(시뮬 체결),
`etf_arb/ws_client.py`(웹소켓), `kis_common.py`(인증).

---

## 매수/매도 원칙

### 부호 관례 (호가 방향 구분)

- **진입 괴리** = `(매도1호가 − NAV) / NAV × 100` [%] — 매수는 ask를 지불하므로 ask 기준.
- **청산 괴리** = `(매수1호가 − NAV) / NAV × 100` [%] — 매도는 bid를 받으므로 bid 기준.

### 진입 (매수) 조건 — 모든 게이트를 순서대로 통과해야 함

0. **워치리스트 진입자격 게이트** (`evaluate_entry` 밖, 러너 레벨): 보유종목 히스테리시스로
   핀된 종목이 하드필터/스프레드 필터를 실제로 통과 못 했으면(`entry_eligible=false`) 신규
   진입 자체를 차단(스킵사유 `watchlist_entry_ineligible`) — 핀은 청산 신호 수신을
   보장하려는 것이지 신규 진입을 허가하는 게 아님. 상세: [watchlist.md](watchlist.md#핀--청산-보장이지-신규-진입-허가가-아님-entry_eligible).
1. **리스크 게이트**: 이미 보유 중 아님, 오늘 진입 횟수 < `max_entries_per_day`(6), 동시
   포지션 < `max_positions`(4), 해당 종목 쿨다운(청산 후 `cooldown_minutes`=60분) 아님.
2. **진입 시간창**: `no_entry_before`(09:05) ~ `no_entry_after`(15:00). 장 초반 LP 호가 안정
   전과 마감 동시호가를 피함.
3. **정규장 게이트**: `hour_cls_code == '0'`(정규장). 장전 동시호가는 `'B'`, 마감 동시호가는
   `'A'`로 오며 이때 호가 필드가 예상체결가 의미로 바뀌므로 거래 금지. (2026-07-22 실측: 마감
   동시호가에 청산괴리가 +1.18%까지 튀었으나 게이트가 정확히 차단.)
4. **신선도 가드**: 호가 틱 나이 ≤ `quote_max_age_seconds`(10초), NAV 틱 나이 ≤
   `nav_max_age_seconds`(30초). 오래된 데이터로 판단 금지. (장전엔 NAV가 null/stale로 와서
   이 가드가 자동으로 진입을 막음.)
5. **괴리 조건**: 진입 괴리 ≤ `−entry_threshold_pct`(−0.5%).
6. **디바운스**: 위 조건이 `entry_confirm_seconds`(3초) 이상 **연속**으로, 그리고 서로 다른
   호가 틱 2개 이상에서 관측돼야 함. 순간적 글리치로 진입하는 것 방지. 조건이 깨지면 리셋.
7. **사이징 + 호가 깊이 더블체크**: 아래 [포지션 사이징](#포지션-사이징) 참조.

### 청산 (매도) 조건 — 보유 포지션마다 매 틱 평가

- **정상 청산**: 청산 괴리 ≥ `−exit_threshold_pct`(−0.1%). NAV가 신선해야 함. 즉 매수1호가가
  거의 NAV까지 회복(괴리 해소)되면 전량 매도.
- **강제 청산**: `today ≥ 진입일 + force_exit_days`(5거래일) 이고 현재 시각 ≥
  `force_exit_time`(14:50)이면 괴리와 무관하게 bid 전량 매도. 신선도 가드 무시(무조건 청산).
  거래일 계산은 `etf_arb/calendar.py`(휴장일 반영).
- **재앙 경고**: 진입 괴리가 `−disaster_alert_pct`(−3%) 이하로 더 벌어지면 저널에 경고만
  기록(청산 안 함 — 평균회귀 가설 유지). 사후 리뷰용.

---

## 포지션 사이징

고정 배분도, 괴리 깊이에 비례한 배분도 아닙니다. **자본은 상한(ceiling)만 정하고, 실제
매수 수량은 그 상한 안에서 매도 호가창에 실제로 쌓인 물량이 결정**합니다. (구현:
`etf_arb/signals.py`)

> 이전 설계(깊이 비례 사이징, `depth_scale_span_pct`/`min_fill_ratio`)는 폐기되었습니다.
> 이유: 체크 주기가 비교적 고빈도라 괴리가 충분히 깊어지기 전에 이미 진입하게 되는 데다,
> "깊은 괴리 = 큰 사이즈"라는 가정 자체가 얇은 호가창에서는 실제 체결 가능량과 무관했습니다.
> 사이즈는 호가창의 진짜 유동성이 정하는 것이 맞다는 결론.

### 1) 자본 상한 (cap_qty)

```
capital_cap = min(max_alloc_per_position_krw, 남은 현금)
cap_qty     = capital_cap // ask1
```

`cap_qty`는 목표 수량이 아니라 **호가 사다리 워크의 상한**입니다. `max_alloc`(200만) ×
`max_positions`(4) = 800만 ≤ 가상자본 850만 — NAV 낡음 등으로 괴리가 비정상적으로 깊어
보이는 아티팩트에 대비한 자본 안전장치로만 작동합니다.

### 2) 호가 사다리 워크 = 사이징 그 자체

1호가만 믿지 않고, `cap_qty`를 상한으로 **호가창(1~10단계)을 걸어 올라가며 실제 가중평균
매수가(VWAP)**를 계산합니다. 이 워크가 "얼마나 살 수 있는가"와 "그래도 수익성이 있는가"를
동시에 결정합니다 — 수익성 있는 깊이(profitable depth)가 곧 사이즈입니다.

- 호가 사다리를 누적하며 각 단계에서 누적 VWAP과 **실효괴리 `(VWAP − NAV)/NAV`**를 계산.
- 실효괴리가 여전히 `−entry_threshold_pct`를 만족하는 **최대 수량 `qty_ok`**까지만 누적.
- `qty_ok`가 전혀 없으면(1호가부터 수량 0 등) → `book_too_thin_effective`로 스킵.
- 체결된 `qty_ok × 실효VWAP`(노셔널)가 `min_alloc_per_position_krw`(최소유효거래 바닥)보다
  작으면 → `notional_too_small`로 스킵. 왕복 고정비용(수수료 등) 대비 너무 작은 거래를
  거르는 용도로, 더 이상 "목표배분의 일정 비율"이 아니라 절대 노셔널 기준입니다.

**괴리가 깊을수록 사이즈가 커지는 것은 아닙니다.** 깊이는 이론적 상한(얼마나 위 단계까지
걸어도 `max_vwap` 이내인지)만 넓힐 뿐, 실제 `qty_ok`는 그 가격대에 실제로 쌓인 물량이
정합니다 — 깊고 얇은 호가창은 작은 사이즈(한 자릿수 수량, 몇십만원 레벨)로, 얕고 두꺼운
호가창은 `cap_qty`에 가까운 큰 사이즈로 이어질 수 있습니다.

이로써 "1호가는 싼데 물량이 없어 실제론 비싸게 사는" 함정과, 자본 배분 공식이 실제 유동성과
무관하게 목표수량을 정해 체결 품질을 왜곡하는 문제를 동시에 방지합니다. 시뮬 체결기
(`executor_sim.py`)도 동일하게 호가 사다리를 걸어 체결해 신호↔체결 일관성을 유지합니다.
(매도 측 bid-ladder 더블체크는 현재 미구현 — [roadmap.md](roadmap.md) 참조.)

---

## 비용 모델

- **국내 ETF는 증권거래세 면제** → 왕복비용 = 수수료 × 2 + 스프레드 (매도세 항목 없음).
- `config.py`가 로드 시 교차검증: `entry_threshold_pct ≥ 수수료×2 + max_spread_pct + min_margin_pct`.
  기본값으로 0.5% ≥ 0.015×2 + 0.15 + 0.15 = 0.33% → 통과. 이 조건을 어기는 설정은 로드 거부
  ("이 설정으로는 이론상 수익이 나지 않습니다").

---

## 기술적 사항

### KIS 웹소켓 (실시간 데이터)

- **접속키 발급**: `POST {KIS_URL_REST}/oauth2/Approval`, body에 `appkey` + `secretkey`
  (주의: `appsecret`이 아니라 `secretkey`). 발급된 `approval_key`를 `.kis_ws_approval_cache.json`에
  캐시(~23h 재사용).
- **구독 tr_id**: `H0STNAV0`(실시간 NAV — nav/oprc_nav/hprc_nav/lprc_nav),
  `H0STASP0`(실시간 호가 — 10단계 ASKP/BIDP + 잔량, hour_cls_code).
- **등록 한도**: 접속당 41건. 종목당 NAV+호가 2건 → **최대 20종목** (`max_watchlist_size`).
- **프레임**: `0|TR_ID|건수|필드^필드^...` 캐럿 구분. PINGPONG 프레임은 그대로 에코.
- **재접속**: 끊기면 지수 백오프(1s→60s), 접속키 재발급, 전체 재구독. (구현: `ws_client.py`)

### KIS REST (인증/주문/조회)

- **OAuth 토큰**: `POST {KIS_URL_REST}/oauth2/tokenP` (`kis_common.get_access_token`,
  `.kis_token_cache.json` 캐시). 실전/모의는 base URL과 자격증명이 다름.
- **ETF 현재가**: `FHPST02400000` — stck_prpr/nav/dprt(괴리율). 샘플러가 사용.
- **호가**: `FHKST01010200`(inquire-asking-price-exp-ccn) — askp1/bidp1. 스프레드 계산에 사용.
- **주문(현금)**: `TTTC0012U`(실전 매수)/`TTTC0011U`(실전 매도). 실전 실행기(Phase 4)의 기반.
- **잔고**: 실전 `TTTC8434R` / 모의 `VTTC8434R`.
- **체결 조회**: `TTTC0081R` — ⚠️ `EXCG_ID_DVSN_CD`를 `"ALL"` 또는 `"SOR"`로 조회. `"KRX"`로
  조회하면 SOR로 낸 주문이 안 보임(실측 확인).
- **휴장일**: `CTCA0903R` — 하루 1회 호출 권장(캐시 필수).
- **레이트리밋**: 실전 REST ~20 req/s. 샘플러는 100종목×2콜×0.1s ≈ 20s/스윕으로 여유.

> KIS API 스펙(tr_id/파라미터/헤더)은 코드 수정 전 `kis-code-assistant-mcp` MCP 서버로 반드시
> 재확인합니다(CLAUDE.md 규칙 2). 추측 금지.

### 시뮬 ↔ 실전 이중 게이트

- `execution.mode`(`"sim"`/`"live"`) + `execution.live_enabled`(bool). 실전 실행은 **둘 다** 켜야
  하며 기본은 `sim`/`false`. 러너는 `mode != "sim"`이면 실행 거부(실전 실행기는 Phase 4, 미구현).
- 단독 `order_buy_005930_real.py`만 예외적으로 `--live`로 실주문(안전패턴: 기본 dry-run,
  1주 상한, 재시도 제한, 시크릿 마스킹).

### 시크릿 취급

모든 자격증명은 `.env`에서 로드하고, 로그/저널/에러 메시지에 절대 노출하지 않습니다
(`kis_common.sanitize`로 마스킹). `.env`와 토큰 캐시는 gitignore.

---

## config 레퍼런스

`etf_arb_config.json`의 전 필드. **여기서 값을 바꾸면 다음 기동부터 적용됩니다.** 검증은
`etf_arb/config.py::_validate()`.

### `universe` — 워치리스트 선정 (상세: [watchlist.md](watchlist.md))

| 필드 | 기본값 | 의미 |
|---|---|---|
| `lookback_days` | 120 | 일별 데이터 조회 거래일 수 |
| `min_daily_value_krw` | 500000000 | 일거래대금 중앙값 하한(5억) |
| `max_price_krw` | 500000 | 종가 상한(50만) |
| `exclude_foreign_underlying` | true | 해외 기초자산 ETF 제외(NAV 낡음 방지) |
| `max_spread_pct` | 0.15 | 스프레드 상한(%). N일 MA 필터 임계값 겸용 |
| `scan_entry_thresholds_pct` | [0.3,0.5,0.8] | 에피소드 통계 스캔 임계값 후보 |
| `scan_exit_threshold_pct` | 0.1 | 에피소드 해소 기준(%) |
| `max_watchlist_size` | 20 | 감시 종목 수(웹소켓 41한도÷2) |
| `intraday_min_samples` | 60 | 장중 스코어 신뢰 최소 표본 |
| `intraday_weight` | 0.4 | 합성 스코어의 장중 가중치 |
| `intraday_lookback_days` | 10 | 장중 데이터 조회 일수 |
| `intraday_deadline_minutes` | 60 | 장중 에피소드 해소 기한(분) |
| `spread_lookback_days` | 5 | 스프레드 이동평균 일수(N) |
| `spread_min_days` | 2 | 스프레드 필터 적용 최소 이력 일수 |

### `signals` — 시그널 규칙

| 필드 | 기본값 | 의미 |
|---|---|---|
| `entry_threshold_pct` | 0.5 | 진입 괴리 임계값(%) |
| `exit_threshold_pct` | 0.1 | 청산 괴리 임계값(%) |
| `entry_confirm_seconds` | 3 | 디바운스 지속 시간(초) |
| `nav_max_age_seconds` | 30 | NAV 신선도 상한(초) |
| `quote_max_age_seconds` | 10 | 호가 신선도 상한(초) |
| `no_entry_before` | "09:05" | 진입 시작 시각 |
| `no_entry_after` | "15:00" | 진입 종료 시각 |
| `force_exit_days` | 5 | 강제청산 기한(거래일) |
| `force_exit_time` | "14:50" | 강제청산 실행 시각 |
| `disaster_alert_pct` | 3.0 | 재앙 경고 괴리(%) — 경고만 |

### `risk` — 자본/한도

| 필드 | 기본값 | 의미 |
|---|---|---|
| `virtual_capital_krw` | 8500000 | 가상 자본 |
| `min_alloc_per_position_krw` | 1000000 | 최소유효거래 노셔널 바닥 (미달 시 `notional_too_small`) |
| `max_alloc_per_position_krw` | 2000000 | 종목당 자본 상한(호가 사다리 워크의 `cap_qty` 산출용) |
| `max_positions` | 4 | 동시 보유 상한 |
| `max_entries_per_day` | 6 | 하루 진입 상한 |
| `cooldown_minutes` | 60 | 청산 후 재진입 금지 시간 |

### `fees` — 비용

| 필드 | 기본값 | 의미 |
|---|---|---|
| `commission_rate_pct` | 0.015 | 편도 수수료율(%) — 본인 KIS 요율로 조정 |
| `min_margin_pct` | 0.15 | 진입 임계값이 확보해야 할 최소 마진(%) |

### `execution` — 실행 모드

| 필드 | 기본값 | 의미 |
|---|---|---|
| `mode` | "sim" | `"sim"`(가상) / `"live"`(실전, 미구현) |
| `live_enabled` | false | 실전 실행 이중 게이트 |

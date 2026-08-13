"""Load and validate etf_arb_config.json into frozen dataclasses.

Cross-field invariant enforced at load time: the entry threshold must clear
the modeled round-trip cost (commission both sides + expected spread + margin).
Korean ETFs are exempt from 증권거래세, so no tax term appears in the cost
model - round-trip cost = commission x 2 + spread.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import time as dtime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "etf_arb_config.json"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class UniverseConfig:
    lookback_days: int
    min_daily_value_krw: int
    max_price_krw: int
    exclude_foreign_underlying: bool
    max_spread_pct: float
    scan_entry_thresholds_pct: tuple[float, ...]
    scan_exit_threshold_pct: float
    max_watchlist_size: int
    intraday_min_samples: int
    intraday_weight: float
    intraday_lookback_days: int
    intraday_deadline_minutes: float
    # N일 이동평균 스프레드 필터 (장전 리프레셔용): 샘플러가 축적한 종목별
    # 일중 스프레드의 일별 중앙값을 최근 spread_lookback_days일 평균 내어,
    # max_spread_pct를 초과하는 구조적 고스프레드 종목을 워치리스트에서 제외.
    spread_lookback_days: int = 5
    spread_min_days: int = 2


@dataclass(frozen=True)
class SignalsConfig:
    entry_threshold_pct: float
    exit_threshold_pct: float
    entry_confirm_seconds: float
    nav_max_age_seconds: float
    quote_max_age_seconds: float
    no_entry_before: str   # "HH:MM"
    no_entry_after: str    # "HH:MM"
    force_exit_days: int   # trading days
    force_exit_time: str   # "HH:MM"
    disaster_alert_pct: float
    # 진입 괴리 하한(안전장치): 이보다 더 깊은 괴리는 기회가 아니라 데이터
    # 이상으로 보고 진입 거부(disparity_implausible). NAV 피드가 종일 틀렸던
    # 실측 사례(0080Y0 2026-07-28) 대비 - 사후 탐지가 아니라 단일 틱만으로
    # 진입 전에 막는 것이 핵심.
    max_entry_disparity_pct: float = 3.0
    # 매일 force_exit_time에 보유 전량 강제청산(오버나이트 캐리 제거). 켜면
    # force_exit_days(기한)는 15:00에 다 못 판 잔량이 다음날로 넘어간 극단
    # 케이스의 백스톱으로만 남는다. 끄면 기한 청산만 하는 구 동작.
    force_exit_daily: bool = True


@dataclass(frozen=True)
class RiskConfig:
    virtual_capital_krw: int
    max_positions: int
    max_entries_per_day: int
    cooldown_minutes: int
    # 호가창 유동성 기반 사이징: max_alloc은 자본 상한(호가 사다리 워크의 목표
    # 수량 캡), min_alloc은 실제 체결 노셔널이 이보다 작으면 스킵하는 최소유효
    # 거래 바닥(왕복 고정비용 대비 너무 작은 거래를 거름).
    min_alloc_per_position_krw: int = 1_000_000
    max_alloc_per_position_krw: int = 2_000_000


@dataclass(frozen=True)
class FeesConfig:
    commission_rate_pct: float
    min_margin_pct: float


@dataclass(frozen=True)
class ExecutionConfig:
    mode: str            # "sim" | "live"
    live_enabled: bool


@dataclass(frozen=True)
class Config:
    universe: UniverseConfig
    signals: SignalsConfig
    risk: RiskConfig
    fees: FeesConfig
    execution: ExecutionConfig


def _parse_hhmm(value: str, field: str) -> dtime:
    parts = value.split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise ConfigError(f"{field}: 'HH:MM' 형식이어야 합니다 (입력값: {value!r})")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ConfigError(f"{field}: 시각 범위가 잘못되었습니다 (입력값: {value!r})")
    return dtime(h, m)


def load_config(path: Path | None = None) -> Config:
    config_path = path or DEFAULT_CONFIG_PATH
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"설정 파일이 없습니다: {config_path}") from None
    except json.JSONDecodeError as e:
        raise ConfigError(f"설정 파일 JSON 파싱 실패: {e}") from None

    try:
        u = raw["universe"]
        s = raw["signals"]
        r = raw["risk"]
        f = raw["fees"]
        e = raw["execution"]
        cfg = Config(
            universe=UniverseConfig(
                lookback_days=int(u["lookback_days"]),
                min_daily_value_krw=int(u["min_daily_value_krw"]),
                max_price_krw=int(u["max_price_krw"]),
                exclude_foreign_underlying=bool(u["exclude_foreign_underlying"]),
                max_spread_pct=float(u["max_spread_pct"]),
                scan_entry_thresholds_pct=tuple(
                    float(x) for x in u["scan_entry_thresholds_pct"]
                ),
                scan_exit_threshold_pct=float(u["scan_exit_threshold_pct"]),
                max_watchlist_size=int(u["max_watchlist_size"]),
                intraday_min_samples=int(u["intraday_min_samples"]),
                intraday_weight=float(u["intraday_weight"]),
                intraday_lookback_days=int(u["intraday_lookback_days"]),
                intraday_deadline_minutes=float(u["intraday_deadline_minutes"]),
                spread_lookback_days=int(u.get("spread_lookback_days", 5)),
                spread_min_days=int(u.get("spread_min_days", 2)),
            ),
            signals=SignalsConfig(
                entry_threshold_pct=float(s["entry_threshold_pct"]),
                exit_threshold_pct=float(s["exit_threshold_pct"]),
                entry_confirm_seconds=float(s["entry_confirm_seconds"]),
                nav_max_age_seconds=float(s["nav_max_age_seconds"]),
                quote_max_age_seconds=float(s["quote_max_age_seconds"]),
                no_entry_before=str(s["no_entry_before"]),
                no_entry_after=str(s["no_entry_after"]),
                force_exit_days=int(s["force_exit_days"]),
                force_exit_time=str(s["force_exit_time"]),
                disaster_alert_pct=float(s["disaster_alert_pct"]),
                max_entry_disparity_pct=float(
                    s.get("max_entry_disparity_pct", 3.0)
                ),
                force_exit_daily=bool(s.get("force_exit_daily", True)),
            ),
            risk=RiskConfig(
                virtual_capital_krw=int(r["virtual_capital_krw"]),
                max_positions=int(r["max_positions"]),
                max_entries_per_day=int(r["max_entries_per_day"]),
                cooldown_minutes=int(r["cooldown_minutes"]),
                min_alloc_per_position_krw=int(
                    r.get("min_alloc_per_position_krw", 1_000_000)
                ),
                max_alloc_per_position_krw=int(
                    r.get("max_alloc_per_position_krw", 2_000_000)
                ),
            ),
            fees=FeesConfig(
                commission_rate_pct=float(f["commission_rate_pct"]),
                min_margin_pct=float(f["min_margin_pct"]),
            ),
            execution=ExecutionConfig(
                mode=str(e["mode"]),
                live_enabled=bool(e["live_enabled"]),
            ),
        )
    except (KeyError, TypeError, ValueError) as err:
        raise ConfigError(f"설정 필드 누락/형식 오류: {err!r}") from None

    _validate(cfg)
    return cfg


def _validate(cfg: Config) -> None:
    s, u, r, f, e = cfg.signals, cfg.universe, cfg.risk, cfg.fees, cfg.execution

    # 진입 임계값이 왕복비용(수수료x2 + 스프레드 + 최소마진)을 넘는지 확인.
    # 스프레드는 유니버스 필터 상한(max_spread_pct)을 보수적 추정치로 사용.
    round_trip_cost_pct = (
        f.commission_rate_pct * 2 + u.max_spread_pct + f.min_margin_pct
    )
    if s.entry_threshold_pct < round_trip_cost_pct:
        raise ConfigError(
            f"entry_threshold_pct({s.entry_threshold_pct})가 모델링된 왕복비용"
            f"({round_trip_cost_pct:.3f}% = 수수료 {f.commission_rate_pct}x2 + "
            f"스프레드 {u.max_spread_pct} + 마진 {f.min_margin_pct})보다 작습니다. "
            "이 설정으로는 이론상 수익이 나지 않습니다."
        )

    if s.exit_threshold_pct >= s.entry_threshold_pct:
        raise ConfigError("exit_threshold_pct는 entry_threshold_pct보다 작아야 합니다")

    # 진입 괴리 하한이 진입 임계값보다 얕으면 통과 가능한 구간이 사라진다.
    if s.max_entry_disparity_pct <= s.entry_threshold_pct:
        raise ConfigError(
            f"max_entry_disparity_pct({s.max_entry_disparity_pct})는 "
            f"entry_threshold_pct({s.entry_threshold_pct})보다 커야 합니다. "
            "그렇지 않으면 진입 가능한 괴리 구간이 존재하지 않습니다."
        )

    if u.max_watchlist_size * 2 > 41:
        raise ConfigError(
            f"max_watchlist_size({u.max_watchlist_size})가 너무 큽니다: "
            "KIS 웹소켓 등록 한도(41건)에서 종목당 2건(NAV+호가)이 필요하므로 최대 20."
        )

    if u.intraday_min_samples <= 0:
        raise ConfigError("intraday_min_samples는 1 이상이어야 합니다")
    if not (0.0 <= u.intraday_weight <= 1.0):
        raise ConfigError("intraday_weight는 0.0~1.0 범위여야 합니다")
    if u.intraday_lookback_days < 1:
        raise ConfigError("intraday_lookback_days는 1 이상이어야 합니다")
    if u.intraday_deadline_minutes <= 0:
        raise ConfigError("intraday_deadline_minutes는 0보다 커야 합니다")
    if u.spread_lookback_days < 1:
        raise ConfigError("spread_lookback_days는 1 이상이어야 합니다")
    if not (1 <= u.spread_min_days <= u.spread_lookback_days):
        raise ConfigError(
            "spread_min_days는 1 이상 spread_lookback_days 이하여야 합니다 "
            f"({u.spread_min_days}, spread_lookback_days={u.spread_lookback_days})"
        )

    # 호가창 유동성 기반 사이징 안전장치.
    if r.min_alloc_per_position_krw > r.max_alloc_per_position_krw:
        raise ConfigError(
            "min_alloc_per_position_krw는 max_alloc_per_position_krw보다 클 수 없습니다 "
            f"({r.min_alloc_per_position_krw} > {r.max_alloc_per_position_krw})"
        )
    if r.max_alloc_per_position_krw * r.max_positions > r.virtual_capital_krw * 1.05:
        raise ConfigError(
            "max_alloc_per_position_krw x max_positions가 virtual_capital_krw(105%)를 "
            "초과합니다 (동시 최대포지션 자본 안전장치)"
        )

    times = {
        field_name: _parse_hhmm(getattr(s, field_name), field_name)
        for field_name in ("no_entry_before", "no_entry_after", "force_exit_time")
    }

    # 일일 강제청산을 켜면 진입 마감이 청산 시각보다 늦으면 안 된다. 늦으면
    # 그 사이에 잡은 포지션이 다음 틱에 곧바로 플러시돼 왕복 수수료만 나간다.
    if s.force_exit_daily and times["no_entry_after"] > times["force_exit_time"]:
        raise ConfigError(
            f"force_exit_daily=true인데 no_entry_after({s.no_entry_after})가 "
            f"force_exit_time({s.force_exit_time})보다 늦습니다. 진입 직후 곧바로 "
            "강제청산되는 무의미한 거래가 발생하므로 no_entry_after를 "
            "force_exit_time 이하로 설정하세요."
        )

    if s.force_exit_days < 1:
        raise ConfigError("force_exit_days는 1 이상이어야 합니다")

    if e.mode not in ("sim", "live"):
        raise ConfigError(f"execution.mode는 'sim' 또는 'live'여야 합니다: {e.mode!r}")
    if e.mode == "live" and not e.live_enabled:
        raise ConfigError(
            "execution.mode='live'인데 live_enabled=false입니다. "
            "실전 모드는 두 설정을 모두 켜야 합니다 (이중 게이트)."
        )

"""Load and validate etf_arb_config.json into frozen dataclasses.

Cross-field invariant enforced at load time: the entry threshold must clear
the modeled round-trip cost (commission both sides + expected spread + margin).
Korean ETFs are exempt from 증권거래세, so no tax term appears in the cost
model - round-trip cost = commission x 2 + spread.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
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
    # 괴리 깊이(%)를 진입임계값 위로 이만큼 초과하면 max_alloc까지 선형 스케일.
    depth_scale_span_pct: float = 0.3
    # 다단계 호가 유효괴리 이중검증: 목표수량 대비 이 비율 이상을 임계 이내로
    # 채울 수 있어야 진입(그렇지 않으면 book_too_thin_effective로 스킵).
    min_fill_ratio: float = 0.5


@dataclass(frozen=True)
class RiskConfig:
    virtual_capital_krw: int
    alloc_per_position_krw: int
    max_positions: int
    max_entries_per_day: int
    cooldown_minutes: int
    # 괴리 깊이 비례 사이징: 얕으면 min, 깊으면 max까지 자본을 배분.
    # (구 alloc_per_position_krw는 하위호환용으로 남겨두되 사이징에는 미사용.)
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


def _parse_hhmm(value: str, field: str) -> None:
    parts = value.split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise ConfigError(f"{field}: 'HH:MM' 형식이어야 합니다 (입력값: {value!r})")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ConfigError(f"{field}: 시각 범위가 잘못되었습니다 (입력값: {value!r})")


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
                depth_scale_span_pct=float(s.get("depth_scale_span_pct", 0.3)),
                min_fill_ratio=float(s.get("min_fill_ratio", 0.5)),
            ),
            risk=RiskConfig(
                virtual_capital_krw=int(r["virtual_capital_krw"]),
                alloc_per_position_krw=int(r["alloc_per_position_krw"]),
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

    if r.alloc_per_position_krw * r.max_positions > r.virtual_capital_krw * 1.05:
        raise ConfigError(
            "alloc_per_position_krw x max_positions가 virtual_capital_krw를 초과합니다"
        )

    # 깊이 비례 사이징 안전장치.
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
    if s.depth_scale_span_pct <= 0:
        raise ConfigError("depth_scale_span_pct는 0보다 커야 합니다")
    if not (0.0 < s.min_fill_ratio <= 1.0):
        raise ConfigError("min_fill_ratio는 0 초과 1 이하이어야 합니다")

    for field_name in ("no_entry_before", "no_entry_after", "force_exit_time"):
        _parse_hhmm(getattr(s, field_name), field_name)

    if s.force_exit_days < 1:
        raise ConfigError("force_exit_days는 1 이상이어야 합니다")

    if e.mode not in ("sim", "live"):
        raise ConfigError(f"execution.mode는 'sim' 또는 'live'여야 합니다: {e.mode!r}")
    if e.mode == "live" and not e.live_enabled:
        raise ConfigError(
            "execution.mode='live'인데 live_enabled=false입니다. "
            "실전 모드는 두 설정을 모두 켜야 합니다 (이중 게이트)."
        )

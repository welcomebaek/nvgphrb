"""Tests for cross-field config invariants (`etf_arb.config._validate`).

Only the invariants that are easy to get wrong by hand-editing
`etf_arb_config.json` are covered here; the file is loaded from a tmp copy so
the real config is never touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from etf_arb.config import DEFAULT_CONFIG_PATH, ConfigError, load_config


def _write_variant(
    tmp_path: Path,
    risk: dict | None = None,
    universe: dict | None = None,
    **signal_overrides,
) -> Path:
    """실제 config를 베이스로 signals(+선택적 risk/universe) 섹션을 덮어쓴 사본."""
    raw = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    raw["signals"].update(signal_overrides)
    if risk:
        raw["risk"].update(risk)
    if universe:
        raw["universe"].update(universe)
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return path


def test_resolution_filter_defaults_load():
    u = load_config().universe
    assert u.resolution_lookback_days == 20
    assert u.resolution_min_episodes == 10
    assert u.min_resolution_rate == 0.30


def test_resolution_entry_threshold_decoupled_from_live_threshold():
    """등급용 임계값은 실거래 진입 임계값보다 얕게 잡는다.

    갭이 닫히는 성향은 종목의 LP 호가 운영에서 나오는 성질이라 얕은 갭에서도
    드러나는 반면, 실거래 임계값(0.5%)은 왕복 비용을 넘겨야 한다는 별개의
    경제성 제약이다. 둘을 묶어두면 진입 기회가 드문 종목은 등급도 못 매긴다.
    """
    cfg = load_config()
    assert cfg.universe.resolution_entry_threshold_pct == 0.3
    assert cfg.signals.entry_threshold_pct == 0.5
    assert cfg.universe.resolution_entry_threshold_pct < cfg.signals.entry_threshold_pct


def test_min_resolution_rate_floor_is_thirty_percent():
    assert load_config().universe.min_resolution_rate == 0.30


def test_resolution_entry_threshold_must_be_positive(tmp_path):
    path = _write_variant(tmp_path, universe={"resolution_entry_threshold_pct": 0.0})
    with pytest.raises(ConfigError, match="resolution_entry_threshold_pct"):
        load_config(path)


def test_resolution_entry_threshold_below_implausible_cap(tmp_path):
    # 등급 임계값이 이상치 상한 이상이면 에피소드가 하나도 안 잡힌다.
    path = _write_variant(
        tmp_path, max_entry_disparity_pct=3.0,
        universe={"resolution_entry_threshold_pct": 3.0},
    )
    with pytest.raises(ConfigError, match="resolution_entry_threshold_pct"):
        load_config(path)


def test_min_resolution_rate_out_of_range_rejected(tmp_path):
    path = _write_variant(tmp_path, universe={"min_resolution_rate": 1.5})
    with pytest.raises(ConfigError, match="min_resolution_rate"):
        load_config(path)


def test_resolution_min_episodes_must_be_positive(tmp_path):
    path = _write_variant(tmp_path, universe={"resolution_min_episodes": 0})
    with pytest.raises(ConfigError, match="resolution_min_episodes"):
        load_config(path)


def test_real_config_loads():
    cfg = load_config()
    assert cfg.signals.force_exit_daily is True
    # 일일 강제청산이 켜져 있으면 진입 마감이 청산 시각보다 늦으면 안 된다.
    assert cfg.signals.no_entry_after <= cfg.signals.force_exit_time


def test_entry_window_may_not_outlast_daily_force_exit(tmp_path):
    path = _write_variant(
        tmp_path, force_exit_daily=True,
        force_exit_time="15:00", no_entry_after="15:10",
    )
    with pytest.raises(ConfigError, match="no_entry_after"):
        load_config(path)


def test_entry_window_equal_to_force_exit_time_is_allowed(tmp_path):
    path = _write_variant(
        tmp_path, force_exit_daily=True,
        force_exit_time="15:00", no_entry_after="15:00",
    )
    assert load_config(path).signals.no_entry_after == "15:00"


def test_min_runway_minutes_loads_from_real_config():
    s = load_config().signals
    assert s.min_runway_minutes == 60
    # 실효 진입 마감 = force_exit_time - 활주로 = 15:00 - 60분 = 14:00.
    assert s.force_exit_time == "15:00"


def test_negative_min_runway_rejected(tmp_path):
    path = _write_variant(tmp_path, min_runway_minutes=-1)
    with pytest.raises(ConfigError, match="min_runway_minutes"):
        load_config(path)


def test_runway_swallowing_entry_window_rejected(tmp_path):
    # 15:00 - 600분 = 05:00 < no_entry_before(09:05) -> 진입창 소멸.
    path = _write_variant(
        tmp_path, force_exit_daily=True, force_exit_time="15:00",
        no_entry_before="09:05", min_runway_minutes=600,
    )
    with pytest.raises(ConfigError, match="min_runway_minutes"):
        load_config(path)


def test_runway_not_validated_when_daily_force_exit_off(tmp_path):
    # 다중일 모드에선 게이트가 꺼지므로 큰 값이어도 설정 오류가 아니다.
    path = _write_variant(
        tmp_path, force_exit_daily=False, min_runway_minutes=600
    )
    assert load_config(path).signals.min_runway_minutes == 600


def test_entry_disparity_ceiling_must_exceed_threshold(tmp_path):
    # 하한이 임계값보다 얕으면 진입 가능한 구간이 사라진다.
    path = _write_variant(
        tmp_path, entry_threshold_pct=0.5, max_entry_disparity_pct=0.4
    )
    with pytest.raises(ConfigError, match="max_entry_disparity_pct"):
        load_config(path)


def test_entry_disparity_ceiling_default_is_three_pct():
    assert load_config().signals.max_entry_disparity_pct == 3.0


def test_real_config_allows_many_positions_within_single_alloc_cap():
    # max_positions x max_alloc이 자본을 넘어도(구 검증식 기준) 개별 배분
    # 상한 자체가 자본 이하면 통과해야 한다 - depth-driven 사이징이 현금으로
    # 실제 노출을 이미 제한하므로 곱셈 제약은 불필요.
    cfg = load_config()
    assert cfg.risk.max_positions * cfg.risk.max_alloc_per_position_krw \
        > cfg.risk.virtual_capital_krw * 1.05
    assert cfg.risk.max_alloc_per_position_krw <= cfg.risk.virtual_capital_krw * 1.05


def test_single_position_alloc_cap_may_not_exceed_capital(tmp_path):
    path = _write_variant(
        tmp_path, risk={"virtual_capital_krw": 1_000_000,
                         "max_alloc_per_position_krw": 2_000_000}
    )
    with pytest.raises(ConfigError, match="max_alloc_per_position_krw"):
        load_config(path)


def test_late_entry_window_allowed_when_daily_flush_off(tmp_path):
    # 기한 청산만 하는 구 동작에서는 진입창이 더 늦어도 문제되지 않는다.
    path = _write_variant(
        tmp_path, force_exit_daily=False,
        force_exit_time="14:50", no_entry_after="15:00",
    )
    cfg = load_config(path)
    assert cfg.signals.force_exit_daily is False

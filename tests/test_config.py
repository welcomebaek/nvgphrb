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
    tmp_path: Path, risk: dict | None = None, **signal_overrides
) -> Path:
    """실제 config를 베이스로 signals(+선택적 risk) 섹션을 덮어쓴 사본."""
    raw = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    raw["signals"].update(signal_overrides)
    if risk:
        raw["risk"].update(risk)
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return path


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

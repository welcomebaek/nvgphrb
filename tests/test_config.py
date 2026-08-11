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


def _write_variant(tmp_path: Path, **signal_overrides) -> Path:
    """실제 config를 베이스로 signals 섹션만 덮어쓴 사본을 만든다."""
    raw = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    raw["signals"].update(signal_overrides)
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


def test_late_entry_window_allowed_when_daily_flush_off(tmp_path):
    # 기한 청산만 하는 구 동작에서는 진입창이 더 늦어도 문제되지 않는다.
    path = _write_variant(
        tmp_path, force_exit_daily=False,
        force_exit_time="14:50", no_entry_after="15:00",
    )
    cfg = load_config(path)
    assert cfg.signals.force_exit_daily is False

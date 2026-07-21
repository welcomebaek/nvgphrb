"""Tests for the intraday disparity-distribution percentiles (Phase 2.5 stage 1).

Stage 1 only ACCUMULATES the per-ticker expected-disparity distribution into
the watchlist/candidates output; the trading logic does not consume it yet
(that is the deferred stage 2). These tests pin the percentile math so the
data being accumulated is correct from day one.
"""

from __future__ import annotations

from etf_arb import universe


def _session(disparities):
    """Build one (timestamps, disparity_pct) session tuple, 60s apart."""
    ts = [float(i * 60) for i in range(len(disparities))]
    return (ts, list(disparities))


def test_disparity_percentiles_present_and_ordered():
    # 저평가(음수)가 깊은 종목: p5는 p50보다 더 깊어야(더 음수) 한다.
    disp = [-0.9, -0.7, -0.6, -0.5, -0.4, -0.3, -0.2, -0.1, 0.0, 0.1]
    stats = universe.intraday_episode_stats(
        [_session(disp)],
        entry_threshold_pct=0.5,
        exit_threshold_pct=0.1,
        deadline_minutes=60.0,
        commission_rate_pct=0.015,
    )
    assert stats["disparity_p5"] is not None
    assert stats["disparity_p10"] is not None
    assert stats["disparity_p25"] is not None
    assert stats["disparity_median"] is not None
    # 오름차순 정렬 기준 nearest-rank: p5 <= p10 <= p25 <= median
    assert stats["disparity_p5"] <= stats["disparity_p10"] <= stats["disparity_p25"]
    assert stats["disparity_p25"] <= stats["disparity_median"]
    # 이 분포의 최저값이 p5로 잡혀야 한다 (가장 깊은 꼬리).
    assert stats["disparity_p5"] == -0.9


def test_disparity_percentiles_pooled_across_sessions():
    # 여러 세션을 풀링해서 분위수를 계산한다 (에피소드는 세션경계를 안 넘지만
    # 분포 통계는 전체 표본 대상).
    s1 = _session([-0.8, -0.6, -0.2])
    s2 = _session([-0.4, -0.3, 0.0])
    stats = universe.intraday_episode_stats(
        [s1, s2], 0.5, 0.1, 60.0, 0.015
    )
    assert stats["n_samples"] == 6
    # 6개 표본 [-0.8,-0.6,-0.4,-0.3,-0.2,0.0] 의 최저는 p5로 -0.8
    assert stats["disparity_p5"] == -0.8
    assert stats["disparity_median"] is not None


def test_disparity_percentiles_empty_sessions():
    stats = universe.intraday_episode_stats([], 0.5, 0.1, 60.0, 0.015)
    assert stats["n_samples"] == 0
    assert stats["disparity_p5"] is None
    assert stats["disparity_median"] is None

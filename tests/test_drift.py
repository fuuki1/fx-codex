"""ADWIN ドリフト検出(fx_intel/drift.py)のテスト。

標準ライブラリのみの純粋ロジックなので、既知の定常/変化系列で挙動を検証する。
"""

from __future__ import annotations

import random

import pytest

from fx_intel.drift import ADWIN, scan_for_drift

# ---------------------------------------------------------------- 基本挙動


def test_no_drift_on_stationary_stream() -> None:
    # 平均一定(0.5前後)の定常系列では変化を検出せず、窓が伸び続ける
    rng = random.Random(0)
    det = ADWIN(delta=0.002)
    detected = False
    for _ in range(500):
        detected |= det.update(rng.random())  # 一様[0,1)、平均0.5一定
    assert detected is False
    assert det.width > 100  # 変化なし → 窓は縮まず大きく育つ


def test_detects_abrupt_mean_shift() -> None:
    # 前半 0付近、後半 1付近へ急変 → どこかでドリフト検出
    det = ADWIN(delta=0.002)
    drift_indices = []
    for i in range(400):
        value = 0.0 if i < 200 else 1.0
        if det.update(value):
            drift_indices.append(i)
    assert drift_indices  # 少なくとも1回は検出
    # 検出は変化点(200)以降に起きる
    assert min(drift_indices) >= 200
    # 変化後は窓が縮んで直近レジーム(平均≈1)を推定
    assert det.mean > 0.8


def test_window_shrinks_after_drift() -> None:
    det = ADWIN(delta=0.002)
    for i in range(300):
        det.update(0.0 if i < 150 else 1.0)
    # 変化検出で古い側を捨てるため、窓幅は総観測数より小さい
    assert det.width < det.total_seen


def test_gradual_drift_eventually_detected() -> None:
    # 緩やかに平均が上がる系列でも、いずれ検出される
    det = ADWIN(delta=0.01)
    detected = False
    for i in range(600):
        mean = i / 600.0  # 0→1へ線形
        detected |= det.update(mean)
    assert detected is True


# ---------------------------------------------------------------- パラメータ・境界


def test_delta_must_be_valid() -> None:
    with pytest.raises(ValueError):
        ADWIN(delta=0.0)
    with pytest.raises(ValueError):
        ADWIN(delta=1.0)


def test_min_subwindow_must_be_positive() -> None:
    with pytest.raises(ValueError):
        ADWIN(min_subwindow=0)


def test_max_window_caps_growth() -> None:
    det = ADWIN(delta=0.002, max_window=50)
    for _ in range(200):
        det.update(0.5)
    assert det.width <= 50  # 上限で頭打ち


def test_reset_clears_state() -> None:
    det = ADWIN()
    for i in range(300):
        det.update(0.0 if i < 150 else 1.0)
    det.reset()
    assert det.width == 0
    assert det.total_seen == 0
    assert det.drift_detected is False


def test_mean_empty_window_is_zero() -> None:
    assert ADWIN().mean == 0.0


# ---------------------------------------------------------------- バッチ走査


def test_scan_for_drift_lists_change_points() -> None:
    values = [0.0] * 200 + [1.0] * 200
    scan = scan_for_drift(values, delta=0.002)
    assert scan.drift_points  # 変化点を列挙
    assert min(scan.drift_points) >= 200
    assert scan.total == 400
    assert scan.final_mean > 0.8  # 直近レジーム


def test_scan_stationary_has_no_drift_points() -> None:
    rng = random.Random(1)
    values = [rng.random() for _ in range(400)]
    scan = scan_for_drift(values, delta=0.002)
    assert scan.drift_points == []

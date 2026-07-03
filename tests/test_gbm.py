"""純Python GBDT(gbm.py)のテスト(ネットワーク不要・決定論)。"""

from __future__ import annotations

import json
import random

import pytest

from fx_intel.gbm import (
    GradientBoostingClassifier,
    brier_score,
    log_loss,
    platt_calibrate,
)


def _synthetic(n: int, seed: int) -> tuple[list[list[float]], list[int]]:
    rng = random.Random(seed)
    features: list[list[float]] = []
    labels: list[int] = []
    for _ in range(n):
        row = [rng.uniform(-1, 1) for _ in range(6)]
        signal = 1.5 * row[0] - 1.0 * row[1] + 0.5 * row[2] * row[3] + rng.gauss(0, 0.5)
        features.append(row)
        labels.append(1 if signal > 0 else 0)
    return features, labels


def test_learns_better_than_baseline() -> None:
    x_tr, y_tr = _synthetic(800, seed=1)
    x_va, y_va = _synthetic(300, seed=2)
    model = GradientBoostingClassifier(seed=7).fit(x_tr, y_tr, x_va, y_va)
    probs = model.predict_proba_many(x_va)
    base = sum(y_tr) / len(y_tr)
    assert brier_score(y_va, probs) < brier_score(y_va, [base] * len(y_va))
    assert log_loss(y_va, probs) < log_loss(y_va, [base] * len(y_va))


def test_deterministic_same_seed() -> None:
    x_tr, y_tr = _synthetic(400, seed=1)
    x_va, y_va = _synthetic(150, seed=2)
    a = GradientBoostingClassifier(seed=7).fit(x_tr, y_tr, x_va, y_va)
    b = GradientBoostingClassifier(seed=7).fit(x_tr, y_tr, x_va, y_va)
    assert a.predict_proba_many(x_va) == b.predict_proba_many(x_va)


def test_serialization_roundtrip() -> None:
    x_tr, y_tr = _synthetic(400, seed=3)
    x_va, y_va = _synthetic(150, seed=4)
    model = GradientBoostingClassifier(seed=5).fit(x_tr, y_tr, x_va, y_va)
    restored = GradientBoostingClassifier.from_dict(json.loads(json.dumps(model.to_dict())))
    for row in x_va:
        assert abs(model.predict_proba(row) - restored.predict_proba(row)) < 1e-12


def test_early_stopping_limits_trees() -> None:
    x_tr, y_tr = _synthetic(400, seed=1)
    x_va, y_va = _synthetic(150, seed=2)
    model = GradientBoostingClassifier(n_estimators=500, early_stopping_rounds=10, seed=7).fit(
        x_tr, y_tr, x_va, y_va
    )
    assert model.best_iteration_ <= 500
    assert len(model.trees_) == model.best_iteration_


def test_rejects_non_finite_features() -> None:
    with pytest.raises(ValueError):
        GradientBoostingClassifier().fit([[float("nan"), 1.0]], [1])


def test_rejects_single_class() -> None:
    with pytest.raises(ValueError):
        GradientBoostingClassifier().fit([[0.1], [0.2], [0.3]], [1, 1, 1])


def test_rejects_ragged_rows() -> None:
    with pytest.raises(ValueError):
        GradientBoostingClassifier().fit([[0.1, 0.2], [0.3]], [1, 0])


def test_platt_calibration_improves_or_matches() -> None:
    x_tr, y_tr = _synthetic(600, seed=1)
    x_va, y_va = _synthetic(300, seed=2)
    model = GradientBoostingClassifier(seed=7).fit(x_tr, y_tr, x_va, y_va)
    margins = [model.predict_margin(row) for row in x_va]
    cal = platt_calibrate(margins, y_va)
    raw = log_loss(y_va, [model.predict_proba(row) for row in x_va])
    calibrated = log_loss(y_va, [cal.apply(m) for m in margins])
    assert calibrated <= raw + 1e-6


def test_from_dict_rejects_broken_tree() -> None:
    with pytest.raises(ValueError):
        GradientBoostingClassifier.from_dict(
            {"params": {}, "trees": [{"f": 0, "t": 0.5}]}  # 分岐なのにl/rが無い
        )

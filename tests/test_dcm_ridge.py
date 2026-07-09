"""Phase 4: Ridge回帰とシグナル化の検証。

数学的性質(β回復・α→0でOLS一致・標準化の分離・直列化)を確かめる。
"""

from __future__ import annotations

import numpy as np
import pytest

from dukascopy_cftc_model.ridge import RidgeRegressor
from dukascopy_cftc_model.signal import predictions_to_signals, signal_scale


def _linear_data(n: int = 500, d: int = 4, noise: float = 0.01, seed: int = 0):
    rng = np.random.default_rng(seed)
    X = rng.normal(0, 1, (n, d))
    beta = np.array([2.0, -1.0, 0.5, 0.0])[:d]
    y = X @ beta + 3.0 + rng.normal(0, noise, n)  # 切片3.0
    return X, y, beta


def test_ridge_recovers_coefficients_low_alpha() -> None:
    X, y, beta = _linear_data(noise=0.001)
    model = RidgeRegressor(alpha=0.01).fit(X, y)
    # 標準化空間の係数を元スケールへ戻して比較: coef_orig = coef_std / scale
    coef_orig = model.coef_ / model.scale_
    assert np.allclose(coef_orig, beta, atol=0.05)
    assert abs(model.intercept_ - y.mean()) < 1e-9  # 切片は y平均


def test_ridge_predict_is_accurate() -> None:
    X, y, _ = _linear_data(noise=0.001)
    model = RidgeRegressor(alpha=0.01).fit(X, y)
    pred = model.predict(X)
    # 決定係数 R^2 が高い
    ss_res = np.sum((y - pred) ** 2)
    ss_tot = np.sum((y - y.mean()) ** 2)
    r2 = 1 - ss_res / ss_tot
    assert r2 > 0.99


def test_ridge_alpha_zero_matches_ols() -> None:
    X, y, _ = _linear_data(noise=0.1)
    ridge = RidgeRegressor(alpha=0.0).fit(X, y)
    pred_ridge = ridge.predict(X)
    # numpy lstsq による OLS(切片つき)
    Xd = np.column_stack([np.ones(len(X)), X])
    coef, *_ = np.linalg.lstsq(Xd, y, rcond=None)
    pred_ols = Xd @ coef
    assert np.allclose(pred_ridge, pred_ols, atol=1e-6)


def test_ridge_regularization_shrinks_coefficients() -> None:
    X, y, _ = _linear_data(noise=0.5)
    weak = RidgeRegressor(alpha=0.1).fit(X, y)
    strong = RidgeRegressor(alpha=1000.0).fit(X, y)
    assert np.linalg.norm(strong.coef_) < np.linalg.norm(weak.coef_)


def test_standardization_is_fit_on_train_only() -> None:
    X, y, _ = _linear_data()
    model = RidgeRegressor(alpha=1.0).fit(X, y, feature_names=["a", "b", "c", "d"])
    # mean_/scale_ は train X の統計
    assert np.allclose(model.mean_, X.mean(axis=0))
    assert np.allclose(model.scale_, X.std(axis=0, ddof=0))


def test_feature_importance_ranks_by_abs_coef() -> None:
    X, y, _ = _linear_data(noise=0.001)
    model = RidgeRegressor(alpha=0.01).fit(X, y, feature_names=["a", "b", "c", "d"])
    imp = model.feature_importance()
    # beta=[2,-1,0.5,0] なので a が最も効く、d が最も効かない
    assert imp[0][0] == "a"
    assert imp[-1][0] == "d"
    top2 = model.feature_importance(top=2)
    assert len(top2) == 2


def test_serialization_roundtrip() -> None:
    X, y, _ = _linear_data()
    model = RidgeRegressor(alpha=2.5).fit(X, y, feature_names=["a", "b", "c", "d"])
    payload = model.to_dict()
    restored = RidgeRegressor.from_dict(payload)
    assert np.allclose(model.predict(X), restored.predict(X))
    assert restored.alpha == 2.5
    assert restored.feature_names == ["a", "b", "c", "d"]


def test_fit_rejects_non_finite() -> None:
    X, y, _ = _linear_data(n=10)
    X[0, 0] = np.nan
    with pytest.raises(ValueError, match="非有限"):
        RidgeRegressor().fit(X, y)


def test_predict_before_fit_raises() -> None:
    with pytest.raises(RuntimeError):
        RidgeRegressor().predict(np.zeros((2, 3)))


def test_predict_shape_mismatch_raises() -> None:
    X, y, _ = _linear_data(d=4)
    model = RidgeRegressor().fit(X, y)
    with pytest.raises(ValueError, match="形状不整合"):
        model.predict(np.zeros((5, 3)))


# ------------------------------------------------------------------ signal


def test_signal_scale_and_thresholding() -> None:
    train_pred = np.array([-0.02, -0.01, 0.0, 0.01, 0.02])
    scale = signal_scale(train_pred)
    assert scale > 0
    # 予測: 強い正・弱い・強い負
    preds = np.array([0.05, 0.001, -0.05])
    sig = predictions_to_signals(preds, scale, z_threshold=0.5)
    assert sig[0] == 1  # 強い正 → ロング
    assert sig[1] == 0  # 弱い → 様子見
    assert sig[2] == -1  # 強い負 → ショート


def test_signal_zero_scale_all_flat() -> None:
    sig = predictions_to_signals(np.array([1.0, -1.0]), scale=0.0, z_threshold=0.5)
    assert (sig == 0).all()


def test_signal_higher_threshold_fewer_trades() -> None:
    rng = np.random.default_rng(0)
    preds = rng.normal(0, 1, 1000)
    scale = 1.0
    low = predictions_to_signals(preds, scale, z_threshold=0.5)
    high = predictions_to_signals(preds, scale, z_threshold=1.5)
    assert (high != 0).sum() < (low != 0).sum()

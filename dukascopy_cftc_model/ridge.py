"""Ridge回帰(L2正則化線形回帰)の純numpy実装(パイプライン6段目・中核)。

sklearn を入れない(fx_intel/gbm.py が LightGBM を入れずに純Python実装したのと
同じ設計判断)。対象は「特徴量十数個 × 数千サンプル」の小規模で、閉形式解で
一瞬で解ける。

閉形式解(標準化した特徴量に対して):

    w = (XᵀX + αI)⁻¹ Xᵀy

- 特徴量は train の平均・標準偏差で標準化(StandardScaler相当)を内包する。
  同じ統計で predict 時も transform し、リークを防ぐ。
- 切片は標準化後の y 平均で吸収(標準化済みXは平均0なので、切片=ȳ)。
  切片は正則化しない。
- coefficients() は標準化係数を返す。標準化しているので係数の絶対値が
  そのまま「特徴量の効き(寄与)」の比較になる = 最終出力の「特徴量寄与」。

ミッションクリティカル要件:
- 決定論: 同じデータから必ず同じ w。乱数を使わない。
- 直列化: to_dict/from_dict でJSONに保存・復元(来歴を残せる)。
- 入力検証: 非有限値・行列不整合・空データは fit 前に拒否する。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

RIDGE_MODEL_KIND = "ridge_regressor_v1"


@dataclass
class RidgeRegressor:
    """標準化を内包した Ridge 回帰。

    属性(fit後に設定):
        feature_names: 学習に使った列名(順序込み)
        mean_/scale_ : 標準化統計(train由来)
        coef_        : 標準化空間での係数(len=特徴量数)
        intercept_   : 切片(= train の y 平均)
        alpha        : L2正則化強度
    """

    alpha: float = 1.0
    feature_names: list[str] | None = None
    mean_: np.ndarray | None = None
    scale_: np.ndarray | None = None
    coef_: np.ndarray | None = None
    intercept_: float = 0.0

    # ------------------------------------------------------------ fit / predict

    def fit(
        self, X: np.ndarray, y: np.ndarray, feature_names: list[str] | None = None
    ) -> RidgeRegressor:
        X = np.asarray(X, dtype=float)
        y = np.asarray(y, dtype=float).ravel()
        self._validate_fit_inputs(X, y)

        self.feature_names = (
            list(feature_names)
            if feature_names is not None
            else [f"x{i}" for i in range(X.shape[1])]
        )
        # 標準化(定数列は scale=1 にして 0除算を避ける)
        self.mean_ = X.mean(axis=0)
        scale = X.std(axis=0, ddof=0)
        scale[scale == 0] = 1.0
        self.scale_ = scale
        Xs = (X - self.mean_) / self.scale_

        self.intercept_ = float(y.mean())
        yc = y - self.intercept_

        n_features = Xs.shape[1]
        gram = Xs.T @ Xs + self.alpha * np.eye(n_features)
        # 対称正定値なので solve で十分(逆行列を明示的に作らない)
        self.coef_ = np.linalg.solve(gram, Xs.T @ yc)
        return self

    def predict(self, X: np.ndarray) -> np.ndarray:
        if self.coef_ is None or self.mean_ is None or self.scale_ is None:
            raise RuntimeError("fit されていません")
        X = np.asarray(X, dtype=float)
        if X.ndim != 2 or X.shape[1] != len(self.coef_):
            raise ValueError(f"予測入力の形状不整合: {X.shape}, 期待特徴量数={len(self.coef_)}")
        Xs = (X - self.mean_) / self.scale_
        return Xs @ self.coef_ + self.intercept_

    # ------------------------------------------------------------ 特徴量寄与

    def coefficients(self) -> dict[str, float]:
        """特徴量名 → 標準化係数。効きの比較にそのまま使える。"""
        if self.coef_ is None or self.feature_names is None:
            raise RuntimeError("fit されていません")
        return dict(zip(self.feature_names, (float(c) for c in self.coef_)))

    def feature_importance(self, top: int | None = None) -> list[tuple[str, float]]:
        """|係数| 降順の (特徴量名, 係数) リスト。top で上位N件に切る。"""
        items = sorted(self.coefficients().items(), key=lambda kv: abs(kv[1]), reverse=True)
        return items[:top] if top is not None else items

    # ------------------------------------------------------------ 直列化

    def to_dict(self) -> dict[str, Any]:
        if self.coef_ is None or self.mean_ is None or self.scale_ is None:
            raise RuntimeError("fit されていません")
        return {
            "kind": RIDGE_MODEL_KIND,
            "alpha": self.alpha,
            "feature_names": self.feature_names,
            "mean": self.mean_.tolist(),
            "scale": self.scale_.tolist(),
            "coef": self.coef_.tolist(),
            "intercept": self.intercept_,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> RidgeRegressor:
        if payload.get("kind") != RIDGE_MODEL_KIND:
            raise ValueError(f"未知のモデル種別: {payload.get('kind')}")
        model = cls(alpha=float(payload["alpha"]))
        model.feature_names = list(payload["feature_names"])
        model.mean_ = np.asarray(payload["mean"], dtype=float)
        model.scale_ = np.asarray(payload["scale"], dtype=float)
        model.coef_ = np.asarray(payload["coef"], dtype=float)
        model.intercept_ = float(payload["intercept"])
        return model

    # ------------------------------------------------------------ 内部

    @staticmethod
    def _validate_fit_inputs(X: np.ndarray, y: np.ndarray) -> None:
        if X.ndim != 2:
            raise ValueError("X は2次元である必要があります")
        if X.shape[0] != y.shape[0]:
            raise ValueError(f"行数不一致: X={X.shape[0]}, y={y.shape[0]}")
        if X.shape[0] == 0:
            raise ValueError("学習データが空です")
        if not np.isfinite(X).all():
            raise ValueError("X に非有限値(NaN/Inf)が含まれます")
        if not np.isfinite(y).all():
            raise ValueError("y に非有限値(NaN/Inf)が含まれます")

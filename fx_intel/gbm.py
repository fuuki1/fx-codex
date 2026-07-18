"""純Python実装の勾配ブースティング決定木(GBDT)二値分類器。

LightGBM/XGBoostと同じアルゴリズム系統(Newtonブースティング+
ヒストグラム分割探索)を、外部依存ゼロで実装する。fx_intelはMac miniの
軽量venv(requests+tradingview-taのみ)で動かすため、ネイティブ拡張の
重い依存を持ち込まない。overfitting.py がscipy非依存で統計検定を
実装しているのと同じ設計判断。

対象データは「特徴量十数個 × 数百〜数千サンプル」の小規模テーブルで、
この規模ならヒストグラム法の純Python実装で数秒以内に学習が終わる。

アルゴリズム(XGBoost論文の定式化):

- 損失: 二値ロジスティック。勾配 g=p−y、ヘッセ h=p(1−p)
- 葉の値: −Σg/(Σh+λ)(Newtonステップ、L2正則化λ)
- 分割ゲイン: GL²/(HL+λ)+GR²/(HR+λ)−G²/(H+λ)
- 分割候補: 学習開始時に特徴量ごとの分位点(最大max_bins個)へ離散化し、
  ノードごとにヒストグラムを積んで走査する(LightGBM方式)
- 行サブサンプル/特徴量サブサンプルは seed 固定の擬似乱数で決定論的
- early stopping: 検証セットのloglossが改善しなくなったら打ち切り、
  最良イテレーションまでの木だけを残す

ミッションクリティカル要件:

- 決定論: 同じデータ・同じseedから必ず同じモデルが得られる
- 直列化: to_dict/from_dict でJSONに保存・復元でき、来歴を残せる
- 入力検証: 非有限値・クラス欠落・行列不整合は学習前に拒否する
"""

from __future__ import annotations

import math
import random
from bisect import bisect_right
from dataclasses import dataclass
from collections.abc import Mapping, Sequence

MAX_MARGIN = 30.0  # sigmoidのオーバーフロー防止
MIN_GAIN = 1e-6  # これ未満のゲインでは分割しない
LEAF_VALUE_CLIP = 4.0  # 葉の値の上限(1本の木が支配的になるのを防ぐ)
PROB_EPS = 1e-7


def _sigmoid(margin: float) -> float:
    clipped = max(-MAX_MARGIN, min(MAX_MARGIN, margin))
    return 1.0 / (1.0 + math.exp(-clipped))


def log_loss(labels: Sequence[int], probs: Sequence[float]) -> float:
    """平均二値クロスエントロピー。確率は[eps, 1-eps]にクリップ。"""
    if not labels:
        return float("nan")
    total = 0.0
    for y, p in zip(labels, probs):
        p = max(PROB_EPS, min(1.0 - PROB_EPS, p))
        total += -(y * math.log(p) + (1 - y) * math.log(1.0 - p))
    return total / len(labels)


def brier_score(labels: Sequence[int], probs: Sequence[float]) -> float:
    """平均二乗誤差(確率予測の総合精度。低いほど良い)。"""
    if not labels:
        return float("nan")
    return sum((p - y) ** 2 for y, p in zip(labels, probs)) / len(labels)


def rmse(labels: Sequence[float], preds: Sequence[float]) -> float:
    """平均二乗誤差の平方根(回帰の総合精度。低いほど良い)。"""
    if not labels:
        return float("nan")
    return math.sqrt(sum((p - y) ** 2 for y, p in zip(labels, preds)) / len(labels))


def pinball_loss(labels: Sequence[float], preds: Sequence[float], quantile: float) -> float:
    """平均ピンボール損失(分位点予測の精度。低いほど良い)。

    予測がquantile分位点に近いほど小さい。下振れを過小評価する予測(p<0.5で
    予測が高すぎる等)を非対称に罰する。分位点ヘッドの検証指標。
    """
    if not labels:
        return float("nan")
    total = 0.0
    for y, p in zip(labels, preds):
        delta = y - p
        total += max(quantile * delta, (quantile - 1.0) * delta)
    return total / len(labels)


def _quantile_cuts(values: Sequence[float], max_bins: int) -> list[float]:
    """特徴量1本の分割候補(分位点、重複除去済み・昇順)を返す。"""
    unique = sorted(set(values))
    if len(unique) <= 1:
        return []
    if len(unique) <= max_bins:
        # 隣接ユニーク値の中点を候補にする
        return [(unique[i] + unique[i + 1]) / 2.0 for i in range(len(unique) - 1)]
    cuts: list[float] = []
    ordered = sorted(values)
    for step in range(1, max_bins):
        index = int(len(ordered) * step / max_bins)
        candidate = ordered[min(index, len(ordered) - 1)]
        if not cuts or candidate > cuts[-1]:
            cuts.append(candidate)
    return cuts


@dataclass
class _FitContext:
    """学習中に共有する前処理済みデータ。"""

    binned: list[list[int]]  # [feature][row] -> bin index
    cuts: list[list[float]]  # [feature] -> 分割候補(閾値)
    gradients: list[float]
    hessians: list[float]


class _TreeBooster:
    """勾配ブースティングの木成長ロジック(損失非依存)。

    分類器・回帰器で共有する。gradients/hessians だけを見て木を育て、葉値は
    Newtonステップ -Σg/(Σh+λ) で決める。損失固有の処理(初期マージン・毎反復の
    gradient/hessian・予測変換・検証指標)はサブクラスが与える。ハイパラ属性
    (max_depth 等)と feature_importance_ はサブクラスの __init__ が設定する。
    """

    # サブクラスの __init__ が設定する属性(型チェッカ向けの宣言)
    max_depth: int
    min_samples_leaf: int
    min_child_hessian: float
    reg_lambda: float
    feature_importance_: dict[int, float]

    def _grow_tree(
        self, context: _FitContext, rows: list[int], columns: list[int], depth: int
    ) -> dict:
        grad_sum = sum(context.gradients[i] for i in rows)
        hess_sum = sum(context.hessians[i] for i in rows)
        leaf = self._leaf(grad_sum, hess_sum)
        if depth >= self.max_depth or len(rows) < 2 * self.min_samples_leaf:
            return leaf

        best_gain = MIN_GAIN
        best: tuple[int, int] | None = None  # (feature, split_bin)
        parent_score = grad_sum * grad_sum / (hess_sum + self.reg_lambda)
        for f in columns:
            cuts = context.cuts[f]
            if not cuts:
                continue
            n_bins = len(cuts) + 1
            hist_g = [0.0] * n_bins
            hist_h = [0.0] * n_bins
            hist_n = [0] * n_bins
            binned_f = context.binned[f]
            for i in rows:
                b = binned_f[i]
                hist_g[b] += context.gradients[i]
                hist_h[b] += context.hessians[i]
                hist_n[b] += 1
            left_g = left_h = 0.0
            left_n = 0
            for b in range(n_bins - 1):  # 最後のビンで割ると右が空
                left_g += hist_g[b]
                left_h += hist_h[b]
                left_n += hist_n[b]
                right_n = len(rows) - left_n
                if left_n < self.min_samples_leaf or right_n < self.min_samples_leaf:
                    continue
                right_g = grad_sum - left_g
                right_h = hess_sum - left_h
                if left_h < self.min_child_hessian or right_h < self.min_child_hessian:
                    continue
                gain = (
                    left_g * left_g / (left_h + self.reg_lambda)
                    + right_g * right_g / (right_h + self.reg_lambda)
                    - parent_score
                )
                if gain > best_gain:
                    best_gain = gain
                    best = (f, b)
        if best is None:
            return leaf

        feature, split_bin = best
        threshold = context.cuts[feature][split_bin]
        binned_f = context.binned[feature]
        left_rows = [i for i in rows if binned_f[i] <= split_bin]
        right_rows = [i for i in rows if binned_f[i] > split_bin]
        self.feature_importance_[feature] = self.feature_importance_.get(feature, 0.0) + best_gain
        return {
            "f": feature,
            "t": threshold,
            "b": split_bin,
            "l": self._grow_tree(context, left_rows, columns, depth + 1),
            "r": self._grow_tree(context, right_rows, columns, depth + 1),
        }

    def _leaf(self, grad_sum: float, hess_sum: float) -> dict:
        value = -grad_sum / (hess_sum + self.reg_lambda)
        value = max(-LEAF_VALUE_CLIP, min(LEAF_VALUE_CLIP, value))
        return {"v": value}

    def _predict_tree_binned(self, tree: dict, context: _FitContext, row: int) -> float:
        node = tree
        while "v" not in node:
            node = node["l"] if context.binned[node["f"]][row] <= node["b"] else node["r"]
        return node["v"]


class GradientBoostingClassifier(_TreeBooster):
    """依存ゼロのGBDT二値分類器(fit → predict_proba → to_dict/from_dict)。"""

    def __init__(
        self,
        n_estimators: int = 200,
        learning_rate: float = 0.05,
        max_depth: int = 3,
        min_samples_leaf: int = 20,
        min_child_hessian: float = 1.0,
        subsample: float = 0.8,
        feature_fraction: float = 0.9,
        reg_lambda: float = 1.0,
        max_bins: int = 32,
        early_stopping_rounds: int = 30,
        seed: int = 7,
    ) -> None:
        if not (0.0 < learning_rate <= 1.0):
            raise ValueError("learning_rate は (0, 1] で指定する")
        if not (0.0 < subsample <= 1.0) or not (0.0 < feature_fraction <= 1.0):
            raise ValueError("subsample / feature_fraction は (0, 1] で指定する")
        self.n_estimators = n_estimators
        self.learning_rate = learning_rate
        self.max_depth = max_depth
        self.min_samples_leaf = min_samples_leaf
        self.min_child_hessian = min_child_hessian
        self.subsample = subsample
        self.feature_fraction = feature_fraction
        self.reg_lambda = reg_lambda
        self.max_bins = max_bins
        self.early_stopping_rounds = early_stopping_rounds
        self.seed = seed

        self.base_margin_: float = 0.0
        self.trees_: list[dict] = []
        self.best_iteration_: int = 0
        self.feature_importance_: dict[int, float] = {}
        self.train_logloss_: list[float] = []
        self.valid_logloss_: list[float] = []

    # ---------------------------------------------------------------- 学習

    def fit(
        self,
        features: Sequence[Sequence[float]],
        labels: Sequence[int],
        eval_features: Sequence[Sequence[float]] | None = None,
        eval_labels: Sequence[int] | None = None,
    ) -> GradientBoostingClassifier:
        self._validate_inputs(features, labels)
        n_rows = len(features)
        n_features = len(features[0])
        rng = random.Random(self.seed)

        # 前処理: 分割候補の決定と全行のビン化(学習中は不変)
        cuts = [
            _quantile_cuts([row[f] for row in features], self.max_bins) for f in range(n_features)
        ]
        binned = [[bisect_right(cuts[f], row[f]) for row in features] for f in range(n_features)]

        positive = sum(labels)
        base_rate = max(PROB_EPS, min(1.0 - PROB_EPS, positive / n_rows))
        self.base_margin_ = math.log(base_rate / (1.0 - base_rate))
        margins = [self.base_margin_] * n_rows
        eval_margins = [self.base_margin_] * len(eval_features) if eval_features else None

        self.trees_ = []
        self.feature_importance_ = {}
        self.train_logloss_ = []
        self.valid_logloss_ = []
        best_valid = float("inf")
        best_iteration = 0

        context = _FitContext(binned=binned, cuts=cuts, gradients=[], hessians=[])
        all_rows = list(range(n_rows))
        all_features = list(range(n_features))
        sample_size = max(self.min_samples_leaf * 2, int(round(n_rows * self.subsample)))
        sample_size = min(sample_size, n_rows)
        feature_size = max(1, int(round(n_features * self.feature_fraction)))

        for iteration in range(self.n_estimators):
            probs = [_sigmoid(m) for m in margins]
            context.gradients = [p - y for p, y in zip(probs, labels)]
            context.hessians = [max(p * (1.0 - p), 1e-6) for p in probs]

            rows = sorted(rng.sample(all_rows, sample_size)) if sample_size < n_rows else all_rows
            columns = (
                sorted(rng.sample(all_features, feature_size))
                if feature_size < n_features
                else all_features
            )
            tree = self._grow_tree(context, rows, columns, depth=0)
            self.trees_.append(tree)

            for i in range(n_rows):
                margins[i] += self.learning_rate * self._predict_tree_binned(tree, context, i)
            self.train_logloss_.append(log_loss(labels, [_sigmoid(m) for m in margins]))

            if eval_features and eval_labels is not None and eval_margins is not None:
                for i, row in enumerate(eval_features):
                    eval_margins[i] += self.learning_rate * _predict_tree_raw(tree, row)
                valid_loss = log_loss(eval_labels, [_sigmoid(m) for m in eval_margins])
                self.valid_logloss_.append(valid_loss)
                if valid_loss < best_valid - 1e-9:
                    best_valid = valid_loss
                    best_iteration = iteration + 1
                elif iteration + 1 - best_iteration >= self.early_stopping_rounds:
                    break
            else:
                best_iteration = iteration + 1

        self.best_iteration_ = max(1, best_iteration)
        self.trees_ = self.trees_[: self.best_iteration_]
        return self

    def _validate_inputs(self, features: Sequence[Sequence[float]], labels: Sequence[int]) -> None:
        if not features or not labels or len(features) != len(labels):
            raise ValueError("特徴量とラベルの行数が不一致または空")
        width = len(features[0])
        if width == 0:
            raise ValueError("特徴量が0列")
        for row in features:
            if len(row) != width:
                raise ValueError("特徴量の列数が行によって異なる")
            for value in row:
                if not math.isfinite(value):
                    raise ValueError("特徴量に非有限値(NaN/inf)が含まれる")
        classes = set(labels)
        if not classes <= {0, 1}:
            raise ValueError("ラベルは0/1のみ")
        if len(classes) < 2:
            raise ValueError("片方のクラスしか存在しないため学習できない")

    # ---------------------------------------------------------------- 予測

    def predict_margin(self, row: Sequence[float]) -> float:
        margin = self.base_margin_
        for tree in self.trees_:
            margin += self.learning_rate * _predict_tree_raw(tree, row)
        return margin

    def predict_proba(self, row: Sequence[float]) -> float:
        """1行の陽性クラス確率。"""
        return _sigmoid(self.predict_margin(row))

    def predict_proba_many(self, rows: Sequence[Sequence[float]]) -> list[float]:
        return [self.predict_proba(row) for row in rows]

    # ---------------------------------------------------------------- 直列化

    def to_dict(self) -> dict:
        return {
            "algorithm": "gbdt_logistic",
            "params": {
                "n_estimators": self.n_estimators,
                "learning_rate": self.learning_rate,
                "max_depth": self.max_depth,
                "min_samples_leaf": self.min_samples_leaf,
                "subsample": self.subsample,
                "feature_fraction": self.feature_fraction,
                "reg_lambda": self.reg_lambda,
                "max_bins": self.max_bins,
                "seed": self.seed,
            },
            "base_margin": self.base_margin_,
            "best_iteration": self.best_iteration_,
            "trees": self.trees_,
            "feature_importance": {str(k): v for k, v in self.feature_importance_.items()},
        }

    @classmethod
    def from_dict(cls, payload: Mapping) -> GradientBoostingClassifier:
        params = dict(payload.get("params", {}))
        model = cls(
            n_estimators=int(params.get("n_estimators", 200)),
            learning_rate=float(params.get("learning_rate", 0.05)),
            max_depth=int(params.get("max_depth", 3)),
            min_samples_leaf=int(params.get("min_samples_leaf", 20)),
            subsample=float(params.get("subsample", 0.8)),
            feature_fraction=float(params.get("feature_fraction", 0.9)),
            reg_lambda=float(params.get("reg_lambda", 1.0)),
            max_bins=int(params.get("max_bins", 32)),
            seed=int(params.get("seed", 7)),
        )
        model.base_margin_ = float(payload.get("base_margin", 0.0))
        model.best_iteration_ = int(payload.get("best_iteration", 0))
        trees = payload.get("trees", [])
        if not isinstance(trees, list):
            raise ValueError("trees の形式が不正")
        model.trees_ = [_validate_tree(tree) for tree in trees]
        importance = payload.get("feature_importance", {})
        if isinstance(importance, Mapping):
            model.feature_importance_ = {int(k): float(v) for k, v in importance.items()}
        return model


# 回帰の葉値・予測はRスケール(±数R)なので、確率マージン用の狭いクリップでは足りない。
REGRESSION_VALUE_CLIP = 20.0  # 葉値/予測の上限(1本の木が支配的になるのを防ぐ、R単位)


def _weighted_quantile_start(labels: Sequence[float], quantile: float) -> float:
    """分位点回帰の初期予測: ラベルの経験分位点(ソートして線形補間)。"""
    ordered = sorted(labels)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    pos = quantile * (len(ordered) - 1)
    low = int(math.floor(pos))
    high = min(low + 1, len(ordered) - 1)
    frac = pos - low
    return ordered[low] * (1.0 - frac) + ordered[high] * frac


class GradientBoostingRegressor(_TreeBooster):
    """依存ゼロのGBDT回帰器(二乗誤差 または ピンボール損失/分位点)。

    分類器と同じ木成長(_TreeBooster)を使い、損失だけ差し替える。収益ラベル
    realized_net_r を教師に、期待R(objective="squared")と分位点(objective=
    "quantile", quantile=q)を学習する。予測は生マージン(確率変換なし)で、
    Rスケールに合わせて REGRESSION_VALUE_CLIP でクリップする。
    """

    def __init__(
        self,
        objective: str = "squared",
        quantile: float = 0.5,
        n_estimators: int = 200,
        learning_rate: float = 0.05,
        max_depth: int = 3,
        min_samples_leaf: int = 20,
        min_child_hessian: float = 1.0,
        subsample: float = 0.8,
        feature_fraction: float = 0.9,
        reg_lambda: float = 1.0,
        max_bins: int = 32,
        early_stopping_rounds: int = 30,
        seed: int = 7,
    ) -> None:
        if objective not in ("squared", "quantile"):
            raise ValueError("objective は 'squared' か 'quantile'")
        if objective == "quantile" and not (0.0 < quantile < 1.0):
            raise ValueError("quantile は (0, 1) で指定する")
        if not (0.0 < learning_rate <= 1.0):
            raise ValueError("learning_rate は (0, 1] で指定する")
        if not (0.0 < subsample <= 1.0) or not (0.0 < feature_fraction <= 1.0):
            raise ValueError("subsample / feature_fraction は (0, 1] で指定する")
        self.objective = objective
        self.quantile = quantile
        self.n_estimators = n_estimators
        self.learning_rate = learning_rate
        self.max_depth = max_depth
        self.min_samples_leaf = min_samples_leaf
        self.min_child_hessian = min_child_hessian
        self.subsample = subsample
        self.feature_fraction = feature_fraction
        self.reg_lambda = reg_lambda
        self.max_bins = max_bins
        self.early_stopping_rounds = early_stopping_rounds
        self.seed = seed

        self.base_prediction_: float = 0.0
        self.trees_: list[dict] = []
        self.best_iteration_: int = 0
        self.feature_importance_: dict[int, float] = {}
        self.train_loss_: list[float] = []
        self.valid_loss_: list[float] = []

    def _leaf(self, grad_sum: float, hess_sum: float) -> dict:
        # 回帰はRスケールなので確率用の狭いクリップ(LEAF_VALUE_CLIP)ではなく広い方を使う
        value = -grad_sum / (hess_sum + self.reg_lambda)
        value = max(-REGRESSION_VALUE_CLIP, min(REGRESSION_VALUE_CLIP, value))
        return {"v": value}

    def _grad_hess(self, preds: Sequence[float], labels: Sequence[float]) -> tuple[list, list]:
        """損失別の勾配・ヘシアン。二乗誤差は g=pred-y,h=1。ピンボールは劣勾配。"""
        if self.objective == "squared":
            gradients = [p - y for p, y in zip(preds, labels)]
            hessians = [1.0] * len(preds)
        else:  # quantile (pinball): g = -(q - 1[y<=pred]) = 1[y<=pred] - q
            q = self.quantile
            gradients = [(1.0 if y <= p else 0.0) - q for p, y in zip(preds, labels)]
            hessians = [1.0] * len(preds)
        return gradients, hessians

    def _loss(self, labels: Sequence[float], preds: Sequence[float]) -> float:
        if self.objective == "squared":
            return rmse(labels, preds)
        return pinball_loss(labels, preds, self.quantile)

    def fit(
        self,
        features: Sequence[Sequence[float]],
        labels: Sequence[float],
        eval_features: Sequence[Sequence[float]] | None = None,
        eval_labels: Sequence[float] | None = None,
    ) -> GradientBoostingRegressor:
        if not features or not labels or len(features) != len(labels):
            raise ValueError("特徴量とラベルの行数が不一致または空")
        width = len(features[0])
        if width == 0:
            raise ValueError("特徴量が0列")
        for row in features:
            if len(row) != width:
                raise ValueError("特徴量の列数が行によって異なる")
            for value in row:
                if not math.isfinite(value):
                    raise ValueError("特徴量に非有限値(NaN/inf)が含まれる")
        for y in labels:
            if not math.isfinite(y):
                raise ValueError("ラベルに非有限値(NaN/inf)が含まれる")

        n_rows = len(features)
        n_features = width
        rng = random.Random(self.seed)
        cuts = [
            _quantile_cuts([row[f] for row in features], self.max_bins) for f in range(n_features)
        ]
        binned = [[bisect_right(cuts[f], row[f]) for row in features] for f in range(n_features)]

        if self.objective == "squared":
            self.base_prediction_ = sum(labels) / n_rows
        else:
            self.base_prediction_ = _weighted_quantile_start(labels, self.quantile)
        preds = [self.base_prediction_] * n_rows
        eval_preds = [self.base_prediction_] * len(eval_features) if eval_features else None

        self.trees_ = []
        self.feature_importance_ = {}
        self.train_loss_ = []
        self.valid_loss_ = []
        best_valid = float("inf")
        best_iteration = 0

        context = _FitContext(binned=binned, cuts=cuts, gradients=[], hessians=[])
        all_rows = list(range(n_rows))
        all_features = list(range(n_features))
        sample_size = max(self.min_samples_leaf * 2, int(round(n_rows * self.subsample)))
        sample_size = min(sample_size, n_rows)
        feature_size = max(1, int(round(n_features * self.feature_fraction)))

        for iteration in range(self.n_estimators):
            context.gradients, context.hessians = self._grad_hess(preds, labels)
            rows = sorted(rng.sample(all_rows, sample_size)) if sample_size < n_rows else all_rows
            columns = (
                sorted(rng.sample(all_features, feature_size))
                if feature_size < n_features
                else all_features
            )
            tree = self._grow_tree(context, rows, columns, depth=0)
            self.trees_.append(tree)

            for i in range(n_rows):
                preds[i] += self.learning_rate * self._predict_tree_binned(tree, context, i)
            self.train_loss_.append(self._loss(labels, preds))

            if eval_features and eval_labels is not None and eval_preds is not None:
                for i, row in enumerate(eval_features):
                    eval_preds[i] += self.learning_rate * _predict_tree_raw(tree, row)
                valid_loss = self._loss(eval_labels, eval_preds)
                self.valid_loss_.append(valid_loss)
                if valid_loss < best_valid - 1e-9:
                    best_valid = valid_loss
                    best_iteration = iteration + 1
                elif iteration + 1 - best_iteration >= self.early_stopping_rounds:
                    break
            else:
                best_iteration = iteration + 1

        self.best_iteration_ = max(1, best_iteration)
        self.trees_ = self.trees_[: self.best_iteration_]
        return self

    def predict(self, row: Sequence[float]) -> float:
        value = self.base_prediction_
        for tree in self.trees_:
            value += self.learning_rate * _predict_tree_raw(tree, row)
        return max(-REGRESSION_VALUE_CLIP, min(REGRESSION_VALUE_CLIP, value))

    def predict_many(self, rows: Sequence[Sequence[float]]) -> list[float]:
        return [self.predict(row) for row in rows]

    def to_dict(self) -> dict:
        return {
            "algorithm": "gbdt_regressor",
            "objective": self.objective,
            "quantile": self.quantile,
            "params": {
                "n_estimators": self.n_estimators,
                "learning_rate": self.learning_rate,
                "max_depth": self.max_depth,
                "min_samples_leaf": self.min_samples_leaf,
                "subsample": self.subsample,
                "feature_fraction": self.feature_fraction,
                "reg_lambda": self.reg_lambda,
                "max_bins": self.max_bins,
                "seed": self.seed,
            },
            "base_prediction": self.base_prediction_,
            "best_iteration": self.best_iteration_,
            "trees": self.trees_,
            "feature_importance": {str(k): v for k, v in self.feature_importance_.items()},
        }

    @classmethod
    def from_dict(cls, payload: Mapping) -> GradientBoostingRegressor:
        params = dict(payload.get("params", {}))
        model = cls(
            objective=str(payload.get("objective", "squared")),
            quantile=float(payload.get("quantile", 0.5)),
            n_estimators=int(params.get("n_estimators", 200)),
            learning_rate=float(params.get("learning_rate", 0.05)),
            max_depth=int(params.get("max_depth", 3)),
            min_samples_leaf=int(params.get("min_samples_leaf", 20)),
            subsample=float(params.get("subsample", 0.8)),
            feature_fraction=float(params.get("feature_fraction", 0.9)),
            reg_lambda=float(params.get("reg_lambda", 1.0)),
            max_bins=int(params.get("max_bins", 32)),
            seed=int(params.get("seed", 7)),
        )
        model.base_prediction_ = float(payload.get("base_prediction", 0.0))
        model.best_iteration_ = int(payload.get("best_iteration", 0))
        trees = payload.get("trees", [])
        if not isinstance(trees, list):
            raise ValueError("trees の形式が不正")
        model.trees_ = [_validate_tree(tree) for tree in trees]
        importance = payload.get("feature_importance", {})
        if isinstance(importance, Mapping):
            model.feature_importance_ = {int(k): float(v) for k, v in importance.items()}
        return model


def _predict_tree_raw(tree: dict, row: Sequence[float]) -> float:
    """生の特徴量ベクトルで木を辿る(予測経路。ビン化不要)。"""
    node = tree
    while "v" not in node:
        node = node["l"] if row[node["f"]] <= node["t"] else node["r"]
    return node["v"]


def _validate_tree(node: object) -> dict:
    """直列化された木の構造を検証する(壊れたモデルの読み込み事故防止)。"""
    if not isinstance(node, Mapping):
        raise ValueError("木ノードがdictでない")
    if "v" in node:
        value = float(node["v"])
        if not math.isfinite(value):
            raise ValueError("葉の値が非有限")
        return {"v": value}
    for key in ("f", "t", "l", "r"):
        if key not in node:
            raise ValueError(f"分岐ノードに {key} が無い")
    threshold = float(node["t"])
    if not math.isfinite(threshold):
        raise ValueError("分岐の閾値が非有限")
    return {
        "f": int(node["f"]),
        "t": threshold,
        "b": int(node.get("b", 0)),
        "l": _validate_tree(node["l"]),
        "r": _validate_tree(node["r"]),
    }


@dataclass
class CalibrationResult:
    """Plattスケーリング(sigmoid(a·margin+b))の較正パラメータ。"""

    scale: float = 1.0
    offset: float = 0.0
    iterations: int = 0

    def apply(self, margin: float) -> float:
        return _sigmoid(self.scale * margin + self.offset)


def platt_calibrate(
    margins: Sequence[float],
    labels: Sequence[int],
    iterations: int = 200,
    learning_rate: float = 0.1,
) -> CalibrationResult:
    """検証セットのマージンからPlattスケーリングを勾配降下で当てはめる。

    予測確率が実際の的中率と一致するように(確信度の誇張・過小を補正)、
    p = sigmoid(a·margin + b) の a, b をloglossで最適化する。決定論的。
    """
    if not margins or len(margins) != len(labels) or len(set(labels)) < 2:
        return CalibrationResult()
    scale, offset = 1.0, 0.0
    n = len(margins)
    for step in range(iterations):
        grad_a = grad_b = 0.0
        for margin, y in zip(margins, labels):
            p = _sigmoid(scale * margin + offset)
            grad_a += (p - y) * margin
            grad_b += p - y
        scale -= learning_rate * grad_a / n
        offset -= learning_rate * grad_b / n
        # スケールが負(順位が反転)まで行くのは較正ではなく崩壊なので止める
        if scale <= 0.0:
            return CalibrationResult()
    return CalibrationResult(scale=round(scale, 6), offset=round(offset, 6), iterations=iterations)

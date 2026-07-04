"""メタラベリング戦略 — 一次モデルが方向、二次モデルが張る/見送るを決める。

レポート(FX AI.md)ギャップ②の合成。labeling.py の3プリミティブ
(分数次差分・トリプルバリア・メタラベリング)を組み合わせ、López de Prado の
定石「一次=方向、二次=サイズ(張るか否か)」をバックテスト戦略として実装する。

ai_logistic との違い:

- ラベル: ai_logistic は「次足が上がったか」の単純方向。本戦略は一次シグナル
  (MAクロス)の方向に対しトリプルバリアで「利確に届いたか(=張って正解か)」を
  メタラベルにする。損切/時間切れを織り込んだパス依存の教師信号。
- 特徴量: ai_logistic は生リターン(整数次差分で記憶を消しがち)。本戦略は主要な
  価格特徴を分数次差分(FFD, d指定)で定常化しつつ記憶を保持する。
- 出力: 二次モデルの P(張るべき) がゲート閾値(meta_threshold)を超えたときだけ
  一次方向にエントリ。超えなければ position=0(見送り)。これがサイズ判断
  (張る=1 / 見送る=0)に相当する。

すべて確定済みの過去データだけで各時点を判断するローリング学習(リーク防止)。
ネットワーク非依存で、既存の CLI/バックテスターにそのまま載る。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import pandas as pd

from fx_backtester.indicators import average_true_range, rsi, sma
from fx_backtester.labeling import (
    cusum_filter,
    frac_diff_ffd,
    meta_labels,
    triple_barrier_labels,
)
from fx_backtester.strategies.ai_logistic import (
    _fit_logistic,
    _filled_finite_frame,
    _finite_frame,
    _sigmoid,
)
from fx_backtester.strategies.base import Strategy

# 二次モデルの共通契約: 1行の特徴量DataFrame → P(張るべき) の float
_MetaPredictor = Callable[[pd.DataFrame], float]


@dataclass
class AIMetaLabeledStrategy(Strategy):
    """一次=MAクロス方向、二次=トリプルバリアのメタラベルを学習するゲート。"""

    fast_window: int = 12
    slow_window: int = 48
    frac_diff_d: float = 0.4  # 分数次差分の次数(0<d<1、López de Prado推奨帯)
    # FFDの重み打ち切り閾値。小さいほど窓が広く記憶を残すが、窓が数百〜数千本に
    # なると現実的なFX足長では系列がほぼNaNになる。1e-3で窓≈50本に収め、
    # 実用的な記憶保持と学習可能性を両立する(必要なら下げて記憶を伸ばす)。
    frac_diff_threshold: float = 1e-3
    upper_multiple: float = 2.0  # 上バリア = σ × これ(利確)
    lower_multiple: float = 2.0  # 下バリア = σ × これ(損切)
    vertical_bars: int = 24  # 垂直バリア(時間切れ)までの足数
    # CUSUMイベントサンプリング(既定OFF)。ONだと「一次方向が出た全バー」ではなく
    # 「累積変化がσ×cusum_multipleを超えた点」だけをエントリ候補にし、自己相関で
    # 実効サンプルを過大評価するのを避ける(López de Prado の定石)。
    use_cusum_events: bool = False
    cusum_multiple: float = 1.0  # CUSUM閾値 = σ(volatility_window) × これ
    volatility_window: int = 20
    rsi_window: int = 14
    atr_window: int = 14
    min_train_bars: int = 200
    retrain_interval: int = 24
    learning_rate: float = 0.08
    epochs: int = 160
    l2: float = 0.001
    meta_threshold: float = 0.55  # 二次 P(張るべき) がこれ以上で一次方向にエントリ
    stop_atr_multiple: float = 2.0
    # 二次モデルの選択: "logistic"(既定・依存ゼロの自前ロジスティック)か
    # "gbdt"(レポートが実務的王者とするGBDT=fx_intel.gbm。Newtonブースティング+
    # ヒストグラム分割の純Python実装)。GBDTは非線形・特徴量交互作用を拾える。
    secondary_model: str = "logistic"
    gbdt_n_estimators: int = 120
    gbdt_max_depth: int = 3
    gbdt_learning_rate: float = 0.1

    @property
    def name(self) -> str:
        return "ai_meta_labeled"

    def generate(self, symbol: str, data: pd.DataFrame) -> pd.DataFrame:
        self._validate_params()
        close = data["close"].astype(float)

        # --- 一次モデル: MAクロスの方向(+1/-1)。0(未確定)は張らない ---
        side = self._primary_side(close)

        # --- トリプルバリア → メタラベル(side方向で利確に届いたか) ---
        volatility = close.pct_change().rolling(
            self.volatility_window, min_periods=self.volatility_window
        ).std()
        # イベント点: 既定は「一次方向が出た全バー」。CUSUM ON なら累積変化が
        # σ×cusum_multiple を超えた点だけに絞り(自己相関の間引き)、さらに一次方向が
        # 出ている点との積を取る(方向の無い点にはトリプルバリアを張れないため)。
        directional = side.index[side != 0]
        if self.use_cusum_events:
            cusum_threshold = (volatility * self.cusum_multiple).dropna()
            cusum_events = cusum_filter(close, cusum_threshold)
            event_index = cusum_events.intersection(directional)
        else:
            event_index = directional
        barriers = triple_barrier_labels(
            close,
            events_index=event_index,
            upper_multiple=self.upper_multiple,
            lower_multiple=self.lower_multiple,
            vertical_bars=self.vertical_bars,
            volatility=volatility,
            side=side,
        )
        meta_y = meta_labels(barriers)  # index=イベント点、値=0/1

        # --- 二次モデルの特徴量(分数次差分で定常化+記憶保持) ---
        features = self._features(data)

        target = pd.Series(0, index=data.index, dtype=int)
        meta_prob = pd.Series(np.nan, index=data.index, dtype=float)
        model_ready = pd.Series(False, index=data.index, dtype=bool)
        train_rows = pd.Series(0, index=data.index, dtype=int)

        # メタラベルが確定するのは touch_ts(バリア到達)時点。学習に使えるのは
        # 「判断時点までに touch 済み」のイベントだけ(リーク防止)。
        touch_ts = barriers["touch_ts"]
        positions = {ts: i for i, ts in enumerate(data.index)}

        # 二次モデルは「特徴量→P(張るべき)」を返す予測器。ロジスティックとGBDTの
        # どちらでも同じ契約(fit→predict closure)にして学習ループをモデル非依存にする。
        predictor: _MetaPredictor | None = None
        last_train_position: int | None = None

        for position, timestamp in enumerate(data.index):
            if side.iloc[position] == 0:
                continue
            if not bool(features.iloc[position].notna().all()):
                continue

            # touch_ts が現時点以前に確定しているイベントだけを学習に使う
            matured = touch_ts[touch_ts.map(lambda t: positions.get(t, len(data)) < position)]
            train_index = matured.index.intersection(meta_y.index)
            train_index = train_index[
                features.reindex(train_index).notna().all(axis=1)
            ]
            train_count = len(train_index)
            train_rows.at[timestamp] = train_count
            if train_count < self.min_train_bars:
                continue

            should_retrain = (
                predictor is None
                or last_train_position is None
                or position - last_train_position >= self.retrain_interval
            )
            if should_retrain:
                fitted = self._fit_secondary(
                    features.loc[train_index], meta_y.loc[train_index].astype(float)
                )
                if fitted is None:
                    continue
                predictor = fitted
                last_train_position = position

            if predictor is None:
                continue
            probability = predictor(features.loc[[timestamp]])
            meta_prob.at[timestamp] = probability
            model_ready.at[timestamp] = True
            # 二次が「張るべき」と言ったときだけ一次方向にエントリ(=サイズ1)
            if probability >= self.meta_threshold:
                target.at[timestamp] = int(side.iloc[position])

        atr = average_true_range(data, self.atr_window)
        stop_distance = atr * self.stop_atr_multiple
        return self._validated_output(
            data,
            pd.DataFrame(
                {
                    "target_position": target,
                    "stop_distance": stop_distance,
                    "primary_side": side,
                    "meta_probability": meta_prob,
                    "meta_model_ready": model_ready,
                    "meta_train_rows": train_rows,
                },
                index=data.index,
            ),
        )

    def _primary_side(self, close: pd.Series) -> pd.Series:
        """一次モデル: fast>slow でロング(+1)、fast<slow でショート(-1)、未確定0。"""
        fast = sma(close, self.fast_window)
        slow = sma(close, self.slow_window)
        side = pd.Series(0, index=close.index, dtype=int)
        side[fast > slow] = 1
        side[fast < slow] = -1
        side[fast.isna() | slow.isna()] = 0
        return side

    def _fit_secondary(
        self, train_features: pd.DataFrame, train_labels: pd.Series
    ) -> _MetaPredictor | None:
        """二次モデルを学習し、1行の特徴量→P(張るべき) を返す予測器を作る。

        secondary_model="logistic" は既存の依存ゼロロジスティック、"gbdt" は
        fx_intel.gbm の勾配ブースティング木。学習不能(片方のラベルしか無い等)は None。
        """
        if self.secondary_model == "logistic":
            fitted = _fit_logistic(
                train_features, train_labels,
                learning_rate=self.learning_rate, epochs=self.epochs, l2=self.l2,
            )
            if fitted is None:
                return None
            weights, mean, std = fitted

            def predict_logistic(row: pd.DataFrame) -> float:
                transformed = _filled_finite_frame((row - mean) / std)
                x = np.concatenate(([1.0], transformed.iloc[0].to_numpy(dtype=float)))
                return float(_sigmoid(np.array([x @ weights]))[0])

            return predict_logistic

        # GBDT: fx_intel.gbm(標準ライブラリのみ)。fx_intel→fx_backtester の
        # 逆依存は無いため、この一方向リーフ import は循環を作らない。
        from fx_intel.gbm import GradientBoostingClassifier

        labels = [int(v) for v in train_labels.to_list()]
        if len(set(labels)) < 2:
            return None  # 片方のクラスしか無いと学習できない
        rows = _filled_finite_frame(train_features).to_numpy(dtype=float).tolist()
        model = GradientBoostingClassifier(
            n_estimators=self.gbdt_n_estimators,
            max_depth=self.gbdt_max_depth,
            learning_rate=self.gbdt_learning_rate,
            seed=0,
        )
        model.fit(rows, labels)

        def predict_gbdt(row: pd.DataFrame) -> float:
            x = _filled_finite_frame(row).iloc[0].to_numpy(dtype=float).tolist()
            return float(model.predict_proba(x))

        return predict_gbdt

    def _features(self, data: pd.DataFrame) -> pd.DataFrame:
        """二次モデルの特徴量。価格系はFFDで定常化しつつ記憶を保持する。"""
        close = data["close"].astype(float)
        high = data["high"].astype(float)
        low = data["low"].astype(float)
        atr = average_true_range(data, self.atr_window)

        output = pd.DataFrame(index=data.index)
        # 分数次差分(記憶を残した定常系列)。log価格に当てるのが定石。
        log_close = np.log(close.replace(0, pd.NA))
        output["ffd_close"] = frac_diff_ffd(log_close, self.frac_diff_d, self.frac_diff_threshold)
        output["ffd_atr"] = frac_diff_ffd(
            atr.replace(0, pd.NA), self.frac_diff_d, self.frac_diff_threshold
        )
        # 方向を持つ既存の当てにできる特徴も併用
        output["fast_slow_gap"] = sma(close, self.fast_window) / sma(close, self.slow_window) - 1
        output["rsi_scaled"] = (rsi(close, self.rsi_window) - 50) / 50
        output["atr_pct"] = atr / close
        output["range_pct"] = (high - low) / close.replace(0, pd.NA)
        output["volatility"] = close.pct_change().rolling(
            self.volatility_window, min_periods=self.volatility_window
        ).std()
        return _finite_frame(output)

    def _validate_params(self) -> None:
        if not 0.0 < self.frac_diff_d < 1.0:
            raise ValueError("frac_diff_d must satisfy 0 < d < 1")
        if self.frac_diff_threshold <= 0:
            raise ValueError("frac_diff_threshold must be positive")
        if self.fast_window >= self.slow_window:
            raise ValueError("fast_window must be smaller than slow_window")
        if self.upper_multiple <= 0 or self.lower_multiple <= 0:
            raise ValueError("barrier multiples must be positive")
        if self.vertical_bars <= 1:
            raise ValueError("vertical_bars must be > 1")
        if self.use_cusum_events and self.cusum_multiple <= 0:
            raise ValueError("cusum_multiple must be positive when use_cusum_events is on")
        if self.min_train_bars <= 20:
            raise ValueError("min_train_bars must be > 20")
        if self.retrain_interval <= 0:
            raise ValueError("retrain_interval must be positive")
        if not 0.5 <= self.meta_threshold < 1.0:
            raise ValueError("meta_threshold must satisfy 0.5 <= t < 1")
        if self.secondary_model not in ("logistic", "gbdt"):
            raise ValueError("secondary_model must be 'logistic' or 'gbdt'")
        if self.secondary_model == "gbdt":
            if self.gbdt_n_estimators <= 0 or self.gbdt_max_depth <= 0:
                raise ValueError("gbdt_n_estimators/gbdt_max_depth must be positive")
            if self.gbdt_learning_rate <= 0:
                raise ValueError("gbdt_learning_rate must be positive")
        for name, value in (
            ("volatility_window", self.volatility_window),
            ("rsi_window", self.rsi_window),
            ("atr_window", self.atr_window),
        ):
            if value <= 1:
                raise ValueError(f"{name} must be > 1")
        if self.stop_atr_multiple <= 0:
            raise ValueError("stop_atr_multiple must be positive")
        if self.learning_rate <= 0 or self.epochs <= 0 or self.l2 < 0:
            raise ValueError("learning_rate/epochs must be positive and l2 >= 0")

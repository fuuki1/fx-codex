"""履歴コーパスで測ったセル間分散の上限を、学習ループの収縮に渡す。

`learning.derive_profile` の状態×方向セル学習は、セルごとの的中率をそのセルの
標本だけで推定する。これは「セル間の真の分散は無限大」＝「他のセルの情報は
一切参考にならない」という仮定と等価で、標本が少ないセルでは推定が標本誤差に
支配される(無情報入力でも新規判断の4〜7割が減衰される)。

本モジュールは、履歴バーで測った**セル間分散の片側95%上限** tau を受け取り、
James-Stein 収縮でセル率推定を全体率へ寄せる。

    lambda = tau^2 / (tau^2 + sigma^2),  sigma^2 = b(1-b)/n
    rate'  = b + lambda * (rate - b)

tau -> ∞ で lambda -> 1 となり現行の推定量と一致する。したがって現行挙動は
本モジュールの特殊ケースであり、prior を渡さなければ挙動は変わらない。

設計上の制約(意図的な非対称性):

- 履歴は「位置」ではなく「分散の上限」だけを渡す。セル偏差を事前平均として
  注入しないため、履歴期間のトレンドやレジームが将来の判断へ流れ込まない。
- 収縮は可動域を**狭める**方向にしか働かない(lambda <= 1)。priorが壊れていても
  減衰が強くなることはない。
- artifactが無い/壊れている/対象セルを含まない場合は None を返し、
  呼び出し側は現行挙動へfail-openする。

詳細な根拠は docs/design/P1_HISTORICAL_PRIOR.md と
reports/research/p1-historical-prior-feasibility-20260823/ を参照。

ネットワークアクセスと追加依存を持たない純粋ロジックで、テストから直接検証できる。
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 1
PRIOR_CONTRACT = "learning-prior-v1"
DEFAULT_PRIOR_PATH = "config/learning_prior_v1.json"
# 融合判断(1h特徴量・24hホライズン)と時間足別スライスのキー。
FUSION_KEY = "fusion"
TIMEFRAME_KEY_PREFIX = "tf:"
# パリティ未検証・除外の特徴量は収縮に使わない(現行挙動を維持する)。
PARITY_VERIFIED = "verified"
PARITY_UNVERIFIED = "unverified"
PARITY_EXCLUDED = "excluded"
SHRINKABLE_PARITY = (PARITY_VERIFIED, PARITY_UNVERIFIED)
SOURCE_HISTORICAL = "historical"
SOURCE_LIVE_JOURNAL = "live_journal"
VALID_SOURCES = (SOURCE_HISTORICAL, SOURCE_LIVE_JOURNAL)


def timeframe_key(timeframe: str) -> str:
    """時間足別スライスのpriorキー。"""
    return f"{TIMEFRAME_KEY_PREFIX}{timeframe}"


@dataclass(frozen=True)
class FeaturePrior:
    """特徴量1個ぶんのセル間分散の測定結果。"""

    feature: str
    tau: float  # 運用で使う上限(片側95%)
    tau_hat: float  # 点推定(参考)
    cells: int
    q_statistic: float
    n_eff_median: float
    parity: str = PARITY_UNVERIFIED
    # 測定元。"historical"=履歴コーパス、"live_journal"=運用ジャーナル実測。
    # 履歴再現できない特徴量は後者でしか測れないため区別して記録する。
    source: str = SOURCE_HISTORICAL

    @property
    def shrinkable(self) -> bool:
        """このpriorで収縮してよいか。パリティ不合格は使わない。"""
        return self.parity in SHRINKABLE_PARITY and self.tau >= 0.0


@dataclass(frozen=True)
class LearningPrior:
    """artifact1本ぶん。スライスキー×特徴量で FeaturePrior を引く。"""

    contract: str
    generated_at: str
    corpus_id: str
    slices: Mapping[str, Mapping[str, FeaturePrior]]
    promotion_admissible: bool = False

    def feature(self, slice_key: str, feature: str) -> FeaturePrior | None:
        bucket = self.slices.get(slice_key)
        if bucket is None:
            return None
        return bucket.get(feature)

    def summary_ja(self, slice_key: str) -> str:
        """学習メモ用の1行。測定済みと未測定を区別して出す。"""
        bucket = self.slices.get(slice_key)
        if not bucket:
            return f"状態別prior: {slice_key}は未測定(現行の無補正推定)"
        worst = max(bucket.values(), key=lambda item: item.tau)
        return (
            f"状態別prior: {len(bucket)}特徴量を{self.corpus_id}で測定"
            f"(セル間分散の上限 最大{worst.tau * 100:.2f}%)"
        )


def shrink_rate(rate: float, samples: float, baseline: float, tau: float) -> float:
    """セル率をJames-Stein収縮で全体率へ寄せる。

    tau は「セル間の真の分散の上限」。tau が大きいほどセル固有の推定を信じ、
    小さいほど全体率へ寄せる。tau -> ∞ で rate をそのまま返す(現行挙動)。
    """
    if samples <= 0:
        return rate
    if tau <= 0.0:
        # 分散ゼロ = 全セルは基準値と区別できない。
        return baseline
    variance = max(baseline * (1.0 - baseline), 1e-9) / samples
    weight = tau * tau / (tau * tau + variance)
    return baseline + weight * (rate - baseline)


def _lower_gamma_regularized(shape: float, value: float) -> float:
    """正則化下側不完全ガンマ関数 P(a, x)。chi2 CDF に使う。"""
    if value <= 0.0:
        return 0.0
    log_gamma = math.lgamma(shape)
    if value < shape + 1.0:
        # 級数展開
        term = 1.0 / shape
        total = term
        for index in range(1, 1000):
            term *= value / (shape + index)
            total += term
            if abs(term) < abs(total) * 1e-15:
                break
        return total * math.exp(-value + shape * math.log(value) - log_gamma)
    # 連分数展開(Lentz)
    tiny = 1e-300
    b = value + 1.0 - shape
    c = 1.0 / tiny
    d = 1.0 / b
    h = d
    for index in range(1, 1000):
        an = -index * (index - shape)
        b += 2.0
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-15:
            break
    return 1.0 - math.exp(-value + shape * math.log(value) - log_gamma) * h


def chi_square_cdf(value: float, degrees: int) -> float:
    return _lower_gamma_regularized(degrees / 2.0, value / 2.0)


def chi_square_quantile(probability: float, degrees: int) -> float:
    """chi2 分位点を二分法で求める(scipy非依存、決定論的)。"""
    if degrees <= 0:
        return 0.0
    low, high = 0.0, max(10.0, float(degrees) * 10.0)
    while chi_square_cdf(high, degrees) < probability:
        high *= 2.0
        if high > 1e6:
            break
    for _ in range(200):
        mid = (low + high) / 2.0
        if chi_square_cdf(mid, degrees) < probability:
            low = mid
        else:
            high = mid
    return (low + high) / 2.0


@dataclass(frozen=True)
class HeterogeneityFit:
    """DerSimonian-Laird の推定結果。任意の alpha で境界を引き直せるよう scale を持つ。"""

    tau_hat: float
    tau_upper: float
    q_statistic: float
    degrees: int
    scale: float

    def bound(self, alpha: float, *, upper: bool) -> float:
        """片側境界。upper=True で上限(否定できない最大)、False で下限(示せる最小)。

        上限は Q の帰無分布の下側 alpha 点、下限は上側 alpha 点を使う。
        下限が 0 を超えるのは「Q が帰無分布のほぼ全域より大きい」場合だけなので、
        「実測の異質性が確かに存在する」と言えるときにしか正の値にならない。
        """
        return tau_bound(self.q_statistic, self.degrees, self.scale, alpha, upper=upper)


def tau_bound(
    q_statistic: float, degrees: int, scale: float, alpha: float, *, upper: bool
) -> float:
    if scale <= 0 or degrees < 1:
        return 0.0
    probability = alpha if upper else 1.0 - alpha
    critical = chi_square_quantile(probability, degrees)
    return math.sqrt(max(0.0, (q_statistic - critical) / scale))


def fit_heterogeneity(
    deviations: Sequence[float], variances: Sequence[float], alpha: float = 0.05
) -> HeterogeneityFit:
    """メタ分析標準の tau^2 推定(DerSimonian-Laird)。

    deviations は各セルの偏差、variances はその標本分散(実効nに基づくもの)。
    """
    count = len(deviations)
    if count < 2 or len(variances) != count:
        return HeterogeneityFit(0.0, 0.0, 0.0, 0, 0.0)
    weights = [1.0 / variance for variance in variances if variance > 0]
    if len(weights) != count:
        return HeterogeneityFit(0.0, 0.0, 0.0, 0, 0.0)
    weight_sum = sum(weights)
    weighted_mean = sum(w * d for w, d in zip(weights, deviations)) / weight_sum
    q_statistic = sum(w * (d - weighted_mean) ** 2 for w, d in zip(weights, deviations))
    scale = weight_sum - sum(w * w for w in weights) / weight_sum
    degrees = count - 1
    if scale <= 0:
        return HeterogeneityFit(0.0, 0.0, q_statistic, degrees, 0.0)
    tau2_hat = max(0.0, (q_statistic - degrees) / scale)
    return HeterogeneityFit(
        tau_hat=math.sqrt(tau2_hat),
        tau_upper=tau_bound(q_statistic, degrees, scale, alpha, upper=True),
        q_statistic=q_statistic,
        degrees=degrees,
        scale=scale,
    )


def heterogeneity_pvalue(
    deviations: Sequence[float], variances: Sequence[float], tau: float
) -> float | None:
    """帰無仮説「セル間分散はちょうど tau」に対する片側p値。

    ランダム効果の重み 1/(v_i + tau^2) で Q を組み直すと、tau が正しいとき
    Q は近似的に chi2(k-1) に従う。したがって Q が上側にずれた度合いが
    「tau では説明しきれない異質性」の証拠になる。

    tau=0 を渡せば通常の異質性検定と一致する。セルが2件未満なら None。
    """
    count = len(deviations)
    if count < 2 or len(variances) != count:
        return None
    weights = [1.0 / (variance + tau * tau) for variance in variances if variance > 0]
    if len(weights) != count:
        return None
    weight_sum = sum(weights)
    weighted_mean = sum(w * d for w, d in zip(weights, deviations)) / weight_sum
    q_statistic = sum(w * (d - weighted_mean) ** 2 for w, d in zip(weights, deviations))
    return 1.0 - chi_square_cdf(q_statistic, count - 1)


def holm_rejections(p_values: Sequence[float], alpha: float = 0.05) -> list[bool]:
    """Holm-Bonferroni。返り値は入力順の棄却フラグ。

    多重比較を補正しないと、8特徴量×5スライスの比較で偶然だけで数件が
    「有意」になる。Bonferroniより検出力が高く、family-wise error は同じく制御する。
    """
    total = len(p_values)
    if total == 0:
        return []
    order = sorted(range(total), key=lambda index: p_values[index])
    rejected = [False] * total
    for rank, index in enumerate(order):
        if p_values[index] <= alpha / (total - rank):
            rejected[index] = True
        else:
            break
    return rejected


def dersimonian_laird(
    deviations: Sequence[float], variances: Sequence[float], alpha: float = 0.05
) -> tuple[float, float, float]:
    """後方互換の3-tuple版。返り値は (tau_hat, tau_upper, Q)。"""
    fit = fit_heterogeneity(deviations, variances, alpha)
    return fit.tau_hat, fit.tau_upper, fit.q_statistic


@dataclass(frozen=True)
class CellMeasurement:
    """特徴量1個ぶんのセル偏差。dersimonian_laird へそのまま渡せる形。"""

    feature: str
    deviations: tuple[float, ...]
    variances: tuple[float, ...]
    effective: tuple[float, ...]
    detail: Mapping[str, Mapping[str, float]]


def cell_deviations(
    observations: Iterable[tuple[str, str, Mapping[str, float], bool]],
    overlap: float,
    features: Sequence[str],
    bucketize: Callable[[str, float], str | None],
) -> dict[str, CellMeasurement]:
    """(グループ×方向)の無条件的中率を差し引いたセル偏差を、実効nつきで作る。

    observations は (グループ, 方向, 特徴量, hit)。グループは de-mean の単位で、
    履歴コーパスでは通貨ペア、運用ジャーナルでも通貨ペアを渡す。ここで
    de-mean しないと、その期間の相場方向がセルの優位に化ける
    (2024年24hのUSDJPYは long 58.0% / short 42.0% で16ポイントの無条件差)。

    overlap は重複ラベル係数。1時間間隔の標本に24時間ラベルを貼れば24を渡す。
    生の件数のまま分散を測ると、標本誤差を24倍過小評価する。

    履歴側と運用側でこの関数を共有することで、両者の tau が同じ定義で比較できる。
    """
    materialized = list(observations)
    baseline: dict[tuple[str, str], list[float]] = {}
    for group, direction, _features, hit in materialized:
        stats = baseline.setdefault((group, direction), [0.0, 0.0])
        stats[0] += 1.0
        stats[1] += float(hit)
    cells: dict[str, dict[str, list[float]]] = {}
    for group, direction, values, hit in materialized:
        stats = baseline[(group, direction)]
        base_rate = stats[1] / stats[0]
        for feature in features:
            value = values.get(feature)
            if not isinstance(value, (int, float)):
                continue
            label = bucketize(feature, float(value))
            if label is None:
                continue
            cell = cells.setdefault(feature, {}).setdefault(f"{label}|{direction}", [0.0, 0.0, 0.0])
            cell[0] += 1.0
            cell[1] += float(hit)
            cell[2] += base_rate
    measured: dict[str, CellMeasurement] = {}
    for feature, feature_cells in cells.items():
        deviations: list[float] = []
        variances: list[float] = []
        effective: list[float] = []
        detail: dict[str, dict[str, float]] = {}
        for name, (count, hits, base_sum) in sorted(feature_cells.items()):
            effective_n = count / overlap if overlap > 0 else count
            base_rate = base_sum / count
            deviation = hits / count - base_rate
            deviations.append(deviation)
            variances.append(max(base_rate * (1.0 - base_rate), 1e-6) / effective_n)
            effective.append(effective_n)
            detail[name] = {
                # 0.0 + は負のゼロを正規化する。丸め後に -0.0 が残ると、
                # 浮動小数の加算順序が変わっただけでartifactのhashがぶれる。
                "deviation": round(deviation, 6) + 0.0,
                "n_eff": round(effective_n, 1),
                "n_raw": count,
            }
        measured[feature] = CellMeasurement(
            feature=feature,
            deviations=tuple(deviations),
            variances=tuple(variances),
            effective=tuple(effective),
            detail=detail,
        )
    return measured


def _feature_from_mapping(feature: str, payload: Mapping[str, object]) -> FeaturePrior | None:
    tau = payload.get("tau_upper95")
    if not isinstance(tau, (int, float)) or tau < 0:
        return None
    parity = str(payload.get("parity") or PARITY_UNVERIFIED)
    if parity not in (PARITY_VERIFIED, PARITY_UNVERIFIED, PARITY_EXCLUDED):
        return None
    source = str(payload.get("source") or SOURCE_HISTORICAL)
    if source not in VALID_SOURCES:
        return None
    tau_hat = payload.get("tau_hat")
    q_statistic = payload.get("Q")
    cells = payload.get("cells")
    n_eff = payload.get("n_eff_median")
    return FeaturePrior(
        feature=feature,
        tau=float(tau),
        tau_hat=float(tau_hat) if isinstance(tau_hat, (int, float)) else 0.0,
        cells=int(cells) if isinstance(cells, int) else 0,
        q_statistic=float(q_statistic) if isinstance(q_statistic, (int, float)) else 0.0,
        n_eff_median=float(n_eff) if isinstance(n_eff, (int, float)) else 0.0,
        parity=parity,
        source=source,
    )


def prior_from_mapping(payload: Mapping[str, object]) -> LearningPrior | None:
    """artifactのdictを検証して LearningPrior にする。契約違反は None。"""
    if payload.get("contract") != PRIOR_CONTRACT:
        return None
    if payload.get("schema") != SCHEMA_VERSION:
        return None
    raw_slices = payload.get("slices")
    if not isinstance(raw_slices, Mapping):
        return None
    slices: dict[str, dict[str, FeaturePrior]] = {}
    for slice_key, raw_features in raw_slices.items():
        if not isinstance(raw_features, Mapping):
            continue
        features: dict[str, FeaturePrior] = {}
        for feature, raw in raw_features.items():
            if not isinstance(raw, Mapping):
                continue
            parsed = _feature_from_mapping(str(feature), raw)
            if parsed is not None:
                features[str(feature)] = parsed
        if features:
            slices[str(slice_key)] = features
    if not slices:
        return None
    corpus = payload.get("corpus")
    corpus_id = ""
    if isinstance(corpus, Mapping):
        corpus_id = str(corpus.get("corpus_id") or "")
    return LearningPrior(
        contract=PRIOR_CONTRACT,
        generated_at=str(payload.get("generated_at") or ""),
        corpus_id=corpus_id,
        slices=slices,
        promotion_admissible=bool(payload.get("promotion_admissible")),
    )


def load_prior(path: str | Path = DEFAULT_PRIOR_PATH) -> LearningPrior | None:
    """artifactを読む。存在しない・壊れている場合は None(現行挙動へfail-open)。"""
    try:
        with Path(path).open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    return prior_from_mapping(payload)

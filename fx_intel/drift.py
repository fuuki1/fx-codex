"""ADWIN(ADaptive WINdowing)によるコンセプトドリフト検出。

レポート(FX AI.md)ギャップ⑨「ADWINドリフト監視」の実装。モデルの予測誤差や
的中フラグの系列を逐次に監視し、「最近の平均が過去の平均から統計的に有意に
ずれた=レジームが変わった」瞬間を検出して再学習トリガーにする。

ADWIN(Bifet & Gavaldà 2007)の要点:

- 可変長ウィンドウ W に観測を逐次追加する。
- W をあらゆる位置で古い部分 W0 と新しい部分 W1 に分割し、両者の平均差が
  Hoeffding 由来の閾値 ε_cut を超える分割が1つでもあれば「変化あり」と判定し、
  古い側 W0 を丸ごと捨てる(窓が縮む=最近のデータに適応)。変化が無ければ窓は
  伸び続ける(=より多くのデータで平均を精緻化)。
- これによりドリフトが無ければ長い窓で安定推定、あればすぐ縮んで即応、という
  「変化が無ければ拡大・あれば縮小」の適応窓を実現する。

本実装は標準ライブラリのみ(gbm/ml/promotion と同じ依存ゼロ方針)。厳密な
指数ヒストグラム版ではなく、実装が読みやすく監査しやすい素朴なリスト窓版
(観測数が数千規模の再学習トリガー用途では十分)。値は [0,1] 範囲(誤差率・
的中フラグ・確率など有界系列)を想定して分散上限を見積もる。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class ADWIN:
    """適応窓によるドリフト検出器。

    delta: 誤検出許容確率(小さいほど鈍感=誤検出は減るが検出が遅れる)。既定0.002。
    max_window: 窓の上限(古い観測を無制限に溜めない安全弁)。0以下で無制限。
    min_subwindow: 分割の両側に最低これだけ観測が無いと検定しない(過小標本除外)。
    """

    delta: float = 0.002
    max_window: int = 5000
    min_subwindow: int = 5
    _window: list[float] = field(default_factory=list, init=False, repr=False)
    _drift_detected: bool = field(default=False, init=False)
    _last_drift_index: int = field(default=-1, init=False)
    _total_seen: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if not 0.0 < self.delta < 1.0:
            raise ValueError("delta は (0, 1) の範囲であること")
        if self.min_subwindow < 1:
            raise ValueError("min_subwindow は1以上であること")

    @property
    def width(self) -> int:
        """現在の窓幅(観測数)。"""
        return len(self._window)

    @property
    def mean(self) -> float:
        """現在の窓の平均(推定量)。窓が空なら0。"""
        return sum(self._window) / len(self._window) if self._window else 0.0

    @property
    def drift_detected(self) -> bool:
        """直近の update で変化が検出されたか。"""
        return self._drift_detected

    @property
    def total_seen(self) -> int:
        return self._total_seen

    def update(self, value: float) -> bool:
        """観測を1件追加し、ドリフトを検出したら True を返す。

        value は有界(おおむね [0,1])の系列を想定(誤差率・的中フラグ・確率など)。
        変化を検出した場合は古い側の部分窓を捨てて窓を縮める。
        """
        self._total_seen += 1
        self._window.append(float(value))
        if self.max_window > 0 and len(self._window) > self.max_window:
            # 上限超過分は最古から落とす(適応窓の実効長を保つ)
            del self._window[: len(self._window) - self.max_window]

        self._drift_detected = False
        # あらゆる分割位置で W0(古)と W1(新)の平均差を検定。最古側から縮める。
        changed = True
        while changed and len(self._window) >= 2 * self.min_subwindow:
            changed = False
            n = len(self._window)
            total = sum(self._window)
            prefix = 0.0
            for cut in range(self.min_subwindow, n - self.min_subwindow + 1):
                prefix += self._window[cut - 1]
                n0 = cut
                n1 = n - cut
                mean0 = prefix / n0
                mean1 = (total - prefix) / n1
                if abs(mean0 - mean1) > self._epsilon_cut(n0, n1):
                    # 変化あり: 古い側 W0 を捨てて窓を縮め、残りで再検定する
                    del self._window[:cut]
                    self._drift_detected = True
                    self._last_drift_index = self._total_seen
                    changed = True
                    break
        return self._drift_detected

    def _epsilon_cut(self, n0: int, n1: int) -> float:
        """Hoeffding 由来の分割閾値 ε_cut。

        調和平均 m = 1/(1/n0 + 1/n1)、δ' = δ/n(Bonferroni 的な多重検定補正)を使い
        ε_cut = sqrt( (1/(2m)) · ln(4/δ') )。値域 [0,1] を想定した分散上限。
        """
        n = n0 + n1
        m = 1.0 / (1.0 / n0 + 1.0 / n1)
        delta_prime = self.delta / n
        return math.sqrt((1.0 / (2.0 * m)) * math.log(4.0 / delta_prime))

    def reset(self) -> None:
        self._window.clear()
        self._drift_detected = False
        self._last_drift_index = -1
        self._total_seen = 0


@dataclass(frozen=True)
class DriftScan:
    """系列を一括走査した結果(バッチ用途)。"""

    drift_points: list[int]  # ドリフトを検出した観測インデックス(0起点)
    final_width: int  # 走査後の窓幅
    final_mean: float  # 走査後の窓平均(=直近レジームの推定量)
    total: int


def scan_for_drift(
    values: list[float] | tuple[float, ...],
    *,
    delta: float = 0.002,
    max_window: int = 5000,
    min_subwindow: int = 5,
) -> DriftScan:
    """誤差/的中系列を一括で ADWIN に流し、ドリフト点を列挙するバッチ入口。

    再学習判断で「学習後にドリフトが起きたか」を確認する用途を想定。最後の
    drift_point 以降のデータだけで再学習する、といった運用に使える。
    """
    detector = ADWIN(delta=delta, max_window=max_window, min_subwindow=min_subwindow)
    drift_points: list[int] = []
    for index, value in enumerate(values):
        if detector.update(value):
            drift_points.append(index)
    return DriftScan(
        drift_points=drift_points,
        final_width=detector.width,
        final_mean=detector.mean,
        total=detector.total_seen,
    )

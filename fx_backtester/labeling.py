"""López de Prado 金融ML方法論のラベリング・前処理プリミティブ。

レポート(FX AI.md)ギャップ②「トリプルバリア＋メタラベリング＋分数次差分」の
土台。indicators.py がテクニカル指標の純粋関数を提供するのと同じ層で、
ここでは『Advances in Financial Machine Learning』の中核ツールを純粋関数として
実装する(strategies/ から合成して使う)。

提供する3プリミティブ:

1. frac_diff_ffd — 分数次差分(固定幅窓 FFD)。整数次差分(=リターン化)は定常化
   するが記憶を消しすぎる。0<d<1 の分数次で「定常性を得つつ最大限の記憶を保持」。
   López de Prado は87の流動先物すべてで d<0.6 で定常化を達成した。

2. triple_barrier_labels — トリプルバリア・ラベリング。上バリア(利確)・下バリア
   (損切)・垂直バリア(時間切れ)の3つで各観測をパス依存にラベル付けし、
   「損切を考慮した現実的な結果」を反映する。単純な次足方向ラベルと違い、
   途中でストップに触れたかどうかを織り込む。

3. meta_labels — メタラベリング。一次モデルが方向(long/short)を決め、二次モデルが
   「その方向に張って当たるか否か(=張る/見送る)」を学習する。二次ラベルは
   「一次の方向がトリプルバリアで利確に届いたか」。F1改善とサイズ判断に効く。

4. cusum_filter — CUSUMフィルターによるイベントサンプリング。全バーを機械的に
   学習点にすると自己相関で実効サンプルを過大評価する。累積偏差が閾値hを超えた
   「意味のある変化が起きた点」だけをイベントとして抽出し、そこにトリプルバリアを
   張る。López de Prado の定石(CUSUMでサンプリング→トリプルバリアでラベル付け)。

すべてネットワーク非依存の純粋関数で、fixtureのSeries/DataFrameからテストできる。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# ---------------------------------------------------------------- 分数次差分(FFD)


def frac_diff_weights(d: float, threshold: float = 1e-5, max_width: int = 10_000) -> np.ndarray:
    """分数次差分の重み系列 w_k を、|w_k|<threshold で打ち切って返す(固定幅窓用)。

    w_0=1、w_k = -w_{k-1} * (d - k + 1) / k の漸化式。返り値は [w_0, w_1, ...] の
    昇順(最新の観測に w_0 が掛かる向き)。d=0 で [1.0](恒等)、d=1 で [1, -1]
    (1次差分)に一致する。
    """
    if d < 0:
        raise ValueError("d must be >= 0")
    weights = [1.0]
    k = 1
    while k < max_width:
        w = -weights[-1] * (d - k + 1) / k
        if abs(w) < threshold:
            break
        weights.append(w)
        k += 1
    return np.array(weights, dtype=float)


def frac_diff_ffd(series: pd.Series, d: float, threshold: float = 1e-5) -> pd.Series:
    """固定幅窓(FFD)の分数次差分を計算する。

    各時点 t で、直近 len(weights) 本に固定重みを畳み込む。窓が埋まらない
    先頭は NaN。窓幅が一定なので「ドリフトの無い一様な記憶の系列」になる
    (López de Prado 推奨の FFD)。d=0 は原系列、d=1 は1次差分に一致する。
    """
    values = series.astype(float)
    weights = frac_diff_weights(d, threshold=threshold)
    width = len(weights)
    # 最新観測に w[0]、width-1 本前に w[-1] が掛かるよう畳み込む
    kernel = weights[::-1]
    out = pd.Series(np.nan, index=series.index, dtype=float)
    raw = values.to_numpy(dtype=float)
    for end in range(width - 1, len(raw)):
        window = raw[end - width + 1 : end + 1]
        if np.isnan(window).any():
            continue
        out.iloc[end] = float(np.dot(kernel, window))
    return out


def min_ffd_order(
    series: pd.Series,
    thresholds: tuple[float, ...] = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
    weight_threshold: float = 1e-5,
) -> float | None:
    """ADF風の簡易定常性判定で、定常化する最小の d を探す(scipy非依存)。

    厳密なADF検定はscipy/statsmodelsを要するため、ここでは「分数次差分系列の
    ラグ1自己相関が閾値未満まで下がる最小 d」を代理指標に使う(依存ゼロ方針)。
    自己相関が 0.5 未満になった最初の d を返す。全滅なら None。

    注: 厳密な単位根検定ではなく実務上の目安。用途は「記憶を最大限残す d の
    当たりを付ける」ことで、確定検定は別途 statsmodels 等で行う想定。
    """
    for d in thresholds:
        diffed = frac_diff_ffd(series, d, threshold=weight_threshold).dropna()
        if len(diffed) < 20:
            continue
        autocorr = diffed.autocorr(lag=1)
        if autocorr is not None and not np.isnan(autocorr) and abs(autocorr) < 0.5:
            return d
    return None


# ---------------------------------------------------------------- CUSUM イベント抽出


def cusum_filter(
    close: pd.Series,
    threshold: float | pd.Series,
    *,
    use_log_returns: bool = True,
) -> pd.DatetimeIndex:
    """対称CUSUMフィルターで「意味のある変化が起きた点」だけをイベント抽出する。

    各バーのリターン(既定は対数リターン)を上振れ累積 S+ と下振れ累積 S- に積み、
    どちらかが threshold を超えた時点をイベントとして記録し、その累積をリセットする。
    こうして得た点だけにトリプルバリアを張ると、全バーを学習点にする場合の
    自己相関(実効サンプルの過大評価)を避けられる(López de Prado の定石)。

    threshold は固定値(float)か、各時点で異なる閾値(pd.Series。ボラ連動の
    σ×係数を渡す運用)。閾値<=0 の点は「変化を拾わない」としてスキップする。
    戻り値はイベント時刻の DatetimeIndex(元の close の index 部分集合)。
    """
    prices = close.astype(float)
    if use_log_returns:
        diff = np.log(prices).diff()
    else:
        diff = prices.pct_change()

    # 閾値をSeriesに正規化する。float指定は全時点一定のSeriesに広げておくと、
    # ループ内で分岐せず get() で引ける(型も pd.Series に固定できる)。
    if isinstance(threshold, pd.Series):
        threshold_series = threshold
    else:
        if threshold <= 0:
            raise ValueError("threshold(float)は正であること")
        threshold_series = pd.Series(float(threshold), index=diff.index)

    events: list = []
    s_pos = 0.0
    s_neg = 0.0
    for ts, value in diff.items():
        if value is None or (isinstance(value, float) and np.isnan(value)):
            continue
        h = float(threshold_series.get(ts, np.nan))
        if not np.isfinite(h) or h <= 0:
            # 閾値が無い/非正の点は累積だけ進めて判定はしない(ボラ欠損時の保護)
            s_pos = max(0.0, s_pos + float(value))
            s_neg = min(0.0, s_neg + float(value))
            continue
        s_pos = max(0.0, s_pos + float(value))
        s_neg = min(0.0, s_neg + float(value))
        if s_pos >= h:
            s_pos = 0.0
            events.append(ts)
        elif s_neg <= -h:
            s_neg = 0.0
            events.append(ts)
    return pd.DatetimeIndex(events, name=close.index.name)


# ---------------------------------------------------------------- トリプルバリア


def triple_barrier_labels(
    close: pd.Series,
    events_index: pd.Index | None = None,
    *,
    upper_multiple: float = 2.0,
    lower_multiple: float = 2.0,
    vertical_bars: int = 24,
    volatility: pd.Series | None = None,
    side: pd.Series | None = None,
) -> pd.DataFrame:
    """各エントリ点にトリプルバリア・ラベルを付ける(パス依存・ルックアヘッド無し)。

    - 上下バリアは volatility(各点のσ、未指定なら close の日次リターン20本std)に
      upper/lower_multiple を掛けた幅。side を渡すと(メタラベリング用)、side方向の
      利確側を「上バリア」として符号を揃える。
    - 垂直バリアは vertical_bars 本先(時間切れ)。
    - 各エントリから前方を1本ずつ歩き、最初に触れたバリアでラベルを確定する
      (途中でストップに触れたパスを正しく反映)。将来のバーだけを見るので
      ルックアヘッドは無い(ラベルは「そのエントリ後に何が起きたか」の教師信号)。

    戻り値は events_index を index に持つ DataFrame(label/ret/touch_ts)。
    """
    close = close.astype(float)
    index = events_index if events_index is not None else close.index
    if volatility is None:
        volatility = close.pct_change().rolling(20, min_periods=20).std()

    records: dict[str, list] = {"label": [], "ret": [], "touch_ts": []}
    valid_index: list = []
    positions = {ts: i for i, ts in enumerate(close.index)}

    for ts in index:
        start = positions.get(ts)
        if start is None:
            continue
        sigma = volatility.get(ts)
        if sigma is None or np.isnan(sigma) or sigma <= 0:
            continue
        entry_price = close.iloc[start]
        direction = 1.0
        if side is not None:
            side_value = side.get(ts)
            if side_value is None or np.isnan(side_value) or side_value == 0:
                continue
            direction = float(np.sign(side_value))

        up = sigma * upper_multiple
        down = sigma * lower_multiple
        end = min(start + vertical_bars, len(close) - 1)
        label = 0
        touch_pos = end
        for pos in range(start + 1, end + 1):
            ret = direction * (close.iloc[pos] / entry_price - 1.0)
            if ret >= up:
                label = 1
                touch_pos = pos
                break
            if ret <= -down:
                label = -1
                touch_pos = pos
                break
        final_ret = direction * (close.iloc[touch_pos] / entry_price - 1.0)
        records["label"].append(label)
        records["ret"].append(round(final_ret, 8))
        records["touch_ts"].append(close.index[touch_pos])
        valid_index.append(ts)

    return pd.DataFrame(records, index=pd.Index(valid_index, name=close.index.name))


# ---------------------------------------------------------------- メタラベリング


def meta_labels(barrier_labels: pd.DataFrame) -> pd.Series:
    """トリプルバリア結果からメタラベル(1=張るべき / 0=見送るべき)を作る。

    一次モデルが side(方向)を決めた前提で triple_barrier_labels を side 付きで
    呼ぶと、label=+1 は「その方向で利確に届いた=張って正解」、label=-1/0 は
    「損切/時間切れ=張らない方が良かった」を意味する。二次モデルはこの 0/1 を
    教師にして『一次の方向シグナルに乗るか否か(=サイズ)』を学習する。
    """
    if "label" not in barrier_labels.columns:
        raise ValueError("barrier_labels must have a 'label' column")
    return (barrier_labels["label"] > 0).astype(int)


def sample_weights_by_return(barrier_labels: pd.DataFrame) -> pd.Series:
    """リターンの絶対値でサンプル重みを付ける(利益整合の簡易版)。

    レポートの核心「精度でなく利益に整合させよ」に沿って、大きく動いた
    (=高価値な)サンプルを重く学習する。重みは |ret| を平均1になるよう正規化。
    ret が全て0の退化ケースでは一様重み(1.0)を返す。
    """
    if "ret" not in barrier_labels.columns:
        raise ValueError("barrier_labels must have a 'ret' column")
    abs_ret = barrier_labels["ret"].abs()
    total = abs_ret.sum()
    if total <= 0:
        return pd.Series(1.0, index=barrier_labels.index)
    return abs_ret / abs_ret.mean()

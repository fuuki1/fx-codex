"""テクニカル特徴量 + COT特徴量の生成(パイプライン4段目)。

設計の肝はリークゼロ:

- テクニカルは判断時刻 t までの確定バーのみから計算する(未来を見ない)。
- COT は週次+発表ラグがある。価格の各バー t には「t より前に確実に公開済みの
  週」だけを as-of 結合する。report_date(火曜集計)+ publication_lag_days
  (既定3日=金曜発表)を「公開時刻」とみなし、公開時刻 <= t の最新週を使う。
- base通貨とquote通貨のCOTを両方使い、差分(base - quote)を特徴量にする。
  例: EURUSD なら EUR net と USD net の差が「ユーロ強気度」を表す。

出力は build_feature_matrix(prices, cot, symbol) -> pd.DataFrame。index は
価格のtimestamp、各列が1特徴量。NaN行(ウォームアップ期間)は呼び出し側で落とす。

indicators は fx_backtester/indicators.py を再利用する(sma/rsi/average_true_range)。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from fx_backtester.indicators import average_true_range, rsi, sma
from fx_backtester.models import instrument_for, normalize_symbol

from .config import FeatureConfig

# ---------------------------------------------------------------- テクニカル特徴量


def technical_features(prices: pd.DataFrame, cfg: FeatureConfig) -> pd.DataFrame:
    """価格OHLCVからテクニカル特徴量を組む(すべて過去情報のみ)。"""
    close = prices["close"]
    feats: dict[str, pd.Series] = {}

    # 過去リターン(複数lag)。log-return を使う。
    log_close = np.log(close)
    log_ret = log_close.diff()
    for lag in cfg.return_lags:
        feats[f"ret_{lag}"] = log_close.diff(lag)

    # ボラティリティ: ATR% (ATR / close)
    atr = average_true_range(prices, cfg.atr_window)
    feats["atr_pct"] = atr / close

    # RSI(0-100 → 中心化して -50..50)
    feats["rsi"] = rsi(close, cfg.rsi_window) - 50.0

    # MA乖離(ATR換算): (close - SMA) / ATR。単位を無次元化してスケール不変に。
    for window in cfg.sma_windows:
        ma = sma(close, window)
        feats[f"ma_gap_{window}"] = (close - ma) / atr.replace(0, np.nan)

    # モメンタム(短期 - 長期リターンの符号的な勢い)
    feats["momentum"] = log_close.diff(cfg.sma_windows[0]) - log_close.diff(cfg.sma_windows[-1])

    # レンジ内位置: 直近N本の高安レンジ内での終値位置(0..1 → -0.5..0.5)
    window = max(cfg.sma_windows)
    roll_high = prices["high"].rolling(window, min_periods=window).max()
    roll_low = prices["low"].rolling(window, min_periods=window).min()
    rng = (roll_high - roll_low).replace(0, np.nan)
    feats["range_pos"] = (close - roll_low) / rng - 0.5

    # 実現ボラ(log-returnのローリング標準偏差)
    feats["realized_vol"] = log_ret.rolling(cfg.atr_window, min_periods=cfg.atr_window).std()

    frame = pd.DataFrame(feats, index=prices.index)
    return frame


# ---------------------------------------------------------------- COT特徴量


def _cot_public_time(cot: pd.DataFrame, lag_days: int) -> pd.DataFrame:
    """COT週次に「公開時刻」列を付け、公開時刻昇順に整列する。"""
    out = cot.copy()
    out["report_date"] = pd.to_datetime(out["report_date"])
    # tz-naive の report_date を価格(UTC)と比較できるよう UTC 付与
    if out["report_date"].dt.tz is None:
        out["report_date"] = out["report_date"].dt.tz_localize("UTC")
    out["public_time"] = out["report_date"] + pd.Timedelta(days=lag_days)
    return out.sort_values("public_time").reset_index(drop=True)


def _cot_currency_features(cot: pd.DataFrame, cfg: FeatureConfig) -> pd.DataFrame:
    """1通貨のCOT時系列から週次特徴量を作る(public_time をindexに持つ)。

    - net: 投機筋ネット(long-short)
    - net_chg: 前週差
    - cot_index: 直近 cot_index_weeks 週の net の min-max 正規化(0-100 → -50..50)
    - oi_chg_pct: 建玉の前週変化率
    - noncomm_ratio: long/(long+short) の偏り(-0.5..0.5)
    """
    # public_time を index にしてから全列を計算する。こうすると各系列が同じ
    # index を共有し、DataFrame 組み立て時のインデックス再整列によるNaN化を防ぐ。
    pub = _cot_public_time(cot, cfg.cot_publication_lag_days).set_index("public_time")
    net = pub["net_noncomm"].astype(float)
    weeks = cfg.cot_index_weeks

    roll_min = net.rolling(weeks, min_periods=max(4, weeks // 4)).min()
    roll_max = net.rolling(weeks, min_periods=max(4, weeks // 4)).max()
    span = (roll_max - roll_min).replace(0, np.nan)
    cot_index = (net - roll_min) / span * 100.0  # 0-100

    long = pub["noncomm_long"].astype(float)
    short = pub["noncomm_short"].astype(float)
    total = (long + short).replace(0, np.nan)
    oi = pub["open_interest"].astype(float)

    feats = pd.DataFrame(
        {
            "cot_net": net,
            "cot_net_chg": net.diff(),
            "cot_index": cot_index - 50.0,
            "cot_oi_chg_pct": oi.pct_change(),
            "cot_noncomm_ratio": long / total - 0.5,
        }
    )
    feats.index.name = "public_time"
    return feats


def cot_features(
    cot_by_ccy: dict[str, pd.DataFrame],
    symbol: str,
    price_index: pd.DatetimeIndex,
    cfg: FeatureConfig,
) -> pd.DataFrame:
    """base/quote 両通貨のCOTを as-of 結合し、差分特徴量を price_index に整列。

    リーク防止: merge_asof(direction='backward') で「各バー時刻以前に公開済み」
    の最新週だけを結合する。base と quote の差(base - quote)も足す。
    """
    inst = instrument_for(normalize_symbol(symbol))
    base, quote = inst.base, inst.quote

    per_ccy: dict[str, pd.DataFrame] = {}
    for ccy in (base, quote):
        frame = cot_by_ccy.get(ccy)
        if frame is None or frame.empty:
            continue
        per_ccy[ccy] = _cot_currency_features(frame, cfg)

    # 価格timestampを明示的な "timestamp" 列に持つ DataFrame(merge_asof の左表)。
    price_df = pd.DataFrame({"timestamp": pd.DatetimeIndex(price_index)})

    result = pd.DataFrame(index=price_index)
    aligned: dict[str, pd.DataFrame] = {}
    for ccy, feats in per_ccy.items():
        merged = pd.merge_asof(
            price_df.sort_values("timestamp"),
            feats.reset_index().sort_values("public_time"),
            left_on="timestamp",
            right_on="public_time",
            direction="backward",
        ).set_index("timestamp")
        cols = list(feats.columns)
        renamed = merged[cols].add_prefix(f"{ccy.lower()}_")
        renamed.index = price_index  # 価格indexへ厳密に整列
        aligned[ccy] = renamed
        for col in renamed.columns:
            result[col] = renamed[col]

    # base - quote の差分(方向性のあるポジショニング差)
    if base in aligned and quote in aligned:
        for col in ("cot_net", "cot_index", "cot_noncomm_ratio"):
            b = aligned[base].get(f"{base.lower()}_{col}")
            q = aligned[quote].get(f"{quote.lower()}_{col}")
            if b is not None and q is not None:
                result[f"cot_diff_{col}"] = b - q
    return result


# ---------------------------------------------------------------- 統合


def build_feature_matrix(
    prices: pd.DataFrame,
    cot_by_ccy: dict[str, pd.DataFrame],
    symbol: str,
    cfg: FeatureConfig | None = None,
) -> pd.DataFrame:
    """テクニカル + COT を結合した特徴量行列を返す(index=価格timestamp)。

    NaN行(ウォームアップ)は残したまま返す。学習側で labels と揃えて dropna する。
    use_cot=False ならテクニカルのみ(ベースライン)。
    """
    cfg = cfg or FeatureConfig()
    tech = technical_features(prices, cfg)
    if not cfg.use_cot:
        return tech
    cot = cot_features(cot_by_ccy, symbol, prices.index, cfg)
    return tech.join(cot, how="left")

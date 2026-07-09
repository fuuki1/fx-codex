"""データ品質チェックと正規化(パイプライン2〜3段目)。

価格とCOTの両方を検査し、監査可能な warnings と 0.0〜1.0 の coverage スコアを
返す(fx_intel/macro.py の coverage() 思想)。coverage が低ければ呼び出し側
(pipeline)は学習を止められる。正規化は「クリーンなOHLCV」と「log-return」、
COTの「週次フォワードフィル用の整列済み時系列」を用意する。

すべてネットワーク非依存の純粋関数。DataFrame を受けて DataFrame/レポートを返す。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

# COTの週次間隔(日)。これより大きく飛んでいれば欠測週とみなす。
COT_WEEK_DAYS = 7
COT_GAP_TOLERANCE_DAYS = 10  # 祝日ずれを許容
COT_STALE_DAYS = 21  # fx_intel/macro.py COT_STALE_DAYS と同値

# 価格の外れ値リターン閾値(1バーのlog-returnがこのσ倍を超えたら警告)。
OUTLIER_RETURN_SIGMA = 12.0


@dataclass
class QualityReport:
    """品質チェック結果(監査可能な warnings + カバレッジスコア)。"""

    price_bars: int
    price_warnings: list[str] = field(default_factory=list)
    cot_warnings: dict[str, list[str]] = field(default_factory=dict)
    price_coverage: float = 0.0
    cot_coverage: float = 0.0

    @property
    def coverage(self) -> float:
        """総合カバレッジ(価格0.6・COT0.4)。学習ゲートの入力。"""
        return round(0.6 * self.price_coverage + 0.4 * self.cot_coverage, 3)

    @property
    def is_usable(self) -> bool:
        return self.price_bars > 0 and self.coverage >= 0.5

    def summary(self) -> str:
        lines = [
            f"[qa] 価格バー {self.price_bars} 本 / coverage={self.coverage:.2f}"
            f" (価格 {self.price_coverage:.2f}, COT {self.cot_coverage:.2f})",
            f"  usable={self.is_usable}",
        ]
        for w in self.price_warnings:
            lines.append(f"  ⚠ 価格: {w}")
        for ccy, warns in self.cot_warnings.items():
            for w in warns:
                lines.append(f"  ⚠ COT[{ccy}]: {w}")
        if not self.price_warnings and not any(self.cot_warnings.values()):
            lines.append("  ✓ 重大な品質問題なし")
        return "\n".join(lines)


def check_prices(prices: pd.DataFrame, timeframe: str) -> tuple[list[str], float]:
    """価格OHLCVを検査し (warnings, coverage 0-1) を返す。

    coverage は「深刻な欠陥がどれだけ少ないか」の代理。深刻な欠陥
    (非正価格・high<low・重複index)は coverage を大きく削る。
    """
    warnings: list[str] = []
    if prices.empty:
        return ["価格バーが0本"], 0.0

    penalties = 0.0

    # 非正のOHLC
    ohlc = prices[["open", "high", "low", "close"]]
    nonpositive = int((ohlc <= 0).any(axis=1).sum())
    if nonpositive:
        warnings.append(f"非正のOHLCが {nonpositive} 本")
        penalties += min(0.4, nonpositive / len(prices))

    # high < low の破綻バー
    broken = int((prices["high"] < prices["low"]).sum())
    if broken:
        warnings.append(f"high<lowの破綻バーが {broken} 本")
        penalties += min(0.4, broken / len(prices))

    # 重複timestamp(index)
    if prices.index.has_duplicates:
        dup = int(prices.index.duplicated().sum())
        warnings.append(f"重複timestampが {dup} 本")
        penalties += min(0.3, dup / len(prices))

    # 時系列ギャップ(想定バー間隔に対して大きく空く=データ欠損)
    gap_warn, gap_penalty = _check_time_gaps(prices, timeframe)
    if gap_warn:
        warnings.append(gap_warn)
    penalties += gap_penalty

    # 外れ値リターン
    log_ret = np.log(prices["close"]).diff().dropna()
    if len(log_ret) > 10 and log_ret.std(ddof=1) > 0:
        z = (log_ret - log_ret.mean()).abs() / log_ret.std(ddof=1)
        outliers = int((z > OUTLIER_RETURN_SIGMA).sum())
        if outliers:
            warnings.append(f"外れ値リターン(> {OUTLIER_RETURN_SIGMA:.0f}σ)が {outliers} 本")
            penalties += min(0.2, outliers / len(prices))

    coverage = round(max(0.0, 1.0 - penalties), 3)
    return warnings, coverage


def _check_time_gaps(prices: pd.DataFrame, timeframe: str) -> tuple[str | None, float]:
    """バー間隔の欠損を検査。週末クローズ(FXは土日休)は正常として除外。"""
    if len(prices) < 3:
        return None, 0.0
    idx = prices.index
    deltas = idx.to_series().diff().dropna().dt.total_seconds() / 3600.0  # hours
    if deltas.empty:
        return None, 0.0
    expected = float(deltas.median())
    if expected <= 0:
        return None, 0.0
    # 週末(約49時間)を除いた「平日中の異常ギャップ」だけ数える
    weekday_gaps = deltas[(deltas > expected * 2.5) & (deltas < 48)]
    n_gaps = int(weekday_gaps.count())
    if n_gaps == 0:
        return None, 0.0
    penalty = min(0.2, n_gaps / len(prices))
    return f"平日中の異常な時系列ギャップが {n_gaps} 箇所", penalty


def check_cot(cot: pd.DataFrame, currency: str) -> tuple[list[str], float]:
    """COT時系列を検査し (warnings, coverage 0-1) を返す。"""
    warnings: list[str] = []
    if cot.empty:
        return [f"{currency} のCOT時系列が空"], 0.0

    penalties = 0.0
    dates = pd.to_datetime(cot["report_date"]).sort_values()
    gaps = dates.diff().dropna().dt.days
    missing_weeks = int((gaps > COT_GAP_TOLERANCE_DAYS).sum())
    if missing_weeks:
        warnings.append(f"週次データの欠測が {missing_weeks} 箇所")
        penalties += min(0.3, missing_weeks / max(1, len(cot)))

    # 週数が少なすぎると特徴量(COT index等)が組めない
    if len(cot) < 20:
        warnings.append(f"COT週数が {len(cot)} と少ない(index窓に不足の恐れ)")
        penalties += 0.3

    coverage = round(max(0.0, 1.0 - penalties), 3)
    return warnings, coverage


def build_report(
    prices: pd.DataFrame,
    cot: dict[str, pd.DataFrame],
    timeframe: str,
) -> QualityReport:
    """価格 + 複数通貨COT を検査して QualityReport を組み立てる。"""
    price_warns, price_cov = check_prices(prices, timeframe)
    cot_warns: dict[str, list[str]] = {}
    cot_covs: list[float] = []
    for ccy, frame in cot.items():
        warns, cov = check_cot(frame, ccy)
        cot_warns[ccy] = warns
        cot_covs.append(cov)
    cot_coverage = round(float(np.mean(cot_covs)), 3) if cot_covs else 0.0
    return QualityReport(
        price_bars=len(prices),
        price_warnings=price_warns,
        cot_warnings=cot_warns,
        price_coverage=price_cov,
        cot_coverage=cot_coverage,
    )


# ---------------------------------------------------------------- 正規化


def normalize_prices(prices: pd.DataFrame) -> pd.DataFrame:
    """クリーンなOHLCV + log_return 列を付けた DataFrame を返す。

    - 非正/破綻バーを除去、重複indexは最初を採用、昇順ソート。
    - log_return = log(close).diff()(最初はNaN)。
    """
    if prices.empty:
        out = prices.copy()
        out["log_return"] = pd.Series(dtype=float)
        return out
    clean = prices.copy()
    clean = clean[~clean.index.duplicated(keep="first")].sort_index()
    ohlc = clean[["open", "high", "low", "close"]]
    valid = (ohlc > 0).all(axis=1) & (clean["high"] >= clean["low"])
    clean = clean[valid]
    clean["log_return"] = np.log(clean["close"]).diff()
    return clean


def normalize_cot(cot: pd.DataFrame) -> pd.DataFrame:
    """COT時系列を report_date 昇順・重複除去して返す(特徴量結合の前処理)。"""
    if cot.empty:
        return cot.copy()
    out = cot.copy()
    out["report_date"] = pd.to_datetime(out["report_date"])
    out = out.drop_duplicates(subset="report_date", keep="last").sort_values("report_date")
    return out.reset_index(drop=True)

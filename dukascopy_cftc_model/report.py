"""結果の集約とレポート化(パイプライン8段目=最終出力)。

walk-forward の結果 + データ来歴 + 品質スコア + 過剰最適化検定 を1つの
辞書(JSON化可能)と人間可読サマリにまとめる。ユーザー要求の最終出力:

    期待値・勝率・DD・PF・Sharpe・特徴量寄与

に加えて、それが「本物のエッジか多重検定の当たりくじか」を PBO/DSR で併記する
(fx_backtester/overfitting.py を再利用)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, UTC
from typing import Any

import numpy as np

from fx_backtester.overfitting import deflated_sharpe_ratio

from .quality import QualityReport
from .walk_forward import WalkForwardResult


@dataclass
class PipelineReport:
    """パイプライン全体の最終レポート。"""

    symbol: str
    timeframe: str
    horizon: int
    metrics: dict[str, float | int]
    feature_importance: list[tuple[str, float]]
    fold_summaries: list[dict[str, Any]]
    quality: dict[str, Any]
    provenance: dict[str, Any]
    overfitting: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "horizon": self.horizon,
            "generated_at": datetime.now(UTC).isoformat(),
            "metrics": self.metrics,
            "feature_importance": [
                {"feature": name, "coefficient": coef} for name, coef in self.feature_importance
            ],
            "folds": self.fold_summaries,
            "quality": self.quality,
            "provenance": self.provenance,
            "overfitting": self.overfitting,
        }

    def summary(self) -> str:
        m = self.metrics
        lines = [
            "=" * 60,
            f" Dukascopy×CFTC Ridgeモデル レポート — {self.symbol} {self.timeframe}",
            f" 将来リターン ホライズン: {self.horizon} バー",
            "=" * 60,
            "",
            "【バックテスト成績(ウォークフォワード, out-of-sample)】",
            f"  トレード数     : {m.get('trade_count', 0)}",
            f"  勝率           : {m.get('win_rate', 0.0):.1%}",
            f"  期待値/トレード: {m.get('expectancy_usd', 0.0):,.2f} USD"
            f" ({m.get('expectancy_r', 0.0):+.3f} R)",
            f"  プロフィットファクター(PF): {m.get('profit_factor', 0.0):.2f}",
            f"  最大ドローダウン: {m.get('max_drawdown_pct', 0.0):.2%}"
            f" ({m.get('max_drawdown_usd', 0.0):,.0f} USD)",
            f"  Sharpe(年率)   : {m.get('sharpe_ratio', 0.0):.2f}",
            f"  総リターン     : {m.get('total_return_pct', 0.0):.2%}",
            "",
            "【特徴量寄与(fold平均・標準化係数, |係数|降順 上位10)】",
        ]
        for name, coef in self.feature_importance[:10]:
            bar = (
                "█" * min(30, int(abs(coef) / self._max_abs_coef() * 30))
                if self._max_abs_coef()
                else ""
            )
            lines.append(f"  {name:<26} {coef:+.4f}  {bar}")

        lines += ["", "【データ品質】", f"  {self.quality.get('summary', '(なし)')}"]

        if self.overfitting:
            lines += ["", "【過剰最適化の検定】"]
            dsr = self.overfitting.get("dsr")
            if dsr is not None:
                verdict = (
                    "本物のエッジの可能性(≥0.95)" if dsr >= 0.95 else "まぐれ当たりの疑い(<0.95)"
                )
                lines.append(f"  Deflated Sharpe Ratio: {dsr:.3f} → {verdict}")
            if "note" in self.overfitting:
                lines.append(f"  {self.overfitting['note']}")

        lines += ["", "【来歴】"]
        for k, v in self.provenance.items():
            lines.append(f"  {k}: {v}")
        return "\n".join(lines)

    def _max_abs_coef(self) -> float:
        if not self.feature_importance:
            return 0.0
        return max(abs(c) for _, c in self.feature_importance) or 0.0


def _fold_summaries(result: WalkForwardResult) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for f, sharpe in zip(result.folds, result.fold_sharpes or [0.0] * len(result.folds)):
        out.append(
            {
                "fold": f.fold,
                "train": [str(f.train_start), str(f.train_end)],
                "test": [str(f.test_start), str(f.test_end)],
                "alpha": f.alpha,
                "n_train": f.n_train,
                "n_test": f.n_test,
                "n_trades": f.n_trades,
                "test_sharpe": round(sharpe, 4),
            }
        )
    return out


def _overfitting_assessment(result: WalkForwardResult) -> dict[str, Any]:
    """OOSトレードのリターン + fold別Sharpe から DSR を計算する。

    trial_sharpes は「各foldのtest Sharpe」= 探索した試行の代理。データ不足時は
    note を付けてスキップ(検定は劣化しても死なない)。
    """
    trades = result.trades
    if trades.empty or "net_pnl" not in trades.columns or len(trades) < 5:
        return {"note": "トレード数が少なくDSRを計算できません(5件以上必要)"}
    trial_sharpes = [s for s in result.fold_sharpes if np.isfinite(s)]
    if len(trial_sharpes) < 1:
        return {"note": "fold別Sharpeが不足しDSRを計算できません"}
    try:
        dsr_result = deflated_sharpe_ratio(trades["net_pnl"].to_numpy(dtype=float), trial_sharpes)
    except ValueError as exc:
        return {"note": f"DSR計算不能: {exc}"}
    return {
        "dsr": round(dsr_result["dsr"], 4),
        "sharpe_per_trade": round(dsr_result["sharpe_per_period"], 4),
        "expected_max_sharpe": round(dsr_result["expected_max_sharpe"], 4),
        "n_trials": dsr_result["n_trials"],
    }


def build_report(
    symbol: str,
    timeframe: str,
    horizon: int,
    wf_result: WalkForwardResult,
    quality: QualityReport,
    provenance: dict[str, Any],
) -> PipelineReport:
    """walk-forward結果 + 品質 + 来歴 を最終レポートに集約する。"""
    return PipelineReport(
        symbol=symbol,
        timeframe=timeframe,
        horizon=horizon,
        metrics=wf_result.metrics,
        feature_importance=wf_result.feature_importance,
        fold_summaries=_fold_summaries(wf_result),
        quality={
            "coverage": quality.coverage,
            "price_coverage": quality.price_coverage,
            "cot_coverage": quality.cot_coverage,
            "usable": quality.is_usable,
            "summary": quality.summary().replace("\n", " | "),
        },
        provenance=provenance,
        overfitting=_overfitting_assessment(wf_result),
    )

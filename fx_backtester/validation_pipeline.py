"""検証パイプライン — walk-forward → PBO/DSR/SPA → ドリフト → デプロイ合否判定。

レポート(FX AI.md)の核心「予測精度そのものより総合システム(予測×執行×リスク×
検証×規律)で差がつく」を、既にある部品を1本の判定パイプラインに結線して実現する層。
新しい戦略ロジックは足さず、以下を1コマンドの合否判定に束ねる:

    walk_forward.WalkForwardValidator  … ローリング学習/検証(purge+embargo)
        └ trial_log.TrialLogger        … 全試行の記録(過剰最適化検定の入力)
    overfitting.probability_of_backtest_overfitting  … PBO(CSCV)
    overfitting.deflated_sharpe_ratio                … コスト込みDSR
    overfitting.superior_predictive_ability          … SPA検定
    drift.scan_for_drift (ADWIN)                      … OOSリターンのドリフト検出

レポートが「第1段階の閾値」「方針を変えるベンチマーク」として挙げる合否基準を、
そのままデプロイ拒否条件にする(ユーザー選択で全4基準):

  1. コスト込みDSRが非有意(DSR < dsr_min。既定0.95) → 棄却
  2. PBO ≥ pbo_max(既定0.5。IS順位にOOS予測力ゼロ) → 棄却
  3. SPA p値 ≥ spa_max(既定0.05。最良の優位が有意でない) → 棄却
  4. OOS期間でADWINがドリフトを検出 → 「要再学習」警告(=デプロイ保留)

いずれか1つでも該当すればデプロイ拒否。全て通れば合格。判定理由は必ず文字列で
残す(監査可能性。params_gate / promotion と同じ「来歴+明示基準」の思想)。

依存は既存モジュールのみ(numpy/pandas)。ネットワーク非依存でテストできる。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from fx_intel.drift import scan_for_drift
from fx_backtester.overfitting import (
    deflated_sharpe_ratio,
    probability_of_backtest_overfitting,
    superior_predictive_ability,
)
from fx_backtester.trial_log import TrialLogger
from fx_backtester.walk_forward import WalkForwardResult, WalkForwardValidator


@dataclass
class DeployGateConfig:
    """デプロイ合否の閾値。既定値はレポートの第1段階基準に一致。"""

    dsr_min: float = 0.95  # コスト込みDSRがこれ未満なら棄却
    pbo_max: float = 0.5  # PBOがこれ以上なら棄却
    spa_max: float = 0.05  # SPA p値がこれ以上なら棄却
    pbo_blocks: int = 8  # CSCVのブロック数(偶数, >=4)
    spa_bootstrap: int = 1000  # SPAの定常ブートストラップ反復数
    spa_seed: int | None = 7  # 再現性のための乱数シード
    drift_delta: float = 0.002  # ADWINの誤検出許容確率
    require_no_drift: bool = True  # OOSドリフト検出をデプロイ拒否条件にするか


@dataclass
class DeployVerdict:
    """パイプラインの最終判定と、各ゲートの根拠。"""

    deploy_ok: bool
    reasons: list[str]  # 棄却/保留の理由(合格なら空)
    dsr: float | None
    pbo: float | None
    spa_pvalue: float | None
    drift_points: list[int]
    n_folds: int
    n_oos_observations: int
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "deploy_ok": self.deploy_ok,
            "reasons": self.reasons,
            "dsr": self.dsr,
            "pbo": self.pbo,
            "spa_pvalue": self.spa_pvalue,
            "drift_points": self.drift_points,
            "n_folds": self.n_folds,
            "n_oos_observations": self.n_oos_observations,
            "metrics": self.metrics,
        }


def _oos_returns(result: WalkForwardResult) -> pd.Series:
    """採択された各foldのOOSテスト区間のリターンを時系列に連結する。

    デプロイ判定は「学習で選んだパラメータが未使用のOOSでどうだったか」で下すため、
    DSR/ドリフトの入力は必ずテスト区間のリターン(コスト控除後のequity変化)を使う。
    """
    pieces: list[pd.Series] = []
    for test_result in result.selected_test_results:
        equity = test_result.equity_curve.get("equity")
        if equity is None or len(equity) < 2:
            continue
        pieces.append(equity.pct_change().dropna())
    if not pieces:
        return pd.Series(dtype=float)
    combined = pd.concat(pieces).sort_index()
    return combined[~combined.index.duplicated(keep="last")]


def evaluate_deploy_gate(
    validator: WalkForwardValidator,
    data: dict[str, pd.DataFrame],
    config: DeployGateConfig | None = None,
    trial_logger: TrialLogger | None = None,
) -> DeployVerdict:
    """walk-forward を回し、4基準でデプロイ合否を判定する。

    validator は trial_logger を持っていれば全試行を記録する。持っていなければ
    ここで作って差し込む(PBO/SPAはリターン行列=全IS試行が必要なため)。
    """
    config = config or DeployGateConfig()
    if trial_logger is None:
        trial_logger = validator.trial_logger or TrialLogger(
            context={"generated_by": "validation_pipeline.evaluate_deploy_gate"}
        )
    validator.trial_logger = trial_logger

    result = validator.run(data)
    reasons: list[str] = []
    metrics: dict[str, Any] = {}

    oos = _oos_returns(result)
    n_oos = int(len(oos))
    matrix = trial_logger.returns_matrix()

    # --- ゲート1: コスト込みDSR(OOSリターン + 全試行Sharpe) ---
    dsr_value: float | None = None
    trial_sharpes = _trial_sharpes(trial_logger)
    if n_oos >= 3 and trial_sharpes:
        try:
            dsr_result = deflated_sharpe_ratio(oos, trial_sharpes)
            dsr_value = float(dsr_result["dsr"])
            metrics["dsr_detail"] = dsr_result
            if dsr_value < config.dsr_min:
                reasons.append(
                    f"DSR {dsr_value:.3f} < {config.dsr_min:.2f}(コスト込みSharpeが有意でない)"
                )
        except ValueError as error:
            reasons.append(f"DSRを計算できない({error})")
    else:
        reasons.append(f"DSRの標本不足(OOS {n_oos}件 / 試行{len(trial_sharpes)}件)")

    # --- ゲート2: PBO(CSCV。全IS試行のリターン行列が必要) ---
    pbo_value: float | None = None
    if not matrix.empty and matrix.shape[1] >= 2:
        try:
            pbo_result = probability_of_backtest_overfitting(matrix, n_blocks=config.pbo_blocks)
            pbo_value = float(pbo_result["pbo"])
            metrics["pbo_detail"] = pbo_result
            if pbo_value >= config.pbo_max:
                reasons.append(
                    f"PBO {pbo_value:.3f} ≥ {config.pbo_max:.2f}(IS順位にOOS予測力なし)"
                )
        except ValueError as error:
            reasons.append(f"PBOを計算できない({error})")
    else:
        reasons.append("PBOの試行不足(リターン行列が2列未満)")

    # --- ゲート3: SPA検定(全IS試行を戦略群とみなす) ---
    spa_value: float | None = None
    if not matrix.empty and matrix.shape[0] >= 3:
        try:
            spa_result = superior_predictive_ability(
                matrix,
                n_bootstrap=config.spa_bootstrap,
                seed=config.spa_seed,
            )
            spa_value = float(spa_result["spa_pvalue"])
            metrics["spa_detail"] = spa_result
            if spa_value >= config.spa_max:
                reasons.append(
                    f"SPA p値 {spa_value:.3f} ≥ {config.spa_max:.2f}(最良の優位が有意でない)"
                )
        except ValueError as error:
            reasons.append(f"SPAを計算できない({error})")
    else:
        reasons.append("SPAの標本不足")

    # --- ゲート4: OOSドリフト(ADWIN。的中の代理として損益フラグ系列を監視) ---
    drift_points: list[int] = []
    if config.require_no_drift and n_oos >= 10:
        # OOSリターンの符号(勝ち=1/負け=0)を的中フラグ相当としてADWINに流す
        win_flags = [1.0 if r > 0 else 0.0 for r in oos.to_list()]
        drift_scan = scan_for_drift(win_flags, delta=config.drift_delta)
        drift_points = list(drift_scan.drift_points)
        metrics["drift_detail"] = {
            "drift_points": drift_points,
            "final_win_rate": drift_scan.final_mean,
            "total": drift_scan.total,
        }
        if drift_points:
            reasons.append(
                f"OOS期間で勝率ドリフトを検出(点{drift_points})→要再学習(デプロイ保留)"
            )

    return DeployVerdict(
        deploy_ok=not reasons,
        reasons=reasons,
        dsr=dsr_value,
        pbo=pbo_value,
        spa_pvalue=spa_value,
        drift_points=drift_points,
        n_folds=len(result.folds),
        n_oos_observations=n_oos,
        metrics=metrics,
    )


def _trial_sharpes(trial_logger: TrialLogger) -> list[float]:
    """試行ログの各IS試行の per-period Sharpe を集める(DSRの探索回数控除に使う)。

    trials.jsonl の metrics.sharpe_ratio を使う(engine が per-period で出す前提)。
    非有限は除外する。学習フェーズ(wf_train)だけを対象=探索の母集団。
    """
    sharpes: list[float] = []
    for trial in trial_logger.trials:
        if trial.get("phase") != "wf_train":
            continue
        metrics = trial.get("metrics") or {}
        value = metrics.get("sharpe_ratio")
        if isinstance(value, (int, float)) and value == value:  # NaN 除外
            sharpes.append(float(value))
    return sharpes

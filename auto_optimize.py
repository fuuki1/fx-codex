#!/usr/bin/env python3
"""自動パラメータ最適化スクリプト（安全ゲート付き）。

実データ CSV（--data または環境変数 OPTIMIZE_DATA で必須指定）でグリッドサーチを
走らせ、最良パラメータを strategy_params.candidate.json に書き出す。

安全設計（params_gate.py と対になる）:
- データゲート: 同梱の合成サンプル（乱数生成）や行数・期間不足のデータは
  params_gate.validate_data_source() が拒否し、何も書き出さない。
- IS/OOS 分割: 前半 70% でパラメータを選び、後半 30% で検証。OOS で劣化する
  パラメータには overfit 警告を付ける。
- 来歴メタデータ: データのパス・sha256・期間・取引数・検証結果を provenance として
  出力に埋め込む。来歴の無いパラメータは promote_params.py / 読み込み側が拒否する。
- 試行ログ: グリッドサーチの全試行(パラメータ・指標・リターン系列)を
  runs/trial_logs/<run_id>/ に記録し、provenance.trials から参照する。
  過剰最適化検定(PBO/DSR)の入力であり、探索履歴の監査証跡でもある。
- 過剰最適化検定: 試行ログから PBO(CSCV) と Deflated Sharpe Ratio を自動計算して
  provenance.overfitting に記録する。PBO >= 0.5 / DSR < 0.95 は warning になり、
  promote_params.py が既定(--force無し)で昇格を拒否する。
- 直接配備しない: 出力は candidate ファイルまで。strategy_params.json への昇格は
  promote_params.py での明示的な承認が必要。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, UTC
from itertools import product
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

import params_gate
from fx_backtester.engine import BacktestConfig, BacktestEngine, BacktestResult
from fx_backtester.execution import ExecutionConfig
from fx_backtester.overfitting import (
    deflated_sharpe_ratio,
    per_period_sharpe,
    probability_of_backtest_overfitting,
)
from fx_backtester.risk import RiskConfig
from fx_backtester.strategies.moving_average_cross import MovingAverageCross
from fx_backtester.trial_log import TrialLogger

CANDIDATE_OUTPUT = Path(__file__).parent / "strategy_params.candidate.json"
DEFAULT_TRIAL_LOG_DIR = Path(__file__).parent / "runs" / "trial_logs"

# 過剰最適化検定の警告閾値。warning が付いた candidate は promote が既定で拒否する。
# PBO 0.5 = 「ISの最良がOOSで中央値未満に沈む確率が五分五分」＝探索順位に予測力なし
# DSR 0.95 = 「探索回数を考慮してもSharpeが偶然でない確率95%」の慣例的な合格線
PBO_WARN_THRESHOLD = 0.5
DSR_WARN_THRESHOLD = 0.95
# PBOのブロック数は観測数に応じて自動選択(1ブロックあたり最低8観測を確保)
PBO_BLOCK_CHOICES = (16, 12, 8, 6, 4)
PBO_MIN_OBS_PER_BLOCK = 8

# ── グリッド定義 ──────────────────────────────────────────────────────────────
FAST_WINDOWS = [10, 15, 20, 25]
SLOW_WINDOWS = [40, 50, 60, 80, 100]
ATR_WINDOWS = [14]
ATR_MULTS = [1.5, 2.0, 2.5]

OOS_FRACTION = 0.3  # 末尾30%を検証専用（アウトオブサンプル）に取り分ける


def score(metrics: dict) -> float:
    sharpe = metrics.get("sharpe_ratio", 0) or 0
    pf = metrics.get("profit_factor", 0) or 0
    dd = abs(metrics.get("max_drawdown_pct", 100) or 100)
    if dd == 0:
        return 0.0
    return sharpe * pf / (dd / 100 + 1e-9)


def load_data(data_file: Path) -> dict[str, pd.DataFrame]:
    df = pd.read_csv(data_file, parse_dates=["timestamp"])
    frames: dict[str, pd.DataFrame] = {}
    for symbol, group in df.groupby("symbol"):
        frames[str(symbol)] = group.set_index("timestamp").sort_index()
    return frames


def run_backtest(
    symbol: str, df: pd.DataFrame, fast: int, slow: int, atr: int, mult: float
) -> BacktestResult | None:
    if len(df) < slow + 10:
        return None
    strategy = MovingAverageCross(
        fast_window=fast, slow_window=slow, atr_window=atr, stop_atr_multiple=mult
    )
    config = BacktestConfig(
        initial_cash=100_000,
        risk=RiskConfig(
            risk_per_trade_pct=0.005,
            max_daily_loss_pct=0.015,
        ),
        execution=ExecutionConfig(
            spread_pips={symbol: 0.6},
            slippage_pips={symbol: 0.1},
        ),
        max_open_positions=2,
    )
    engine = BacktestEngine(strategy=strategy, config=config)
    try:
        return engine.run({symbol: df})
    except Exception:
        return None


def optimize(
    frames: dict[str, pd.DataFrame],
    symbols: list[str],
    trial_logger: TrialLogger | None = None,
) -> dict:
    """前半(IS)でグリッドサーチ、後半(OOS)で検証。candidate の中身を返す。

    trial_logger を渡すと、有効な全試行(IS)と採択パラメータのOOS評価を記録する。
    """
    best_score = -1e9
    best: dict = {}
    total = len(FAST_WINDOWS) * len(SLOW_WINDOWS) * len(ATR_WINDOWS) * len(ATR_MULTS) * len(symbols)
    done = 0

    print(
        f"[optimize] グリッドサーチ開始: {total} 通り（IS {1 - OOS_FRACTION:.0%} / OOS {OOS_FRACTION:.0%}）"
    )

    for symbol, fast, slow, atr, mult in product(
        symbols, FAST_WINDOWS, SLOW_WINDOWS, ATR_WINDOWS, ATR_MULTS
    ):
        done += 1
        if fast >= slow:
            continue
        df = frames[symbol]
        split = int(len(df) * (1 - OOS_FRACTION))
        result = run_backtest(symbol, df.iloc[:split], fast, slow, atr, mult)
        metrics = result.metrics if result is not None else {}
        s = score(metrics)
        trial_id = f"{symbol}-f{fast}-s{slow}-a{atr}-m{mult}"
        if trial_logger is not None and result is not None:
            equity = result.equity_curve["equity"]
            trial_logger.log(
                trial_id,
                params={
                    "symbol": symbol,
                    "fast_window": fast,
                    "slow_window": slow,
                    "atr_window": atr,
                    "atr_multiple": mult,
                },
                phase="is_grid",
                metrics=metrics,
                score=s,
                window={
                    "kind": "IS",
                    "start": result.equity_curve.index[0],
                    "end": result.equity_curve.index[-1],
                },
                returns=equity.pct_change().dropna(),
            )
        if done % 20 == 0:
            print(f"  {done}/{total} 完了...")
        if metrics and s > best_score:
            best_score = s
            best = {
                "symbol": symbol,
                "fast": fast,
                "slow": slow,
                "atr": atr,
                "mult": mult,
                "trial_id": trial_id,
                "is_metrics": metrics,
                "score": s,
            }

    if not best:
        return {}

    df = frames[best["symbol"]]
    split = int(len(df) * (1 - OOS_FRACTION))
    oos_result = run_backtest(
        best["symbol"], df.iloc[split:], best["fast"], best["slow"], best["atr"], best["mult"]
    )
    oos_metrics = oos_result.metrics if oos_result is not None else {}
    best["oos_metrics"] = oos_metrics
    if trial_logger is not None:
        trial_logger.mark_selected(best["trial_id"])
        if oos_result is not None:
            trial_logger.log(
                f"{best['trial_id']}-oos",
                params={
                    "symbol": best["symbol"],
                    "fast_window": best["fast"],
                    "slow_window": best["slow"],
                    "atr_window": best["atr"],
                    "atr_multiple": best["mult"],
                },
                phase="oos",
                metrics=oos_metrics,
                window={
                    "kind": "OOS",
                    "start": oos_result.equity_curve.index[0],
                    "end": oos_result.equity_curve.index[-1],
                },
            )
    print(
        f"[optimize] 完了: score={best_score:.4f} "
        f"IS sharpe={best['is_metrics'].get('sharpe_ratio')} "
        f"OOS sharpe={oos_metrics.get('sharpe_ratio', 'n/a')}"
    )
    return best


def evaluate_overfitting(trial_logger: TrialLogger, selected_trial_id: str | None) -> dict:
    """試行ログから PBO と DSR を計算する。計算不能な側は status=skipped と理由を返す。"""
    matrix = trial_logger.returns_matrix()
    summary: dict = {}

    try:
        n_obs = len(matrix)
        blocks = next((s for s in PBO_BLOCK_CHOICES if n_obs >= s * PBO_MIN_OBS_PER_BLOCK), None)
        if blocks is None:
            raise ValueError(f"観測数が不足({n_obs}件)でCSCVブロックを構成できない")
        summary["pbo"] = probability_of_backtest_overfitting(matrix, n_blocks=blocks)
    except ValueError as error:
        summary["pbo"] = {"status": "skipped", "reason": str(error)}

    try:
        if matrix.empty or selected_trial_id not in matrix.columns:
            raise ValueError("採択試行のリターン系列が試行ログに無い")
        trial_sharpes = [per_period_sharpe(matrix[column]) for column in matrix.columns]
        summary["dsr"] = deflated_sharpe_ratio(matrix[selected_trial_id], trial_sharpes)
    except ValueError as error:
        summary["dsr"] = {"status": "skipped", "reason": str(error)}

    return summary


def build_candidate(
    best: dict,
    data_path: Path,
    frames: dict[str, pd.DataFrame],
    min_trades: int,
    trials: dict | None = None,
    overfitting: dict | None = None,
) -> dict:
    is_m, oos_m = best["is_metrics"], best["oos_metrics"]
    is_sharpe = is_m.get("sharpe_ratio", 0) or 0
    oos_sharpe = oos_m.get("sharpe_ratio", 0) or 0
    trade_count = int(is_m.get("trade_count", 0) or 0) + int(oos_m.get("trade_count", 0) or 0)

    warnings: list[str] = []
    if not oos_m:
        warnings.append("OOS区間の検証を実行できなかった（データ不足）")
    elif oos_sharpe <= 0:
        warnings.append(f"overfit警告: OOS sharpe が非正 ({oos_sharpe:.4f})")
    elif is_sharpe > 0 and oos_sharpe < 0.5 * is_sharpe:
        warnings.append(
            f"overfit警告: OOS sharpe ({oos_sharpe:.4f}) が IS ({is_sharpe:.4f}) の半分未満"
        )
    if trade_count < min_trades:
        warnings.append(f"取引数不足: {trade_count}（最低 {min_trades}）")

    if overfitting:
        pbo_value = (overfitting.get("pbo") or {}).get("pbo")
        if isinstance(pbo_value, int | float) and pbo_value >= PBO_WARN_THRESHOLD:
            warnings.append(
                f"PBO警告: 過剰最適化確率 {pbo_value:.2f}（閾値 {PBO_WARN_THRESHOLD}）。"
                "ISで最良の試行がOOSでは中央値未満に沈みやすい"
            )
        dsr_stats = overfitting.get("dsr") or {}
        dsr_value = dsr_stats.get("dsr")
        if isinstance(dsr_value, int | float) and dsr_value < DSR_WARN_THRESHOLD:
            warnings.append(
                f"DSR警告: deflated Sharpe確率 {dsr_value:.3f} < {DSR_WARN_THRESHOLD}。"
                f"{dsr_stats.get('n_trials')}回の探索を考慮するとISのSharpeは偶然の域を出ない"
            )

    all_rows = sum(len(f) for f in frames.values())
    start = min(f.index.min() for f in frames.values())
    end = max(f.index.max() for f in frames.values())

    provenance_extra: dict = {}
    if trials:
        provenance_extra["trials"] = trials
    if overfitting:
        provenance_extra["overfitting"] = overfitting
    return {
        "fast_window": best["fast"],
        "slow_window": best["slow"],
        "atr_window": best["atr"],
        "atr_multiple": best["mult"],
        "best_symbol": best["symbol"],
        "score": round(best["score"], 4),
        "sharpe": round(is_sharpe, 4),
        "profit_factor": round(is_m.get("profit_factor", 0) or 0, 4),
        "max_drawdown_pct": round(is_m.get("max_drawdown_pct", 0) or 0, 4),
        "updated_at": datetime.now(UTC).isoformat(),
        "provenance": {
            "schema": params_gate.SCHEMA_VERSION,
            "generated_by": "auto_optimize.py",
            "data": params_gate.data_provenance(data_path, all_rows, str(start), str(end)),
            "grid": {
                "fast_window": FAST_WINDOWS,
                "slow_window": SLOW_WINDOWS,
                "atr_window": ATR_WINDOWS,
                "atr_multiple": ATR_MULTS,
            },
            "split": {"oos_fraction": OOS_FRACTION},
            **provenance_extra,
            "trade_count": trade_count,
            "oos": {
                "sharpe": round(oos_sharpe, 4),
                "trade_count": int(oos_m.get("trade_count", 0) or 0),
                "is_sharpe": round(is_sharpe, 4),
            },
            "warnings": warnings,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--data",
        default=os.environ.get("OPTIMIZE_DATA"),
        help="実データ CSV のパス（必須。環境変数 OPTIMIZE_DATA でも指定可）",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=CANDIDATE_OUTPUT,
        help=f"candidate の出力先（既定: {CANDIDATE_OUTPUT.name}）",
    )
    parser.add_argument(
        "--symbols",
        nargs="*",
        default=None,
        help="対象シンボル（既定: データ内の全シンボル）",
    )
    parser.add_argument(
        "--min-trades",
        type=int,
        default=params_gate.MIN_TRADE_COUNT,
        help="取引数の下限（未満は警告が付き、promote が既定で拒否する）",
    )
    parser.add_argument(
        "--trial-log-dir",
        type=Path,
        default=DEFAULT_TRIAL_LOG_DIR,
        help=f"試行ログの出力先（既定: {DEFAULT_TRIAL_LOG_DIR}。run_idごとにサブディレクトリを作る）",
    )
    args = parser.parse_args(argv)

    data_path, errors = params_gate.validate_data_source(args.data)
    if data_path is None:
        for e in errors:
            print(f"[optimize] ⛔ {e}")
        print("[optimize] candidate は書き出しません（既存パラメータを維持）。")
        return 1

    frames = load_data(data_path)
    symbols = args.symbols or sorted(frames)
    missing = [s for s in symbols if s not in frames]
    if missing:
        print(f"[optimize] ⛔ データに存在しないシンボル: {missing}（存在: {sorted(frames)}）")
        return 1

    trial_logger = TrialLogger(
        context={
            "generated_by": "auto_optimize.py",
            "data_path": str(data_path),
            "symbols": symbols,
            "grid": {
                "fast_window": FAST_WINDOWS,
                "slow_window": SLOW_WINDOWS,
                "atr_window": ATR_WINDOWS,
                "atr_multiple": ATR_MULTS,
            },
            "oos_fraction": OOS_FRACTION,
        }
    )
    best = optimize(frames, symbols, trial_logger)
    if not best:
        print("[optimize] ⛔ 有効なバックテスト結果が得られなかった。candidate は書き出しません。")
        return 1

    log_paths = trial_logger.write(args.trial_log_dir)
    print(f"[optimize] 試行ログ: {log_paths['run_dir']}（{trial_logger.trial_count} 試行）")
    trials_info = {
        "count": trial_logger.trial_count,
        "run_id": trial_logger.run_id,
        "log_dir": str(log_paths["run_dir"]),
    }

    overfitting = evaluate_overfitting(trial_logger, best.get("trial_id"))
    pbo_stats, dsr_stats = overfitting["pbo"], overfitting["dsr"]
    if "pbo" in pbo_stats:
        print(
            f"[optimize] PBO={pbo_stats['pbo']:.2f} "
            f"(blocks={pbo_stats['n_blocks']}, combinations={pbo_stats['n_combinations']})"
        )
    else:
        print(f"[optimize] PBO 計算スキップ: {pbo_stats.get('reason')}")
    if "dsr" in dsr_stats:
        print(
            f"[optimize] DSR={dsr_stats['dsr']:.3f} "
            f"(SR*={dsr_stats['expected_max_sharpe']:.4f}, trials={dsr_stats['n_trials']})"
        )
    else:
        print(f"[optimize] DSR 計算スキップ: {dsr_stats.get('reason')}")

    candidate = build_candidate(
        best, data_path, frames, args.min_trades, trials=trials_info, overfitting=overfitting
    )
    args.output.write_text(json.dumps(candidate, indent=2, ensure_ascii=False))
    print(f"[optimize] → {args.output}")
    for w in candidate["provenance"]["warnings"]:
        print(f"[optimize] ⚠️ {w}")
    print("[optimize] 配備するには: python3 promote_params.py  （検証+承認+ロールバック退避）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

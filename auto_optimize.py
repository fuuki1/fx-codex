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
- 直接配備しない: 出力は candidate ファイルまで。strategy_params.json への昇格は
  promote_params.py での明示的な承認が必要。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from itertools import product
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

import params_gate
from fx_backtester.engine import BacktestConfig, BacktestEngine
from fx_backtester.execution import ExecutionConfig
from fx_backtester.risk import RiskConfig
from fx_backtester.strategies.moving_average_cross import MovingAverageCross

CANDIDATE_OUTPUT = Path(__file__).parent / "strategy_params.candidate.json"

# ── グリッド定義 ──────────────────────────────────────────────────────────────
FAST_WINDOWS = [10, 15, 20, 25]
SLOW_WINDOWS = [40, 50, 60, 80, 100]
ATR_WINDOWS  = [14]
ATR_MULTS    = [1.5, 2.0, 2.5]

OOS_FRACTION = 0.3  # 末尾30%を検証専用（アウトオブサンプル）に取り分ける


def score(metrics: dict) -> float:
    sharpe = metrics.get("sharpe_ratio", 0) or 0
    pf     = metrics.get("profit_factor", 0) or 0
    dd     = abs(metrics.get("max_drawdown_pct", 100) or 100)
    if dd == 0:
        return 0.0
    return sharpe * pf / (dd / 100 + 1e-9)


def load_data(data_file: Path) -> dict[str, pd.DataFrame]:
    df = pd.read_csv(data_file, parse_dates=["timestamp"])
    frames: dict[str, pd.DataFrame] = {}
    for symbol, group in df.groupby("symbol"):
        frames[str(symbol)] = group.set_index("timestamp").sort_index()
    return frames


def run_backtest(symbol: str, df: pd.DataFrame,
                 fast: int, slow: int, atr: int, mult: float) -> dict:
    if len(df) < slow + 10:
        return {}
    strategy = MovingAverageCross(fast_window=fast, slow_window=slow,
                                  atr_window=atr, stop_atr_multiple=mult)
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
        result = engine.run({symbol: df})
        return result.metrics
    except Exception:
        return {}


def optimize(frames: dict[str, pd.DataFrame], symbols: list[str]) -> dict:
    """前半(IS)でグリッドサーチ、後半(OOS)で検証。candidate の中身を返す。"""
    best_score  = -1e9
    best: dict = {}
    total = (len(FAST_WINDOWS) * len(SLOW_WINDOWS) * len(ATR_WINDOWS)
             * len(ATR_MULTS) * len(symbols))
    done  = 0

    print(f"[optimize] グリッドサーチ開始: {total} 通り（IS {1 - OOS_FRACTION:.0%} / OOS {OOS_FRACTION:.0%}）")

    for symbol, fast, slow, atr, mult in product(
        symbols, FAST_WINDOWS, SLOW_WINDOWS, ATR_WINDOWS, ATR_MULTS
    ):
        done += 1
        if fast >= slow:
            continue
        df = frames[symbol]
        split = int(len(df) * (1 - OOS_FRACTION))
        metrics = run_backtest(symbol, df.iloc[:split], fast, slow, atr, mult)
        s = score(metrics)
        if done % 20 == 0:
            print(f"  {done}/{total} 完了...")
        if metrics and s > best_score:
            best_score = s
            best = {
                "symbol": symbol,
                "fast": fast, "slow": slow, "atr": atr, "mult": mult,
                "is_metrics": metrics,
                "score": s,
            }

    if not best:
        return {}

    df = frames[best["symbol"]]
    split = int(len(df) * (1 - OOS_FRACTION))
    oos_metrics = run_backtest(best["symbol"], df.iloc[split:],
                               best["fast"], best["slow"], best["atr"], best["mult"])
    best["oos_metrics"] = oos_metrics
    print(f"[optimize] 完了: score={best_score:.4f} "
          f"IS sharpe={best['is_metrics'].get('sharpe_ratio')} "
          f"OOS sharpe={oos_metrics.get('sharpe_ratio', 'n/a')}")
    return best


def build_candidate(best: dict, data_path: Path, frames: dict[str, pd.DataFrame],
                    min_trades: int) -> dict:
    is_m, oos_m = best["is_metrics"], best["oos_metrics"]
    is_sharpe  = is_m.get("sharpe_ratio", 0) or 0
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

    all_rows = sum(len(f) for f in frames.values())
    start = min(f.index.min() for f in frames.values())
    end   = max(f.index.max() for f in frames.values())

    return {
        "fast_window":  best["fast"],
        "slow_window":  best["slow"],
        "atr_window":   best["atr"],
        "atr_multiple": best["mult"],
        "best_symbol":  best["symbol"],
        "score":            round(best["score"], 4),
        "sharpe":           round(is_sharpe, 4),
        "profit_factor":    round(is_m.get("profit_factor", 0) or 0, 4),
        "max_drawdown_pct": round(is_m.get("max_drawdown_pct", 0) or 0, 4),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "provenance": {
            "schema": params_gate.SCHEMA_VERSION,
            "generated_by": "auto_optimize.py",
            "data": params_gate.data_provenance(
                data_path, all_rows, str(start), str(end)
            ),
            "grid": {
                "fast_window": FAST_WINDOWS,
                "slow_window": SLOW_WINDOWS,
                "atr_window": ATR_WINDOWS,
                "atr_multiple": ATR_MULTS,
            },
            "split": {"oos_fraction": OOS_FRACTION},
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
        "--data", default=os.environ.get("OPTIMIZE_DATA"),
        help="実データ CSV のパス（必須。環境変数 OPTIMIZE_DATA でも指定可）",
    )
    parser.add_argument(
        "--output", type=Path, default=CANDIDATE_OUTPUT,
        help=f"candidate の出力先（既定: {CANDIDATE_OUTPUT.name}）",
    )
    parser.add_argument(
        "--symbols", nargs="*", default=None,
        help="対象シンボル（既定: データ内の全シンボル）",
    )
    parser.add_argument(
        "--min-trades", type=int, default=params_gate.MIN_TRADE_COUNT,
        help="取引数の下限（未満は警告が付き、promote が既定で拒否する）",
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

    best = optimize(frames, symbols)
    if not best:
        print("[optimize] ⛔ 有効なバックテスト結果が得られなかった。candidate は書き出しません。")
        return 1

    candidate = build_candidate(best, data_path, frames, args.min_trades)
    args.output.write_text(json.dumps(candidate, indent=2, ensure_ascii=False))
    print(f"[optimize] → {args.output}")
    for w in candidate["provenance"]["warnings"]:
        print(f"[optimize] ⚠️ {w}")
    print("[optimize] 配備するには: python3 promote_params.py  （検証+承認+ロールバック退避）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

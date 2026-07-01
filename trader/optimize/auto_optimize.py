"""
Mac mini 側の自律最適化エンジン（ウォークフォワード版）。

Docker コンテナ内で fx_backtester の `optimize` を 1 回実行し、アウトオブサンプル(OOS)
検証で選ばれた配備用パラメータを strategy_params.json に書き出す。
strategy.py はファイルを監視して自動で読み込む（再起動不要）。

旧版は単一データでのグリッドサーチ（過剰最適化しやすい）だったが、本版は
walk-forward / OOS でパラメータの汎化性能を検証し、overfit_warning も記録する。
さらに、(1) 実行前に IB Gateway から実ヒストリカルデータを取得して最適化対象にする
（取得できない場合のみ同梱サンプルへフォールバック）、(2) overfit_warning /
insufficient_trades が立った場合は strategy_params.json を上書きしない、という
2 点で「検証に落ちたパラメータをそのまま配備してしまう」ギャップを解消している。

fx_backtester は本リポジトリの trader/fx-codex に同梱。実行イメージは pandas/numpy を
含む trader-app（FXBT_IMAGE で上書き可）。deploy/optimize.sh + launchd
(com.trader.optimize.plist) が定期実行する（`make optimize` で手動実行も可能）。
"""
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

TRADER_DIR = Path(__file__).resolve().parent.parent
FXCODEX_DIR = TRADER_DIR / "fx-codex"
DATA_DIR = FXCODEX_DIR / "data"
SAMPLE_PRICES = FXCODEX_DIR / "examples" / "sample_prices.csv"
SAMPLE_EVENTS = FXCODEX_DIR / "examples" / "sample_events.csv"
PARAMS_FILE = FXCODEX_DIR / "strategy_params.json"
RESULT_LOG = FXCODEX_DIR / "optimize_result.log"
IMAGE = os.environ.get("FXBT_IMAGE", "trader-app:latest")
HISTORY_YEARS = int(os.environ.get("FXBT_HISTORY_YEARS", "5"))

# 探索グリッド（atr_multiple はライブ strategy のキー名。バックテスタでは stop_atr_multiple）
GRID = {
    "fast_window": [10, 20, 30],
    "slow_window": [40, 60, 80],
    "atr_window": [14],
    "atr_multiple": [1.5, 2.0, 2.5],
}


def score(m: dict) -> float:
    """単発バックテスト指標の総合スコア（後方互換のため維持）。"""
    sharpe = m.get("sharpe_ratio", 0) or 0
    pf = min(m.get("profit_factor", 0) or 0, 5)
    dd = abs(m.get("max_drawdown_pct", 100) or 100)
    if dd == 0:
        return 0
    return sharpe * 0.4 + pf * 0.4 - (dd / 100) * 0.2


def _grid_args() -> list[str]:
    grid_map = {
        "fast_window": GRID["fast_window"],
        "slow_window": GRID["slow_window"],
        "atr_window": GRID["atr_window"],
        "stop_atr_multiple": GRID["atr_multiple"],
    }
    args: list[str] = []
    for key, vals in grid_map.items():
        args += ["--grid", f"{key}=" + ",".join(str(v) for v in vals)]
    return args


def _export_history(symbol: str, asset: str, log) -> bool:
    """IB Gateway から実ヒストリカルデータを取得し DATA_DIR/history.csv へ書き出す。

    docker compose の `executor` サービス定義（env/ネットワーク）を借りて実行することで
    ib-gateway に compose のサービス名で到達できる（`make reconcile` と同じ手法）。
    docker/IB が使えない環境（CI 等）でも自律最適化自体は止めない: 失敗時は False を返し、
    呼び出し側は同梱サンプルデータへフォールバックする。
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [
        "docker", "compose", "run", "--rm", "--no-deps",
        "-v", f"{DATA_DIR}:/fx-codex-data",
        "executor", "python", "export_history.py",
        "--out", "/fx-codex-data/history.csv",
        "--symbol", symbol, "--asset", asset, "--years", str(HISTORY_YEARS),
    ]
    try:
        result = subprocess.run(
            cmd, cwd=TRADER_DIR, capture_output=True, text=True, timeout=300
        )
    except Exception as e:
        log(f"⚠️ ヒストリカルデータ取得を実行できません（サンプルデータで継続）: {e}")
        return False
    if result.returncode != 0:
        log(f"⚠️ ヒストリカルデータ取得に失敗 rc={result.returncode}（サンプルデータで継続）: {result.stderr[-500:]}")
        return False
    log(f"✅ 実ヒストリカルデータを取得（symbol={symbol}, {HISTORY_YEARS}年分）")
    return True


def _select_data_source(symbol: str, asset: str, log) -> tuple[str, list[str]]:
    """--data と --events の引数を選ぶ。実データ優先、取得できなければ同梱サンプル。

    サンプルの events は sample_prices.csv の日付に紐づく合成データなので、実データ側では使わない。
    """
    if _export_history(symbol, asset, log):
        return "/fx-codex/data/history.csv", []
    log("ℹ️ 同梱サンプルデータ（examples/sample_prices.csv）で最適化します。")
    return "/fx-codex/examples/sample_prices.csv", ["--events", "/fx-codex/examples/sample_events.csv"]


def optimize() -> dict:
    log_lines: list[str] = []

    def log(msg: str) -> None:
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        log_lines.append(line)

    def flush() -> None:
        RESULT_LOG.write_text("\n".join(log_lines))

    log("=== 最適化開始（walk-forward / OOS）===")

    symbol = os.environ.get("STRATEGY_SYMBOL", "USDJPY").upper()
    asset = os.environ.get("STRATEGY_ASSET", "fx")
    data_path, events_args = _select_data_source(symbol, asset, log)

    cmd = [
        "docker", "run", "--rm",
        "-v", f"{FXCODEX_DIR}:/fx-codex",
        "-e", "PYTHONPATH=/fx-codex",
        IMAGE,
        "python3", "-m", "fx_backtester.cli", "optimize",
        "--data", data_path,
        *events_args,
        "--strategy", "ma_cross",
        *_grid_args(),
        "--spread-pips", f"{symbol}=0.3",
        "--slippage-pips", f"{symbol}=0.1",
        "--train", "252", "--test", "63", "--min-trades", "20",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except Exception as e:
        log(f"⚠️ 実行失敗: {e}")
        flush()
        return {}
    if result.returncode != 0:
        log(f"⚠️ optimize 失敗 rc={result.returncode}: {result.stderr[:500]}")
        flush()
        return {}
    try:
        best = json.loads(result.stdout)
    except Exception:
        log(f"⚠️ JSON 解析失敗: {result.stdout[:300]}")
        flush()
        return {}

    v = best.get("_validation", {})
    params_only = {k: best[k] for k in best if k != "_validation"}
    log(f"推奨パラメータ: {params_only}")
    log(
        f"OOS sharpe(mean)={v.get('oos_sharpe_mean')} "
        f"OOS/IS={v.get('oos_is_ratio')} stability={v.get('param_stability')} "
        f"OOS_trades={v.get('oos_total_trades')} overfit_warning={v.get('overfit_warning')}"
    )

    # 過学習/取引数不足の疑いがあるパラメータは配備しない（既存 strategy_params.json を維持）。
    # strategy.py 側にも安全な DEFAULT_PARAMS があるため、初回でも「見送り」で問題ない。
    if v.get("overfit_warning") or v.get("insufficient_trades"):
        log("⚠️ 検証フラグあり: OOS 劣化 or 取引数不足。")
        log(f"🛑 {PARAMS_FILE.name} は更新しません（既存パラメータ/既定値を維持）。")
        best["deployed"] = False
        flush()
        return best

    best["updated_at"] = datetime.now().isoformat()
    best["deployed"] = True
    PARAMS_FILE.write_text(json.dumps(best, indent=2, ensure_ascii=False))
    log(f"✅ strategy_params.json を更新: {PARAMS_FILE}")
    log("=== 最適化完了 ===")
    flush()
    return best


if __name__ == "__main__":
    result = optimize()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    # "deployed" が無いのは validation まで到達できなかった実行時失敗（docker/JSON 解析等）。
    # deploy/optimize.sh はこの終了コードで「見送り」と「失敗」を区別して通知する。
    sys.exit(0 if "deployed" in result else 1)

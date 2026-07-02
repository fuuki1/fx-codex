"""
Mac mini 側の自律最適化エンジン（ウォークフォワード版）。

Docker コンテナ内で fx_backtester の `optimize` を 1 回実行し、アウトオブサンプル(OOS)
検証で選ばれた配備用パラメータを strategy_params.json に書き出す。
strategy.py はファイルを監視して自動で読み込む（再起動不要）。

旧版は単一データでのグリッドサーチ（過剰最適化しやすい）だったが、本版は
walk-forward / OOS でパラメータの汎化性能を検証し、overfit_warning も記録する。

データは環境変数 OPTIMIZE_DATA（実データ CSV のパス）で必ず指定する。
未指定・同梱サンプル（乱数生成の合成データ）指定の場合は実行を拒否し、
strategy_params.json を更新しない。合成データに過剰適合したパラメータが
ライブ戦略へ自動配備される事故を防ぐため。

fx_backtester は本リポジトリの trader/fx-codex に同梱。実行イメージは pandas/numpy を
含む trader-app（FXBT_IMAGE で上書き可）。
"""
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

TRADER_DIR = Path(__file__).resolve().parent.parent
FXCODEX_DIR = TRADER_DIR / "fx-codex"
PARAMS_FILE = FXCODEX_DIR / "strategy_params.json"
RESULT_LOG = FXCODEX_DIR / "optimize_result.log"
IMAGE = os.environ.get("FXBT_IMAGE", "trader-app:latest")
# 同梱の合成サンプル（乱数生成）。これで最適化したパラメータを実弾に配備してはならない。
SAMPLE_DATA = FXCODEX_DIR / "examples" / "sample_prices.csv"


def validate_data_path(raw: str | None, sample: Path = SAMPLE_DATA) -> tuple[Path | None, str]:
    """OPTIMIZE_DATA（実データ CSV）を検証する。(path, error) を返す。

    path が None の場合は最適化を実行してはならない。合成サンプルに過剰適合した
    「見かけ上優秀な」パラメータが strategy_params.json 経由でライブ戦略に
    自動配備される事故を防ぐため、実データの明示指定を必須とする。
    """
    if raw is None or not raw.strip():
        return None, (
            "OPTIMIZE_DATA が未設定。実データ CSV のパスを指定すること"
            "（同梱サンプルでの最適化は禁止）"
        )
    path = Path(raw.strip()).expanduser()
    try:
        resolved = path.resolve()
    except OSError as e:
        return None, f"OPTIMIZE_DATA を解決できない: {raw!r} ({e})"
    if not resolved.is_file():
        return None, f"OPTIMIZE_DATA が存在しない: {resolved}"
    if resolved == sample.resolve():
        return None, (
            "OPTIMIZE_DATA が同梱サンプル（乱数生成の合成データ）を指している。"
            "実データを指定すること"
        )
    return resolved, ""

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


def optimize() -> dict:
    log_lines: list[str] = []

    def log(msg: str) -> None:
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        log_lines.append(line)

    def flush() -> None:
        RESULT_LOG.write_text("\n".join(log_lines))

    data_path, error = validate_data_path(os.environ.get("OPTIMIZE_DATA"))
    if data_path is None:
        log(f"⛔ 中止: {error}")
        log("strategy_params.json は更新しません（既存パラメータを維持）。")
        flush()
        return {}

    events_args: list[str] = []
    mounts = ["-v", f"{FXCODEX_DIR}:/fx-codex", "-v", f"{data_path.parent}:/optdata:ro"]
    events_raw = os.environ.get("OPTIMIZE_EVENTS", "").strip()
    if events_raw:
        events_path = Path(events_raw).expanduser().resolve()
        if not events_path.is_file():
            log(f"⛔ 中止: OPTIMIZE_EVENTS が存在しない: {events_path}")
            flush()
            return {}
        mounts += ["-v", f"{events_path.parent}:/optevents:ro"]
        events_args = ["--events", f"/optevents/{events_path.name}"]

    log(f"=== 最適化開始（walk-forward / OOS, data={data_path}）===")
    cmd = [
        "docker", "run", "--rm",
        *mounts,
        "-e", "PYTHONPATH=/fx-codex",
        IMAGE,
        "python3", "-m", "fx_backtester.cli", "optimize",
        "--data", f"/optdata/{data_path.name}",
        *events_args,
        "--strategy", "ma_cross",
        *_grid_args(),
        "--spread-pips", "USDJPY=0.3",
        "--slippage-pips", "USDJPY=0.1",
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
    if v.get("overfit_warning") or v.get("insufficient_trades"):
        log("⚠️ 検証フラグあり: OOS 劣化 or 取引数不足。配備は慎重に（必要なら採用を見送る）。")

    best["updated_at"] = datetime.now().isoformat()
    PARAMS_FILE.write_text(json.dumps(best, indent=2, ensure_ascii=False))
    log(f"✅ strategy_params.json を更新: {PARAMS_FILE}")
    log("=== 最適化完了 ===")
    flush()
    return best


if __name__ == "__main__":
    result = optimize()
    print(json.dumps(result, ensure_ascii=False, indent=2))

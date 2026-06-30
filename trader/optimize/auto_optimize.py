"""
Mac mini 側の自律最適化エンジン。
Docker コンテナ内で fx-codex バックテストを実行し、
最良パラメータを strategy_params.json に書き出す。
strategy.py はファイルを監視して自動で読み込む（再起動不要）。

注意: 本スクリプトは別パッケージ `fx_backtester`（fx-codex バックテスタ）に依存する。
本リポジトリの範囲外（ライブ取引一式を実装する今回のタスクの対象外）なので、
fx-codex のコードは別途用意すること。score() などの純粋ロジックはテスト対象。
"""
import itertools
import json
import subprocess
from datetime import datetime
from pathlib import Path

TRADER_DIR = Path(__file__).resolve().parent.parent
FXCODEX_DIR = TRADER_DIR / "fx-codex"
PARAMS_FILE = FXCODEX_DIR / "strategy_params.json"
RESULT_LOG = FXCODEX_DIR / "optimize_result.log"
PRICES_CSV = FXCODEX_DIR / "examples" / "sample_prices.csv"
EVENTS_CSV = FXCODEX_DIR / "examples" / "sample_events.csv"

GRID = {
    "fast_window": [10, 20, 30],
    "slow_window": [40, 60, 80],
    "atr_window": [14],
    "atr_multiple": [1.5, 2.0, 2.5],
}


def run_backtest(fast: int, slow: int, atr_w: int, atr_m: float) -> dict | None:
    if fast >= slow:
        return None
    # stdout に JSON を直接出力させて受け取る（ファイル共有不要）
    cmd = [
        "docker", "run", "--rm",
        "-v", f"{FXCODEX_DIR}:/fx-codex",
        "-e", "PYTHONPATH=/fx-codex",
        "trader-strategy",
        "python3", "-m", "fx_backtester.cli", "backtest",
        "--data", "/fx-codex/examples/sample_prices.csv",
        "--events", "/fx-codex/examples/sample_events.csv",
        "--strategy", "ma_cross",
        "--param", f"fast_window={fast}",
        "--param", f"slow_window={slow}",
        "--param", f"atr_window={atr_w}",
        "--param", f"stop_atr_multiple={atr_m}",
        "--spread-pips", "USDJPY=0.3",
        "--slippage-pips", "USDJPY=0.1",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except Exception:
        return None


def score(m: dict) -> float:
    sharpe = m.get("sharpe_ratio", 0) or 0
    pf = min(m.get("profit_factor", 0) or 0, 5)
    dd = abs(m.get("max_drawdown_pct", 100) or 100)
    if dd == 0:
        return 0
    return sharpe * 0.4 + pf * 0.4 - (dd / 100) * 0.2


def optimize():
    log_lines = []

    def log(msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line, flush=True)
        log_lines.append(line)

    log("=== 最適化開始 ===")
    combos = [
        (fw, sw, aw, am)
        for fw, sw, aw, am in itertools.product(
            GRID["fast_window"], GRID["slow_window"],
            GRID["atr_window"], GRID["atr_multiple"],
        )
        if fw < sw
    ]
    log(f"組み合わせ数: {len(combos)}")

    best_score = -999
    best_params = {}
    best_metrics = {}

    for i, (fw, sw, aw, am) in enumerate(combos):
        m = run_backtest(fw, sw, aw, am)
        if m is None:
            log(f"  [{i+1}/{len(combos)}] fast={fw} slow={sw} → スキップ")
            continue
        s = score(m)
        log(f"  [{i+1}/{len(combos)}] fast={fw} slow={sw} atr_mult={am} "
            f"score={s:.3f} sharpe={m.get('sharpe_ratio',0):.2f} "
            f"pf={m.get('profit_factor',0):.2f} dd={m.get('max_drawdown_pct',0):.1f}%")
        if s > best_score:
            best_score = s
            best_params = {"fast_window": fw, "slow_window": sw,
                           "atr_window": aw, "atr_multiple": am,
                           "score": round(s, 4),
                           "updated_at": datetime.now().isoformat()}
            best_metrics = m

    if best_params:
        PARAMS_FILE.write_text(json.dumps(best_params, indent=2, ensure_ascii=False))
        log(f"\n✅ 最良パラメータ: {best_params}")
        log(f"   sharpe={best_metrics.get('sharpe_ratio',0):.2f} "
            f"pf={best_metrics.get('profit_factor',0):.2f} "
            f"dd={best_metrics.get('max_drawdown_pct',0):.1f}%")
    else:
        log("⚠️ 有効な結果なし")

    log("=== 最適化完了 ===")
    RESULT_LOG.write_text("\n".join(log_lines))
    return best_params


if __name__ == "__main__":
    result = optimize()
    print(json.dumps(result, ensure_ascii=False, indent=2))

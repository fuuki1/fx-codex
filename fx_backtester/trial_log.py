"""最適化の試行ログ(trial log)。

グリッドサーチやwalk-forwardで評価した「全ての」パラメータ組み合わせを記録する。
過剰最適化の検定(PBO/DSR)は「選ばれた1つ」ではなく「試行した全体」を入力に取るため、
探索の履歴が残っていないと後から計算も監査もできない。deep-research-report の
「試行回数を記録し、PBOを算出」に対応する基盤モジュール。

出力(1回の最適化 = 1ディレクトリ):
- run.json           — 実行メタデータ(run_id、開始・書き出し時刻、コンテキスト、採択試行)
- trials.jsonl       — 1行=1試行(パラメータ、フェーズ、区間、指標、スコア、採否)
- returns_matrix.csv — 時刻×試行のリターン行列。PBO(CSCV)の入力。
                       リターンを渡した試行(通常はIS探索フェーズ)だけが列になる
"""

from __future__ import annotations

import json
import math
from datetime import datetime, UTC
from pathlib import Path
from typing import Any
from collections.abc import Mapping

import numpy as np
import pandas as pd

RUN_FILENAME = "run.json"
TRIALS_FILENAME = "trials.jsonl"
RETURNS_MATRIX_FILENAME = "returns_matrix.csv"


def _json_safe(value: Any) -> Any:
    """JSONへ安全に落とす。非有限のfloatはNone(=計算不能/非有界)に変換する。"""
    if isinstance(value, bool | np.bool_):
        return bool(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        value = float(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, pd.Timestamp | datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, str | int):
        return value
    return str(value)


class TrialLogger:
    """最適化1回分の試行を蓄積し、まとめてディスクへ書き出す。

    使い方:
        logger = TrialLogger(context={"generated_by": "auto_optimize.py", ...})
        for ...:  # 探索ループ
            logger.log(trial_id, params=..., phase="is_grid", metrics=...,
                       score=..., returns=equity.pct_change().dropna())
        logger.mark_selected(best_trial_id)
        paths = logger.write("runs/trial_logs")
    """

    def __init__(self, run_id: str | None = None, context: dict[str, Any] | None = None) -> None:
        started = datetime.now(UTC)
        self.run_id = run_id or started.strftime("%Y%m%dT%H%M%S%fZ")
        self.started_at = started.isoformat()
        self.context = dict(context or {})
        self.trials: list[dict[str, Any]] = []
        self._trial_ids: set[str] = set()
        self._returns: dict[str, pd.Series] = {}
        self.selected_trial_id: str | None = None

    @property
    def trial_count(self) -> int:
        return len(self.trials)

    def log(
        self,
        trial_id: str,
        *,
        params: Mapping[str, Any],
        phase: str,
        metrics: Mapping[str, Any],
        score: float | None = None,
        window: Mapping[str, Any] | None = None,
        selected: bool = False,
        returns: pd.Series | None = None,
    ) -> None:
        """1試行を記録する。returns を渡した試行はリターン行列の列になる。"""
        if trial_id in self._trial_ids:
            raise ValueError(f"trial_id が重複している: {trial_id}")
        self._trial_ids.add(trial_id)
        self.trials.append(
            {
                "trial_id": trial_id,
                "phase": phase,
                "params": _json_safe(params),
                "window": _json_safe(window) if window is not None else None,
                "metrics": _json_safe(metrics),
                "score": _json_safe(score),
                "selected": bool(selected),
            }
        )
        if selected:
            self.selected_trial_id = trial_id
        if returns is not None:
            series = pd.Series(returns, dtype=float).replace([np.inf, -np.inf], np.nan).dropna()
            # 強制クローズで最終バーが二重になることがあるため重複時刻は後勝ちで畳む
            series = series[~series.index.duplicated(keep="last")]
            if not series.empty:
                self._returns[trial_id] = series

    def mark_selected(self, trial_id: str) -> None:
        """探索完了後に採択された試行へ印を付ける(グリッドサーチのbest確定用)。"""
        if trial_id not in self._trial_ids:
            raise ValueError(f"未記録の trial_id: {trial_id}")
        for trial in self.trials:
            if trial["trial_id"] == trial_id:
                trial["selected"] = True
        self.selected_trial_id = trial_id

    def returns_matrix(self) -> pd.DataFrame:
        """時刻×試行のリターン行列。時刻は全試行の和集合で、欠測はNaNのまま返す。"""
        if not self._returns:
            return pd.DataFrame()
        matrix = pd.DataFrame(self._returns)
        matrix.index.name = "timestamp"
        return matrix.sort_index()

    def write(self, directory: str | Path) -> dict[str, Path]:
        """<directory>/<run_id>/ に run.json / trials.jsonl / returns_matrix.csv を書き出す。"""
        run_dir = Path(directory) / self.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        paths = {
            "run_dir": run_dir,
            "run": run_dir / RUN_FILENAME,
            "trials": run_dir / TRIALS_FILENAME,
        }

        run_meta = {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "written_at": datetime.now(UTC).isoformat(),
            "trial_count": self.trial_count,
            "selected_trial_id": self.selected_trial_id,
            "context": _json_safe(self.context),
        }
        paths["run"].write_text(
            json.dumps(run_meta, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        with paths["trials"].open("w", encoding="utf-8") as handle:
            for trial in self.trials:
                handle.write(json.dumps(trial, ensure_ascii=False) + "\n")

        matrix = self.returns_matrix()
        if not matrix.empty:
            paths["returns_matrix"] = run_dir / RETURNS_MATRIX_FILENAME
            matrix.to_csv(paths["returns_matrix"], index_label="timestamp")
        return paths


def read_trials(path: str | Path) -> list[dict[str, Any]]:
    """trials.jsonl を読み込む(後からPBO/DSRを再計算・監査する用)。"""
    trials: list[dict[str, Any]] = []
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                trials.append(json.loads(line))
    return trials


def read_returns_matrix(path: str | Path) -> pd.DataFrame:
    """returns_matrix.csv を読み込む(PBO再計算用)。"""
    matrix = pd.read_csv(path, index_col="timestamp", parse_dates=["timestamp"])
    return matrix.astype(float)

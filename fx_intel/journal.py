"""ブリーフィング判断のジャーナル記録と自己検証。

各実行のトレードプラン(方向・確信度・記録時点の終値)をJSONLへ追記し、
次回以降の実行で「一定時間経過した過去の方向判断」が現在価格に対して
的中していたかを集計する。分析の確実性を数字で継続的に可視化するための
フィードバックループ。

- 評価対象: direction が long/short で、記録から min_age〜max_age 時間経過した判断
- 的中判定: 記録時点の終値と現在の終値の差の符号が判断方向と一致
- 状態を持たない: 毎回JSONL全体を読み、その時点の窓で再集計する
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, UTC
from pathlib import Path
from collections.abc import Mapping, Sequence

from .briefing import TradePlan

DEFAULT_MIN_AGE_HOURS = 4.0
DEFAULT_MAX_AGE_HOURS = 48.0


@dataclass(frozen=True)
class DirectionalStats:
    """方向判断の的中集計。"""

    evaluated: int = 0
    hits: int = 0

    @property
    def hit_rate(self) -> float | None:
        if self.evaluated == 0:
            return None
        return self.hits / self.evaluated


def append_plans(path: str | Path, plans: Sequence[TradePlan], now: datetime | None = None) -> None:
    """今回の判断をJSONLへ追記する(1プラン1行)。"""
    now = now or datetime.now(UTC)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        for plan in plans:
            handle.write(
                json.dumps(
                    {
                        "ts": now.isoformat(),
                        "symbol": plan.symbol,
                        "direction": plan.direction,
                        "conviction": plan.conviction,
                        "composite": plan.composite,
                        "close": plan.close,
                        "data_quality": plan.data_quality,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )


def _iter_entries(path: Path):
    """壊れた行はスキップしてJSONLを読む。"""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            yield entry


def evaluate_directional_accuracy(
    path: str | Path,
    current_closes: Mapping[str, float | None],
    now: datetime | None = None,
    min_age_hours: float = DEFAULT_MIN_AGE_HOURS,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
) -> DirectionalStats:
    """過去の方向判断を現在の終値と突き合わせて的中率を出す。"""
    now = now or datetime.now(UTC)
    target = Path(path)
    if not target.exists():
        return DirectionalStats()

    evaluated = 0
    hits = 0
    for entry in _iter_entries(target):
        direction = entry.get("direction")
        if direction not in ("long", "short"):
            continue
        entry_close = entry.get("close")
        current_close = current_closes.get(str(entry.get("symbol", "")))
        if not isinstance(entry_close, (int, float)) or current_close is None:
            continue
        try:
            recorded_at = datetime.fromisoformat(str(entry.get("ts", "")))
        except ValueError:
            continue
        if recorded_at.tzinfo is None:
            recorded_at = recorded_at.replace(tzinfo=UTC)
        age_hours = (now - recorded_at).total_seconds() / 3600.0
        if not (min_age_hours <= age_hours <= max_age_hours):
            continue
        move = float(current_close) - float(entry_close)
        evaluated += 1
        if (direction == "long" and move > 0) or (direction == "short" and move < 0):
            hits += 1
    return DirectionalStats(evaluated=evaluated, hits=hits)


def format_stats_ja(
    stats: DirectionalStats,
    min_age_hours: float = DEFAULT_MIN_AGE_HOURS,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
) -> str:
    """Discord表示用の1行要約。評価対象が無ければ空文字。"""
    if stats.evaluated == 0 or stats.hit_rate is None:
        return ""
    return (
        f"直近{max_age_hours:.0f}h内の方向判断(記録後{min_age_hours:.0f}h以上経過)"
        f" {stats.evaluated}件中 {stats.hits}件的中 — 的中率 {stats.hit_rate:.0%}"
    )

"""ブリーフィング判断のジャーナル記録と自己検証。

各実行のトレードプラン(方向・確信度・スコア内訳・記録時点の終値/ATR/SL/TP)を
JSONLへ追記し、次回以降の実行で過去の方向判断が的中していたかを集計する。
分析の確実性を数字で継続的に可視化するためのフィードバックループ。

評価設計(統計として使える数字にするための3原則):

1. 固定ホライズン — 記録から約24時間(±2時間)経過した判断だけを評価する。
   広い窓で毎回再評価すると同じ判断が経過時間ごとに違う結果でカウントされ、
   的中率が安定しないため。
2. 市場オープン時間換算 — 経過時間は週末クローズ(market.open_hours_between)を
   除いて数える。週末を跨いだ「価格が動きようがない区間」で的中率が
   機械的に押し下げられるのを防ぐ。
3. ATR閾値 — 記録時ATRの一定割合(既定10%)未満の値動きは「小動き」として
   的中/不的中のどちらにも数えない。符号だけの判定ではノイズが混ざるため。

記録スキーマにはスコア内訳(tech_score/news_score)とSL/TPを含む。
この蓄積を学習データとして使うのが learning.py: 履歴全体を相互採点して
確信度帯別キャリブレーション・複合スコア重みの再推定・不調ペアの
確信度減衰を導き、次回ブリーフィングの分析に自動反映する。

- 状態を持たない: 毎回JSONL全体を読み、その時点の窓で再集計する
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, UTC
from pathlib import Path
from collections.abc import Mapping, Sequence

from .briefing import TradePlan
from .market import open_hours_between
from .timeframe import TimeframePlan

DEFAULT_HORIZON_HOURS = 24.0
DEFAULT_TOLERANCE_HOURS = 2.0
DEFAULT_ATR_FRACTION = 0.1  # |値動き| がATRのこの割合未満なら判定しない


@dataclass(frozen=True)
class DirectionalStats:
    """方向判断の的中集計。flatは小動きで判定除外した件数。"""

    evaluated: int = 0
    hits: int = 0
    flat: int = 0

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
                        "tech_score": plan.tech_score,
                        "news_score": plan.news_score,
                        "close": plan.close,
                        "atr": plan.atr,
                        "stop": plan.stop,
                        "target1": plan.target1,
                        "target2": plan.target2,
                        "target_policy": plan.target_policy,
                        "data_quality": plan.data_quality,
                        # チャート状態の特徴量(learning.pyの状態別学習に使う)
                        "features": plan.features,
                        # 複合スコアの内訳(委員別スコアと正規化重み。監査証跡)
                        "components": plan.components,
                        # 執行コスト(R換算)と期待R予測。採点(trade_outcome)が
                        # realized_net_r を作る入力で、MLの収益ラベルの源になる。
                        **_plan_execution(plan),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )


def append_timeframe_plans(
    path: str | Path, plans: Sequence[TimeframePlan], now: datetime | None = None
) -> None:
    """時間足別の判断をJSONLへ追記する(1プラン1行)。

    append_plans(融合1判断)と同じスキーマに timeframe と horizon_hours を
    加える。この2フィールドで learning.py が「どの時間足の・どの主ホライズンの
    判断か」を区別し、symbol×timeframe のセル単位で採点・学習する。

    close はその時間足自身の終値。後続の実行で同じ (symbol, timeframe) の
    エントリが追記されるので、その close 列が「過去判断から見た将来価格」に
    なる(price_history.build_close_series が (symbol, timeframe) 別に組む)。
    """
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
                        # 時間足別化の中核。旧スキーマの行にはこの2つが無く、
                        # 読み込み側は timeframe 欠落=融合判断(horizon 24h)として扱う
                        "timeframe": plan.timeframe,
                        "horizon_hours": plan.horizon_hours,
                        "direction": plan.direction,
                        "conviction": plan.conviction,
                        "composite": plan.composite,
                        # 融合版の tech_score に相当(時間足単体の方向スコア)。
                        # learning._signal_hit_rate が読むキー名に合わせる
                        "tech_score": plan.tf_score,
                        "news_score": plan.news_score,
                        "close": plan.close,
                        "atr": plan.atr,
                        "rsi": plan.rsi,
                        "adx": plan.adx,
                        "stop": plan.stop,
                        "target1": plan.target1,
                        "target2": plan.target2,
                        "target_policy": plan.target_policy,
                        "data_quality": plan.data_quality,
                        "features": plan.features,
                        "components": plan.components,
                        **_plan_execution(plan),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )


def _plan_execution(plan: object) -> dict[str, object]:
    """plan.checklist から執行コスト系の値を採点・学習用に取り出す。

    値は build_checklist が判断時の実測 spread から既に計算済み。realized_net_r
    (コスト控除後の実現R=収益ラベル)を trade_outcome が作るのに使う。checklist を
    持たない plan(時間足別など)は None。欠損は採点側が欠損として扱う。
    """
    checklist = getattr(plan, "checklist", None)
    if not isinstance(checklist, Mapping):
        return {"execution_cost_r": None, "net_expected_r": None}
    cost = checklist.get("execution_cost_r")
    net = checklist.get("net_expected_r")
    return {
        "execution_cost_r": float(cost) if isinstance(cost, (int, float)) else None,
        "net_expected_r": float(net) if isinstance(net, (int, float)) else None,
    }


def read_entries(path: str | Path):
    """壊れた行はスキップしてJSONLジャーナルを読む(learning.pyの入力にも使う)。"""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
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
    horizon_hours: float = DEFAULT_HORIZON_HOURS,
    tolerance_hours: float = DEFAULT_TOLERANCE_HOURS,
    atr_fraction: float = DEFAULT_ATR_FRACTION,
) -> DirectionalStats:
    """固定ホライズンに達した過去の方向判断を現在の終値と突き合わせる。

    記録から horizon±tolerance (市場オープン時間換算)の判断だけを評価し、
    記録時ATR×atr_fraction 未満の値動きは flat として判定から除外する。
    """
    now = now or datetime.now(UTC)
    target = Path(path)
    if not target.exists():
        return DirectionalStats()

    evaluated = 0
    hits = 0
    flat = 0
    for entry in read_entries(target):
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
        age_hours = open_hours_between(recorded_at, now)
        if not (horizon_hours - tolerance_hours <= age_hours <= horizon_hours + tolerance_hours):
            continue
        move = float(current_close) - float(entry_close)
        signed_move = move if direction == "long" else -move
        atr = entry.get("atr")
        threshold = atr_fraction * float(atr) if isinstance(atr, (int, float)) and atr > 0 else 0.0
        if signed_move > threshold:
            evaluated += 1
            hits += 1
        elif signed_move < -threshold:
            evaluated += 1
        else:
            flat += 1
    return DirectionalStats(evaluated=evaluated, hits=hits, flat=flat)


def format_stats_ja(
    stats: DirectionalStats,
    horizon_hours: float = DEFAULT_HORIZON_HOURS,
) -> str:
    """Discord表示用の1行要約。評価対象が無ければ空文字。"""
    if stats.evaluated == 0 and stats.flat == 0:
        return ""
    if stats.evaluated == 0:
        return (
            f"約{horizon_hours:.0f}時間前(市場オープン時間換算)の方向判断"
            f" {stats.flat}件はいずれも小動きのため判定除外"
        )
    line = (
        f"約{horizon_hours:.0f}時間前(市場オープン時間換算)の方向判断"
        f" {stats.evaluated}件中 {stats.hits}件的中 — 的中率 {stats.hit_rate:.0%}"
    )
    if stats.flat:
        line += f" (ほか{stats.flat}件は小動きのため判定除外)"
    return line

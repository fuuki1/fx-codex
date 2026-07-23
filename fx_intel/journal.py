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
from collections.abc import Iterable, Mapping, Sequence

from .briefing import TradePlan
from .market import open_hours_between
from .timeframe import TimeframePlan

DEFAULT_HORIZON_HOURS = 24.0
DEFAULT_TOLERANCE_HOURS = 2.0
DEFAULT_ATR_FRACTION = 0.1  # |値動き| がATRのこの割合未満なら判定しない
FUSION_PIT_DATA_CONTRACT = "fusion-pit-v1"


class PointInTimeError(ValueError):
    """Raised when a journal row cannot prove feature availability before prediction."""


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise PointInTimeError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _parse_aware_ts(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def is_pit_eligible_entry(entry: Mapping[str, object]) -> bool:
    """Return whether a fusion row proves all inputs were available before prediction."""
    if entry.get("pit_eligible") is not True:
        return False
    recorded = _parse_aware_ts(entry.get("ts"))
    prediction = _parse_aware_ts(entry.get("prediction_time"))
    source_cutoff = _parse_aware_ts(entry.get("source_cutoff"))
    feature_available = _parse_aware_ts(entry.get("max_feature_available_time"))
    if None in (recorded, prediction, source_cutoff, feature_available):
        return False
    assert recorded is not None
    assert prediction is not None
    assert source_cutoff is not None
    assert feature_available is not None
    return recorded == prediction and source_cutoff <= feature_available <= prediction


# 期待値ガード反実仮想の対象ゲート。このゲート「だけ」で見送りになった行を
# counterfactual_guard_entries が復元する。event_window / low_data_quality 等の
# データ・リスク由来の見送りは、ガードが無くても見送っていた行なので含めない
# (含めると反実仮想の根拠が汚染される)。
GUARD_COUNTERFACTUAL_GATE = "expectancy_guard"
SHADOW_FUSION_PRODUCER = "fusion_raw"
# 時間足別判断のシャドー producer。融合判断は fusion_raw、時間足別判断は
# timeframe_raw を凍結記録する(fx_intel.timeframe が build_shadow_predictions で生成)。
# 反実仮想の再構成は、その行自身の判断経路と同じ producer から行う。
SHADOW_TIMEFRAME_PRODUCER = "timeframe_raw"
# 合成行に立てるマーカー。採点側(learning / trade_outcome)はこのキーで
# 「実際の推奨」と「ガード見送り中のシャドー計画」を区別して集計に注記する。
COUNTERFACTUAL_ENTRY_KEY = "counterfactual_guard"


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


def append_plans(
    path: str | Path,
    plans: Sequence[TradePlan],
    now: datetime | None = None,
    *,
    source_cutoff: datetime | None = None,
    max_feature_available_time: datetime | None = None,
) -> None:
    """今回の判断をJSONLへ追記する(1プラン1行)。

    source_cutoff と max_feature_available_time の両方がある行だけをGBDTの
    PIT適格行として記録する。旧呼出しは互換のため記録できるが、学習対象外になる。
    """
    now = _aware_utc(now or datetime.now(UTC), "prediction_time")
    pit_eligible = source_cutoff is not None and max_feature_available_time is not None
    source_utc: datetime | None = None
    feature_utc: datetime | None = None
    if pit_eligible:
        assert source_cutoff is not None
        assert max_feature_available_time is not None
        source_utc = _aware_utc(source_cutoff, "source_cutoff")
        feature_utc = _aware_utc(max_feature_available_time, "max_feature_available_time")
        if not source_utc <= feature_utc <= now:
            raise PointInTimeError(
                "PIT ordering must satisfy source_cutoff <= "
                "max_feature_available_time <= prediction_time"
            )
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        for plan in plans:
            handle.write(
                json.dumps(
                    {
                        "ts": now.isoformat(),
                        "prediction_time": now.isoformat(),
                        "source_cutoff": source_utc.isoformat() if source_utc else None,
                        "max_feature_available_time": (
                            feature_utc.isoformat() if feature_utc else None
                        ),
                        "pit_eligible": pit_eligible,
                        "symbol": plan.symbol,
                        "direction": plan.direction,
                        "analysis_direction": plan.analysis_direction,
                        "analysis_conviction": plan.analysis_conviction,
                        "conviction": plan.conviction,
                        "composite": plan.composite,
                        "tech_score": plan.tech_score,
                        "news_score": plan.news_score,
                        "close": plan.close,
                        "atr": plan.atr,
                        "stop": plan.stop,
                        "target1": plan.target1,
                        "target2": plan.target2,
                        "entry_bid": plan.entry_bid,
                        "entry_ask": plan.entry_ask,
                        "quote_observed_at": plan.quote_observed_at,
                        "cost_model_id": plan.cost_model_id,
                        "slippage_r": plan.slippage_r,
                        "commission_r": plan.commission_r,
                        "direction_threshold": plan.direction_threshold,
                        "target_policy": plan.target_policy,
                        "data_quality": plan.data_quality,
                        # チャート状態の特徴量(learning.pyの状態別学習に使う)
                        "features": plan.features,
                        # 複合スコアの内訳(委員別スコアと正規化重み。監査証跡)
                        "components": plan.components,
                        # 執行コスト(R換算)と期待R予測。採点(trade_outcome)が
                        # realized_net_r を作る入力で、MLの収益ラベルの源になる。
                        **_plan_execution(plan),
                        "learning_dimensions": plan.learning_dimensions,
                        "gate_trace": plan.gate_trace,
                        "shadow_predictions": plan.shadow_predictions,
                        "input_context_id": plan.input_context_id,
                        "input_features": plan.input_features,
                        "input_feature_masks": plan.input_feature_masks,
                        "input_context_schema_version": plan.input_context.get(
                            "context_schema_version"
                        ),
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
                        "analysis_direction": plan.analysis_direction,
                        "analysis_conviction": plan.analysis_conviction,
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
                        "entry_bid": plan.entry_bid,
                        "entry_ask": plan.entry_ask,
                        "quote_observed_at": plan.quote_observed_at,
                        "cost_model_id": plan.cost_model_id,
                        "slippage_r": plan.slippage_r,
                        "commission_r": plan.commission_r,
                        "direction_threshold": plan.direction_threshold,
                        "target_policy": plan.target_policy,
                        "data_quality": plan.data_quality,
                        "features": plan.features,
                        "components": plan.components,
                        **_plan_execution(plan),
                        "learning_dimensions": plan.learning_dimensions,
                        "gate_trace": plan.gate_trace,
                        "shadow_predictions": plan.shadow_predictions,
                        "input_context_id": plan.input_context_id,
                        "input_features": plan.input_features,
                        "input_feature_masks": plan.input_feature_masks,
                        "input_context_schema_version": plan.input_context.get(
                            "context_schema_version"
                        ),
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


def blocked_gate_names(entry: Mapping[str, object]) -> set[str]:
    """gate_traceからstatus=blockedのゲート名集合を返す(observed等は含めない)。"""
    trace = entry.get("gate_trace")
    if not isinstance(trace, (list, tuple)):
        return set()
    names: set[str] = set()
    for row in trace:
        if isinstance(row, Mapping) and row.get("status") == "blocked":
            name = str(row.get("gate", "")).strip()
            if name:
                names.add(name)
    return names


def counterfactual_guard_entries(
    entries: Iterable[Mapping[str, object]],
) -> list[dict[str, object]]:
    """expectancy_guard単独で見送りになった行を、判断時凍結のシャドー計画で復元する。

    期待値ガードは自分がブロックした判断の結果を観測できないため、放置すると
    根拠サンプルが増えず永久ブロックに陥る(学習飢餓)。この関数は、ゲート前の
    分析方向(analysis_direction)と判断時に凍結記録済みのシャドーSL/TP
    (融合判断は fusion_raw、時間足別判断は timeframe_raw)から「ガードが
    無ければ推奨していた計画」を合成し、既存の採点エンジンへそのまま流せる行として返す。
    どちらの経路も同じ contract で採点し、その行自身の経路の producer からのみ再構成する。

    PIT安全性: 合成に使う値はすべて判断時点で記録済みのもの(分析方向・
    分析確信度・凍結SL/TP)に限る。事後の再計算・推定は行わず、必要な記録が
    欠けた行は黙って除外する(fail-closed)。
    """
    output: list[dict[str, object]] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        if blocked_gate_names(entry) != {GUARD_COUNTERFACTUAL_GATE}:
            continue
        direction = entry.get("analysis_direction")
        if direction not in ("long", "short"):
            continue
        prediction = _guard_shadow_prediction(entry)
        if prediction is None:
            continue
        if prediction.get("direction") != direction:
            # 凍結スコアと分析方向の不整合は記録欠陥として採点しない
            continue
        stop = _level(prediction.get("stop"))
        target1 = _level(prediction.get("target1"))
        target2 = _level(prediction.get("target2"))
        if stop is None or target1 is None or target2 is None:
            continue
        conviction = entry.get("analysis_conviction")
        target_policy = prediction.get("target_policy")
        synthesized: dict[str, object] = dict(entry)
        synthesized["direction"] = str(direction)
        synthesized["conviction"] = int(conviction) if isinstance(conviction, (int, float)) else 0
        synthesized["stop"] = stop
        synthesized["target1"] = target1
        synthesized["target2"] = target2
        synthesized["target_policy"] = (
            dict(target_policy)
            if isinstance(target_policy, Mapping)
            else {"policy_id": "shadow-default-atr-v1"}
        )
        synthesized[COUNTERFACTUAL_ENTRY_KEY] = True
        output.append(synthesized)
    return output


def _guard_shadow_producer(entry: Mapping[str, object]) -> str:
    """その行の判断経路に対応するシャドー producer を返す。

    時間足別判断(timeframe フィールドあり)は timeframe_raw を、融合判断は
    fusion_raw を凍結記録する。反実仮想は必ずその行自身の経路の producer から
    再構成する(別経路のシャドーを混ぜない)。
    """
    timeframe = str(entry.get("timeframe", "")).strip()
    return SHADOW_TIMEFRAME_PRODUCER if timeframe else SHADOW_FUSION_PRODUCER


def _guard_shadow_prediction(entry: Mapping[str, object]) -> Mapping[str, object] | None:
    predictions = entry.get("shadow_predictions")
    if not isinstance(predictions, (list, tuple)):
        return None
    producer = _guard_shadow_producer(entry)
    for row in predictions:
        if (
            isinstance(row, Mapping)
            and str(row.get("producer", "")) == producer
            and row.get("eligible_for_scoring") is True
        ):
            return row
    return None


def _level(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


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

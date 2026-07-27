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

import fcntl
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, UTC
from pathlib import Path
from collections.abc import Iterable, Mapping, Sequence
from typing import BinaryIO
import uuid

from . import decision_commit
from .briefing import TradePlan
from .market import open_hours_between
from .timeframe import TimeframePlan

DEFAULT_HORIZON_HOURS = 24.0
DEFAULT_TOLERANCE_HOURS = 2.0
DEFAULT_ATR_FRACTION = 0.1  # |値動き| がATRのこの割合未満なら判定しない
DECISION_JOURNAL_PIT_CONTRACT = "decision-journal-pit-v2"
FUSION_PIT_DATA_CONTRACT = DECISION_JOURNAL_PIT_CONTRACT
FUSION_PRODUCER = "fusion_raw"
TIMEFRAME_PRODUCER = "timeframe_raw"
FUSION_PRODUCER_VERSION = "fusion-journal-v2"
TIMEFRAME_PRODUCER_VERSION = "timeframe-journal-v2"
JOURNAL_BATCH_COMMIT = "journal_batch_commit"


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
    """Return whether a decision proves identity, provenance, and temporal ordering."""
    if entry.get("pit_eligible") is not True:
        return False
    if entry.get("pit_contract") != DECISION_JOURNAL_PIT_CONTRACT:
        return False
    expected_identity = {
        "fusion": (FUSION_PRODUCER, FUSION_PRODUCER_VERSION),
        "per_timeframe": (TIMEFRAME_PRODUCER, TIMEFRAME_PRODUCER_VERSION),
    }.get(str(entry.get("mode") or ""))
    identity = (
        str(entry.get("producer") or ""),
        str(entry.get("producer_version") or ""),
    )
    if expected_identity is None or identity != expected_identity:
        return False
    if not str(entry.get("decision_id") or "").strip():
        return False
    if not str(entry.get("input_context_id") or "").strip():
        return False
    source_ids = entry.get("source_record_ids")
    if not isinstance(source_ids, Sequence) or isinstance(source_ids, (str, bytes)):
        return False
    if not any(str(value).strip() for value in source_ids):
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


def _pit_times(
    prediction_time: datetime | None,
    source_cutoff: datetime | None,
    max_feature_available_time: datetime | None,
) -> tuple[datetime, datetime | None, datetime | None]:
    prediction = _aware_utc(prediction_time or datetime.now(UTC), "prediction_time")
    source = _aware_utc(source_cutoff, "source_cutoff") if source_cutoff is not None else None
    feature = (
        _aware_utc(max_feature_available_time, "max_feature_available_time")
        if max_feature_available_time is not None
        else None
    )
    if source is not None and source > prediction:
        raise PointInTimeError("source_cutoff must not exceed prediction_time")
    if feature is not None and feature > prediction:
        raise PointInTimeError("max_feature_available_time must not exceed prediction_time")
    if source is not None and feature is not None and source > feature:
        raise PointInTimeError("source_cutoff must not exceed max_feature_available_time")
    return prediction, source, feature


def _source_record_ids(plan: object) -> list[str]:
    values: list[str] = []

    def add(value: object) -> None:
        text = str(value or "").strip()
        if text and text not in values:
            values.append(text)

    add(getattr(plan, "quote_source_record_id", ""))
    context = getattr(plan, "input_context", None)
    if not isinstance(context, Mapping):
        return sorted(values)
    macro = context.get("macro")
    if isinstance(macro, Mapping):
        raw_values = macro.get("values")
        if isinstance(raw_values, Mapping):
            for raw in raw_values.values():
                if isinstance(raw, Mapping):
                    add(raw.get("source_record_id"))
    liquidity = context.get("liquidity")
    if isinstance(liquidity, Mapping):
        quote = liquidity.get("quote")
        if isinstance(quote, Mapping):
            add(quote.get("source_record_id"))
    return sorted(values)


def pit_metadata_for_plan(
    plan: object,
    *,
    prediction_time: datetime,
    source_cutoff: datetime | None,
    max_feature_available_time: datetime | None,
    decision_id: str,
    mode: str,
) -> dict[str, object]:
    """Build the canonical fail-closed PIT envelope for one decision."""

    prediction, source, feature = _pit_times(
        prediction_time,
        source_cutoff,
        max_feature_available_time,
    )
    expected_identity = {
        "fusion": (FUSION_PRODUCER, FUSION_PRODUCER_VERSION),
        "per_timeframe": (TIMEFRAME_PRODUCER, TIMEFRAME_PRODUCER_VERSION),
    }.get(mode)
    if expected_identity is None:
        raise PointInTimeError(f"unsupported decision mode: {mode}")
    producer, producer_version = expected_identity
    normalized_decision_id = str(decision_id).strip()
    input_context_id = str(getattr(plan, "input_context_id", "") or "").strip()
    source_record_ids = _source_record_ids(plan)
    pit_eligible = (
        source is not None
        and feature is not None
        and bool(normalized_decision_id)
        and bool(input_context_id)
        and bool(source_record_ids)
    )
    return {
        "prediction_time": prediction.isoformat(),
        "source_cutoff": source.isoformat() if source else None,
        "max_feature_available_time": feature.isoformat() if feature else None,
        "pit_eligible": pit_eligible,
        "pit_contract": DECISION_JOURNAL_PIT_CONTRACT,
        "decision_id": normalized_decision_id or None,
        "mode": mode,
        "producer": producer,
        "producer_version": producer_version,
        "input_context_id": input_context_id,
        "source_record_ids": source_record_ids,
    }


def _decision_id_at(decision_ids: Sequence[str] | None, index: int) -> str:
    if decision_ids is None:
        return ""
    return str(decision_ids[index]).strip()


# 期待値ガード反実仮想の対象ゲート。このゲート「だけ」で見送りになった行を
# counterfactual_guard_entries が復元する。event_window / low_data_quality 等の
# データ・リスク由来の見送りは、ガードが無くても見送っていた行なので含めない
# (含めると反実仮想の根拠が汚染される)。
GUARD_COUNTERFACTUAL_GATE = "expectancy_guard"
SHADOW_FUSION_PRODUCER = "fusion_raw"
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


def _append_journal_batch(
    target: Path,
    rows: Sequence[Mapping[str, object]],
    *,
    decision_transaction_id: str | None,
) -> dict[str, object]:
    if not rows:
        raise ValueError("journal batch must contain at least one row")
    requires_cross_log_commit = any(is_pit_eligible_entry(row) for row in rows)
    raw_decision_ids = [row.get("decision_id") for row in rows]
    try:
        decision_ids = decision_commit.normalize_decision_ids(raw_decision_ids)
    except decision_commit.DecisionCommitError:
        if requires_cross_log_commit or decision_transaction_id:
            raise
        decision_ids = []
    expected_transaction_id = (
        decision_commit.transaction_id_for(decision_ids) if decision_ids else ""
    )
    normalized_transaction_id = str(decision_transaction_id or "").strip()
    if requires_cross_log_commit and normalized_transaction_id != expected_transaction_id:
        raise PointInTimeError(
            "PIT-eligible compact decisions require the canonical cross-log transaction ID"
        )
    if normalized_transaction_id and normalized_transaction_id != expected_transaction_id:
        raise PointInTimeError("decision transaction ID does not match decision IDs")

    batch_id = uuid.uuid4().hex
    committed_rows = [
        {
            "journal_batch_id": batch_id,
            "journal_batch_index": index,
            "journal_batch_size": len(rows),
            "decision_transaction_id": normalized_transaction_id or None,
            **dict(row),
        }
        for index, row in enumerate(rows)
    ]
    batch_sha256 = decision_commit.canonical_sha256(committed_rows)
    marker = {
        "event_type": JOURNAL_BATCH_COMMIT,
        "journal_batch_id": batch_id,
        "journal_batch_size": len(rows),
        "decision_transaction_id": normalized_transaction_id or None,
        "decision_ids": decision_ids,
        "decision_ids_sha256": (
            decision_commit.decision_ids_sha256(decision_ids)
            if decision_ids
            else decision_commit.canonical_sha256([])
        ),
        "journal_batch_sha256": batch_sha256,
        "requires_cross_log_commit": requires_cross_log_commit,
    }
    payload = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
        for row in [*committed_rows, marker]
    ).encode("utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a+b", buffering=0) as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            line_start_offset = os.lseek(handle.fileno(), 0, os.SEEK_END)
            written = handle.write(payload)
            if written != len(payload):
                raise OSError(f"short append to {target}: {written}/{len(payload)}")
            os.fsync(handle.fileno())
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return {
        "decision_transaction_id": normalized_transaction_id or None,
        "decision_ids": decision_ids,
        "batch_sha256": batch_sha256,
        "journal_batch_id": batch_id,
        "line_start_offset": line_start_offset,
        "byte_length": len(payload),
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
    }


def append_plans(
    path: str | Path,
    plans: Sequence[TradePlan],
    now: datetime | None = None,
    *,
    source_cutoff: datetime | None = None,
    max_feature_available_time: datetime | None = None,
    decision_ids: Sequence[str] | None = None,
    decision_transaction_id: str | None = None,
) -> dict[str, object]:
    """今回の判断をJSONLへ追記する(1プラン1行)。

    source_cutoff と max_feature_available_time の両方がある行だけをGBDTの
    PIT適格行として記録する。旧呼出しは互換のため記録できるが、学習対象外になる。
    """
    now, source_utc, feature_utc = _pit_times(now, source_cutoff, max_feature_available_time)
    if decision_ids is not None and len(decision_ids) != len(plans):
        raise PointInTimeError("decision_ids must contain exactly one ID per plan")
    target = Path(path)
    rows: list[dict[str, object]] = []
    for index, plan in enumerate(plans):
        pit_metadata = pit_metadata_for_plan(
            plan,
            prediction_time=now,
            source_cutoff=source_utc,
            max_feature_available_time=feature_utc,
            decision_id=_decision_id_at(decision_ids, index),
            mode="fusion",
        )
        rows.append(
            {
                "ts": now.isoformat(),
                **pit_metadata,
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
                "input_context_schema_version": plan.input_context.get("context_schema_version"),
            }
        )
    return _append_journal_batch(
        target,
        rows,
        decision_transaction_id=decision_transaction_id,
    )


def append_timeframe_plans(
    path: str | Path,
    plans: Sequence[TimeframePlan],
    now: datetime | None = None,
    *,
    source_cutoff: datetime | None = None,
    max_feature_available_time: datetime | None = None,
    decision_ids: Sequence[str] | None = None,
    decision_transaction_id: str | None = None,
) -> dict[str, object]:
    """時間足別の判断をJSONLへ追記する(1プラン1行)。

    append_plans(融合1判断)と同じスキーマに timeframe と horizon_hours を
    加える。この2フィールドで learning.py が「どの時間足の・どの主ホライズンの
    判断か」を区別し、symbol×timeframe のセル単位で採点・学習する。

    close はその時間足自身の終値。後続の実行で同じ (symbol, timeframe) の
    エントリが追記されるので、その close 列が「過去判断から見た将来価格」に
    なる(price_history.build_close_series が (symbol, timeframe) 別に組む)。
    """
    now, source_utc, feature_utc = _pit_times(now, source_cutoff, max_feature_available_time)
    if decision_ids is not None and len(decision_ids) != len(plans):
        raise PointInTimeError("decision_ids must contain exactly one ID per plan")
    target = Path(path)
    rows: list[dict[str, object]] = []
    for index, plan in enumerate(plans):
        pit_metadata = pit_metadata_for_plan(
            plan,
            prediction_time=now,
            source_cutoff=source_utc,
            max_feature_available_time=feature_utc,
            decision_id=_decision_id_at(decision_ids, index),
            mode="per_timeframe",
        )
        rows.append(
            {
                "ts": now.isoformat(),
                **pit_metadata,
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
                "input_context_schema_version": plan.input_context.get("context_schema_version"),
            }
        )
    return _append_journal_batch(
        target,
        rows,
        decision_transaction_id=decision_transaction_id,
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
    """Read legacy rows and PIT batches with exact full-line-backed receipts."""

    target = Path(path)
    expected_mode = next(
        (
            mode
            for mode, filename in decision_commit.COMPACT_FILENAMES.items()
            if filename == target.name
        ),
        "",
    )
    with decision_commit.verified_compact_reader(target) as (handle, receipts):
        if handle is None:
            return
        yield from _read_entries_from_handle(
            handle,
            expected_mode=expected_mode,
            receipts=receipts,
        )


def _read_entries_from_handle(
    handle: BinaryIO,
    *,
    expected_mode: str,
    receipts: Mapping[str, Mapping[str, object]],
):
    pending_id = ""
    pending_rows: list[dict[str, object]] = []
    for raw_line in handle:
        line = raw_line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("event_type") == decision_commit.RECEIPT_EVENT_TYPE:
            pending_id = ""
            pending_rows = []
            continue
        batch_id = str(entry.get("journal_batch_id") or "")
        if entry.get("event_type") == JOURNAL_BATCH_COMMIT:
            expected_size = entry.get("journal_batch_size")
            indices = [row.get("journal_batch_index") for row in pending_rows]
            decision_ids = [row.get("decision_id") for row in pending_rows]
            locally_complete = (
                batch_id
                and batch_id == pending_id
                and isinstance(expected_size, int)
                and expected_size == len(pending_rows)
                and indices == list(range(expected_size))
            )
            has_integrity_envelope = "journal_batch_sha256" in entry
            if locally_complete and not has_integrity_envelope:
                if not any(is_pit_eligible_entry(row) for row in pending_rows):
                    yield from pending_rows
            elif locally_complete:
                marker_decision_ids = entry.get("decision_ids")
                try:
                    batch_sha256 = decision_commit.canonical_sha256(pending_rows)
                    normalized_marker_ids = (
                        decision_commit.normalize_decision_ids(marker_decision_ids)
                        if marker_decision_ids
                        else []
                    )
                    expected_ids_hash = (
                        decision_commit.decision_ids_sha256(normalized_marker_ids)
                        if normalized_marker_ids
                        else decision_commit.canonical_sha256([])
                    )
                    integrity_matches = (
                        normalized_marker_ids
                        == [
                            str(value or "").strip()
                            for value in decision_ids
                            if str(value or "").strip()
                        ]
                        and entry.get("decision_ids_sha256") == expected_ids_hash
                        and entry.get("journal_batch_sha256") == batch_sha256
                    )
                except (decision_commit.DecisionCommitError, TypeError, ValueError):
                    integrity_matches = False
                requires_commit = bool(entry.get("requires_cross_log_commit")) or any(
                    is_pit_eligible_entry(row) for row in pending_rows
                )
                transaction_id = str(entry.get("decision_transaction_id") or "")
                transaction_matches_rows = all(
                    str(row.get("decision_transaction_id") or "") == transaction_id
                    for row in pending_rows
                )
                receipt_matches = False
                if integrity_matches and transaction_matches_rows:
                    try:
                        receipt_matches = decision_commit.receipt_matches_compact(
                            receipts.get(transaction_id),
                            transaction_id=transaction_id,
                            decision_ids=decision_ids,
                            batch_sha256=batch_sha256,
                            mode=expected_mode,
                        )
                    except decision_commit.DecisionCommitError:
                        receipt_matches = False
                if integrity_matches and (not requires_commit or receipt_matches):
                    yield from pending_rows
            pending_id = ""
            pending_rows = []
            continue
        if batch_id:
            if batch_id != pending_id:
                pending_id = batch_id
                pending_rows = []
            pending_rows.append(entry)
            continue
        # Rows written before commit-marked batching remain readable. A
        # standalone v2 row has no proof that its full audit twin committed.
        pending_id = ""
        pending_rows = []
        if not is_pit_eligible_entry(entry):
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
    (shadow_predictionsのfusion_raw)から「ガードが無ければ推奨していた計画」を
    合成し、既存の採点エンジンへそのまま流せる行として返す。

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
        prediction = _fusion_shadow_prediction(entry)
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


def _fusion_shadow_prediction(entry: Mapping[str, object]) -> Mapping[str, object] | None:
    predictions = entry.get("shadow_predictions")
    if not isinstance(predictions, (list, tuple)):
        return None
    for row in predictions:
        if (
            isinstance(row, Mapping)
            and str(row.get("producer", "")) == SHADOW_FUSION_PRODUCER
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
    *,
    require_pit: bool = False,
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
        if require_pit and not is_pit_eligible_entry(entry):
            continue
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

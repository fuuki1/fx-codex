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

from dataclasses import dataclass
from datetime import datetime, UTC
import hashlib
import os
from pathlib import Path
import socket
from collections.abc import Mapping, Sequence

from .append_only import (
    AppendOnlyReadError,
    AppendOnlyWriteError,
    append_jsonl_idempotent,
    canonical_row_hash,
    read_jsonl_strict,
)
from .briefing import TradePlan
from .market import open_hours_between
from .timeframe import TimeframePlan

DEFAULT_HORIZON_HOURS = 24.0
DEFAULT_TOLERANCE_HOURS = 2.0
DEFAULT_ATR_FRACTION = 0.1  # |値動き| がATRのこの割合未満なら判定しない
RUN_CADENCE_MINUTES = 5


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
    run_slot: datetime | None = None,
) -> None:
    """今回の判断をJSONLへ追記する(1プラン1行)。"""
    now = _utc(now or datetime.now(UTC))
    resolved_run_slot = _resolve_run_slot(now, run_slot)
    rows = []
    for plan in plans:
        rows.append(
            _journal_envelope(
                {
                    "ts": now.isoformat(),
                    "symbol": plan.symbol,
                    "timeframe": "fusion",
                    "direction": plan.direction,
                    "action": _plan_action(plan),
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
                now,
                run_slot=resolved_run_slot,
            )
        )
    append_jsonl_idempotent(
        path,
        rows,
        identity=_journal_identity_for_write,
        row_digest=_journal_logical_digest,
        tolerate_legacy_conflicts=True,
    )


def append_timeframe_plans(
    path: str | Path,
    plans: Sequence[TimeframePlan],
    now: datetime | None = None,
    *,
    run_slot: datetime | None = None,
) -> None:
    """時間足別の判断をJSONLへ追記する(1プラン1行)。

    append_plans(融合1判断)と同じスキーマに timeframe と horizon_hours を
    加える。この2フィールドで learning.py が「どの時間足の・どの主ホライズンの
    判断か」を区別し、symbol×timeframe のセル単位で採点・学習する。

    close はその時間足自身の終値。後続の実行で同じ (symbol, timeframe) の
    エントリが追記されるので、その close 列が「過去判断から見た将来価格」に
    なる(price_history.build_close_series が (symbol, timeframe) 別に組む)。
    """
    now = _utc(now or datetime.now(UTC))
    resolved_run_slot = _resolve_run_slot(now, run_slot)
    rows = []
    for plan in plans:
        rows.append(
            _journal_envelope(
                {
                    "ts": now.isoformat(),
                    "symbol": plan.symbol,
                    # 時間足別化の中核。旧スキーマの行にはこの2つが無く、
                    # 読み込み側は timeframe 欠落=融合判断(horizon 24h)として扱う
                    "timeframe": plan.timeframe,
                    "horizon_hours": plan.horizon_hours,
                    "direction": plan.direction,
                    "action": _plan_action(plan),
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
                now,
                run_slot=resolved_run_slot,
            )
        )
    append_jsonl_idempotent(
        path,
        rows,
        identity=_journal_identity_for_write,
        row_digest=_journal_logical_digest,
        tolerate_legacy_conflicts=True,
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


def _journal_envelope(
    row: dict[str, object],
    now: datetime,
    *,
    run_slot: datetime,
) -> dict[str, object]:
    stamp = _utc(now)
    row.update(
        {
            "schema_version": 3,
            "event_time": stamp.isoformat(),
            "available_time": stamp.isoformat(),
            "ingested_time": stamp.isoformat(),
            "source": "fx_briefing",
            "run_slot": run_slot.isoformat(),
            "run_id": f"briefing-{run_slot.strftime('%Y%m%dT%H%M%SZ')}",
            "writer_id": os.environ.get("FX_WRITER_ID") or f"{socket.gethostname()}:{os.getpid()}",
        }
    )
    identity = _journal_natural_identity(row)
    row["decision_id"] = identity
    row["source_record_id"] = identity
    return row


def _plan_action(plan: object) -> str:
    action = str(getattr(plan, "action", "no_trade"))
    return action if action in ("long", "short") else "no_trade"


def _journal_natural_identity(row: Mapping[str, object]) -> str:
    try:
        timestamp = datetime.fromisoformat(str(row.get("run_slot") or row.get("ts") or ""))
    except ValueError as error:
        raise ValueError("journal run_slot/ts is invalid") from error
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("journal run_slot/ts must be timezone-aware")
    slot = _cadence_slot(timestamp)
    if row.get("run_slot") is not None and _utc(timestamp) != slot:
        raise ValueError("journal run_slot must align to the five-minute cadence")
    symbol = str(row.get("symbol") or "").strip().upper()
    timeframe = str(row.get("timeframe") or "fusion").strip()
    if not symbol or not timeframe:
        raise ValueError("journal symbol/timeframe is missing")
    raw = f"journal-v2|{slot.isoformat()}|{symbol}|{timeframe}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _validated_journal_identity(row: Mapping[str, object]) -> str:
    identity = _journal_natural_identity(row)
    schema = row.get("schema_version")
    if schema is None:
        return identity
    if not isinstance(schema, int) or isinstance(schema, bool) or schema < 1:
        raise ValueError("journal schema_version is invalid")
    # Schema-v2 rows predate explicit natural-identity enforcement. They remain
    # readable for migration/scoring, while every newly written schema-v3 row
    # must prove that all stored identifiers were derived from the same slot.
    if schema < 3:
        return identity
    decision_id = str(row.get("decision_id") or "").strip()
    if decision_id != identity:
        raise ValueError("journal decision_id does not match run_slot/symbol/timeframe")
    source_record_id = str(row.get("source_record_id") or "").strip()
    if source_record_id != identity:
        raise ValueError("journal source_record_id does not match natural identity")
    slot = datetime.fromisoformat(str(row.get("run_slot") or ""))
    slot = _utc(slot)
    expected_run_id = f"briefing-{slot.strftime('%Y%m%dT%H%M%SZ')}"
    if str(row.get("run_id") or "").strip() != expected_run_id:
        raise ValueError("journal run_id does not match run_slot")
    return identity


def _journal_identity_for_write(row: Mapping[str, object]) -> str:
    try:
        return _validated_journal_identity(row)
    except (TypeError, ValueError) as error:
        raise AppendOnlyWriteError(f"invalid journal natural identity: {error}") from error


def _journal_identity_for_read(row: Mapping[str, object]) -> str:
    try:
        return _validated_journal_identity(row)
    except (TypeError, ValueError) as error:
        raise AppendOnlyReadError(f"invalid journal natural identity: {error}") from error


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("journal timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _cadence_slot(value: datetime) -> datetime:
    utc = _utc(value)
    minute = utc.minute - utc.minute % RUN_CADENCE_MINUTES
    return utc.replace(minute=minute, second=0, microsecond=0)


def _resolve_run_slot(now: datetime, run_slot: datetime | None) -> datetime:
    if run_slot is None:
        return _cadence_slot(now)
    slot = _utc(run_slot)
    if slot != _cadence_slot(slot):
        raise ValueError("run_slot must align to the five-minute cadence")
    if slot > now:
        raise ValueError("run_slot cannot be later than now")
    return slot


def _journal_logical_digest(row: Mapping[str, object]) -> str:
    """Digest decision content while excluding retry-attempt metadata."""

    volatile = {
        "content_hash",
        "ts",
        "event_time",
        "available_time",
        "ingested_time",
        "run_id",
        "writer_id",
        "source_record_id",
    }
    return canonical_row_hash({key: value for key, value in row.items() if key not in volatile})


def read_entries(
    path: str | Path,
    *,
    as_of: datetime | None = None,
    allow_legacy_unhashed: bool = False,
):
    """Strictly read a journal; corruption, naive time, and future rows are fatal."""

    yield from read_jsonl_strict(
        path,
        as_of=as_of,
        allow_legacy_unhashed=allow_legacy_unhashed,
        identity=_journal_identity_for_read,
    )


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
    for entry in read_entries(target, as_of=now):
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
        if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
            raise ValueError("journal entry timestamp must be timezone-aware")
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

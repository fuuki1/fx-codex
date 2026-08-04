"""判断トレース解析(Decision Trace Analytics)MVP — 読み取り専用の純関数層。

このモジュールは判断イベント(decision event)を**読むだけ**で、判断結果を一切変更しない。
ログ・SQLite・JSONLへの書き込み経路を持たない(テストでAST検査により固定)。

正準入力は判断イベントの以下フィールドのみ:
    decision_id, ts, source(producer), mode, symbol(pair), timeframe,
    decision.direction, decision.blocked_by, decision.gate_trace,
    decision.learning_dimensions.regime

実装が依拠する構造的事実(`fx_intel/timeframe.py` の実コードで確認済み):

1. `gate_trace` は `status="blocked"` のエントリのみ記録される(timeframe.py:625-638)。
   **pass事象は存在しない**。よって「通過したゲート」を推論で捏造してはならない。

2. liquidity のトレースだけ形が違う(input_context.py:480-498)。
   `status="observed"` / `would_block` / `applied: False` を持つ観測専用行であり、
   ブロックではない。indeterminate でもなく "observed" として別分類する。

3. `blocked_by` は固定評価順で append される(timeframe.py:434-462, 487, 569):
       operational_data_stale → market_closed → event_window
       → missing_technical / low_data_quality → below_production_threshold
       → expectancy_guard → missing_atr
   したがって index 0 が真の first blocker。

4. 上記のうち operational_data_stale / market_closed / event_window /
   missing_technical / low_data_quality / below_production_threshold は
   **相互排他な if/elif 分岐**(timeframe.py:435-462)。同時出現は不正遷移。

5. expectancy_guard(timeframe.py:487)と missing_atr(timeframe.py:569)は
   elif 連鎖の**外**にある独立した if なので、他ゲートとの共起は**合法**。
   ここを不正扱いすると偽陽性を量産するため、絶対に不正判定してはならない。

コードとこの docstring が食い違う場合は**コードが正**。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

__all__ = [
    "EXCLUSIVE_GATES",
    "GATE_EVALUATION_ORDER",
    "INDEPENDENT_GATES",
    "FunnelReconciliationError",
    "GateRecord",
    "TraceEvent",
    "FunnelSummary",
    "TraceReport",
    "parse_event",
    "parse_events",
    "build_report",
    "report_to_dict",
]

# blocked_by が append される固定評価順(timeframe.py:434-462, 487, 569)。
GATE_EVALUATION_ORDER: Final[tuple[str, ...]] = (
    "operational_data_stale",
    "market_closed",
    "event_window",
    "missing_technical",
    "low_data_quality",
    "below_production_threshold",
    "expectancy_guard",
    "missing_atr",
)

# 相互排他な if/elif 分岐に属するゲート。同時出現は不正遷移。
EXCLUSIVE_GATES: Final[frozenset[str]] = frozenset(
    {
        "operational_data_stale",
        "market_closed",
        "event_window",
        "missing_technical",
        "low_data_quality",
        "below_production_threshold",
    }
)

# elif連鎖の外にある独立した if。他ゲートとの共起は合法。
INDEPENDENT_GATES: Final[frozenset[str]] = frozenset({"expectancy_guard", "missing_atr"})

_UNKNOWN: Final[str] = "unknown"
_GATE_LATENCY_UNMEASURABLE_REASON: Final[str] = "gate_level_timestamps_absent"


class FunnelReconciliationError(RuntimeError):
    """ファネルの件数恒等式が破れた場合に送出する。

    passed_all_gates + Σblocked_at + indeterminate == total_decisions
    が成立しない場合、集計は信頼できないので黙って続行せずFAILさせる。
    """


@dataclass(frozen=True)
class GateRecord:
    """gate_trace の1行。status は "blocked" か "observed" のみ。"""

    name: str
    status: str
    order_index: int


@dataclass(frozen=True)
class TraceEvent:
    """1判断イベントの解析済み表現。

    parse_status が "indeterminate" の行も**捨てずに**保持する。
    """

    decision_id: str | None
    ts: datetime | None
    pair: str
    timeframe: str
    producer: str
    regime: str
    final_action: str
    gates: tuple[GateRecord, ...]
    parse_status: str
    parse_reasons: tuple[str, ...] = ()

    @property
    def blocked_gates(self) -> tuple[str, ...]:
        """status="blocked" のゲート名を順序保持で返す(observedは含めない)。"""
        return tuple(gate.name for gate in self.gates if gate.status == "blocked")

    @property
    def observed_gates(self) -> tuple[str, ...]:
        """status="observed" のゲート名(liquidity等)。ブロックではない。"""
        return tuple(gate.name for gate in self.gates if gate.status == "observed")

    @property
    def first_blocker(self) -> str | None:
        """真のfirst blocker。blocked_by は固定評価順なので index 0 を採る。"""
        blocked = self.blocked_gates
        return blocked[0] if blocked else None


@dataclass(frozen=True)
class FunnelSummary:
    """判断ファネル。件数恒等式は build 時に必ず検証される。"""

    total_decisions: int
    blocked_at: dict[str, int]
    passed_all_gates: int
    indeterminate: int
    dominance: dict[str, float]
    invalid_transitions: tuple[dict[str, Any], ...] = ()
    missing_transitions: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class TraceReport:
    """MVPの出力全体。層別(pair × timeframe × regime)を含む。"""

    overall: FunnelSummary
    by_pair: dict[str, FunnelSummary] = field(default_factory=dict)
    by_timeframe: dict[str, FunnelSummary] = field(default_factory=dict)
    by_regime: dict[str, FunnelSummary] = field(default_factory=dict)
    by_cell: dict[str, FunnelSummary] = field(default_factory=dict)
    latency: dict[str, Any] = field(default_factory=dict)
    parse_reason_counts: dict[str, int] = field(default_factory=dict)


def _as_mapping(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


def _clean_str(value: object, fallback: str) -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    return text or fallback


def _parse_timestamp(value: object) -> tuple[datetime | None, str | None]:
    """aware UTC の datetime を返す。naive/不正は理由付きで失敗を返す。"""
    if value is None:
        return None, "missing_ts"
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return None, "missing_ts"
        normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None, "unparseable_ts"
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        # naive時刻を勝手にUTCとみなすとPIT整合が壊れるので、捏造せず失敗にする。
        return None, "naive_ts"
    return parsed.astimezone(UTC), None


def _gate_records(
    gate_trace: object,
    blocked_by: object,
) -> tuple[tuple[GateRecord, ...], tuple[str, ...]]:
    """gate_trace と blocked_by から GateRecord 列を組む。

    gate_trace は blocked と observed の両方を含みうる。observed(liquidity等)は
    blocked に混入させない。gate_trace が無い場合のみ blocked_by を代替入力にする。
    """
    reasons: list[str] = []
    records: list[GateRecord] = []
    seen_blocked: list[str] = []

    if isinstance(gate_trace, Sequence) and not isinstance(gate_trace, str | bytes):
        for index, row in enumerate(gate_trace):
            mapping = _as_mapping(row)
            if mapping is None:
                reasons.append("gate_trace_row_not_mapping")
                continue
            name = _clean_str(mapping.get("gate"), "")
            if not name:
                reasons.append("gate_trace_row_missing_name")
                continue
            status = _clean_str(mapping.get("status"), "")
            if status == "blocked":
                records.append(GateRecord(name=name, status="blocked", order_index=index))
                seen_blocked.append(name)
            elif status == "observed":
                # liquidity のような観測専用行。ブロックではない。
                records.append(GateRecord(name=name, status="observed", order_index=index))
            else:
                # 未知のstatusは推測せず indeterminate 理由として残す。
                reasons.append(f"gate_trace_unknown_status:{status or 'empty'}")
    elif gate_trace is not None:
        reasons.append("gate_trace_not_sequence")

    # gate_trace が blocked を持たない場合に限り blocked_by を代替入力にする。
    if isinstance(blocked_by, Sequence) and not isinstance(blocked_by, str | bytes):
        names = [_clean_str(item, "") for item in blocked_by]
        names = [name for name in names if name]
        if not seen_blocked and names:
            base = len(records)
            for offset, name in enumerate(names):
                records.append(GateRecord(name=name, status="blocked", order_index=base + offset))
    elif blocked_by is not None:
        reasons.append("blocked_by_not_sequence")

    return tuple(records), tuple(reasons)


def parse_event(event: object) -> TraceEvent:
    """1イベントを TraceEvent へ。壊れていても捨てずに indeterminate で返す。"""
    mapping = _as_mapping(event)
    if mapping is None:
        return TraceEvent(
            decision_id=None,
            ts=None,
            pair=_UNKNOWN,
            timeframe=_UNKNOWN,
            producer=_UNKNOWN,
            regime=_UNKNOWN,
            final_action=_UNKNOWN,
            gates=(),
            parse_status="indeterminate",
            parse_reasons=("event_not_mapping",),
        )

    reasons: list[str] = []

    decision_id_raw = mapping.get("decision_id")
    decision_id = _clean_str(decision_id_raw, "")
    if not decision_id:
        reasons.append("missing_decision_id")

    ts, ts_reason = _parse_timestamp(mapping.get("ts"))
    if ts_reason is not None:
        reasons.append(ts_reason)

    decision = _as_mapping(mapping.get("decision")) or {}

    # symbol が正準。pair は別名として受け入れる。
    pair = _clean_str(mapping.get("symbol") or mapping.get("pair"), "")
    if not pair:
        reasons.append("missing_pair")
        pair = _UNKNOWN

    timeframe = _clean_str(
        mapping.get("timeframe") or decision.get("timeframe"),
        "",
    )
    if not timeframe:
        reasons.append("missing_timeframe")
        timeframe = _UNKNOWN

    producer = _clean_str(mapping.get("source") or mapping.get("producer"), _UNKNOWN)

    dimensions = _as_mapping(decision.get("learning_dimensions")) or {}
    # regime 欠損は "unknown"。値を捏造しない。
    regime = _clean_str(dimensions.get("regime"), _UNKNOWN)

    final_action = _clean_str(decision.get("direction"), "")
    if not final_action:
        reasons.append("missing_direction")
        final_action = _UNKNOWN

    gates, gate_reasons = _gate_records(
        decision.get("gate_trace"),
        decision.get("blocked_by"),
    )
    reasons.extend(gate_reasons)

    status = "indeterminate" if reasons else "ok"
    return TraceEvent(
        decision_id=decision_id or None,
        ts=ts,
        pair=pair,
        timeframe=timeframe,
        producer=producer,
        regime=regime,
        final_action=final_action,
        gates=gates,
        parse_status=status,
        parse_reasons=tuple(reasons),
    )


def parse_events(events: Iterable[object]) -> list[TraceEvent]:
    """複数イベントを解析する。壊れた行も除外せず保持する。"""
    return [parse_event(event) for event in events]


def _invalid_transition(event: TraceEvent) -> dict[str, Any] | None:
    """相互排他ゲートの同時出現のみを不正とする。

    expectancy_guard / missing_atr は独立 if なので共起しても**合法**。
    """
    exclusive_hits = [name for name in event.blocked_gates if name in EXCLUSIVE_GATES]
    # 同一ゲートの重複計上を避けるため順序保持で一意化する。
    unique_hits: list[str] = []
    for name in exclusive_hits:
        if name not in unique_hits:
            unique_hits.append(name)
    if len(unique_hits) <= 1:
        return None
    return {
        "decision_id": event.decision_id,
        "pair": event.pair,
        "timeframe": event.timeframe,
        "gates": tuple(unique_hits),
        "reason": "mutually_exclusive_gates_co_occurred",
    }


def _missing_transition(event: TraceEvent) -> dict[str, Any] | None:
    """final_action=="neutral" かつ blocked_by==[] の判断(欠損遷移)。"""
    if event.final_action != "neutral":
        return None
    if event.blocked_gates:
        return None
    return {
        "decision_id": event.decision_id,
        "pair": event.pair,
        "timeframe": event.timeframe,
        "reason": "neutral_without_blocking_gate",
    }


def _summarize(events: Sequence[TraceEvent]) -> FunnelSummary:
    """1グループのファネルを組む。恒等式が破れたら送出する。"""
    total = len(events)
    blocked_at: dict[str, int] = {}
    passed = 0
    indeterminate = 0
    invalid: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []

    for event in events:
        if event.parse_status == "indeterminate":
            indeterminate += 1
            continue
        if event.parse_status != "ok":
            # 未知のparse_statusを"通過"に落とすと恒等式が偶然成立して誤集計を隠す。
            # 分類不能はfail-closedで送出する。
            raise FunnelReconciliationError(
                f"unknown parse_status: {event.parse_status!r} "
                f"(decision_id={event.decision_id!r})"
            )
        blocker = event.first_blocker
        if blocker is None:
            passed += 1
        else:
            blocked_at[blocker] = blocked_at.get(blocker, 0) + 1
        invalid_row = _invalid_transition(event)
        if invalid_row is not None:
            invalid.append(invalid_row)
        missing_row = _missing_transition(event)
        if missing_row is not None:
            missing.append(missing_row)

    accounted = passed + sum(blocked_at.values()) + indeterminate
    if accounted != total:
        raise FunnelReconciliationError(
            "funnel reconciliation failed: "
            f"passed({passed}) + blocked({sum(blocked_at.values())}) "
            f"+ indeterminate({indeterminate}) = {accounted} != total({total})"
        )

    denominator = total - indeterminate
    dominance = {
        name: (count / denominator if denominator > 0 else 0.0)
        for name, count in sorted(blocked_at.items())
    }
    return FunnelSummary(
        total_decisions=total,
        blocked_at=dict(sorted(blocked_at.items())),
        passed_all_gates=passed,
        indeterminate=indeterminate,
        dominance=dominance,
        invalid_transitions=tuple(invalid),
        missing_transitions=tuple(missing),
    )


def _group(
    events: Sequence[TraceEvent],
    key: Any,
) -> dict[str, FunnelSummary]:
    """層別集計。キーはsortしてdict順序依存を排除する。"""
    buckets: dict[str, list[TraceEvent]] = {}
    for event in events:
        buckets.setdefault(key(event), []).append(event)
    return {name: _summarize(buckets[name]) for name in sorted(buckets)}


def _latency_section(events: Sequence[TraceEvent]) -> dict[str, Any]:
    """工程別遅延。

    gate単位のtimestampが存在しないため measurable=false とし、推定値を捏造しない。
    判断単位の遅延分布のみ算出する(連続する判断のts差分)。
    """
    stamps = sorted(event.ts for event in events if event.ts is not None)
    section: dict[str, Any] = {
        "per_gate": {
            "measurable": False,
            "reason": _GATE_LATENCY_UNMEASURABLE_REASON,
        },
        "per_decision": {
            "measurable": len(stamps) >= 2,
            "sample_count": len(stamps),
        },
    }
    if len(stamps) < 2:
        section["per_decision"]["reason"] = "insufficient_timestamps"
        return section
    # stamps[1:] は1件短いので strict=False(既定)で隣接ペアを作る。
    deltas = sorted(
        (later - earlier).total_seconds()
        for earlier, later in zip(stamps, stamps[1:], strict=False)
    )
    midpoint = len(deltas) // 2
    median = (
        deltas[midpoint]
        if len(deltas) % 2 == 1
        else (deltas[midpoint - 1] + deltas[midpoint]) / 2.0
    )
    section["per_decision"].update(
        {
            "span_seconds": (stamps[-1] - stamps[0]).total_seconds(),
            "min_gap_seconds": deltas[0],
            "median_gap_seconds": median,
            "max_gap_seconds": deltas[-1],
        }
    )
    return section


def build_report(events: Iterable[object]) -> TraceReport:
    """イベント列から MVP レポートを構築する。

    同一入力からは決定論的に同一出力を返す(キーは常にsort)。
    """
    parsed = parse_events(events)
    reason_counts: dict[str, int] = {}
    for event in parsed:
        for reason in event.parse_reasons:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1

    return TraceReport(
        overall=_summarize(parsed),
        by_pair=_group(parsed, lambda event: event.pair),
        by_timeframe=_group(parsed, lambda event: event.timeframe),
        by_regime=_group(parsed, lambda event: event.regime),
        by_cell=_group(
            parsed,
            lambda event: f"{event.pair}|{event.timeframe}|{event.regime}",
        ),
        latency=_latency_section(parsed),
        parse_reason_counts=dict(sorted(reason_counts.items())),
    )


def _summary_to_dict(summary: FunnelSummary) -> dict[str, Any]:
    return {
        "total_decisions": summary.total_decisions,
        "blocked_at": dict(summary.blocked_at),
        "passed_all_gates": summary.passed_all_gates,
        "indeterminate": summary.indeterminate,
        "dominance": dict(summary.dominance),
        "invalid_transitions": [dict(row) for row in summary.invalid_transitions],
        "missing_transitions": [dict(row) for row in summary.missing_transitions],
    }


def report_to_dict(report: TraceReport) -> dict[str, Any]:
    """JSON化可能な素のdictへ。キー順序は決定論的。"""
    return {
        "contract": "decision-trace-analytics-mvp-v1",
        "read_only": True,
        "overall": _summary_to_dict(report.overall),
        "by_pair": {name: _summary_to_dict(value) for name, value in report.by_pair.items()},
        "by_timeframe": {
            name: _summary_to_dict(value) for name, value in report.by_timeframe.items()
        },
        "by_regime": {name: _summary_to_dict(value) for name, value in report.by_regime.items()},
        "by_cell": {name: _summary_to_dict(value) for name, value in report.by_cell.items()},
        "latency": report.latency,
        "parse_reason_counts": dict(report.parse_reason_counts),
    }

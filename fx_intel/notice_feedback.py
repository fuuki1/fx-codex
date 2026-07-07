"""Feedback profile for detailed trade notices.

This profile summarizes notice-quality outcomes by stable, explainable
conditions.  It is intentionally separate from the core briefing learning
profile so detailed-notice copy and entry-condition quality can mature without
changing the underlying directional signal.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime, UTC
from pathlib import Path
from collections.abc import Mapping, Sequence

from .coerce import (
    float_field,
    int_field,
    to_float,
    to_float_or_none,
    to_int,
    to_int_or_none,
)
from .market_structure import EntryLevels
from .notice_quality import (
    OUTCOME_AMBIGUOUS,
    OUTCOME_HIT,
    OUTCOME_MISS,
    OUTCOME_NO_ENTRY,
    OUTCOME_NO_TOUCH,
    OUTCOME_SKIPPED,
    ENTRY_CHECK_NOT_TRIGGERED,
    ENTRY_CHECK_TRIGGERED,
    ENTRY_SCENARIO_BREAKOUT,
    ENTRY_SCENARIO_PULLBACK,
    NoticeQualityOutcome,
)
from .trade_notice import DetailedTradeNotice

SCHEMA_VERSION = 3
MIN_CONDITION_EVALUATED = 5
WEAK_HIT_RATE = 0.45
FACTOR_MIN = 0.7
FACTOR_BASELINE = 0.5
EXPECTANCY_BLOCK_FACTOR = 0.45
EXPECTANCY_WEAK_FACTOR = 0.80
CONVICTION_BANDS = ((0, 40), (40, 55), (55, 70), (70, 101))
ENTRY_SCENARIO_LABELS = {
    ENTRY_SCENARIO_PULLBACK: "エントリー条件 押し目/戻り売り確認",
    ENTRY_SCENARIO_BREAKOUT: "エントリー条件 ブレイク維持",
}
ENTRY_CHECK_LABELS = {
    ENTRY_CHECK_TRIGGERED: "エントリー条件 発火後",
    ENTRY_CHECK_NOT_TRIGGERED: "エントリー条件 未発火",
}


@dataclass
class FeedbackCell:
    key: str
    label_ja: str
    total: int = 0
    evaluated: int = 0
    hits: int = 0
    misses: int = 0
    ambiguous: int = 0
    no_touch: int = 0
    no_entry_trigger: int = 0
    skipped: int = 0
    factor: float = 1.0

    @property
    def hit_rate(self) -> float | None:
        if self.evaluated == 0:
            return None
        return self.hits / self.evaluated

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label_ja": self.label_ja,
            "total": self.total,
            "evaluated": self.evaluated,
            "hits": self.hits,
            "misses": self.misses,
            "ambiguous": self.ambiguous,
            "no_touch": self.no_touch,
            "no_entry_trigger": self.no_entry_trigger,
            "skipped": self.skipped,
            "hit_rate": self.hit_rate,
            "factor": self.factor,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> FeedbackCell:
        return cls(
            key=str(raw.get("key", "")),
            label_ja=str(raw.get("label_ja", "")),
            total=int_field(raw, "total"),
            evaluated=int_field(raw, "evaluated"),
            hits=int_field(raw, "hits"),
            misses=int_field(raw, "misses"),
            ambiguous=int_field(raw, "ambiguous"),
            no_touch=int_field(raw, "no_touch"),
            no_entry_trigger=int_field(raw, "no_entry_trigger"),
            skipped=int_field(raw, "skipped"),
            factor=float_field(raw, "factor", 1.0),
        )


@dataclass
class NoticeFeedbackProfile:
    generated_at: str = ""
    total: int = 0
    evaluated: int = 0
    hits: int = 0
    cells: dict[str, FeedbackCell] = field(default_factory=dict)
    weak_keys: list[str] = field(default_factory=list)
    notes_ja: list[str] = field(default_factory=list)

    @property
    def hit_rate(self) -> float | None:
        if self.evaluated == 0:
            return None
        return self.hits / self.evaluated

    def weak_cells(self) -> list[FeedbackCell]:
        return [self.cells[key] for key in self.weak_keys if key in self.cells]

    def to_dict(self) -> dict:
        return {
            "schema": SCHEMA_VERSION,
            "generated_at": self.generated_at,
            "total": self.total,
            "evaluated": self.evaluated,
            "hits": self.hits,
            "hit_rate": self.hit_rate,
            "cells": {key: cell.to_dict() for key, cell in sorted(self.cells.items())},
            "weak_keys": list(self.weak_keys),
            "notes_ja": list(self.notes_ja),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> NoticeFeedbackProfile:
        cells_raw = raw.get("cells")
        cells = (
            {
                str(key): FeedbackCell.from_dict(value)
                for key, value in cells_raw.items()
                if isinstance(value, Mapping)
            }
            if isinstance(cells_raw, Mapping)
            else {}
        )
        weak_raw = raw.get("weak_keys")
        notes_raw = raw.get("notes_ja")
        return cls(
            generated_at=str(raw.get("generated_at", "")),
            total=int_field(raw, "total"),
            evaluated=int_field(raw, "evaluated"),
            hits=int_field(raw, "hits"),
            cells=cells,
            weak_keys=[str(item) for item in weak_raw] if isinstance(weak_raw, list) else [],
            notes_ja=[str(item) for item in notes_raw] if isinstance(notes_raw, list) else [],
        )


def build_feedback_profile(
    entries: Sequence[Mapping[str, object]],
    outcomes: Sequence[NoticeQualityOutcome],
    *,
    now: datetime | None = None,
    min_evaluated: int = MIN_CONDITION_EVALUATED,
    weak_hit_rate: float = WEAK_HIT_RATE,
) -> NoticeFeedbackProfile:
    """Aggregate quality outcomes into an explainable feedback profile."""
    now = now or datetime.now(UTC)
    cells: dict[str, FeedbackCell] = {}
    for entry, outcome in zip(entries, outcomes, strict=False):
        for key, label in _unique_keys(
            [*condition_keys_for_entry(entry), *condition_keys_for_outcome(outcome)]
        ):
            cell = cells.setdefault(key, FeedbackCell(key=key, label_ja=label))
            _apply_outcome(cell, outcome)

    for cell in cells.values():
        rate = cell.hit_rate
        if rate is not None and cell.evaluated >= min_evaluated and rate < weak_hit_rate:
            cell.factor = round(max(FACTOR_MIN, rate / FACTOR_BASELINE), 2)

    weak_cells = sorted(
        (cell for cell in cells.values() if cell.factor < 1.0),
        key=lambda cell: (cell.hit_rate if cell.hit_rate is not None else 1.0, -cell.evaluated),
    )
    total = len(outcomes)
    evaluated = sum(1 for outcome in outcomes if outcome.evaluated)
    hits = sum(1 for outcome in outcomes if outcome.outcome == OUTCOME_HIT)
    profile = NoticeFeedbackProfile(
        generated_at=now.isoformat(),
        total=total,
        evaluated=evaluated,
        hits=hits,
        cells=cells,
        weak_keys=[cell.key for cell in weak_cells],
    )
    profile.notes_ja = _notes(profile)
    return profile


def apply_feedback_to_notice(
    notice: DetailedTradeNotice,
    profile: NoticeFeedbackProfile,
    entry_level: EntryLevels | None = None,
    *,
    expectancy_summary: Mapping[str, object] | None = None,
    limit: int = 3,
) -> DetailedTradeNotice:
    """Add profile-derived caution text to a detailed notice."""
    warnings = feedback_warnings_for_notice(
        notice,
        profile,
        entry_level,
        expectancy_summary=expectancy_summary,
        limit=limit,
    )
    adjustment = (
        _empty_expectancy_adjustment()
        if _expectancy_already_applied(notice)
        else expectancy_adjustment_for_notice(notice, expectancy_summary)
    )
    if adjustment["warning"] and adjustment["warning"] not in warnings:
        warnings.append(str(adjustment["warning"]))
    factor = to_float(adjustment["factor"], 1.0)
    if not warnings and factor >= 1.0:
        return notice
    final_actions = list(notice.final_actions)
    action = str(adjustment["action"])
    if action and action not in final_actions:
        final_actions = [action, *final_actions]
    final_evaluation = str(adjustment["final_evaluation"] or notice.final_evaluation)
    conviction = notice.conviction
    priority = notice.priority
    if factor < 1.0:
        conviction = round(notice.conviction * factor)
        priority = str(adjustment["priority"] or notice.priority)
    return replace(
        notice,
        conviction=conviction,
        priority=priority,
        caution_factors=[*notice.caution_factors, *warnings],
        final_actions=final_actions,
        final_evaluation=final_evaluation,
        warnings=[*notice.warnings, *warnings],
    )


def feedback_warnings_for_notice(
    notice: DetailedTradeNotice,
    profile: NoticeFeedbackProfile,
    entry_level: EntryLevels | None = None,
    *,
    expectancy_summary: Mapping[str, object] | None = None,
    limit: int = 3,
) -> list[str]:
    """Return Japanese caution lines that match a notice's weak conditions."""
    entry = notice_to_condition_entry(notice, entry_level)
    warnings = (
        []
        if _expectancy_already_applied(notice)
        else expectancy_warnings_for_notice(notice, expectancy_summary, limit=limit)
    )
    keys = _unique_keys(
        [*planned_scenario_keys_for_notice(notice), *condition_keys_for_entry(entry)]
    )
    for key, _label in keys:
        cell = profile.cells.get(key)
        if cell is None or cell.factor >= 1.0 or cell.evaluated <= 0:
            continue
        rate = cell.hit_rate
        rate_text = "—" if rate is None else f"{rate:.0%}"
        warnings.append(
            f"詳細通知学習: 「{cell.label_ja}」は過去のT1先着率{rate_text}"
            f"({cell.evaluated}件)と低いため、条件確認を厳格化"
        )
        if len(warnings) >= limit:
            break
    return warnings[:limit]


def expectancy_warnings_for_notice(
    notice: DetailedTradeNotice,
    expectancy_summary: Mapping[str, object] | None,
    *,
    limit: int = 2,
) -> list[str]:
    """Return warnings from MFE/MAE/TP/SL expectancy stats for this notice."""
    if not isinstance(expectancy_summary, Mapping):
        return []
    warnings: list[str] = []
    for label, stats in _matching_expectancy_stats(notice, expectancy_summary):
        warning = _expectancy_warning(label, stats)
        if warning and warning not in warnings:
            warnings.append(warning)
        if len(warnings) >= limit:
            break
    return warnings


def expectancy_adjustment_for_notice(
    notice: DetailedTradeNotice,
    expectancy_summary: Mapping[str, object] | None,
) -> dict[str, object]:
    """Return conviction/action adjustments from expectancy stats for this notice."""
    if not isinstance(expectancy_summary, Mapping):
        return _empty_expectancy_adjustment()
    best = _empty_expectancy_adjustment()
    for label, stats in _matching_expectancy_stats(notice, expectancy_summary):
        candidate = _expectancy_adjustment(label, stats)
        if to_float(candidate["factor"], 1.0) < to_float(best["factor"], 1.0):
            best = candidate
    return best


def _expectancy_already_applied(notice: DetailedTradeNotice) -> bool:
    texts = [*notice.warnings, *notice.caution_factors]
    return any("期待値ガード" in text for text in texts)


def condition_keys_for_entry(entry: Mapping[str, object]) -> list[tuple[str, str]]:
    """Stable condition keys for notice-quality aggregation."""
    keys = [("overall", "全詳細通知")]
    symbol = str(entry.get("symbol", "")).strip().upper()
    direction = str(entry.get("direction", "")).strip()
    if symbol:
        keys.append((f"symbol:{symbol}", f"通貨ペア {symbol}"))
    if direction in ("long", "short"):
        keys.append((f"direction:{direction}", f"方向 {direction}"))
    band = conviction_band(entry.get("conviction"))
    if band is not None:
        keys.append((f"conviction:{band[0]}-{band[1]}", f"確信度 {band[0]}〜{band[1] - 1}"))
    level = entry.get("entry_level_source")
    source = ""
    if isinstance(level, Mapping):
        source = str(level.get("source", "")).strip()
    if source:
        keys.append((f"entry_source:{source}", f"エントリー根拠 {source}"))
    event = entry.get("important_event")
    keys.append(
        (
            "event:present" if isinstance(event, Mapping) and event else "event:none",
            "重要イベントあり" if isinstance(event, Mapping) and event else "重要イベントなし",
        )
    )
    no_entry = entry.get("no_entry_window")
    keys.append(
        (
            (
                "no_entry_window:present"
                if isinstance(no_entry, Mapping) and no_entry
                else "no_entry_window:none"
            ),
            (
                "新規禁止時間あり"
                if isinstance(no_entry, Mapping) and no_entry
                else "新規禁止時間なし"
            ),
        )
    )
    return keys


def condition_keys_for_outcome(outcome: NoticeQualityOutcome) -> list[tuple[str, str]]:
    """Stable condition keys derived from quality-scoring outcomes."""
    keys: list[tuple[str, str]] = []
    if outcome.entry_check in ENTRY_CHECK_LABELS:
        keys.append(
            (f"entry_trigger:{outcome.entry_check}", ENTRY_CHECK_LABELS[outcome.entry_check])
        )
    if outcome.entry_scenario in ENTRY_SCENARIO_LABELS:
        keys.append(
            (
                f"entry_scenario:{outcome.entry_scenario}",
                ENTRY_SCENARIO_LABELS[outcome.entry_scenario],
            )
        )
    return keys


def planned_scenario_keys_for_notice(notice: DetailedTradeNotice) -> list[tuple[str, str]]:
    """Return entry-scenario keys a future notice may execute."""
    keys: list[tuple[str, str]] = []
    for scenario in notice.entry_scenarios:
        scenario_key = _scenario_key_from_title(scenario.title)
        if scenario_key is None:
            continue
        keys.append((f"entry_scenario:{scenario_key}", ENTRY_SCENARIO_LABELS[scenario_key]))
    return _unique_keys(keys)


def notice_to_condition_entry(
    notice: DetailedTradeNotice, entry_level: EntryLevels | None = None
) -> dict:
    """Convert a notice into the minimal journal-like shape used for matching."""
    return {
        "symbol": notice.symbol,
        "direction": notice.direction,
        "conviction": notice.conviction,
        "entry_level_source": (
            {"source": entry_level.source}
            if entry_level is not None
            else {"source": "atr_fallback"}
        ),
        "important_event": (
            {} if notice.important_event is None else {"title": notice.important_event.title}
        ),
        "no_entry_window": (
            {}
            if notice.no_entry_window is None
            else {"start": notice.no_entry_window.start.isoformat()}
        ),
    }


def _matching_expectancy_stats(
    notice: DetailedTradeNotice,
    expectancy_summary: Mapping[str, object],
) -> list[tuple[str, Mapping[str, object]]]:
    output: list[tuple[str, Mapping[str, object]]] = []
    by_symbol = expectancy_summary.get("by_symbol")
    if isinstance(by_symbol, Mapping):
        stats = by_symbol.get(notice.symbol)
        if isinstance(stats, Mapping):
            output.append((f"通貨ペア {notice.symbol}", stats))
    by_direction = expectancy_summary.get("by_direction")
    if isinstance(by_direction, Mapping):
        stats = by_direction.get(notice.direction)
        if isinstance(stats, Mapping):
            output.append((f"方向 {notice.direction}", stats))
    overall = expectancy_summary.get("overall")
    if isinstance(overall, Mapping):
        output.append(("全体", overall))
    return output


def _expectancy_warning(label: str, stats: Mapping[str, object]) -> str:
    return str(_expectancy_adjustment(label, stats)["warning"])


def _expectancy_adjustment(label: str, stats: Mapping[str, object]) -> dict[str, object]:
    tradable = _int_value(stats.get("tradable"))
    min_samples = _int_value(stats.get("min_samples"))
    expectancy_r = _float_value(stats.get("expectancy_r"))
    profit_factor = _float_value(stats.get("profit_factor_r"))
    avg_mfe = _float_value(stats.get("avg_mfe_r"))
    avg_mae = _float_value(stats.get("avg_mae_r"))
    sample_ok = bool(stats.get("sample_ok"))
    if tradable <= 0:
        return _empty_expectancy_adjustment()
    sample_text = f"n={tradable}" + (f"/{min_samples}" if min_samples else "")
    if not sample_ok:
        return _weak_expectancy_adjustment(
            (
                f"期待値学習: {label}は有効サンプル不足({sample_text})のため、"
                "確信度を過信せず条件確認を厳格化"
            ),
            "期待値ガード: サンプル不足のため、成行は避けて条件確認後のみ検討",
            "期待値サンプルが不足しているため、方向目線は参考扱いです。条件達成まで見送りを優先します。",
        )
    if expectancy_r is not None and expectancy_r <= 0:
        return _block_expectancy_adjustment(
            (
                f"期待値学習: {label}の期待Rは{expectancy_r:+.2f}R({sample_text})で非正。"
                "新規エントリーは見送り優先"
            ),
            "期待値ガード: 新規エントリーは見送り優先。再評価まで監視のみ",
            "期待値学習ではこの条件の期待Rが非正です。方向目線は参考に留め、今回は見送り優先です。",
        )
    if profit_factor is not None and profit_factor < 1.05:
        return _weak_expectancy_adjustment(
            (
                f"期待値学習: {label}のPFは{profit_factor:.2f}({sample_text})で薄い。"
                "利確/損切り条件を厳格化"
            ),
            "期待値ガード: PFが薄いため、T1/SL条件を満たすまで見送り",
            "期待値は薄いため、方向目線は維持しても実行は条件確認後に限定します。",
        )
    if avg_mfe is not None and avg_mae is not None and avg_mfe <= avg_mae:
        return _weak_expectancy_adjustment(
            (
                f"期待値学習: {label}は平均MFE{avg_mfe:.2f}Rに対し平均MAE{avg_mae:.2f}R。"
                "逆行圧力が強いためエントリー条件を厳格化"
            ),
            "期待値ガード: MAE圧力が強いため、押し目/戻り確認なしでは見送り",
            "平均MAEが重いため、飛び乗りではなく確認型エントリーに限定します。",
        )
    return _empty_expectancy_adjustment()


def _empty_expectancy_adjustment() -> dict[str, object]:
    return {
        "factor": 1.0,
        "warning": "",
        "action": "",
        "final_evaluation": "",
        "priority": "",
    }


def _weak_expectancy_adjustment(
    warning: str,
    action: str,
    final_evaluation: str,
) -> dict[str, object]:
    return {
        "factor": EXPECTANCY_WEAK_FACTOR,
        "warning": warning,
        "action": action,
        "final_evaluation": final_evaluation,
        "priority": "期待値ガードを優先し、条件達成時のみ実行",
    }


def _block_expectancy_adjustment(
    warning: str,
    action: str,
    final_evaluation: str,
) -> dict[str, object]:
    return {
        "factor": EXPECTANCY_BLOCK_FACTOR,
        "warning": warning,
        "action": action,
        "final_evaluation": final_evaluation,
        "priority": "期待値ガードを優先し、新規エントリーは見送り",
    }


def _int_value(value: object) -> int:
    return to_int(value)


def _float_value(value: object) -> float | None:
    return to_float_or_none(value)


def _scenario_key_from_title(title: str) -> str | None:
    if "ブレイク" in title:
        return ENTRY_SCENARIO_BREAKOUT
    if "押し目" in title or "戻り売り" in title:
        return ENTRY_SCENARIO_PULLBACK
    return None


def _unique_keys(keys: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    output: list[tuple[str, str]] = []
    seen: set[str] = set()
    for key, label in keys:
        if key in seen:
            continue
        seen.add(key)
        output.append((key, label))
    return output


def conviction_band(value: object) -> tuple[int, int] | None:
    conviction = to_int_or_none(value)
    if conviction is None:
        return None
    for low, high in CONVICTION_BANDS:
        if low <= conviction < high:
            return low, high
    return None


def save_profile(profile: NoticeFeedbackProfile, path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(profile.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def load_profile(path: str | Path) -> NoticeFeedbackProfile:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return NoticeFeedbackProfile()
    if not isinstance(raw, Mapping):
        return NoticeFeedbackProfile()
    return NoticeFeedbackProfile.from_dict(raw)


def format_profile_ja(profile: NoticeFeedbackProfile, limit: int = 5) -> str:
    if profile.total == 0:
        return "詳細通知フィードバック: 対象なし"
    rate = "—" if profile.hit_rate is None else f"{profile.hit_rate:.0%}"
    lines = [
        f"詳細通知フィードバック: 対象{profile.total}件 / 評価{profile.evaluated}件 / T1先着率{rate}"
    ]
    weak = profile.weak_cells()[:limit]
    if weak:
        lines.append("弱い条件:")
        for cell in weak:
            hit_rate = "—" if cell.hit_rate is None else f"{cell.hit_rate:.0%}"
            lines.append(
                f"・{cell.label_ja}: T1先着率{hit_rate}({cell.evaluated}件) → 注意係数×{cell.factor:.2f}"
            )
    else:
        lines.append("弱い条件: サンプル不足または該当なし")
    return "\n".join(lines)


def _apply_outcome(cell: FeedbackCell, outcome: NoticeQualityOutcome) -> None:
    cell.total += 1
    if outcome.outcome == OUTCOME_HIT:
        cell.evaluated += 1
        cell.hits += 1
    elif outcome.outcome == OUTCOME_MISS:
        cell.evaluated += 1
        cell.misses += 1
    elif outcome.outcome == OUTCOME_AMBIGUOUS:
        cell.ambiguous += 1
    elif outcome.outcome == OUTCOME_NO_TOUCH:
        cell.no_touch += 1
    elif outcome.outcome == OUTCOME_NO_ENTRY:
        cell.no_entry_trigger += 1
    elif outcome.outcome == OUTCOME_SKIPPED:
        cell.skipped += 1


def _notes(profile: NoticeFeedbackProfile) -> list[str]:
    notes = [format_profile_ja(profile)]
    return notes

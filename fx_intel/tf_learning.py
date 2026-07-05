"""時間足別の自己採点・学習。

learning.py が「融合1判断を24h固定で採点し symbol 別に学習」するのに対し、
このモジュールは時間足別ジャーナル(journal.append_timeframe_plans)を入力に、
各判断を**その時間足の主ホライズン**で採点し、**symbol×timeframe のセル単位**で
学習プロファイルを導く。

    15m → 15分後   1h → 1時間後   4h → 4時間後   1d → 24時間後

設計方針は「既存の実績ある採点・学習コアを、切り出したスライスへ適用し直す」:

- 採点は learning.evaluate_history をそのまま使う。時間足でエントリを絞り、
  その時間足の主ホライズン(timeframe.PRIMARY_HORIZON_HOURS)と許容誤差
  (timeframe.tolerance_for)を渡す。evaluate_history の内部価格系列は
  symbol 別だが、事前に1時間足へ絞るのでその時間足の close 列になる。
- 学習は learning.derive_profile をそのまま使う。(symbol, timeframe) の
  スライスごとに呼べば、重み再推定・確信度キャリブレーション・ペア別減衰・
  状態×方向学習・反省レポート・Brier がセル単位で得られる。
  → learning.py のロジックを一切複製せず、粒度だけ (symbol, timeframe) に上げる。

補助ホライズン(15m:30分/1h、1h:4h/8h 等)は観測専用。学習には主ホライズン
だけを使う(同じ判断を複数の未来時間で採点すると多重検定になるため)。

profile_lookup は timeframe.build_timeframe_plans がそのまま受け取れる契約
((symbol, timeframe) -> (tech_weight, news_weight, conviction_factor,
condition_adjuster))を返す。学習前・データ不足のセルは既定値(=無調整)を返す
ため、後方互換で安全にフォールバックする。

ネットワークアクセスを持たない純粋ロジックで、テストから直接検証できる。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, UTC
from pathlib import Path

from .learning import (
    CONDITION_FACTOR_MIN,
    DERIVE_THIN_GAP_HOURS,
    ConvictionBin,
    EvaluatedCall,
    LearnedProfile,
    NEWS_WEIGHT,
    SYMBOL_FACTOR_MIN,
    TECH_WEIGHT,
    TECH_WEIGHT_MAX,
    TECH_WEIGHT_MIN,
    derive_profile,
    evaluate_history,
    thin_calls,
)
from .timeframe import (
    AUXILIARY_HORIZON_HOURS,
    DEFAULT_TIMEFRAMES,
    PRIMARY_HORIZON_HOURS,
    tolerance_for,
)

ConditionAdjuster = Callable[[Mapping[str, float], str], tuple[float, str]]
ProfileLookup = Callable[[str, str], tuple[float, float, float, ConditionAdjuster | None]]

TIMEFRAME_LABEL_JA = {"15m": "15分足", "1h": "1時間足", "4h": "4時間足", "1d": "日足"}
BASELINE_MIN_LIVE_EVALUATED = 20


def entries_for_timeframe(entries: Iterable[Mapping[str, object]], timeframe: str) -> list[dict]:
    """指定した時間足のジャーナル行だけを取り出す。

    timeframe を持たない旧スキーマ行(融合1判断)は時間足別採点の対象外なので
    含めない(それらは learning.py 側で従来どおり24h採点される)。
    """
    return [
        dict(entry)
        for entry in entries
        if isinstance(entry, Mapping) and str(entry.get("timeframe", "")) == timeframe
    ]


def evaluate_timeframe_history(
    entries: Iterable[Mapping[str, object]],
    timeframe: str,
    atr_fraction: float | None = None,
) -> list[EvaluatedCall]:
    """1時間足ぶんの全判断を、その時間足の主ホライズンで採点する。

    evaluate_history をそのまま使い、horizon/tolerance をこの時間足のものに
    差し替える。将来価格は同じ (symbol, timeframe) スライスの後続 close から
    取る(price_history 源Aと同じ「後続行が将来価格」方式)。
    """
    filtered = entries_for_timeframe(entries, timeframe)
    horizon = PRIMARY_HORIZON_HOURS.get(timeframe, 24.0)
    tolerance = tolerance_for(horizon)
    kwargs: dict[str, float] = {"horizon_hours": horizon, "tolerance_hours": tolerance}
    if atr_fraction is not None:
        kwargs["atr_fraction"] = atr_fraction
    return evaluate_history(filtered, **kwargs)


def _calls_for_symbol(calls: Sequence[EvaluatedCall], symbol: str) -> list[EvaluatedCall]:
    return [call for call in calls if call.symbol == symbol]


@dataclass
class TimeframeLearning:
    """時間足別の学習スナップショット。

    profiles は (symbol, timeframe) → その主ホライズンで採点した LearnedProfile。
    per_timeframe は timeframe → その時間足全ペア合算の LearnedProfile(表示用)。
    """

    generated_at: str = ""
    # (symbol, timeframe) 別プロファイル。build_timeframe_plans の注入元
    profiles: dict[tuple[str, str], LearnedProfile] = field(default_factory=dict)
    # timeframe 別の全ペア合算プロファイル(Discord・dashboard 表示用)
    per_timeframe: dict[str, LearnedProfile] = field(default_factory=dict)

    def profile_for(self, symbol: str, timeframe: str) -> LearnedProfile:
        """(symbol, timeframe) のプロファイル(無ければ既定=無調整)。"""
        return self.profiles.get((symbol, timeframe), LearnedProfile())

    def profile_lookup(
        self, symbol: str, timeframe: str
    ) -> tuple[float, float, float, ConditionAdjuster | None]:
        """build_timeframe_plans に渡す学習調整タプルを返す。

        (tech_weight, news_weight, conviction_factor, condition_adjuster)。
        学習前・データ不足のセルは (TECH_WEIGHT, NEWS_WEIGHT, 1.0, None) =
        既定挙動(無調整)。
        """
        profile = self.profiles.get((symbol, timeframe))
        if profile is None:
            return TECH_WEIGHT, NEWS_WEIGHT, 1.0, None
        conviction_factor = profile.conviction_factor(symbol)
        return (
            profile.tech_weight,
            profile.news_weight,
            conviction_factor,
            profile.condition_adjustment,
        )

    def summary_ja(self, timeframes: Sequence[str] = DEFAULT_TIMEFRAMES) -> str:
        """時間足別の学習メモを1本にまとめる(Discord 表示用)。"""
        lines: list[str] = []
        for timeframe in timeframes:
            profile = self.per_timeframe.get(timeframe)
            if profile is None or (profile.evaluated == 0 and profile.flat == 0):
                continue
            label = TIMEFRAME_LABEL_JA.get(timeframe, timeframe)
            horizon = PRIMARY_HORIZON_HOURS.get(timeframe, 24.0)
            header = f"【{label}】(主ホライズン{_fmt_hours(horizon)})"
            body = profile.summary_ja()
            lines.append(header + "\n" + body)
        if not lines:
            return (
                "時間足別の学習データ蓄積中 — 採点可能な過去判断がまだありません"
                "(各時間足が主ホライズンぶん経過すると自己学習が始まります)"
            )
        return "\n\n".join(lines)


def _fmt_hours(hours: float) -> str:
    if hours < 1.0:
        return f"{round(hours * 60)}分後"
    if hours == int(hours):
        return f"{int(hours)}時間後"
    return f"{hours}時間後"


def derive_timeframe_learning(
    entries: Iterable[Mapping[str, object]],
    now: datetime | None = None,
    timeframes: Sequence[str] = DEFAULT_TIMEFRAMES,
    thin_gap_hours: float = DERIVE_THIN_GAP_HOURS,
) -> TimeframeLearning:
    """時間足別ジャーナルから (symbol, timeframe) 別の学習を導く。

    各時間足をその主ホライズンで採点し、(symbol, timeframe) のスライスごとに
    derive_profile を呼ぶ。時間足全体の合算プロファイルも表示用に作る。
    """
    now = now or datetime.now(UTC)
    materialized = list(entries)
    profiles: dict[tuple[str, str], LearnedProfile] = {}
    per_timeframe: dict[str, LearnedProfile] = {}

    for timeframe in timeframes:
        calls = evaluate_timeframe_history(materialized, timeframe)
        if not calls:
            continue
        horizon_label = _fmt_hours(PRIMARY_HORIZON_HOURS.get(timeframe, 24.0))
        # 時間足全体(全ペア合算)のプロファイル(表示用)
        per_timeframe[timeframe] = derive_profile(
            calls, now=now, thin_gap_hours=thin_gap_hours, horizon_label=horizon_label
        )
        # (symbol, timeframe) 別プロファイル(注入元)
        for symbol in {call.symbol for call in calls}:
            symbol_calls = _calls_for_symbol(calls, symbol)
            profiles[(symbol, timeframe)] = derive_profile(
                symbol_calls,
                now=now,
                thin_gap_hours=thin_gap_hours,
                horizon_label=horizon_label,
            )

    return TimeframeLearning(
        generated_at=now.isoformat(),
        profiles=profiles,
        per_timeframe=per_timeframe,
    )


def auxiliary_horizon_report_ja(
    entries: Iterable[Mapping[str, object]],
    timeframe: str,
    thin_gap_hours: float = DERIVE_THIN_GAP_HOURS,
) -> str:
    """補助ホライズン(観測専用)での的中率を1行にまとめる。データ無しは空文字。

    学習には使わない。「主ホライズンだけでなく、少し先まで方向が続いたか」を
    観測して分析確認に使う(15m なら 30分後/1h後、1h なら 4h後/8h後 等)。
    """
    filtered = entries_for_timeframe(entries, timeframe)
    if not filtered:
        return ""
    aux = AUXILIARY_HORIZON_HOURS.get(timeframe, ())
    parts: list[str] = []
    total_scored = 0
    for horizon in aux:
        calls = evaluate_history(
            filtered, horizon_hours=horizon, tolerance_hours=tolerance_for(horizon)
        )
        if thin_gap_hours > 0:
            calls = thin_calls(calls, thin_gap_hours)
        scored = [call for call in calls if call.outcome in ("hit", "miss")]
        if not scored:
            parts.append(f"{_fmt_hours(horizon)} —(n=0)")
            continue
        hits = sum(1 for call in scored if call.outcome == "hit")
        total_scored += len(scored)
        parts.append(f"{_fmt_hours(horizon)} {hits / len(scored):.0%}(n={len(scored)})")
    if total_scored == 0:
        return ""
    label = TIMEFRAME_LABEL_JA.get(timeframe, timeframe)
    return f"{label} 補助ホライズン(観測用・学習には不使用): " + " / ".join(parts)


def save_timeframe_learning(learning: TimeframeLearning, path: str | Path) -> None:
    """時間足別学習をJSONへ保存する(毎回上書き。dashboard が読む)。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": learning.generated_at,
        "profiles": {
            f"{symbol}|{timeframe}": _profile_to_dict(profile)
            for (symbol, timeframe), profile in learning.profiles.items()
        },
        "per_timeframe": {
            timeframe: _profile_to_dict(profile)
            for timeframe, profile in learning.per_timeframe.items()
        },
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_timeframe_learning(path: str | Path) -> TimeframeLearning:
    """保存済みの時間足別学習を読む。無い/壊れている場合は空プロファイル。"""
    target = Path(path)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return TimeframeLearning()
    if not isinstance(payload, dict):
        return TimeframeLearning()

    profiles: dict[tuple[str, str], LearnedProfile] = {}
    raw_profiles = payload.get("profiles")
    if isinstance(raw_profiles, dict):
        for key, raw_profile in raw_profiles.items():
            parts = str(key).split("|", 1)
            if len(parts) != 2 or not isinstance(raw_profile, dict):
                continue
            symbol, timeframe = parts[0].upper(), parts[1]
            profile = _profile_from_dict(raw_profile)
            if profile is not None:
                profiles[(symbol, timeframe)] = profile

    per_timeframe: dict[str, LearnedProfile] = {}
    raw_per_timeframe = payload.get("per_timeframe")
    if isinstance(raw_per_timeframe, dict):
        for timeframe, raw_profile in raw_per_timeframe.items():
            if not isinstance(raw_profile, dict):
                continue
            profile = _profile_from_dict(raw_profile)
            if profile is not None:
                per_timeframe[str(timeframe)] = profile

    return TimeframeLearning(
        generated_at=str(payload.get("generated_at", "")),
        profiles=profiles,
        per_timeframe=per_timeframe,
    )


def merge_timeframe_learning(
    live: TimeframeLearning,
    baseline: TimeframeLearning,
    min_live_evaluated: int = BASELINE_MIN_LIVE_EVALUATED,
) -> TimeframeLearning:
    """ライブ学習が薄いセルだけ、履歴ベースラインで補う。

    live.evaluated が十分に貯まったセルは live を優先する。履歴側は初期学習の
    安定化にだけ使い、現行運用の実績が揃ったら自然に外れる設計。
    """
    profiles = dict(live.profiles)
    for key, baseline_profile in baseline.profiles.items():
        live_profile = live.profiles.get(key)
        if _should_use_baseline(live_profile, baseline_profile, min_live_evaluated):
            profiles[key] = _with_baseline_note(
                baseline_profile,
                live_evaluated=0 if live_profile is None else live_profile.evaluated,
                min_live_evaluated=min_live_evaluated,
            )

    per_timeframe = dict(live.per_timeframe)
    for timeframe, baseline_profile in baseline.per_timeframe.items():
        live_profile = live.per_timeframe.get(timeframe)
        if _should_use_baseline(live_profile, baseline_profile, min_live_evaluated):
            per_timeframe[timeframe] = _with_baseline_note(
                baseline_profile,
                live_evaluated=0 if live_profile is None else live_profile.evaluated,
                min_live_evaluated=min_live_evaluated,
            )

    return TimeframeLearning(
        generated_at=live.generated_at or baseline.generated_at,
        profiles=profiles,
        per_timeframe=per_timeframe,
    )


def _should_use_baseline(
    live_profile: LearnedProfile | None,
    baseline_profile: LearnedProfile,
    min_live_evaluated: int,
) -> bool:
    if baseline_profile.evaluated <= 0:
        return False
    return live_profile is None or live_profile.evaluated < min_live_evaluated


def _with_baseline_note(
    profile: LearnedProfile,
    *,
    live_evaluated: int,
    min_live_evaluated: int,
) -> LearnedProfile:
    clone = _profile_from_dict(_profile_to_dict(profile)) or LearnedProfile()
    note = (
        "履歴ベースラインを使用中"
        f" — ライブ採点{live_evaluated}/{min_live_evaluated}件"
        "が貯まるまでは過去チャート学習を優先"
    )
    clone.notes_ja = [note, *clone.notes_ja]
    return clone


def _profile_to_dict(profile: LearnedProfile) -> dict:
    """LearnedProfile を保存用の素の辞書にする(save_profile と同じ形)。"""
    return {
        "generated_at": profile.generated_at,
        "evaluated": profile.evaluated,
        "hits": profile.hits,
        "flat": profile.flat,
        "hit_rate": profile.hit_rate,
        "tech_weight": profile.tech_weight,
        "news_weight": profile.news_weight,
        "tech_hit_rate": profile.tech_hit_rate,
        "news_hit_rate": profile.news_hit_rate,
        "conviction_brier": profile.conviction_brier,
        "conviction_brier_base": profile.conviction_brier_base,
        "bins": [
            {"low": b.low, "high": b.high, "evaluated": b.evaluated, "hits": b.hits}
            for b in profile.bins
        ],
        "symbol_stats": profile.symbol_stats,
        "symbol_factors": profile.symbol_factors,
        "condition_stats": profile.condition_stats,
        "condition_factors": profile.condition_factors,
        "notes_ja": profile.notes_ja,
    }


def _profile_from_dict(payload: Mapping[str, object]) -> LearnedProfile | None:
    try:
        tech_weight = _float_value(payload.get("tech_weight"), TECH_WEIGHT)
        news_weight = _float_value(payload.get("news_weight"), NEWS_WEIGHT)
        if not (TECH_WEIGHT_MIN <= tech_weight <= TECH_WEIGHT_MAX):
            tech_weight, news_weight = TECH_WEIGHT, NEWS_WEIGHT

        bins: list[ConvictionBin] = []
        raw_bins = payload.get("bins")
        if isinstance(raw_bins, list):
            for row in raw_bins:
                if not isinstance(row, Mapping):
                    continue
                bins.append(
                    ConvictionBin(
                        low=_int_value(row.get("low")),
                        high=_int_value(row.get("high")),
                        evaluated=_int_value(row.get("evaluated")),
                        hits=_int_value(row.get("hits")),
                    )
                )

        symbol_factors: dict[str, float] = {}
        raw_symbol_factors = payload.get("symbol_factors")
        if isinstance(raw_symbol_factors, Mapping):
            for symbol, factor in raw_symbol_factors.items():
                value = _float_value(factor, 1.0)
                symbol_factors[str(symbol)] = max(SYMBOL_FACTOR_MIN, min(1.0, value))

        symbol_stats: dict[str, dict[str, int]] = {}
        raw_symbol_stats = payload.get("symbol_stats")
        if isinstance(raw_symbol_stats, Mapping):
            for symbol, stat in raw_symbol_stats.items():
                if not isinstance(stat, Mapping):
                    continue
                symbol_stats[str(symbol)] = {
                    "evaluated": _int_value(stat.get("evaluated")),
                    "hits": _int_value(stat.get("hits")),
                }

        condition_stats: dict[str, dict[str, dict[str, dict[str, int]]]] = {}
        raw_condition_stats = payload.get("condition_stats")
        if isinstance(raw_condition_stats, Mapping):
            for key, buckets in raw_condition_stats.items():
                if not isinstance(buckets, Mapping):
                    continue
                bucket_stats: dict[str, dict[str, dict[str, int]]] = {}
                for label, directions in buckets.items():
                    if not isinstance(directions, Mapping):
                        continue
                    direction_stats: dict[str, dict[str, int]] = {}
                    for direction, cell in directions.items():
                        if direction not in ("long", "short") or not isinstance(cell, Mapping):
                            continue
                        direction_stats[str(direction)] = {
                            "evaluated": _int_value(cell.get("evaluated")),
                            "hits": _int_value(cell.get("hits")),
                        }
                    bucket_stats[str(label)] = direction_stats
                condition_stats[str(key)] = bucket_stats

        condition_factors: dict[str, dict[str, dict[str, float]]] = {}
        raw_condition_factors = payload.get("condition_factors")
        if isinstance(raw_condition_factors, Mapping):
            for key, buckets in raw_condition_factors.items():
                if not isinstance(buckets, Mapping):
                    continue
                bucket_factors: dict[str, dict[str, float]] = {}
                for label, directions in buckets.items():
                    if not isinstance(directions, Mapping):
                        continue
                    direction_factors: dict[str, float] = {}
                    for direction, factor in directions.items():
                        if direction not in ("long", "short"):
                            continue
                        value = _float_value(factor, 1.0)
                        direction_factors[str(direction)] = max(
                            CONDITION_FACTOR_MIN, min(1.0, value)
                        )
                    bucket_factors[str(label)] = direction_factors
                condition_factors[str(key)] = bucket_factors

        raw_notes = payload.get("notes_ja")
        notes = [str(note) for note in raw_notes] if isinstance(raw_notes, list) else []

        return LearnedProfile(
            generated_at=str(payload.get("generated_at", "")),
            evaluated=_int_value(payload.get("evaluated")),
            hits=_int_value(payload.get("hits")),
            flat=_int_value(payload.get("flat")),
            tech_weight=tech_weight,
            news_weight=news_weight,
            tech_hit_rate=_optional_float(payload.get("tech_hit_rate")),
            news_hit_rate=_optional_float(payload.get("news_hit_rate")),
            conviction_brier=_optional_float(payload.get("conviction_brier")),
            conviction_brier_base=_optional_float(payload.get("conviction_brier_base")),
            bins=bins,
            symbol_stats=symbol_stats,
            symbol_factors=symbol_factors,
            condition_stats=condition_stats,
            condition_factors=condition_factors,
            notes_ja=notes,
        )
    except (TypeError, ValueError):
        return None


def _float_value(value: object, default: float) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float, str)):
        return float(value)
    return default


def _optional_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, str)):
        return float(value)
    return None


def _int_value(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float, str)):
        return int(value)
    return default

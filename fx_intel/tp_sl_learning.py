"""TP/SL first-touch learning for the chart-analysis MVP.

The older learning loop answers "was the direction right?".  This module uses
the trade plan itself: a long/short call is a hit only when TP1/TP2 is touched
before SL, a miss only when SL is touched first, and unresolved paths are kept
out of the hit-rate denominator.  The result is a non-blocking confidence
adjuster for the per-timeframe briefing path.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, UTC
import json
from pathlib import Path

from .timeframe import PRIMARY_HORIZON_HOURS, tolerance_for
from .trade_outcome import TradeOutcome, evaluate_trade_outcomes, json_safe

MVP_SYMBOLS: tuple[str, ...] = ("USDJPY", "EURUSD", "GBPUSD")
MVP_TIMEFRAMES: tuple[str, ...] = ("15m", "1h", "4h")

MIN_ADJUSTMENT_SAMPLES = 30
FULL_ADJUSTMENT_SAMPLES = 100
FACTOR_MIN = 0.70
FACTOR_MAX = 1.10
WEAK_HIT_RATE = 0.45
STRONG_HIT_RATE = 0.60
CONFIDENCE_BINS = ((0, 25), (25, 50), (50, 75), (75, 101))


@dataclass(frozen=True)
class TpSlCall:
    """One scored long/short decision under the TP/SL first-touch rule."""

    symbol: str
    timeframe: str
    direction: str
    conviction: int
    outcome: str  # hit / miss / unresolved / unscored
    ts: str
    first_touch: str = "none"
    realized_r: float | None = None
    path_quality: float = 0.0


@dataclass(frozen=True)
class TpSlBin:
    low: int
    high: int
    evaluated: int
    hits: int

    @property
    def hit_rate(self) -> float | None:
        return self.hits / self.evaluated if self.evaluated else None

    def to_dict(self) -> dict[str, object]:
        return {
            "low": self.low,
            "high": self.high,
            "evaluated": self.evaluated,
            "hits": self.hits,
            "hit_rate": self.hit_rate,
        }


@dataclass(frozen=True)
class TpSlStats:
    evaluated: int = 0
    hits: int = 0
    unresolved: int = 0
    unscored: int = 0

    @property
    def hit_rate(self) -> float | None:
        return self.hits / self.evaluated if self.evaluated else None

    @property
    def factor(self) -> float:
        return confidence_factor(self.evaluated, self.hits)

    def to_dict(self) -> dict[str, object]:
        return {
            "evaluated": self.evaluated,
            "hits": self.hits,
            "hit_rate": self.hit_rate,
            "unresolved": self.unresolved,
            "unscored": self.unscored,
            "factor": self.factor,
        }


@dataclass
class TpSlProfile:
    """Confidence-calibration profile derived from TP/SL first-touch results."""

    generated_at: str = ""
    evaluated: int = 0
    hits: int = 0
    unresolved: int = 0
    unscored: int = 0
    brier: float | None = None
    adjusted_brier: float | None = None
    brier_base: float | None = None
    bins: list[TpSlBin] = field(default_factory=list)
    by_direction: dict[str, TpSlStats] = field(default_factory=dict)
    notes_ja: list[str] = field(default_factory=list)

    @property
    def hit_rate(self) -> float | None:
        return self.hits / self.evaluated if self.evaluated else None

    @property
    def factor(self) -> float:
        return confidence_factor(self.evaluated, self.hits)

    def stats_for_direction(self, direction: str) -> TpSlStats:
        stats = self.by_direction.get(direction)
        if stats is not None and stats.evaluated >= MIN_ADJUSTMENT_SAMPLES:
            return stats
        return TpSlStats(
            evaluated=self.evaluated,
            hits=self.hits,
            unresolved=self.unresolved,
            unscored=self.unscored,
        )

    def adjustment(self, direction: str) -> tuple[float, str]:
        """Return a non-blocking confidence factor and Japanese reason."""

        if direction not in ("long", "short"):
            return 1.0, ""
        stats = self.stats_for_direction(direction)
        factor = stats.factor
        if factor == 1.0:
            return 1.0, ""
        label = "ロング" if direction == "long" else "ショート"
        rate = stats.hit_rate or 0.0
        reason = (
            f"TP/SL先着の過去成績: {label} {rate:.0%}(n={stats.evaluated})"
            f" → 確信度を×{factor:.2f}に補正"
        )
        return factor, reason

    def summary_ja(self) -> str:
        if not self.notes_ja:
            return (
                "TP/SL学習データ蓄積中 — TP/SL先着で採点できる過去判断がまだありません"
                f"(補正は{MIN_ADJUSTMENT_SAMPLES}件から)"
            )
        return "\n".join(self.notes_ja)

    def to_dict(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at,
            "evaluated": self.evaluated,
            "hits": self.hits,
            "hit_rate": self.hit_rate,
            "unresolved": self.unresolved,
            "unscored": self.unscored,
            "factor": self.factor,
            "brier": self.brier,
            "adjusted_brier": self.adjusted_brier,
            "brier_base": self.brier_base,
            "bins": [item.to_dict() for item in self.bins],
            "by_direction": {key: stats.to_dict() for key, stats in self.by_direction.items()},
            "notes_ja": list(self.notes_ja),
        }


@dataclass
class TimeframeTpSlLearning:
    """TP/SL learning snapshot keyed by (symbol, timeframe)."""

    generated_at: str = ""
    profiles: dict[tuple[str, str], TpSlProfile] = field(default_factory=dict)
    per_timeframe: dict[str, TpSlProfile] = field(default_factory=dict)

    def profile_for(self, symbol: str, timeframe: str) -> TpSlProfile:
        return self.profiles.get((symbol.upper(), timeframe), TpSlProfile())

    def expectancy_lookup(
        self, symbol: str, timeframe: str
    ) -> Callable[[str, str, int], tuple[float, str, bool]] | None:
        """Return a timeframe.ExpectancyAdjuster-compatible non-blocking hook."""

        profile = self.profiles.get((symbol.upper(), timeframe))
        if profile is None:
            return None

        def adjust(_symbol: str, direction: str, _conviction: int) -> tuple[float, str, bool]:
            factor, reason = profile.adjustment(direction)
            return factor, reason, False

        return adjust

    def summary_ja(self, timeframes: Sequence[str] = MVP_TIMEFRAMES) -> str:
        lines: list[str] = []
        for timeframe in timeframes:
            profile = self.per_timeframe.get(timeframe)
            if profile is None or (
                profile.evaluated == 0 and profile.unresolved == 0 and profile.unscored == 0
            ):
                continue
            lines.append(f"【{timeframe}】TP/SL先着学習\n{profile.summary_ja()}")
        if not lines:
            return (
                "TP/SL学習データ蓄積中 — 15m/1h/4hでTP/SL先着採点できる" "過去判断がまだありません"
            )
        return "\n\n".join(lines)


def confidence_factor(evaluated: int, hits: int) -> float:
    """Shrink confidence toward TP/SL hit-rate evidence.

    Below MIN_ADJUSTMENT_SAMPLES the factor stays neutral.  At 100 samples the
    evidence is fully reflected, bounded to avoid overfitting-driven swings.
    """

    if evaluated < MIN_ADJUSTMENT_SAMPLES or evaluated <= 0:
        return 1.0
    hit_rate = hits / evaluated
    if WEAK_HIT_RATE <= hit_rate < STRONG_HIT_RATE:
        return 1.0
    if hit_rate < WEAK_HIT_RATE:
        raw = max(FACTOR_MIN, hit_rate / 0.5)
    else:
        raw = min(FACTOR_MAX, 1.0 + (hit_rate - 0.5) * 0.5)
    shrink = min(1.0, evaluated / FULL_ADJUSTMENT_SAMPLES)
    return round(1.0 + shrink * (raw - 1.0), 2)


def evaluate_timeframe_tp_sl_calls(
    entries: Iterable[Mapping[str, object]],
    timeframe: str,
    *,
    symbols: Sequence[str] = MVP_SYMBOLS,
) -> list[TpSlCall]:
    """Score one timeframe with TP/SL first-touch as the primary outcome."""

    allowed = {symbol.upper() for symbol in symbols}
    filtered = [
        entry
        for entry in entries
        if str(entry.get("timeframe", "")) == timeframe
        and str(entry.get("symbol", "")).upper() in allowed
    ]
    horizon = PRIMARY_HORIZON_HOURS.get(timeframe, 24.0)
    outcomes = evaluate_trade_outcomes(
        filtered,
        horizon_hours=horizon,
        tolerance_hours=tolerance_for(horizon),
    )
    return [_call_from_outcome(outcome, timeframe) for outcome in outcomes]


def derive_timeframe_tp_sl_learning(
    entries: Iterable[Mapping[str, object]],
    *,
    now: datetime | None = None,
    symbols: Sequence[str] = MVP_SYMBOLS,
    timeframes: Sequence[str] = MVP_TIMEFRAMES,
) -> TimeframeTpSlLearning:
    """Build TP/SL confidence profiles for the MVP symbol/timeframe cells."""

    now = now or datetime.now(UTC)
    materialized = list(entries)
    profiles: dict[tuple[str, str], TpSlProfile] = {}
    per_timeframe: dict[str, TpSlProfile] = {}

    for timeframe in timeframes:
        calls = evaluate_timeframe_tp_sl_calls(materialized, timeframe, symbols=symbols)
        if not calls:
            continue
        per_timeframe[timeframe] = derive_tp_sl_profile(calls, now=now)
        for symbol in {call.symbol for call in calls}:
            cell_calls = [call for call in calls if call.symbol == symbol]
            profiles[(symbol, timeframe)] = derive_tp_sl_profile(cell_calls, now=now)

    return TimeframeTpSlLearning(
        generated_at=now.isoformat(),
        profiles=profiles,
        per_timeframe=per_timeframe,
    )


def derive_tp_sl_profile(
    calls: Sequence[TpSlCall],
    *,
    now: datetime | None = None,
) -> TpSlProfile:
    now = now or datetime.now(UTC)
    scored = [call for call in calls if call.outcome in ("hit", "miss")]
    hits = sum(1 for call in scored if call.outcome == "hit")
    unresolved = sum(1 for call in calls if call.outcome == "unresolved")
    unscored = sum(1 for call in calls if call.outcome == "unscored")

    by_direction: dict[str, TpSlStats] = {}
    for direction in ("long", "short"):
        direction_calls = [call for call in calls if call.direction == direction]
        direction_scored = [call for call in direction_calls if call.outcome in ("hit", "miss")]
        by_direction[direction] = TpSlStats(
            evaluated=len(direction_scored),
            hits=sum(1 for call in direction_scored if call.outcome == "hit"),
            unresolved=sum(1 for call in direction_calls if call.outcome == "unresolved"),
            unscored=sum(1 for call in direction_calls if call.outcome == "unscored"),
        )

    brier = adjusted_brier = brier_base = None
    if scored:
        overall_rate = hits / len(scored)
        brier = round(_brier(scored, lambda call: call.conviction / 100.0), 4)
        adjusted_brier = round(
            _brier(
                scored,
                lambda call: _adjusted_probability(
                    call.conviction,
                    _direction_factor(by_direction, call.direction, len(scored), hits),
                ),
            ),
            4,
        )
        brier_base = round(_brier(scored, lambda _call: overall_rate), 4)

    profile = TpSlProfile(
        generated_at=now.isoformat(),
        evaluated=len(scored),
        hits=hits,
        unresolved=unresolved,
        unscored=unscored,
        brier=brier,
        adjusted_brier=adjusted_brier,
        brier_base=brier_base,
        bins=_bins(scored),
        by_direction=by_direction,
    )
    profile.notes_ja = _notes_ja(profile)
    return profile


def save_timeframe_tp_sl_learning(learning: TimeframeTpSlLearning, path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": learning.generated_at,
        "profiles": {
            f"{symbol}|{timeframe}": profile.to_dict()
            for (symbol, timeframe), profile in learning.profiles.items()
        },
        "per_timeframe": {
            timeframe: profile.to_dict() for timeframe, profile in learning.per_timeframe.items()
        },
    }
    target.write_text(
        json.dumps(json_safe(payload), ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )


def _call_from_outcome(outcome: TradeOutcome, timeframe: str) -> TpSlCall:
    if not outcome.tradable:
        result = "unscored"
    elif outcome.first_touch in ("tp1", "tp2"):
        result = "hit"
    elif outcome.first_touch == "sl":
        result = "miss"
    else:
        result = "unresolved"
    return TpSlCall(
        symbol=outcome.symbol.upper(),
        timeframe=timeframe,
        direction=outcome.direction,
        conviction=outcome.conviction,
        outcome=result,
        ts=outcome.ts,
        first_touch=outcome.first_touch,
        realized_r=outcome.realized_r,
        path_quality=outcome.path_quality,
    )


def _bins(scored: Sequence[TpSlCall]) -> list[TpSlBin]:
    output: list[TpSlBin] = []
    for low, high in CONFIDENCE_BINS:
        in_bin = [call for call in scored if low <= call.conviction < high]
        output.append(
            TpSlBin(
                low=low,
                high=high,
                evaluated=len(in_bin),
                hits=sum(1 for call in in_bin if call.outcome == "hit"),
            )
        )
    return output


def _direction_factor(
    by_direction: Mapping[str, TpSlStats],
    direction: str,
    overall_evaluated: int,
    overall_hits: int,
) -> float:
    stats = by_direction.get(direction)
    if stats is not None and stats.evaluated >= MIN_ADJUSTMENT_SAMPLES:
        return stats.factor
    return confidence_factor(overall_evaluated, overall_hits)


def _adjusted_probability(conviction: int, factor: float) -> float:
    return max(0.0, min(1.0, conviction / 100.0 * factor))


def _brier(calls: Sequence[TpSlCall], probability_of: Callable[[TpSlCall], float]) -> float:
    return sum(
        (float(probability_of(call)) - (1.0 if call.outcome == "hit" else 0.0)) ** 2
        for call in calls
    ) / len(calls)


def _notes_ja(profile: TpSlProfile) -> list[str]:
    if profile.evaluated == 0 and profile.unresolved == 0 and profile.unscored == 0:
        return []
    notes: list[str] = []
    if profile.evaluated:
        line = (
            f"TP/SL先着で採点 — {profile.evaluated}件中{profile.hits}件正解"
            f" / 勝率 {profile.hit_rate:.0%}"
        )
        extras = []
        if profile.unresolved:
            extras.append(f"未決着{profile.unresolved}件")
        if profile.unscored:
            extras.append(f"低品質/未採点{profile.unscored}件")
        if extras:
            line += " (" + "、".join(extras) + ")"
        notes.append(line)
    elif profile.unresolved or profile.unscored:
        notes.append(
            f"TP/SL先着の採点対象なし"
            f" (未決着{profile.unresolved}件、低品質/未採点{profile.unscored}件)"
        )

    bin_parts = [
        f"{item.low}-{item.high - 1}帯 {item.hit_rate:.0%}(n={item.evaluated})"
        for item in profile.bins
        if item.evaluated > 0
    ]
    if bin_parts:
        notes.append("TP/SL勝率の確信度帯: " + " / ".join(bin_parts))

    if (
        profile.brier is not None
        and profile.adjusted_brier is not None
        and profile.brier_base is not None
    ):
        notes.append(
            f"TP/SL確信度Brier: 補正前 {profile.brier:.3f}"
            f" / 補正後 {profile.adjusted_brier:.3f}"
            f" / 基準 {profile.brier_base:.3f}"
        )

    adjustments: list[str] = []
    for direction, stats in profile.by_direction.items():
        if stats.evaluated < MIN_ADJUSTMENT_SAMPLES or stats.factor == 1.0:
            continue
        label = "ロング" if direction == "long" else "ショート"
        adjustments.append(
            f"{label} {stats.hit_rate:.0%}(n={stats.evaluated}) → 確信度×{stats.factor:.2f}"
        )
    if adjustments:
        notes.append("TP/SL学習補正: " + " / ".join(adjustments))
    elif profile.evaluated < MIN_ADJUSTMENT_SAMPLES:
        notes.append(
            f"TP/SL補正はサンプル{MIN_ADJUSTMENT_SAMPLES}件から" f"(現在{profile.evaluated}件)"
        )
    return notes

"""ホライズン予測の満期採点と(symbol×horizon)セル学習(設計A §4-5)。

採点はステートレス再計算(現行流儀): ジャーナル全履歴+5分価格系列から毎回導く。
1予測につき記録するもの:
- 実現クラス up/down/flat — 閾値は行に記録された max(ATR_h×0.1, spread×2)
- 方向judgmentの hit/miss(方向を張った行のみ)
- 3クラスBrier(較正品質)とクライマトロジー(セル基底率)Brierの比較
- 価格帯: p10-p90包含(カバレッジ)と pinball loss(p10/p50/p90)
- コスト控除後R: (符号付き実現move − スプレッド) / ATR_h (方向行のみ)

学習(セル単位):
- ホライズン別間引き(horizons.HorizonSpec.learn_thin_gap_hours)後に集計
- n>=50 で複合スコアビン別の3クラス較正テーブルを作る(それまで較正なし)
- 経験分位点帯: (symbol,horizon,volバケット,セッション) n>=40 → バケット、
  n>=20 → ホライズン全体、それ未満は無し(ATR既定帯のまま)という縮退цепь
"""

from __future__ import annotations

import json
import math
import tempfile
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, UTC
from pathlib import Path

from .horizon_journal import is_pit_eligible_horizon_entry
from .horizons import (
    HORIZON_BY_LABEL,
    HORIZON_SPECS,
    HorizonSpec,
)
from .market import WEEKEND_CLOSURE, open_hours_between

SCHEMA_VERSION = 1
CALIBRATION_MIN_SAMPLES = 50
BAND_BUCKET_MIN_SAMPLES = 40
BAND_HORIZON_MIN_SAMPLES = 20
COMPOSITE_BINS = ((-1.0, -0.5), (-0.5, -0.15), (-0.15, 0.15), (0.15, 0.5), (0.5, 1.0001))


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    return None


@dataclass
class ScoredHorizonForecast:
    """満期採点済みの1予測。"""

    symbol: str
    horizon: str
    ts: datetime
    direction: str
    composite: float
    move: float  # 実現move(将来close - 記録時close)
    realized_class: str  # up / down / flat
    direction_outcome: str  # hit / miss / flat / none(方向なし行)
    brier: float | None
    pinball_p10: float | None
    pinball_p50: float | None
    pinball_p90: float | None
    band_covered: bool | None
    net_r: float | None  # コスト控除後R(方向行のみ)
    vol_bucket: str
    session: str
    shadow_only: bool


@dataclass
class HorizonScoreResult:
    scored: list[ScoredHorizonForecast] = field(default_factory=list)
    immature: int = 0
    unresolved: int = 0
    pit_ineligible: int = 0


def _pinball(move: float, predicted: float, quantile: float) -> float:
    diff = move - predicted
    return quantile * diff if diff >= 0 else (quantile - 1.0) * diff


def _price_series(
    price_rows: Iterable[Mapping[str, object]],
) -> dict[str, list[tuple[datetime, float]]]:
    """symbol別の将来価格系列。15m行を優先し、無いsymbolは全時間足行で代替する。"""
    preferred: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    fallback: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    for row in price_rows:
        ts = _parse_ts(row.get("ts"))
        close = _number(row.get("close"))
        symbol = row.get("symbol")
        if ts is None or close is None or not isinstance(symbol, str):
            continue
        bucket = preferred if row.get("timeframe") == "15m" else fallback
        bucket[symbol].append((ts, close))
    series: dict[str, list[tuple[datetime, float]]] = {}
    for symbol in set(preferred) | set(fallback):
        points = preferred.get(symbol) or fallback.get(symbol) or []
        points.sort(key=lambda point: point[0])
        series[symbol] = points
    return series


def score_horizon_history(
    entries: Iterable[Mapping[str, object]],
    price_rows: Iterable[Mapping[str, object]],
    now: datetime | None = None,
    *,
    require_pit: bool = True,
) -> HorizonScoreResult:
    """ジャーナル全行を満期採点する。"""
    now = now or datetime.now(UTC)
    series = _price_series(price_rows)
    times = {symbol: [point[0] for point in points] for symbol, points in series.items()}
    result = HorizonScoreResult()

    for entry in entries:
        if require_pit and not is_pit_eligible_horizon_entry(dict(entry)):
            result.pit_ineligible += 1
            continue
        spec = HORIZON_BY_LABEL.get(str(entry.get("horizon")))
        ts = _parse_ts(entry.get("ts"))
        close = _number(entry.get("close"))
        if spec is None or ts is None or close is None:
            result.unresolved += 1
            continue
        age = open_hours_between(ts, now)
        if age < spec.hours + spec.tolerance_hours:
            result.immature += 1
            continue
        symbol = str(entry.get("symbol", ""))
        points = series.get(symbol, [])
        stamp_list = times.get(symbol, [])
        window_lower = ts + timedelta(hours=spec.hours - spec.tolerance_hours)
        window_upper = ts + timedelta(hours=spec.hours + spec.tolerance_hours) + WEEKEND_CLOSURE
        best: tuple[float, float] | None = None
        for index in range(bisect_left(stamp_list, window_lower), len(points)):
            point_ts, point_close = points[index]
            if point_ts > window_upper:
                break
            point_age = open_hours_between(ts, point_ts)
            if not (
                spec.hours - spec.tolerance_hours <= point_age <= spec.hours + spec.tolerance_hours
            ):
                continue
            gap = abs(point_age - spec.hours)
            if best is None or gap < best[0]:
                best = (gap, point_close)
        if best is None:
            result.unresolved += 1
            continue

        move = best[1] - close
        threshold = _number(entry.get("flat_threshold")) or 0.0
        if move > threshold:
            realized = "up"
        elif move < -threshold:
            realized = "down"
        else:
            realized = "flat"

        direction = str(entry.get("direction", ""))
        if direction in ("long", "short"):
            wanted = "up" if direction == "long" else "down"
            if realized == "flat":
                direction_outcome = "flat"
            else:
                direction_outcome = "hit" if realized == wanted else "miss"
        else:
            direction_outcome = "none"

        p_up = _number(entry.get("p_up"))
        p_down = _number(entry.get("p_down"))
        p_flat = _number(entry.get("p_flat"))
        brier = None
        if None not in (p_up, p_down, p_flat):
            assert p_up is not None and p_down is not None and p_flat is not None
            truth = {"up": 0.0, "down": 0.0, "flat": 0.0}
            truth[realized] = 1.0
            brier = round(
                (p_up - truth["up"]) ** 2
                + (p_down - truth["down"]) ** 2
                + (p_flat - truth["flat"]) ** 2,
                6,
            )

        band_p10 = _number(entry.get("band_p10"))
        band_p50 = _number(entry.get("band_p50"))
        band_p90 = _number(entry.get("band_p90"))
        pin10 = _pinball(move, band_p10, 0.10) if band_p10 is not None else None
        pin50 = _pinball(move, band_p50, 0.50) if band_p50 is not None else None
        pin90 = _pinball(move, band_p90, 0.90) if band_p90 is not None else None
        covered = (
            band_p10 <= move <= band_p90 if band_p10 is not None and band_p90 is not None else None
        )

        net_r = None
        atr_h = _number(entry.get("atr_h"))
        if direction in ("long", "short") and atr_h is not None and atr_h > 0:
            signed = move if direction == "long" else -move
            cost = _number(entry.get("spread")) or 0.0
            net_r = round((signed - cost) / atr_h, 4)

        raw_features = entry.get("features")
        features: Mapping[str, object] = raw_features if isinstance(raw_features, dict) else {}
        result.scored.append(
            ScoredHorizonForecast(
                symbol=symbol,
                horizon=spec.label,
                ts=ts,
                direction=direction,
                composite=_number(entry.get("composite")) or 0.0,
                move=round(move, 6),
                realized_class=realized,
                direction_outcome=direction_outcome,
                brier=brier,
                pinball_p10=round(pin10, 6) if pin10 is not None else None,
                pinball_p50=round(pin50, 6) if pin50 is not None else None,
                pinball_p90=round(pin90, 6) if pin90 is not None else None,
                band_covered=covered,
                net_r=net_r,
                vol_bucket=str(features.get("vol_bucket", "mid")),
                session=str(features.get("session", "off")),
                shadow_only=bool(entry.get("shadow_only")),
            )
        )
    return result


def thin_scored(
    scored: Sequence[ScoredHorizonForecast], gap_hours: float
) -> list[ScoredHorizonForecast]:
    """同一(symbol,horizon)でgap_hours未満の間隔の予測を間引く(評価窓重複対策)。"""
    ordered = sorted(scored, key=lambda item: (item.symbol, item.horizon, item.ts))
    kept: list[ScoredHorizonForecast] = []
    last: dict[tuple[str, str], datetime] = {}
    for item in ordered:
        key = (item.symbol, item.horizon)
        previous = last.get(key)
        if previous is not None and (item.ts - previous) < timedelta(hours=gap_hours):
            continue
        last[key] = item.ts
        kept.append(item)
    return kept


def _composite_bin(composite: float) -> int:
    for index, (low, high) in enumerate(COMPOSITE_BINS):
        if low <= composite < high:
            return index
    return len(COMPOSITE_BINS) // 2


@dataclass
class HorizonCellProfile:
    """1 (symbol, horizon) セルの学習状態。"""

    symbol: str
    horizon: str
    n_scored: int = 0
    n_directional: int = 0
    hits: int = 0
    misses: int = 0
    class_counts: dict[str, int] = field(default_factory=dict)
    mean_brier: float | None = None
    climatology_brier: float | None = None
    band_coverage: float | None = None
    mean_pinball_p50: float | None = None
    mean_net_r: float | None = None
    calibration: list[dict] = field(default_factory=list)  # binごとの実現クラス頻度

    @property
    def hit_rate(self) -> float | None:
        scored = self.hits + self.misses
        return self.hits / scored if scored else None


def derive_horizon_learning(
    result: HorizonScoreResult,
    now: datetime | None = None,
    *,
    specs: Sequence[HorizonSpec] = HORIZON_SPECS,
) -> dict:
    """採点結果からセル学習状態と経験分位点帯を導く(JSON保存可能な形)。"""
    now = now or datetime.now(UTC)
    spec_by_label = {spec.label: spec for spec in specs}
    by_cell: dict[tuple[str, str], list[ScoredHorizonForecast]] = defaultdict(list)
    for item in result.scored:
        by_cell[(item.symbol, item.horizon)].append(item)

    profiles: dict[str, dict] = {}
    bands: dict[str, dict] = {}
    for (symbol, horizon), items in sorted(by_cell.items()):
        spec = spec_by_label.get(horizon)
        if spec is None:
            continue
        thinned = thin_scored(items, spec.learn_thin_gap_hours)
        profile = HorizonCellProfile(symbol=symbol, horizon=horizon, n_scored=len(thinned))
        class_counts: dict[str, int] = {"up": 0, "down": 0, "flat": 0}
        briers: list[float] = []
        pin50s: list[float] = []
        covered_flags: list[bool] = []
        net_rs: list[float] = []
        bins: dict[int, dict[str, int]] = defaultdict(lambda: {"up": 0, "down": 0, "flat": 0})
        for item in thinned:
            class_counts[item.realized_class] += 1
            if item.direction_outcome == "hit":
                profile.hits += 1
                profile.n_directional += 1
            elif item.direction_outcome == "miss":
                profile.misses += 1
                profile.n_directional += 1
            elif item.direction_outcome == "flat":
                profile.n_directional += 1
            if item.brier is not None:
                briers.append(item.brier)
            if item.pinball_p50 is not None:
                pin50s.append(item.pinball_p50)
            if item.band_covered is not None:
                covered_flags.append(item.band_covered)
            if item.net_r is not None:
                net_rs.append(item.net_r)
            bins[_composite_bin(item.composite)][item.realized_class] += 1
        profile.class_counts = class_counts
        total = sum(class_counts.values())
        if briers:
            profile.mean_brier = round(sum(briers) / len(briers), 6)
        if total:
            base = {cls: count / total for cls, count in class_counts.items()}
            # クライマトロジー(基底率をそのまま予測に使った場合)の期待Brier
            profile.climatology_brier = round(
                sum(
                    base[cls]
                    * sum((base[other] - (1.0 if other == cls else 0.0)) ** 2 for other in base)
                    for cls in base
                ),
                6,
            )
        if covered_flags:
            profile.band_coverage = round(sum(covered_flags) / len(covered_flags), 4)
        if pin50s:
            profile.mean_pinball_p50 = round(sum(pin50s) / len(pin50s), 6)
        if net_rs:
            profile.mean_net_r = round(sum(net_rs) / len(net_rs), 4)
        if profile.n_scored >= CALIBRATION_MIN_SAMPLES:
            calibration = []
            for index, (low, high) in enumerate(COMPOSITE_BINS):
                counts = bins.get(index, {"up": 0, "down": 0, "flat": 0})
                bin_total = sum(counts.values())
                calibration.append(
                    {
                        "bin": [low, high],
                        "n": bin_total,
                        "p_up": round(counts["up"] / bin_total, 4) if bin_total else None,
                        "p_down": round(counts["down"] / bin_total, 4) if bin_total else None,
                        "p_flat": round(counts["flat"] / bin_total, 4) if bin_total else None,
                    }
                )
            profile.calibration = calibration

        profiles[f"{symbol}|{horizon}"] = {
            "symbol": profile.symbol,
            "horizon": profile.horizon,
            "n_scored": profile.n_scored,
            "n_directional": profile.n_directional,
            "hits": profile.hits,
            "misses": profile.misses,
            "hit_rate": round(profile.hit_rate, 4) if profile.hit_rate is not None else None,
            "class_counts": profile.class_counts,
            "mean_brier": profile.mean_brier,
            "climatology_brier": profile.climatology_brier,
            "band_coverage": profile.band_coverage,
            "mean_pinball_p50": profile.mean_pinball_p50,
            "mean_net_r": profile.mean_net_r,
            "calibrated": bool(profile.calibration),
            "calibration": profile.calibration,
        }

        # 経験分位点帯(縮退chain: バケット→ホライズン全体→なし)
        moves_by_bucket: dict[tuple[str, str], list[float]] = defaultdict(list)
        for item in items:  # 帯は間引き前の全実現moveで作る(分布推定なので独立性要件が緩い)
            moves_by_bucket[(item.vol_bucket, item.session)].append(item.move)
        all_moves = sorted(item.move for item in items)
        cell_bands: dict[str, dict] = {}
        for (bucket, session), moves in moves_by_bucket.items():
            if len(moves) >= BAND_BUCKET_MIN_SAMPLES:
                cell_bands[f"{bucket}|{session}"] = _quantile_band(moves, "vol_session")
        if len(all_moves) >= BAND_HORIZON_MIN_SAMPLES:
            cell_bands["__horizon__"] = _quantile_band(all_moves, "horizon_all")
        if cell_bands:
            bands[f"{symbol}|{horizon}"] = cell_bands

    return {
        "schema": SCHEMA_VERSION,
        "generated_at": now.isoformat(),
        "scored_total": len(result.scored),
        "immature": result.immature,
        "unresolved": result.unresolved,
        "pit_ineligible": result.pit_ineligible,
        "profiles": profiles,
        "bands": bands,
    }


def _quantile_band(moves: Sequence[float], source: str) -> dict:
    ordered = sorted(moves)

    def q(fraction: float) -> float:
        if not ordered:
            return 0.0
        position = fraction * (len(ordered) - 1)
        lower = int(math.floor(position))
        upper = min(lower + 1, len(ordered) - 1)
        weight = position - lower
        return ordered[lower] * (1 - weight) + ordered[upper] * weight

    return {
        "n": len(ordered),
        "p10": round(q(0.10), 6),
        "p50": round(q(0.50), 6),
        "p90": round(q(0.90), 6),
        "source": source,
    }


def save_horizon_learning(state: Mapping[str, object], path: str | Path) -> None:
    """学習状態をatomicに保存する(部分書込みでダッシュボードを壊さない)。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=target.parent, delete=False, suffix=".tmp"
    ) as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temp_path = Path(handle.name)
    temp_path.replace(target)


def load_horizon_learning(path: str | Path) -> dict | None:
    try:
        with Path(path).open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return (
        payload if isinstance(payload, dict) and payload.get("schema") == SCHEMA_VERSION else None
    )


def make_band_provider(state: Mapping[str, object] | None):
    """学習状態から horizon_forecast.band_provider を作る(縮退chain込み)。"""

    def provider(
        symbol: str, horizon: str, bucket: str, session: str
    ) -> tuple[float, float, float, str] | None:
        if not isinstance(state, Mapping):
            return None
        bands = state.get("bands")
        if not isinstance(bands, Mapping):
            return None
        cell = bands.get(f"{symbol}|{horizon}")
        if not isinstance(cell, Mapping):
            return None
        band = cell.get(f"{bucket}|{session}") or cell.get("__horizon__")
        if not isinstance(band, Mapping):
            return None
        p10 = band.get("p10")
        p50 = band.get("p50")
        p90 = band.get("p90")
        if not all(isinstance(value, (int, float)) for value in (p10, p50, p90)):
            return None
        return float(p10), float(p50), float(p90), str(band.get("source", "learned"))  # type: ignore[arg-type]

    return provider


def make_calibration_provider(state: Mapping[str, object] | None):
    """学習状態から3クラス較正providerを作る(n>=50セルのみ較正済み扱い)。"""

    def provider(symbol: str, horizon: str, composite: float) -> tuple[float, float, float] | None:
        if not isinstance(state, Mapping):
            return None
        profiles = state.get("profiles")
        if not isinstance(profiles, Mapping):
            return None
        profile = profiles.get(f"{symbol}|{horizon}")
        if not isinstance(profile, Mapping) or not profile.get("calibrated"):
            return None
        calibration = profile.get("calibration")
        if not isinstance(calibration, list):
            return None
        index = _composite_bin(composite)
        if index >= len(calibration):
            return None
        cell = calibration[index]
        if not isinstance(cell, Mapping) or not cell.get("n"):
            return None
        p_up = cell.get("p_up")
        p_down = cell.get("p_down")
        p_flat = cell.get("p_flat")
        if not all(isinstance(value, (int, float)) for value in (p_up, p_down, p_flat)):
            return None
        return float(p_up), float(p_down), float(p_flat)  # type: ignore[arg-type]

    return provider

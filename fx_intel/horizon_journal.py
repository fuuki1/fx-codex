"""ホライズン予測ジャーナル(horizon-pit-v1契約、追記専用)。

1行 = 1 (symbol, horizon, 実行サイクル)。既存のジャーナル群には一切触れない
新設ファイルで、フラグOFF(=append呼び出し停止)だけで完全にロールバックできる。

PIT契約: source_cutoff <= max_feature_available_time <= prediction_time を
書込み時に検証し、違反はHorizonPointInTimeErrorで拒否する(fail-closed)。
3つの来歴が揃わない行はそもそも書かない(旧形式行を作らない=fusionの教訓)。
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from datetime import datetime, UTC
from pathlib import Path

from .horizon_forecast import HorizonForecast

HORIZON_PIT_CONTRACT = "horizon-pit-v1"
SCHEMA_VERSION = 1


class HorizonPointInTimeError(ValueError):
    """来歴時刻の欠落・naive・順序違反で予測行を書けない場合。"""


def _require_aware(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise HorizonPointInTimeError(f"{name} がdatetimeではありません")
    if value.tzinfo is None or value.utcoffset() is None:
        raise HorizonPointInTimeError(f"{name} はtimezone付きが必要です")
    return value.astimezone(UTC)


def append_horizon_forecasts(
    path: str | Path,
    forecasts: Sequence[HorizonForecast],
    *,
    prediction_time: datetime,
    source_cutoff: datetime,
    max_feature_available_time: datetime,
) -> int:
    """予測をJSONLへ追記する。戻り値は書いた行数。

    PIT順序違反は1行も書かずに例外を投げる(部分書込みで契約を汚さない)。
    """
    prediction_utc = _require_aware(prediction_time, "prediction_time")
    source_utc = _require_aware(source_cutoff, "source_cutoff")
    feature_utc = _require_aware(max_feature_available_time, "max_feature_available_time")
    if not source_utc <= feature_utc <= prediction_utc:
        raise HorizonPointInTimeError(
            "PIT順序違反: source_cutoff <= max_feature_available_time "
            "<= prediction_time を満たしません"
        )

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with target.open("a", encoding="utf-8") as handle:
        for forecast in forecasts:
            row = {
                "schema_version": SCHEMA_VERSION,
                "contract": HORIZON_PIT_CONTRACT,
                "ts": prediction_utc.isoformat(),
                "prediction_time": prediction_utc.isoformat(),
                "source_cutoff": source_utc.isoformat(),
                "max_feature_available_time": feature_utc.isoformat(),
                "pit_eligible": True,
                "symbol": forecast.symbol,
                "horizon": forecast.horizon,
                "horizon_hours": forecast.horizon_hours,
                "shadow_only": forecast.shadow_only,
                "direction": forecast.direction,
                "composite": forecast.composite,
                "conviction": forecast.conviction,
                "p_up": forecast.p_up,
                "p_down": forecast.p_down,
                "p_flat": forecast.p_flat,
                "calibrated": forecast.calibrated,
                "close": forecast.close,
                "atr_h": forecast.atr_h,
                "spread": forecast.spread,
                "flat_threshold": forecast.flat_threshold,
                "band_p10": forecast.band_p10,
                "band_p50": forecast.band_p50,
                "band_p90": forecast.band_p90,
                "band_source": forecast.band_source,
                "expected_range": forecast.expected_range,
                "data_quality": forecast.data_quality,
                "weights": forecast.weights,
                "features": forecast.features,
                "gates": forecast.gates,
                "warnings": forecast.warnings,
                "generator_version": forecast.generator_version,
            }
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            written += 1
    return written


def read_horizon_entries(path: str | Path) -> Iterator[dict]:
    """ジャーナルを1行ずつ読む。壊れた行は黙って飛ばす(採点側で件数を別途監査)。"""
    target = Path(path)
    if not target.exists():
        return
    with target.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


def is_pit_eligible_horizon_entry(entry: dict) -> bool:
    """horizon-pit-v1の来歴を証明できる行か(採点・学習の入場条件)。"""
    if entry.get("contract") != HORIZON_PIT_CONTRACT or entry.get("pit_eligible") is not True:
        return False

    def _aware(value: object) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(UTC)

    source = _aware(entry.get("source_cutoff"))
    feature = _aware(entry.get("max_feature_available_time"))
    prediction = _aware(entry.get("prediction_time"))
    recorded = _aware(entry.get("ts"))
    if None in (source, feature, prediction, recorded):
        return False
    assert source and feature and prediction and recorded
    return recorded == prediction and source <= feature <= prediction

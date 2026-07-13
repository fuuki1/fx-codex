from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from fx_backtester.models import instrument_for, normalize_symbol

REQUIRED_PRICE_COLUMNS = {"timestamp", "open", "high", "low", "close"}
EVENT_COLUMNS = ["timestamp", "currency", "symbol", "impact", "name"]
EVENT_PIT_COLUMNS = [
    "occurrence_id",
    "revision",
    "effective_from",
    "effective_to",
    "is_tombstone",
    "identity_quality",
    "recorded_at",
]
IMPACT_LEVELS = {"low": 1, "medium": 2, "high": 3}
KNOWN_SYMBOLS = (
    "USDJPY",
    "EURUSD",
    "GBPUSD",
    "AUDUSD",
    "USDCHF",
    "USDCAD",
    "NZDUSD",
    "EURJPY",
    "GBPJPY",
    "AUDJPY",
)


def _standardize_columns(frame: pd.DataFrame) -> pd.DataFrame:
    renamed = frame.copy()
    renamed.columns = [str(column).strip().lower() for column in renamed.columns]
    aliases = {
        "datetime": "timestamp",
        "date": "timestamp",
        "time": "timestamp",
        "bidopen": "open",
        "bidhigh": "high",
        "bidlow": "low",
        "bidclose": "close",
    }
    renamed = renamed.rename(columns={k: v for k, v in aliases.items() if k in renamed.columns})
    return renamed


def _symbol_from_path(path: str | Path) -> str:
    stem = Path(path).stem.upper().replace("_", "").replace("-", "")
    for candidate in KNOWN_SYMBOLS:
        if candidate in stem:
            return candidate
    raise ValueError(
        f"{path} has no symbol column. Pass a CSV with symbol column or name the file like EURUSD.csv."
    )


def load_price_csv(
    path: str | Path, symbol: str | None = None, timezone: str | None = "UTC"
) -> dict[str, pd.DataFrame]:
    """Load OHLC price data.

    Accepted columns: timestamp, symbol (optional), open, high, low, close,
    volume/spread_pips/spread_price/spread (optional).
    Returns a dict keyed by normalized symbols such as EURUSD.
    """
    frame = _standardize_columns(pd.read_csv(path))
    missing = REQUIRED_PRICE_COLUMNS - set(frame.columns)
    if missing:
        raise ValueError(f"{path} is missing required columns: {sorted(missing)}")

    # Repository CSVs declare naive source timestamps as UTC through this explicit
    # default. Callers with a local source clock must pass its IANA timezone; DST
    # folds/gaps are rejected rather than guessed. Internally every row is UTC.
    frame["timestamp"] = _parse_utc_timestamp_series(
        frame["timestamp"],
        timezone=timezone,
        label=f"{path} timestamp",
    )

    if "symbol" not in frame.columns:
        frame["symbol"] = normalize_symbol(symbol or _symbol_from_path(path))
    else:
        frame["symbol"] = frame["symbol"].map(normalize_symbol)

    output: dict[str, pd.DataFrame] = {}
    numeric_columns = ["open", "high", "low", "close"]
    for optional_column in ("volume", "spread_pips", "spread_price", "spread"):
        if optional_column in frame.columns:
            numeric_columns.append(optional_column)

    for symbol_name, symbol_frame in frame.groupby("symbol", sort=True):
        instrument_for(symbol_name)
        prepared = symbol_frame.sort_values("timestamp").set_index("timestamp")
        if prepared.index.has_duplicates:
            duplicates = prepared.index[prepared.index.duplicated()].unique()
            raise ValueError(f"{symbol_name} has duplicate timestamps: {duplicates[:3].tolist()}")
        prepared[numeric_columns] = prepared[numeric_columns].astype(float)
        output[symbol_name] = prepared[numeric_columns]

    return output


def load_price_csvs(
    paths: list[str | Path],
    timezone: str | None = "UTC",
) -> dict[str, pd.DataFrame]:
    loaded: dict[str, pd.DataFrame] = {}
    for path in paths:
        for symbol, frame in load_price_csv(path, timezone=timezone).items():
            if symbol in loaded:
                loaded[symbol] = pd.concat([loaded[symbol], frame]).sort_index()
                if loaded[symbol].index.has_duplicates:
                    raise ValueError(f"{symbol} has duplicate timestamps across input files")
            else:
                loaded[symbol] = frame
    if not loaded:
        raise ValueError("No price data loaded")
    return loaded


def filter_price_data_by_date(
    data: dict[str, pd.DataFrame],
    start: Any | None = None,
    end: Any | None = None,
) -> dict[str, pd.DataFrame]:
    start_ts = _parse_datetime_bound(start, is_end=False)
    end_ts = _parse_datetime_bound(end, is_end=True)
    if start_ts is None and end_ts is None:
        return data

    filtered: dict[str, pd.DataFrame] = {}
    for symbol, frame in data.items():
        selected = frame
        aligned_start = _align_timestamp_to_index(start_ts, selected.index)
        aligned_end = _align_timestamp_to_index(end_ts, selected.index)
        if aligned_start is not None:
            selected = selected[selected.index >= aligned_start]
        if aligned_end is not None:
            selected = selected[selected.index <= aligned_end]
        filtered[symbol] = selected.copy()

    if all(frame.empty for frame in filtered.values()):
        raise ValueError("date range removed all price data")
    return filtered


def load_economic_events_csv(
    path: str | Path | None,
    timezone: str | None = "UTC",
    *,
    as_of: Any | None = None,
    require_point_in_time: bool = False,
) -> pd.DataFrame:
    if path is None:
        if require_point_in_time:
            raise ValueError("point-in-time economic events require an archive path")
        return pd.DataFrame(columns=EVENT_COLUMNS).set_index(
            pd.DatetimeIndex([], name="timestamp", tz="UTC")
        )

    frame = _standardize_columns(pd.read_csv(path))
    if "timestamp" not in frame.columns:
        raise ValueError(f"{path} is missing required column: timestamp")

    for column in EVENT_COLUMNS:
        if column not in frame.columns:
            frame[column] = ""

    has_recorded_at = "recorded_at" in frame.columns
    has_revision_contract = set(EVENT_PIT_COLUMNS).issubset(frame.columns)
    # A PIT archive is an internal research artifact, not a raw vendor-local
    # export.  Its event clock must therefore carry its own offset; silently
    # assigning the CLI/source timezone could move blackout windows by hours.
    event_timezone = None if require_point_in_time or has_revision_contract else timezone
    frame["timestamp"] = _parse_utc_timestamp_series(
        frame["timestamp"],
        timezone=event_timezone,
        label=f"{path} timestamp",
    )
    if has_recorded_at:
        # recorded_at is an internal UTC availability boundary.  Never guess a
        # missing offset using the source-event timezone.
        frame["recorded_at"] = _parse_utc_timestamp_series(
            frame["recorded_at"],
            timezone=None,
            label=f"{path} recorded_at",
        )
        if has_revision_contract:
            # Validate the immutable input in full before applying the dataset
            # cutoff; a corrupt future row must not be hidden by as-of filtering.
            frame = _validate_event_revision_contract(frame, path)
        if as_of is None:
            raise ValueError("calendar archive with recorded_at requires an explicit aware as_of")
        cutoff = _parse_aware_utc_scalar(as_of, label="economic event as_of")
        frame = frame[frame["recorded_at"] <= cutoff].copy()
        if has_revision_contract:
            _validate_visible_event_revision_prefix(frame)
    elif as_of is not None or require_point_in_time:
        raise ValueError("point-in-time economic events require recorded_at provenance")
    if require_point_in_time and not has_revision_contract:
        missing = sorted(set(EVENT_PIT_COLUMNS) - set(frame.columns))
        raise ValueError(
            "point-in-time economic events require the revision contract: " + ", ".join(missing)
        )
    frame["currency"] = frame["currency"].astype(str).str.upper().str.strip()
    frame["symbol"] = (
        frame["symbol"].astype(str).str.upper().str.replace("/", "", regex=False).str.strip()
    )
    frame["impact"] = frame["impact"].astype(str).str.lower().str.strip().replace("", "high")
    if has_revision_contract:
        output_columns = [*EVENT_COLUMNS, *EVENT_PIT_COLUMNS]
    elif has_recorded_at:
        output_columns = [*EVENT_COLUMNS, "recorded_at"]
    else:
        output_columns = EVENT_COLUMNS
    result = (
        frame[output_columns].sort_values(["timestamp", "recorded_at"]).set_index("timestamp")
        if has_recorded_at
        else frame[output_columns].sort_values("timestamp").set_index("timestamp")
    )
    result.attrs["event_provenance"] = {
        "pit_revision_contract": has_revision_contract,
        "stable_occurrence_identity": bool(
            has_revision_contract
            and not result.empty
            and (result["identity_quality"] == "source").all()
        ),
        "promotion_eligible": bool(
            has_revision_contract
            and not result.empty
            and (result["identity_quality"] == "source").all()
        ),
    }
    return result


def load_economic_events_for_backtest(
    path: str | Path | None,
    data: dict[str, pd.DataFrame],
    timezone: str | None = "UTC",
) -> pd.DataFrame:
    """Load an event archive at the dataset cutoff for dynamic PIT replay.

    ``build_no_trade_mask`` still applies each row's ``recorded_at`` at every
    historical bar.  The dataset cutoff only prevents records learned after the
    simulated dataset ended from entering the run at all.
    """

    if path is None:
        return load_economic_events_csv(None, timezone=timezone)
    header = _standardize_columns(pd.read_csv(path, nrows=0))
    missing_contract = sorted(set(EVENT_PIT_COLUMNS) - set(header.columns))
    if missing_contract:
        raise ValueError(
            "backtest economic events require recorded_at and the PIT revision contract: "
            + ", ".join(missing_contract)
        )
    cutoffs = [frame.index.max() for frame in data.values() if not frame.empty]
    if not cutoffs:
        raise ValueError("point-in-time event replay requires non-empty price data")
    as_of = max(pd.Timestamp(value) for value in cutoffs)
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("point-in-time event replay requires aware price timestamps")
    return load_economic_events_csv(
        path,
        timezone=timezone,
        as_of=as_of,
        require_point_in_time=True,
    )


def filter_economic_events_by_date(
    events: pd.DataFrame,
    start: Any | None = None,
    end: Any | None = None,
    *,
    minutes_before: int = 0,
    minutes_after: int = 0,
) -> pd.DataFrame:
    if events.empty:
        return events
    # A later revision may move an event outside the selected date range or
    # tombstone it.  Dropping that row would resurrect the earlier vintage.
    # Keep the complete revision chain; build_no_trade_mask bounds it to bars.
    if set(EVENT_PIT_COLUMNS).issubset(events.columns):
        return events.copy()
    start_ts = _parse_datetime_bound(start, is_end=False)
    end_ts = _parse_datetime_bound(end, is_end=True)
    selected = events
    aligned_start = _align_timestamp_to_index(start_ts, selected.index)
    aligned_end = _align_timestamp_to_index(end_ts, selected.index)
    if aligned_start is not None:
        selected = selected[selected.index >= aligned_start - pd.Timedelta(minutes=minutes_before)]
    if aligned_end is not None:
        selected = selected[selected.index <= aligned_end + pd.Timedelta(minutes=minutes_after)]
    return selected.copy()


def build_no_trade_mask(
    index: pd.DatetimeIndex,
    symbol: str,
    events: pd.DataFrame,
    minutes_before: int = 30,
    minutes_after: int = 30,
    min_impact: str = "medium",
) -> pd.Series:
    if events.empty:
        return pd.Series(False, index=index)

    if set(EVENT_PIT_COLUMNS).issubset(events.columns):
        return _build_point_in_time_revision_mask(
            index,
            symbol,
            events,
            minutes_before=minutes_before,
            minutes_after=minutes_after,
            min_impact=min_impact,
        )

    inst = instrument_for(symbol)
    min_level = IMPACT_LEVELS[min_impact.lower()]
    mask = pd.Series(False, index=index)

    for timestamp, event in events.iterrows():
        timestamp = _align_timestamp_to_index(pd.Timestamp(timestamp), index)
        if timestamp is None:
            raise ValueError("economic event timestamp must be valid")
        impact_level = IMPACT_LEVELS.get(str(event.get("impact", "high")).lower(), 3)
        if impact_level < min_level:
            continue

        event_symbol = str(event.get("symbol", "")).strip().upper().replace("/", "")
        event_currency = str(event.get("currency", "")).strip().upper()
        applies_to_symbol = event_symbol in ("", "NAN") or event_symbol == inst.symbol
        applies_to_currency = event_currency in ("", "NAN") or event_currency in {
            inst.base,
            inst.quote,
        }
        if not (applies_to_symbol and applies_to_currency):
            continue

        start = timestamp - pd.Timedelta(minutes=minutes_before)
        end = timestamp + pd.Timedelta(minutes=minutes_after)
        recorded_at = event.get("recorded_at")
        if recorded_at is not None and not pd.isna(recorded_at):
            available = _align_timestamp_to_index(pd.Timestamp(recorded_at), index)
            if available is None:
                raise ValueError("economic event recorded_at must be valid")
            start = max(start, available)
        mask |= (index >= start) & (index <= end)

    return mask


def _build_point_in_time_revision_mask(
    index: pd.DatetimeIndex,
    symbol: str,
    events: pd.DataFrame,
    *,
    minutes_before: int,
    minutes_after: int,
    min_impact: str,
) -> pd.Series:
    """Apply only the latest event vintage visible at each historical bar."""

    inst = instrument_for(symbol)
    min_level = IMPACT_LEVELS[min_impact.lower()]
    mask = pd.Series(False, index=index)
    rows = events.reset_index().sort_values(
        ["occurrence_id", "effective_from", "revision"],
        kind="stable",
    )
    for _, revisions in rows.groupby("occurrence_id", sort=False):
        revision_rows = list(revisions.to_dict("records"))
        for position, event in enumerate(revision_rows):
            active_start = _align_timestamp_to_index(
                max(pd.Timestamp(event["recorded_at"]), pd.Timestamp(event["effective_from"])),
                index,
            )
            if active_start is None:
                raise ValueError("economic event effective_from must be valid")
            active_end: pd.Timestamp | None = None
            if position + 1 < len(revision_rows):
                active_end = _align_timestamp_to_index(
                    pd.Timestamp(revision_rows[position + 1]["effective_from"]),
                    index,
                )
            explicit_end = event.get("effective_to")
            if explicit_end is not None and not pd.isna(explicit_end):
                aligned_explicit_end = _align_timestamp_to_index(
                    pd.Timestamp(explicit_end),
                    index,
                )
                if aligned_explicit_end is None:
                    raise ValueError("economic event effective_to must be valid")
                active_end = (
                    aligned_explicit_end
                    if active_end is None
                    else min(active_end, aligned_explicit_end)
                )
            if bool(event["is_tombstone"]):
                continue

            impact_level = IMPACT_LEVELS.get(str(event.get("impact", "high")).lower(), 3)
            if impact_level < min_level:
                continue
            event_symbol = str(event.get("symbol", "")).strip().upper().replace("/", "")
            event_currency = str(event.get("currency", "")).strip().upper()
            applies_to_symbol = event_symbol in ("", "NAN") or event_symbol == inst.symbol
            applies_to_currency = event_currency in ("", "NAN") or event_currency in {
                inst.base,
                inst.quote,
            }
            if not (applies_to_symbol and applies_to_currency):
                continue

            timestamp = _align_timestamp_to_index(pd.Timestamp(event["timestamp"]), index)
            if timestamp is None:
                raise ValueError("economic event timestamp must be valid")
            start = max(timestamp - pd.Timedelta(minutes=minutes_before), active_start)
            end = timestamp + pd.Timedelta(minutes=minutes_after)
            selected = (index >= start) & (index <= end)
            if active_end is not None:
                selected &= index < active_end
            mask |= selected
    return mask


def _validate_event_revision_contract(frame: pd.DataFrame, path: str | Path) -> pd.DataFrame:
    validated = frame.copy()
    validated["effective_from"] = _parse_utc_timestamp_series(
        validated["effective_from"],
        timezone=None,
        label=f"{path} effective_from",
    )
    effective_to: list[Any] = []
    for position, raw in enumerate(validated["effective_to"].tolist()):
        if pd.isna(raw) or not str(raw).strip():
            effective_to.append(pd.NaT)
            continue
        effective_to.append(
            _parse_aware_utc_scalar(raw, label=f"{path} effective_to at row {position}")
        )
    validated["effective_to"] = pd.Series(
        pd.to_datetime(effective_to, utc=True),
        index=validated.index,
    )

    occurrence_ids = validated["occurrence_id"].astype(str).str.strip()
    if occurrence_ids.eq("").any() or occurrence_ids.str.lower().eq("nan").any():
        raise ValueError("economic event occurrence_id must be non-empty")
    validated["occurrence_id"] = occurrence_ids
    revisions = pd.to_numeric(validated["revision"], errors="raise")
    if bool((revisions <= 0).any()) or bool((revisions % 1 != 0).any()):
        raise ValueError("economic event revision must be a positive integer")
    validated["revision"] = revisions.astype(int)
    if validated.duplicated(["occurrence_id", "revision"]).any():
        raise ValueError("economic event occurrence_id/revision must be unique")

    tombstones = validated["is_tombstone"].map(_parse_strict_bool)
    validated["is_tombstone"] = tombstones.astype(bool)
    identity_quality = validated["identity_quality"].astype(str).str.lower().str.strip()
    if not identity_quality.isin({"source", "heuristic"}).all():
        raise ValueError("economic event identity_quality must be source or heuristic")
    validated["identity_quality"] = identity_quality

    if bool((validated["effective_from"] < validated["recorded_at"]).any()):
        raise ValueError("economic event effective_from cannot precede recorded_at")
    present_end = validated["effective_to"].notna()
    if bool(present_end.any()) and bool(
        (
            validated.loc[present_end, "effective_to"]
            <= validated.loc[present_end, "effective_from"]
        ).any()
    ):
        raise ValueError("economic event effective_to must follow effective_from")
    for occurrence_id, revisions_frame in validated.groupby("occurrence_id", sort=False):
        ordered = revisions_frame.sort_values("revision")
        expected = list(range(1, len(ordered) + 1))
        if ordered["revision"].tolist() != expected:
            raise ValueError(f"economic event {occurrence_id} revisions must be contiguous from 1")
        if (
            not ordered["effective_from"].is_monotonic_increasing
            or ordered["effective_from"].duplicated().any()
        ):
            raise ValueError(
                f"economic event {occurrence_id} effective_from must strictly increase"
            )
        if (
            not ordered["recorded_at"].is_monotonic_increasing
            or ordered["recorded_at"].duplicated().any()
        ):
            raise ValueError(f"economic event {occurrence_id} recorded_at must strictly increase")
    return validated


def _validate_visible_event_revision_prefix(frame: pd.DataFrame) -> None:
    """Prove that an as-of cutoff exposes a prefix, never a later orphan revision."""

    for occurrence_id, revisions_frame in frame.groupby("occurrence_id", sort=False):
        visible = sorted(int(value) for value in revisions_frame["revision"].tolist())
        if visible != list(range(1, len(visible) + 1)):
            raise ValueError(
                f"economic event {occurrence_id} visible revisions must be a prefix from 1"
            )


def _parse_strict_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"true", "1"}:
        return True
    if normalized in {"false", "0"}:
        return False
    raise ValueError("economic event is_tombstone must be boolean")


def _parse_datetime_bound(value: Any | None, *, is_end: bool) -> pd.Timestamp | None:
    if value is None:
        return None
    raw = str(value).strip()
    timestamp = pd.Timestamp(raw)
    if is_end and _looks_like_date_only(raw):
        return timestamp + pd.Timedelta(days=1) - pd.Timedelta(nanoseconds=1)
    return timestamp


def _looks_like_date_only(value: str) -> bool:
    return len(value) == 10 and value[4] == "-" and value[7] == "-"


def _parse_utc_timestamp_series(
    values: pd.Series,
    *,
    timezone: str | None,
    label: str,
) -> pd.Series:
    parsed: list[pd.Timestamp] = []
    for position, raw in enumerate(values.tolist()):
        try:
            timestamp = pd.Timestamp(raw)
            if pd.isna(timestamp):
                raise ValueError("timestamp is missing")
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                if timezone is None:
                    raise ValueError("naive timestamp requires an explicit source timezone")
                timestamp = timestamp.tz_localize(
                    timezone,
                    ambiguous="raise",
                    nonexistent="raise",
                )
            timestamp = timestamp.tz_convert("UTC")
        except Exception as error:  # noqa: BLE001 - pandas timezone backends vary by source
            raise ValueError(f"{label} at row {position} is invalid or DST-ambiguous") from error
        parsed.append(timestamp)
    return pd.Series(pd.DatetimeIndex(parsed), index=values.index, name=values.name)


def _parse_aware_utc_scalar(value: Any, *, label: str) -> pd.Timestamp:
    try:
        timestamp = pd.Timestamp(value)
    except Exception as error:  # noqa: BLE001 - pandas timestamp errors vary by backend
        raise ValueError(f"{label} must be a valid timezone-aware timestamp") from error
    if pd.isna(timestamp) or timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError(f"{label} must be a valid timezone-aware timestamp")
    return timestamp.tz_convert("UTC")


def _align_timestamp_to_index(
    timestamp: pd.Timestamp | None,
    index: pd.DatetimeIndex,
) -> pd.Timestamp | None:
    if timestamp is None:
        return None
    value = pd.Timestamp(timestamp)
    if index.tz is None:
        return value.tz_convert("UTC").tz_localize(None) if value.tzinfo is not None else value
    if value.tzinfo is None:
        return value.tz_localize(index.tz, ambiguous="raise", nonexistent="raise")
    return value.tz_convert(index.tz)

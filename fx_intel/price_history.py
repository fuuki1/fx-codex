"""採点用の「将来価格」を調達する。

時間足別の自己採点では、各判断(記録時刻 t、時間足 tf)を「t + 主ホライズン」
時点の実勢価格と突き合わせる必要がある。ところが TradingView スキャナー
(tradingview_ta)は**現在のスナップショット**しか返さず、過去の任意時刻の
確定足を取り直すことはできない。そこで将来価格を2つの源から調達する:

源A(バックボーン, 依存ゼロ): ジャーナルの後続エントリ。
    ブリーフィングは定期実行で追記され続けるため、後から記録された
    同じ (symbol, timeframe) の close 列が、そのまま「過去判断から見た
    将来価格」になる。実行間隔に依存せず、market.open_hours_between で
    週末クローズを除いた経過時間で最も主ホライズンに近い1点を選ぶ。
    現在スナップショット(今回の実行で記録する close)は、この系列の
    最新点として自然に加わる。

源B(任意, 差し込み式): 外部の履歴OHLC(yfinance/OANDA/Dukascopy 等)。
    future_price_provider として注入すれば、源Aで将来価格が見つからない
    判断(まだ後続エントリが無い/実行が飛んだ区間)を外部確定足で補える。
    既定は None(源Aのみ)。プロバイダは (symbol, timeframe, target_time,
    tolerance_hours) を受けて float|None を返す純粋な契約で、ネットワークの
    有無をこのモジュールの外に閉じ込める。

このモジュール自体はネットワークアクセスを持たない純粋ロジックで、
テストから直接検証できる(源Bはモックを注入する)。
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
import fcntl
import hashlib
import json
from math import isfinite
import os
from pathlib import Path
import socket

from .append_only import exclusive_sidecar_lock
from .market import WEEKEND_CLOSURE, open_hours_between

# 源B(外部履歴OHLC)の注入契約。
# (symbol, timeframe, target_time, tolerance_hours) -> 将来終値 or None
FuturePriceProvider = Callable[[str, str, datetime, float], float | None]
SNAPSHOT_SCHEMA_VERSION = 2
SNAPSHOT_CADENCE_SECONDS = 300
MAX_AUTHORITATIVE_SOURCE_AGE = {
    "15m": timedelta(minutes=45),
    "1h": timedelta(hours=2),
    "4h": timedelta(hours=8),
    "1d": timedelta(hours=36),
}
JOURNAL_ORDER_CLOCK_FIELDS = ("ts", "capture_slot", "available_time", "ingested_time")


class PriceHistoryWriteError(RuntimeError):
    """Raised when an append cannot preserve the single-writer journal contract."""


class PriceHistoryReadError(RuntimeError):
    """Raised when a price journal cannot be verified without guessing."""


def _parse_ts(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def build_close_series(
    entries: Iterable[Mapping[str, object]],
) -> dict[tuple[str, str], list[tuple[datetime, float]]]:
    """ジャーナル履歴を (symbol, timeframe) 別の時系列 close 列にまとめる。

    timeframe を持たない旧スキーマの行は timeframe="" のキーに入れる
    (融合1判断の系列。後方互換のため保持)。各系列は時刻昇順にソートする。
    """
    series: dict[tuple[str, str], list[tuple[datetime, float]]] = {}
    for entry in entries:
        ts = _parse_ts(entry.get("ts"))
        if entry.get("ts") is not None and ts is None:
            raise ValueError("price history timestamp must be valid and timezone-aware")
        close = entry.get("close")
        if ts is None or not isinstance(close, (int, float)) or isinstance(close, bool):
            continue
        symbol = str(entry.get("symbol", ""))
        timeframe = str(entry.get("timeframe", ""))
        series.setdefault((symbol, timeframe), []).append((ts, float(close)))
    for points in series.values():
        points.sort(key=lambda point: point[0])
    return series


def future_close_from_series(
    series: list[tuple[datetime, float]],
    recorded_at: datetime,
    horizon_hours: float,
    tolerance_hours: float,
) -> float | None:
    """系列の中から「記録時刻から主ホライズン後(市場オープン換算)」の終値を返す。

    horizon±tolerance(市場オープン時間換算)の範囲にある点のうち、
    ホライズンに最も近い1点の close を返す。該当が無ければ None。
    二分探索で候補範囲を絞り、毎時追記で系列が長くなっても全走査しない。
    """
    if not series:
        return None
    times = [point[0] for point in series]
    # オープン時間は壁時計時間を超えないため、候補は壁時計で
    # [ホライズン下限, ホライズン上限 + 週末クローズ1回分] に限られる
    window_lower = recorded_at + timedelta(hours=horizon_hours - tolerance_hours)
    window_upper = recorded_at + timedelta(hours=horizon_hours + tolerance_hours) + WEEKEND_CLOSURE
    best: tuple[float, float] | None = None  # (|経過-ホライズン|, 将来終値)
    for index in range(bisect_left(times, window_lower), len(series)):
        point_ts, point_close = series[index]
        if point_ts > window_upper:
            break
        age = open_hours_between(recorded_at, point_ts)
        if not (horizon_hours - tolerance_hours <= age <= horizon_hours + tolerance_hours):
            continue
        gap = abs(age - horizon_hours)
        if best is None or gap < best[0]:
            best = (gap, point_close)
    return best[1] if best is not None else None


def resolve_future_close(
    series_by_key: Mapping[tuple[str, str], list[tuple[datetime, float]]],
    symbol: str,
    timeframe: str,
    recorded_at: datetime,
    horizon_hours: float,
    tolerance_hours: float,
    provider: FuturePriceProvider | None = None,
) -> float | None:
    """1判断ぶんの将来価格を、源A→源Bの順で解決する。

    まずジャーナル系列(源A)から探し、見つからなければ provider(源B)へ
    フォールバックする。provider の target_time は「記録時刻 + 主ホライズン
    (壁時計)」を渡す(外部履歴OHLCはその近傍の確定足を返す想定)。
    どちらでも見つからなければ None(未成熟=採点保留)。
    """
    series = series_by_key.get((symbol, timeframe), [])
    close = future_close_from_series(series, recorded_at, horizon_hours, tolerance_hours)
    if close is not None:
        return close
    if provider is not None:
        target_time = recorded_at + timedelta(hours=horizon_hours)
        return provider(symbol, timeframe, target_time, tolerance_hours)
    return None


def snapshot_entries(
    closes_by_interval: Mapping[str, Mapping[str, float | Mapping[str, object] | None]],
    now: datetime | None = None,
    *,
    source: str = "tradingview_ta_scanner",
    run_id: str | None = None,
    writer_id: str | None = None,
) -> list[dict]:
    """現在スナップショットを、ジャーナル系列に足せる最新点の形に変換する。

    closes_by_interval は {symbol: {timeframe: close}} または
    {symbol: {timeframe: {close, open, high, low, bid, ask, spread}}}。
    今回の実行で TradingView が返した各時間足の現在価格を、
    build_close_series と TP/SL 経路採点が読めるエントリに変換して返す。
    high/low があれば TP/SL 先着判定の品質が上がり、bid/ask/spread は
    後続の運用監視で約定コストを見積もるために残す。
    """
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("snapshot timestamp must be timezone-aware")
    now = now.astimezone(UTC)
    stamp = now.isoformat()
    capture_slot = _capture_slot(stamp)
    resolved_writer = writer_id or f"{socket.gethostname()}:{os.getpid()}"
    resolved_run = run_id or f"snapshot-{now.strftime('%Y%m%dT%H%M%S%fZ')}-{os.getpid()}"
    rows: list[dict] = []
    for symbol, intervals in closes_by_interval.items():
        for timeframe, snapshot in intervals.items():
            row = _snapshot_row(
                stamp,
                str(symbol),
                str(timeframe),
                snapshot,
                source=source,
                capture_slot=capture_slot,
                run_id=resolved_run,
                writer_id=resolved_writer,
            )
            if row is not None:
                rows.append(row)
    return rows


def append_snapshot_entries(path: str | Path, rows: Sequence[Mapping[str, object]]) -> int:
    """Append unique snapshots under an OS advisory lock.

    The natural key is ``(capture_slot, symbol, timeframe)``. Replaying the same
    payload is idempotent. A different payload for an occupied key indicates a
    competing writer or a non-deterministic retry and is rejected instead of
    silently choosing a winner. ``flock`` is released by the kernel on process
    death, so it cannot leave a stale PID lock behind.
    """

    target = Path(path)
    with exclusive_sidecar_lock(target):
        return _append_snapshot_entries_locked(target, rows)


def read_snapshot_entries(
    path: str | Path,
    *,
    as_of: datetime | None = None,
) -> Iterator[dict[str, object]]:
    """Read only verified schema-v2 snapshots under writer/migration locks.

    Legacy or malformed rows are not silently accepted.  They must first be
    preserved with ``prepare_v2_price_journal.py`` and a fresh v2 journal must be
    started.  This keeps contaminated history out of learning and promotion.
    """

    cutoff: datetime | None = None
    if as_of is not None:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise PriceHistoryReadError("price journal as_of must be timezone-aware")
        cutoff = as_of.astimezone(UTC)
    target = Path(path)
    with exclusive_sidecar_lock(target):
        try:
            handle = target.open(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as error:
            raise PriceHistoryReadError(f"cannot read price journal: {target}") from error
        with handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                seen_keys: set[tuple[str, str, str]] = set()
                last_clocks: dict[str, datetime] = {}
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    location = f"{target}:{line_number}"
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError as error:
                        raise PriceHistoryReadError(
                            f"malformed price JSONL at {location}"
                        ) from error
                    if not isinstance(row, dict):
                        raise PriceHistoryReadError(f"non-object price row at {location}")
                    try:
                        _verify_content_hash(row, location=location)
                    except PriceHistoryWriteError as error:
                        raise PriceHistoryReadError(str(error)) from error
                    issues, timestamps = _snapshot_integrity_issues(row)
                    if issues:
                        raise PriceHistoryReadError(
                            f"invalid price snapshot at {location}: {','.join(issues)}"
                        )
                    if cutoff is not None and any(value > cutoff for value in timestamps):
                        raise PriceHistoryReadError(f"future price row beyond as_of at {location}")
                    try:
                        key = _snapshot_key(row)
                    except PriceHistoryWriteError as error:
                        raise PriceHistoryReadError(str(error)) from error
                    if key in seen_keys:
                        raise PriceHistoryReadError(
                            f"duplicate price snapshot natural key {key} at {location}"
                        )
                    clocks = _journal_order_clocks(row)
                    regressed = _regressed_journal_clock(last_clocks, clocks)
                    if regressed is not None:
                        raise PriceHistoryReadError(
                            f"price snapshot {regressed} is not monotonic at {location}"
                        )
                    seen_keys.add(key)
                    last_clocks = clocks
                    yield row
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _append_snapshot_entries_locked(
    target: Path,
    rows: Sequence[Mapping[str, object]],
) -> int:
    """Write while the stable sidecar lock is held across replaceable inodes."""

    target.parent.mkdir(parents=True, exist_ok=True)
    appended = 0
    with target.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            existing: dict[tuple[str, str, str], dict[str, object]] = {}
            last_clocks: dict[str, datetime] = {}
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError as error:
                    raise PriceHistoryWriteError(
                        f"malformed JSONL at {target}:{line_number}"
                    ) from error
                if not isinstance(parsed, dict):
                    raise PriceHistoryWriteError(f"non-object JSONL row at {target}:{line_number}")
                _verify_content_hash(parsed, location=f"{target}:{line_number}")
                issues, _timestamps = _snapshot_integrity_issues(parsed)
                if issues:
                    raise PriceHistoryWriteError(
                        f"invalid existing snapshot at {target}:{line_number}: {','.join(issues)}"
                    )
                key = _snapshot_key(parsed)
                prior = existing.get(key)
                if prior is not None:
                    raise PriceHistoryWriteError(
                        f"duplicate existing snapshots for natural key {key}"
                    )
                clocks = _journal_order_clocks(parsed)
                regressed = _regressed_journal_clock(last_clocks, clocks)
                if regressed is not None:
                    raise PriceHistoryWriteError(
                        f"existing snapshot {regressed} is not monotonic at "
                        f"{target}:{line_number}"
                    )
                existing[key] = parsed
                last_clocks = clocks

            pending: list[dict[str, object]] = []
            for row_number, raw in enumerate(rows, start=1):
                row = dict(raw)
                _verify_content_hash(row, location=f"pending row {row_number}")
                issues, _timestamps = _snapshot_integrity_issues(row)
                if issues:
                    raise PriceHistoryWriteError(
                        f"invalid pending snapshot row {row_number}: {','.join(issues)}"
                    )
                key = _snapshot_key(row)
                prior = existing.get(key)
                if prior is not None:
                    if not _same_snapshot(prior, row):
                        raise PriceHistoryWriteError(
                            f"conflicting snapshot from duplicate writer for natural key {key}"
                        )
                    continue
                clocks = _journal_order_clocks(row)
                regressed = _regressed_journal_clock(last_clocks, clocks)
                if regressed is not None:
                    raise PriceHistoryWriteError(
                        f"pending snapshot {regressed} is not monotonic at row {row_number}"
                    )
                existing[key] = row
                pending.append(row)
                last_clocks = clocks

            handle.seek(0, os.SEEK_END)
            for row in pending:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            if pending:
                handle.flush()
                os.fsync(handle.fileno())
            appended = len(pending)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return appended


def _snapshot_row(
    stamp: str,
    symbol: str,
    timeframe: str,
    snapshot: float | Mapping[str, object] | None,
    *,
    source: str,
    capture_slot: str,
    run_id: str,
    writer_id: str,
) -> dict | None:
    core: dict[str, object]
    if isinstance(snapshot, (int, float)) and not isinstance(snapshot, bool):
        core = {
            "ts": stamp,
            "symbol": symbol,
            "timeframe": timeframe,
            "close": float(snapshot),
        }
    else:
        if not isinstance(snapshot, Mapping):
            return None
        close = _number(snapshot.get("close"))
        if close is None:
            return None
        core = {
            "ts": stamp,
            "symbol": symbol,
            "timeframe": timeframe,
            "close": close,
        }
        for key in ("open", "high", "low", "bid", "ask", "spread"):
            value = _number(snapshot.get(key))
            if value is not None:
                core[key] = value

    flags: list[str] = []
    has_range = "high" in core or "low" in core or "open" in core
    if has_range:
        flags.append("forming_bar_ohlc_not_post_prediction_interval")
    bid = _number(core.get("bid"))
    ask = _number(core.get("ask"))
    if bid is None or ask is None:
        flags.append("bid_ask_unavailable")
    elif bid > ask:
        flags.append("crossed_quote")

    source_time = _aware_optional_timestamp(
        snapshot.get("source_time") if isinstance(snapshot, Mapping) else None
    )
    # available_time/ingested_time はこの1バッチ(同一 capture_slot・同一 ts)の確定時刻
    # stamp に揃える。TradingView から各ペア/各足を順番に取得するため、行ごとの
    # acquired_at をそのまま使うと symbol 境界で available_time が後退し、書き込み側の
    # JOURNAL_ORDER_CLOCK_FIELDS 単調性チェック(_regressed_journal_clock)に必ず引っかかる。
    # PIT 的にも、ペア個別の取得時刻より遅い「バッチが揃った時刻」を可用時刻とみなすのは
    # 保守的方向(将来情報を混入させない)なので安全。snapshot 側が持ち込む
    # available_time/ingested_time は同 slot バッチでは採用しない。
    batch_time = datetime.fromisoformat(stamp)
    available_time = batch_time
    ingested_time = batch_time
    source_record_id = (
        str(snapshot.get("source_record_id") or "").strip() if isinstance(snapshot, Mapping) else ""
    )
    if source_time is None:
        flags.extend(("source_time_unavailable", "event_time_unavailable"))
    if not source_record_id:
        flags.append("source_record_id_unavailable")

    row = {
        **core,
        "event_time": source_time.isoformat() if source_time is not None else None,
        "source_time": source_time.isoformat() if source_time is not None else None,
        "available_time": available_time.astimezone(UTC).isoformat(),
        "ingested_time": ingested_time.astimezone(UTC).isoformat(),
        "source": source,
        "source_record_id": source_record_id or None,
        "local_record_id": f"{symbol}:{timeframe}:{stamp}",
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "run_id": run_id,
        "writer_id": writer_id,
        "capture_slot": capture_slot,
        "ohlc_scope": "forming_bar_snapshot" if has_range else "quote_snapshot",
        "data_quality_flags": flags,
    }
    row["content_hash"] = _content_hash(row)
    return row


def _aware_optional_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
        return result if isfinite(result) else None
    return None


def _capture_slot(value: object) -> str:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as error:
        raise PriceHistoryWriteError("snapshot key requires a valid timestamp") from error
    if parsed.tzinfo is None:
        raise PriceHistoryWriteError("snapshot key requires a timezone-aware timestamp")
    utc = parsed.astimezone(UTC)
    epoch = int(utc.timestamp())
    slot_epoch = epoch - epoch % SNAPSHOT_CADENCE_SECONDS
    return datetime.fromtimestamp(slot_epoch, tz=UTC).isoformat()


def _snapshot_key(row: Mapping[str, object]) -> tuple[str, str, str]:
    stamp = row.get("capture_slot") or row.get("event_time") or row.get("ts")
    if not stamp:
        raise PriceHistoryWriteError("snapshot row is missing ts/event_time/capture_slot")
    symbol = str(row.get("symbol", "")).strip()
    timeframe = str(row.get("timeframe", "")).strip()
    if not symbol or not timeframe:
        raise PriceHistoryWriteError("snapshot row is missing symbol or timeframe")
    return _capture_slot(stamp), symbol, timeframe


def _same_snapshot(left: Mapping[str, object], right: Mapping[str, object]) -> bool:
    """Treat only a byte-semantics-equivalent retry as idempotent."""

    left_hash = left.get("content_hash")
    right_hash = right.get("content_hash")
    return isinstance(left_hash, str) and left_hash == right_hash


def _journal_order_clocks(row: Mapping[str, object]) -> dict[str, datetime]:
    """Return clocks that must not move backward in physical journal order."""

    clocks = {field: _parse_ts(row.get(field)) for field in JOURNAL_ORDER_CLOCK_FIELDS}
    if any(value is None for value in clocks.values()):
        raise PriceHistoryWriteError("snapshot row has an invalid journal-order clock")
    return {field: value for field, value in clocks.items() if value is not None}


def _regressed_journal_clock(
    previous: Mapping[str, datetime],
    current: Mapping[str, datetime],
) -> str | None:
    for field in JOURNAL_ORDER_CLOCK_FIELDS:
        if field in previous and current[field] < previous[field]:
            return field
    return None


def _content_hash(row: Mapping[str, object]) -> str:
    payload = {key: value for key, value in row.items() if key != "content_hash"}
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _verify_content_hash(row: Mapping[str, object], *, location: str) -> None:
    """Fail closed unless ``row`` matches its canonical snapshot digest."""

    supplied = row.get("content_hash")
    if (
        not isinstance(supplied, str)
        or len(supplied) != 64
        or any(character not in "0123456789abcdef" for character in supplied)
    ):
        raise PriceHistoryWriteError(f"invalid snapshot content_hash at {location}")
    try:
        expected = _content_hash(row)
    except (TypeError, ValueError, OverflowError) as error:
        raise PriceHistoryWriteError(
            f"snapshot cannot be canonically hashed at {location}"
        ) from error
    if supplied != expected:
        raise PriceHistoryWriteError(f"snapshot content_hash mismatch at {location}")


def _snapshot_integrity_issues(
    row: Mapping[str, object],
) -> tuple[list[str], list[datetime]]:
    issues: list[str] = []
    timestamps: list[datetime] = []
    if row.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        issues.append("schema_version_not_v2")

    flags = row.get("data_quality_flags")
    if not isinstance(flags, list) or any(not isinstance(flag, str) for flag in flags):
        issues.append("data_quality_flags_invalid")
        flag_set: set[str] = set()
    else:
        flag_set = set(flags)

    parsed_times: dict[str, datetime] = {}
    for field in ("ts", "available_time", "ingested_time", "capture_slot"):
        parsed = _parse_ts(row.get(field))
        if parsed is None:
            issues.append(f"{field}_invalid")
        else:
            parsed_times[field] = parsed
            timestamps.append(parsed)
    for field in ("event_time", "source_time"):
        raw = row.get(field)
        if raw is None:
            if field == "source_time" and "source_time_unavailable" not in flag_set:
                issues.append("source_time_missing_without_flag")
            if field == "event_time" and "event_time_unavailable" not in flag_set:
                issues.append("event_time_missing_without_flag")
            continue
        parsed = _parse_ts(raw)
        if parsed is None:
            issues.append(f"{field}_invalid")
        else:
            parsed_times[field] = parsed
            timestamps.append(parsed)

    available = parsed_times.get("available_time")
    ingested = parsed_times.get("ingested_time")
    source_time = parsed_times.get("source_time")
    timestamp = parsed_times.get("ts")
    capture_slot = parsed_times.get("capture_slot")
    if timestamp is not None and capture_slot is not None:
        expected_slot = datetime.fromisoformat(_capture_slot(timestamp.isoformat()))
        if capture_slot != expected_slot:
            issues.append("capture_slot_mismatch")
    if available is not None and ingested is not None and available < ingested:
        issues.append("available_before_ingestion")
    if available is not None and source_time is not None and available < source_time:
        issues.append("available_before_source")
    timeframe = str(row.get("timeframe") or "").strip()
    source_age_limit = MAX_AUTHORITATIVE_SOURCE_AGE.get(timeframe)
    if (
        available is not None
        and source_time is not None
        and source_age_limit is not None
        and available - source_time > source_age_limit
    ):
        issues.append("authoritative_source_stale")

    required_text = ("symbol", "timeframe", "source", "run_id", "writer_id", "local_record_id")
    for field in required_text:
        if not isinstance(row.get(field), str) or not str(row.get(field)).strip():
            issues.append(f"{field}_missing")
    source_record_id = row.get("source_record_id")
    if not isinstance(source_record_id, str) or not source_record_id.strip():
        if "source_record_id_unavailable" not in flag_set:
            issues.append("source_record_id_missing_without_flag")

    prices: dict[str, float] = {}
    for field in ("open", "high", "low", "close", "bid", "ask", "spread"):
        raw = row.get(field)
        if raw is None:
            continue
        value = _number(raw)
        if value is None or value <= 0:
            issues.append(f"{field}_invalid")
        else:
            prices[field] = value
    if "close" not in prices:
        issues.append("close_missing")
    high = prices.get("high")
    low = prices.get("low")
    if high is not None and low is not None and high < low:
        issues.append("high_below_low")
    if high is not None and any(prices.get(field, high) > high for field in ("open", "close")):
        issues.append("price_above_high")
    if low is not None and any(prices.get(field, low) < low for field in ("open", "close")):
        issues.append("price_below_low")
    bid = prices.get("bid")
    ask = prices.get("ask")
    if (bid is None) != (ask is None):
        issues.append("partial_bid_ask")
    elif bid is not None and ask is not None and bid > ask:
        issues.append("crossed_quote")
    return list(dict.fromkeys(issues)), timestamps

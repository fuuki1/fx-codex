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
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
import fcntl
import hashlib
import json
import os
from pathlib import Path
import socket

from .market import WEEKEND_CLOSURE, WeekendOpenHours

# 源B(外部履歴OHLC)の注入契約。
# (symbol, timeframe, target_time, tolerance_hours) -> 将来終値 or None
FuturePriceProvider = Callable[[str, str, datetime, float], float | None]
SNAPSHOT_SCHEMA_VERSION = 2
SNAPSHOT_CADENCE_SECONDS = 300


class PriceHistoryWriteError(RuntimeError):
    """Raised when an append cannot preserve the single-writer journal contract."""


def _parse_ts(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


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
    二分探索で候補範囲を絞り、5分追記で系列が長くなっても全走査しない。
    """
    if not series:
        return None
    times = [point[0] for point in series]
    # オープン時間は壁時計時間を超えないため、候補は壁時計で
    # [ホライズン下限, ホライズン上限 + 週末クローズ1回分] に限られる
    window_lower = recorded_at + timedelta(hours=horizon_hours - tolerance_hours)
    window_upper = recorded_at + timedelta(hours=horizon_hours + tolerance_hours) + WEEKEND_CLOSURE
    open_hours = WeekendOpenHours(recorded_at, window_upper)
    best: tuple[float, float] | None = None  # (|経過-ホライズン|, 将来終値)
    for index in range(bisect_left(times, window_lower), len(series)):
        point_ts, point_close = series[index]
        if point_ts > window_upper:
            break
        age = open_hours.age(point_ts)
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
    target.parent.mkdir(parents=True, exist_ok=True)
    appended = 0
    with target.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            existing: dict[tuple[str, str, str], dict[str, object]] = {}
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
                key = _snapshot_key(parsed)
                prior = existing.get(key)
                if prior is not None and not _same_snapshot(prior, parsed):
                    raise PriceHistoryWriteError(
                        f"conflicting existing snapshots for natural key {key}"
                    )
                existing[key] = parsed

            pending: list[dict[str, object]] = []
            for raw in rows:
                row = dict(raw)
                key = _snapshot_key(row)
                prior = existing.get(key)
                if prior is not None:
                    if not _same_snapshot(prior, row):
                        raise PriceHistoryWriteError(
                            f"conflicting snapshot from duplicate writer for natural key {key}"
                        )
                    continue
                existing[key] = row
                pending.append(row)

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

    row = {
        **core,
        "event_time": stamp,
        "available_time": stamp,
        "ingested_time": stamp,
        "source": source,
        "source_record_id": f"{symbol}:{timeframe}:{stamp}",
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "run_id": run_id,
        "writer_id": writer_id,
        "capture_slot": capture_slot,
        "ohlc_scope": "forming_bar_snapshot" if has_range else "quote_snapshot",
        "data_quality_flags": flags,
    }
    row["content_hash"] = _content_hash(row)
    return row


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
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
    left_writer = str(left.get("writer_id") or "").strip()
    right_writer = str(right.get("writer_id") or "").strip()
    if left_writer and right_writer and left_writer != right_writer:
        return False
    comparable = (
        "open",
        "high",
        "low",
        "close",
        "bid",
        "ask",
        "spread",
        "source",
        "schema_version",
        "ohlc_scope",
    )
    return all(left.get(key) == right.get(key) for key in comparable)


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

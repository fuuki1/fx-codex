"""TradingViewテクニカルの単一取得・共有snapshot cache。

正規launchd構成では ``com.fx-codex.snapshot`` だけがTradingViewへアクセスし、
取得結果をこのcacheへ原子的に保存する。briefing/horizonはcacheをread-onlyで読み、
欠損・破損・期限超過時にネットワーク取得へfallbackしない。

TradingView scannerには上流source timestampがない。したがってcacheの
``available_time`` は全interval取得が完了したローカルaware UTC時刻であり、
market event timeを意味しない。cache hit時もこの時刻を保持し、古い観測を現在時刻で
再スタンプしない。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, fields
from datetime import datetime, timedelta, UTC
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import socket
import tempfile
from typing import Any, IO

from fx_intel import technicals
from fx_intel.technicals import IntervalView, PairTechnicals

CACHE_SCHEMA_VERSION = 1
CACHE_SOURCE = "tradingview_ta_scanner"
CACHE_CONTRACT = "community-tradingview-current-snapshot-cache-v1"
DEFAULT_SHARED_SYMBOLS = ("USDJPY", "EURUSD", "GBPUSD")
DEFAULT_MAX_AGE_SECONDS = 360.0
DEFAULT_MIN_REFRESH_SECONDS = 240.0
DEFAULT_CACHE_PATH = Path(__file__).resolve().parents[1] / "logs" / "tradingview_technicals.json"
FUTURE_SKEW_TOLERANCE = timedelta(seconds=5)

FetchFunction = Callable[..., tuple[dict[str, PairTechnicals], list[str]]]


class TechnicalCacheError(RuntimeError):
    """共有テクニカルcacheの読書き・契約検証に失敗した。"""


def refresh_pair_technicals(
    symbols: Sequence[str],
    intervals: Sequence[str] = technicals.DEFAULT_INTERVALS,
    fast_window: int = 20,
    slow_window: int = 100,
    exchange: str = technicals.DEFAULT_EXCHANGE,
    screener: str = technicals.DEFAULT_SCREENER,
    *,
    cache_path: str | Path = DEFAULT_CACHE_PATH,
    shared_symbols: Sequence[str] = DEFAULT_SHARED_SYMBOLS,
    min_refresh_seconds: float = DEFAULT_MIN_REFRESH_SECONDS,
    now: datetime | None = None,
    fetcher: FetchFunction | None = None,
) -> tuple[dict[str, PairTechnicals], list[str]]:
    """排他lock下で必要時だけTradingViewを取得し、cacheを原子的に更新する。

    同じcacheへ誤って複数producerが起動しても、直近取得が
    ``min_refresh_seconds``以内なら再取得せず同一snapshotを返す。
    """

    requested = _clean_symbols(symbols)
    requested_intervals = _clean_intervals(intervals)
    fetch_symbols = tuple(dict.fromkeys((*_clean_symbols(shared_symbols), *requested)))
    fetch_intervals = tuple(dict.fromkeys((*technicals.DEFAULT_INTERVALS, *requested_intervals)))
    target = Path(cache_path)
    fixed_now = _aware_utc(now, "now") if now is not None else None
    if min_refresh_seconds < 0:
        raise ValueError("min_refresh_seconds must be non-negative")

    with _cache_lock(target, exclusive=True):
        reference_now = fixed_now or datetime.now(UTC)
        cache_warning: str | None = None
        try:
            cached = _load_verified_payload(target, reference_now)
        except TechnicalCacheError as error:
            cached = None
            if target.exists():
                cache_warning = f"TradingView共有キャッシュを再生成: {error}"

        if cached is not None and _is_compatible(
            cached,
            # Producer cache is a shared contract for every scheduled consumer.
            # A partial legacy/manual cache must be refreshed even when it happens
            # to satisfy only this caller, otherwise horizon can later fail closed
            # on a symbol that the producer should have populated.
            symbols=fetch_symbols,
            intervals=fetch_intervals,
            fast_window=fast_window,
            slow_window=slow_window,
            exchange=exchange,
            screener=screener,
        ):
            available_time = _parse_aware_time(cached["available_time"], "available_time")
            age_seconds = (reference_now - available_time).total_seconds()
            if 0 <= age_seconds <= min_refresh_seconds:
                return _result_from_payload(cached, requested, requested_intervals)

        active_fetcher = fetcher or technicals.fetch_pair_technicals
        fetched, warnings = active_fetcher(
            fetch_symbols,
            intervals=fetch_intervals,
            fast_window=fast_window,
            slow_window=slow_window,
            exchange=exchange,
            screener=screener,
        )
        fetched_available_time = technicals.latest_observed_at(fetched)
        if fetched_available_time is None:
            fetched_available_time = fixed_now or datetime.now(UTC)
            for technical in fetched.values():
                technical.observed_at = fetched_available_time
        if cache_warning is not None:
            warnings = [cache_warning, *warnings]
        ingested_time = fixed_now or datetime.now(UTC)
        if ingested_time < fetched_available_time:
            ingested_time = fetched_available_time
        payload = _build_payload(
            fetched,
            warnings,
            symbols=fetch_symbols,
            intervals=fetch_intervals,
            fast_window=fast_window,
            slow_window=slow_window,
            exchange=exchange,
            screener=screener,
            available_time=fetched_available_time,
            ingested_time=ingested_time,
        )
        _atomic_write_payload(target, payload)
        return _result_from_payload(payload, requested, requested_intervals)


def load_pair_technicals(
    symbols: Sequence[str],
    intervals: Sequence[str] = technicals.DEFAULT_INTERVALS,
    fast_window: int = 20,
    slow_window: int = 100,
    exchange: str = technicals.DEFAULT_EXCHANGE,
    screener: str = technicals.DEFAULT_SCREENER,
    *,
    cache_path: str | Path = DEFAULT_CACHE_PATH,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
    now: datetime | None = None,
) -> tuple[dict[str, PairTechnicals], list[str]]:
    """検証済みの新鮮なcacheだけを読む。ネットワークfallbackは行わない。"""

    requested = _clean_symbols(symbols)
    requested_intervals = _clean_intervals(intervals)
    fixed_now = _aware_utc(now, "now") if now is not None else None
    if max_age_seconds <= 0:
        raise ValueError("max_age_seconds must be positive")
    target = Path(cache_path)
    try:
        with _cache_lock(target, exclusive=False):
            reference_now = fixed_now or datetime.now(UTC)
            payload = _load_verified_payload(target, reference_now)
        if payload is None:
            raise TechnicalCacheError(f"cache is missing: {target}")
        if not _is_compatible(
            payload,
            symbols=requested,
            intervals=requested_intervals,
            fast_window=fast_window,
            slow_window=slow_window,
            exchange=exchange,
            screener=screener,
        ):
            raise TechnicalCacheError("cache request contract does not cover this analysis")
        available_time = _parse_aware_time(payload["available_time"], "available_time")
        age_seconds = (reference_now - available_time).total_seconds()
        if age_seconds < 0:
            raise TechnicalCacheError("cache available_time is in the future")
        if age_seconds > max_age_seconds:
            raise TechnicalCacheError(
                f"cache is stale ({age_seconds:.1f}s > {max_age_seconds:.1f}s)"
            )
        return _result_from_payload(payload, requested, requested_intervals)
    except (OSError, TechnicalCacheError) as error:
        return _unavailable_result(
            requested,
            requested_intervals,
            fast_window,
            slow_window,
            f"TradingView共有キャッシュ利用不可: {error}",
        )


def _build_payload(
    technical_map: Mapping[str, PairTechnicals],
    warnings: Sequence[str],
    *,
    symbols: Sequence[str],
    intervals: Sequence[str],
    fast_window: int,
    slow_window: int,
    exchange: str,
    screener: str,
    available_time: datetime,
    ingested_time: datetime,
) -> dict[str, object]:
    available_time = _aware_utc(available_time, "available_time")
    ingested_time = _aware_utc(ingested_time, "ingested_time")
    if ingested_time < available_time:
        raise TechnicalCacheError("ingested_time precedes available_time")
    body: dict[str, object] = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "source": CACHE_SOURCE,
        "cache_contract": CACHE_CONTRACT,
        "source_event_time": None,
        "available_time": available_time.isoformat(),
        "ingested_time": ingested_time.isoformat(),
        "capture_slot": _capture_slot(available_time).isoformat(),
        "run_id": f"tradingview-cache-{available_time:%Y%m%dT%H%M%S%fZ}-{os.getpid()}",
        "writer_id": f"{socket.gethostname()}:{os.getpid()}",
        "exchange": exchange,
        "screener": screener,
        "fast_window": fast_window,
        "slow_window": slow_window,
        "symbols": list(symbols),
        "intervals": list(intervals),
        "warnings": [str(warning) for warning in warnings],
        "pairs": {
            symbol: {
                "symbol": technical.symbol,
                "transient_failures": list(technical.transient_failures),
                "views": {
                    interval: asdict(view) for interval, view in sorted(technical.views.items())
                },
            }
            for symbol, technical in sorted(technical_map.items())
        },
    }
    _validate_envelope(body)
    content_hash = _content_hash(body)
    return {**body, "content_hash": content_hash}


def _load_verified_payload(
    target: Path,
    reference_now: datetime,
) -> dict[str, object] | None:
    if not target.exists():
        return None
    try:
        raw = json.loads(target.read_text(encoding="utf-8"), parse_constant=_reject_constant)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError, OSError) as error:
        raise TechnicalCacheError(f"malformed cache at {target}") from error
    if not isinstance(raw, dict):
        raise TechnicalCacheError("cache root must be an object")
    stored_hash = raw.get("content_hash")
    body = {key: value for key, value in raw.items() if key != "content_hash"}
    if not isinstance(stored_hash, str) or stored_hash != _content_hash(body):
        raise TechnicalCacheError("cache content hash mismatch")
    if raw.get("schema_version") != CACHE_SCHEMA_VERSION:
        raise TechnicalCacheError("unsupported cache schema")
    if raw.get("source") != CACHE_SOURCE or raw.get("cache_contract") != CACHE_CONTRACT:
        raise TechnicalCacheError("unexpected cache source contract")
    if raw.get("source_event_time") is not None:
        raise TechnicalCacheError("scanner cache must not invent source_event_time")
    available_time = _parse_aware_time(raw.get("available_time"), "available_time")
    ingested_time = _parse_aware_time(raw.get("ingested_time"), "ingested_time")
    capture_slot = _parse_aware_time(raw.get("capture_slot"), "capture_slot")
    if ingested_time < available_time:
        raise TechnicalCacheError("cache ingested_time precedes available_time")
    if available_time > reference_now + FUTURE_SKEW_TOLERANCE:
        raise TechnicalCacheError("cache available_time is in the future")
    if capture_slot != _capture_slot(available_time):
        raise TechnicalCacheError("cache capture_slot does not match available_time")
    _validate_envelope(raw)
    return raw


def _validate_envelope(payload: Mapping[str, object]) -> None:
    symbols = payload.get("symbols")
    intervals = payload.get("intervals")
    warnings = payload.get("warnings")
    pairs = payload.get("pairs")
    if (
        not isinstance(symbols, list)
        or not symbols
        or not all(isinstance(value, str) and value for value in symbols)
        or len(set(symbols)) != len(symbols)
    ):
        raise TechnicalCacheError("cache symbols are invalid")
    if (
        not isinstance(intervals, list)
        or not intervals
        or not all(isinstance(value, str) and value for value in intervals)
        or len(set(intervals)) != len(intervals)
    ):
        raise TechnicalCacheError("cache intervals are invalid")
    if not isinstance(warnings, list) or not all(isinstance(value, str) for value in warnings):
        raise TechnicalCacheError("cache warnings are invalid")
    if not isinstance(pairs, dict) or set(pairs) != set(symbols):
        raise TechnicalCacheError("cache pair coverage is invalid")
    if not isinstance(payload.get("fast_window"), int) or not isinstance(
        payload.get("slow_window"), int
    ):
        raise TechnicalCacheError("cache MA windows are invalid")
    for symbol, raw_pair in pairs.items():
        _validate_pair(symbol, raw_pair, set(intervals))


def _validate_pair(symbol: object, raw_pair: object, intervals: set[str]) -> None:
    if not isinstance(symbol, str) or not isinstance(raw_pair, dict):
        raise TechnicalCacheError("cache pair entry is invalid")
    if raw_pair.get("symbol") != symbol:
        raise TechnicalCacheError(f"cache pair symbol mismatch: {symbol}")
    failures = raw_pair.get("transient_failures")
    views = raw_pair.get("views")
    if (
        not isinstance(failures, list)
        or not all(isinstance(value, str) and value in intervals for value in failures)
        or len(set(failures)) != len(failures)
        or not isinstance(views, dict)
    ):
        raise TechnicalCacheError(f"cache pair state is invalid: {symbol}")
    allowed_fields = {field.name for field in fields(IntervalView)}
    for interval, raw_view in views.items():
        if (
            not isinstance(interval, str)
            or interval not in intervals
            or not isinstance(raw_view, dict)
            or set(raw_view) != allowed_fields
            or raw_view.get("interval") != interval
        ):
            raise TechnicalCacheError(f"cache interval view is invalid: {symbol}:{interval}")
        try:
            view = IntervalView(**raw_view)
        except (TypeError, ValueError) as error:
            raise TechnicalCacheError(
                f"cache interval view cannot be reconstructed: {symbol}:{interval}"
            ) from error
        if (
            not isinstance(view.recommendation, str)
            or isinstance(view.buy, bool)
            or not isinstance(view.buy, int)
            or isinstance(view.sell, bool)
            or not isinstance(view.sell, int)
            or isinstance(view.neutral, bool)
            or not isinstance(view.neutral, int)
        ):
            raise TechnicalCacheError(f"cache interval rating is invalid: {symbol}:{interval}")
        _validate_view_numbers(symbol, view)


def _validate_view_numbers(symbol: str, view: IntervalView) -> None:
    for field_name in (
        "close",
        "open",
        "high",
        "low",
        "bid",
        "ask",
        "spread",
        "rsi",
        "macd",
        "macd_signal",
        "adx",
        "atr",
        "sma_fast",
        "sma_slow",
    ):
        value = getattr(view, field_name)
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise TechnicalCacheError(
                f"cache contains invalid numeric value: {symbol}:{view.interval}:{field_name}"
            )


def _is_compatible(
    payload: Mapping[str, object],
    *,
    symbols: Sequence[str],
    intervals: Sequence[str],
    fast_window: int,
    slow_window: int,
    exchange: str,
    screener: str,
) -> bool:
    cached_symbols = payload.get("symbols")
    cached_intervals = payload.get("intervals")
    return (
        isinstance(cached_symbols, list)
        and set(symbols).issubset(cached_symbols)
        and isinstance(cached_intervals, list)
        and set(intervals).issubset(cached_intervals)
        and payload.get("fast_window") == fast_window
        and payload.get("slow_window") == slow_window
        and payload.get("exchange") == exchange
        and payload.get("screener") == screener
    )


def _result_from_payload(
    payload: Mapping[str, object],
    symbols: Sequence[str],
    intervals: Sequence[str],
) -> tuple[dict[str, PairTechnicals], list[str]]:
    pairs = payload["pairs"]
    if not isinstance(pairs, Mapping):
        raise TechnicalCacheError("cache pairs are invalid")
    available_time = _parse_aware_time(payload["available_time"], "available_time")
    content_hash = payload.get("content_hash")
    fast_window = payload.get("fast_window")
    slow_window = payload.get("slow_window")
    if (
        not isinstance(content_hash, str)
        or not isinstance(fast_window, int)
        or not isinstance(slow_window, int)
    ):
        raise TechnicalCacheError("cache metadata is invalid")
    result: dict[str, PairTechnicals] = {}
    for symbol in symbols:
        raw_pair = pairs.get(symbol)
        if not isinstance(raw_pair, Mapping):
            raise TechnicalCacheError(f"cache pair is missing: {symbol}")
        raw_views = raw_pair.get("views")
        raw_failures = raw_pair.get("transient_failures")
        if not isinstance(raw_views, Mapping) or not isinstance(raw_failures, list):
            raise TechnicalCacheError(f"cache pair is invalid: {symbol}")
        result[symbol] = PairTechnicals(
            symbol=symbol,
            views={
                interval: IntervalView(**raw_view)
                for interval, raw_view in raw_views.items()
                if interval in intervals and isinstance(raw_view, dict)
            },
            fast_window=fast_window,
            slow_window=slow_window,
            observed_at=available_time,
            source_snapshot_hash=content_hash,
            transient_failures=[
                value for value in raw_failures if isinstance(value, str) and value in intervals
            ],
        )
    raw_warnings = payload.get("warnings")
    warnings = [str(value) for value in raw_warnings] if isinstance(raw_warnings, list) else []
    return result, warnings


def _unavailable_result(
    symbols: Sequence[str],
    intervals: Sequence[str],
    fast_window: int,
    slow_window: int,
    warning: str,
) -> tuple[dict[str, PairTechnicals], list[str]]:
    return (
        {
            symbol: PairTechnicals(
                symbol=symbol,
                fast_window=fast_window,
                slow_window=slow_window,
                transient_failures=list(intervals),
            )
            for symbol in symbols
        },
        [warning],
    )


@contextmanager
def _cache_lock(target: Path, *, exclusive: bool) -> Any:
    lock_path = target.with_name(f".{target.name}.lock")
    descriptor: int | None = None
    handle: IO[str] | None = None
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        os.chmod(lock_path, 0o600)
        handle = os.fdopen(descriptor, "a+", encoding="utf-8")
        descriptor = None
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
    except OSError as error:
        if handle is not None:
            handle.close()
        if descriptor is not None:
            os.close(descriptor)
        raise TechnicalCacheError(f"failed to acquire cache lock: {lock_path}") from error
    if handle is None:
        raise TechnicalCacheError(f"failed to acquire cache lock: {lock_path}")
    try:
        yield handle
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _atomic_write_payload(target: Path, payload: Mapping[str, object]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            os.fchmod(handle.fileno(), 0o600)
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, target)
        os.chmod(target, 0o600)
        directory_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except (OSError, TypeError, ValueError) as error:
        raise TechnicalCacheError(f"failed to write cache: {target}") from error
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _content_hash(payload: Mapping[str, object]) -> str:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise TechnicalCacheError("cache payload is not canonical JSON") from error
    return hashlib.sha256(encoded).hexdigest()


def _clean_symbols(symbols: Sequence[str]) -> tuple[str, ...]:
    cleaned = tuple(dict.fromkeys(value.upper().replace("/", "") for value in symbols))
    if not cleaned or any(not value for value in cleaned):
        raise ValueError("symbols must contain at least one non-empty symbol")
    return cleaned


def _clean_intervals(intervals: Sequence[str]) -> tuple[str, ...]:
    cleaned = tuple(dict.fromkeys(str(value).strip() for value in intervals))
    if not cleaned or any(not value for value in cleaned):
        raise ValueError("intervals must contain at least one non-empty interval")
    return cleaned


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _parse_aware_time(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise TechnicalCacheError(f"cache {field_name} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise TechnicalCacheError(f"cache {field_name} is invalid") from error
    if parsed.tzinfo is None:
        raise TechnicalCacheError(f"cache {field_name} must be timezone-aware")
    return parsed.astimezone(UTC)


def _capture_slot(value: datetime) -> datetime:
    value = _aware_utc(value, "capture_slot input")
    return value.replace(minute=(value.minute // 5) * 5, second=0, microsecond=0)


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")

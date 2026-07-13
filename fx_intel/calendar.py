"""経済指標カレンダーの取得とイベントリスク窓の判定。

ForexFactoryの公開フィード(faireconomy.media)から今週・来週の
経済イベントを取得し、fx_backtesterの経済指標CSVと同じ考え方で
「イベント前後の新規エントリー禁止窓」を判定する。

research_pack/research_max_config.json のプリセットと同じ既定値
(前120分・後180分、影響度medium以上)を使う。
"""

from __future__ import annotations

import csv
import fcntl
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, UTC
from pathlib import Path
from collections.abc import Iterable, Mapping, Sequence
from typing import TextIO

import requests

from .append_only import exclusive_sidecar_lock

CALENDAR_URLS = {
    "thisweek": "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "nextweek": "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
}

# fx_backtester.data.IMPACT_LEVELS と同じ序列
IMPACT_ORDER = {"low": 1, "medium": 2, "high": 3}

DEFAULT_MINUTES_BEFORE = 120
DEFAULT_MINUTES_AFTER = 180
DEFAULT_MIN_IMPACT = "medium"

IMPACT_JA = {"high": "高", "medium": "中", "low": "低", "holiday": "休場"}


@dataclass(frozen=True)
class EconomicEvent:
    title: str
    currency: str
    when: datetime  # UTC
    impact: str  # high / medium / low / holiday
    forecast: str = ""
    previous: str = ""
    occurrence_id: str = ""
    cancelled: bool = False

    @property
    def impact_rank(self) -> int:
        return IMPACT_ORDER.get(self.impact, 0)

    @property
    def impact_ja(self) -> str:
        return IMPACT_JA.get(self.impact, self.impact)


@dataclass(frozen=True)
class RiskWindow:
    """イベント前後の新規エントリー警戒窓。"""

    event: EconomicEvent
    start: datetime
    end: datetime

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment <= self.end


def symbol_currencies(symbol: str) -> tuple[str, str]:
    """ "USDJPY" → ("USD", "JPY")。"""
    cleaned = symbol.upper().replace("/", "").strip()
    if len(cleaned) != 6:
        raise ValueError(f"通貨ペア名を解釈できません: {symbol}")
    return cleaned[:3], cleaned[3:]


def parse_calendar_json(raw: Iterable[dict]) -> list[EconomicEvent]:
    """ForexFactoryフィードのJSON配列をEconomicEventに変換する。"""
    events: list[EconomicEvent] = []
    for item in raw:
        date_text = str(item.get("date", "")).strip()
        if not date_text:
            continue
        try:
            when = datetime.fromisoformat(date_text)
        except ValueError:
            continue
        if when.tzinfo is None or when.utcoffset() is None:
            continue
        events.append(
            EconomicEvent(
                title=str(item.get("title", "")).strip(),
                currency=str(item.get("country", "")).strip().upper(),
                when=when.astimezone(UTC),
                impact=str(item.get("impact", "")).strip().lower(),
                forecast=str(item.get("forecast", "")).strip(),
                previous=str(item.get("previous", "")).strip(),
                occurrence_id=str(
                    item.get("source_record_id") or item.get("event_id") or item.get("id") or ""
                ).strip(),
                cancelled=bool(item.get("cancelled", False)),
            )
        )
    events.sort(key=lambda e: e.when)
    return events


def _load_cache(cache_path: Path | None) -> dict | None:
    if cache_path is None or not cache_path.exists():
        return None
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        fetched_at = datetime.fromisoformat(cache["fetched_at"])
        if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
            return None
        return cache if isinstance(cache.get("weeks"), dict) else None
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def _cache_age_minutes(cache: dict, now: datetime) -> float:
    fetched_at = datetime.fromisoformat(cache["fetched_at"])
    if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        raise ValueError("calendar cache fetched_at must be timezone-aware")
    return (now - fetched_at).total_seconds() / 60.0


def _calendar_payload_issues(weeks_raw: Mapping[str, list]) -> list[str]:
    issues: list[str] = []
    for week, rows in weeks_raw.items():
        if not isinstance(rows, list):
            issues.append(f"{week}:payload_not_list")
            continue
        # Forex Factory's current-week feed normally contains the scheduled
        # releases for the week.  Treating an empty current week as a successful
        # partial response would disable every blackout while a populated
        # next-week feed keeps ``bool(events)`` true in the caller.
        if week == "thisweek" and not rows:
            issues.append("thisweek:payload_empty")
            continue
        for position, item in enumerate(rows):
            if not isinstance(item, Mapping):
                issues.append(f"{week}:{position}:row_not_object")
                continue
            raw = item.get("date")
            if not isinstance(raw, str) or not raw.strip():
                issues.append(f"{week}:{position}:date_missing")
                continue
            try:
                parsed = datetime.fromisoformat(raw)
            except ValueError:
                issues.append(f"{week}:{position}:date_invalid")
                continue
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                issues.append(f"{week}:{position}:date_naive")
    return issues


def _merge_weeks(weeks_raw: Mapping[str, list]) -> list[EconomicEvent]:
    merged: list[EconomicEvent] = []
    seen: set[tuple[str, str, datetime]] = set()
    for raw in weeks_raw.values():
        for event in parse_calendar_json(raw):
            key = (event.title, event.currency, event.when)
            if key not in seen:
                seen.add(key)
                merged.append(event)
    merged.sort(key=lambda e: e.when)
    return merged


def fetch_calendar(
    weeks: Sequence[str] = ("thisweek", "nextweek"),
    timeout: float = 15.0,
    session: requests.Session | None = None,
    cache_path: str | Path | None = None,
    cache_max_age_minutes: float = 45.0,
    now: datetime | None = None,
) -> tuple[list[EconomicEvent], list[str]]:
    """今週(および来週)の経済イベントを取得する。

    フィードはレート制限(429)がかかるため、cache_path を渡すと
    成功時にキャッシュし、キャッシュが新鮮な間はネットワークを叩かず、
    取得失敗時も期限切れ・未来・時刻不明キャッシュは使用しない。
    nextweekフィードは週の前半は未公開(404)のことがあるため、
    週単位の失敗は警告として返し、取得できた分で継続する。
    戻り値は (イベント一覧, 警告一覧)。
    """
    observed_at = now or datetime.now(UTC)
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("calendar now must be timezone-aware")
    observed_at = observed_at.astimezone(UTC)
    cache_file = Path(cache_path) if cache_path else None
    cache = _load_cache(cache_file)
    warnings: list[str] = []
    if cache is not None:
        age = _cache_age_minutes(cache, observed_at)
        missing_weeks = sorted(set(weeks) - set(cache["weeks"]))
        issues = _calendar_payload_issues(cache["weeks"])
        if age < 0:
            warnings.append("経済指標カレンダーの未来時刻キャッシュを拒否")
            cache = None
        elif issues:
            warnings.append("経済指標カレンダーキャッシュ品質違反: " + ",".join(issues[:3]))
            cache = None
        elif age <= cache_max_age_minutes:
            if missing_weeks:
                warnings.append(
                    "経済指標カレンダー部分キャッシュ: missing=" + ",".join(missing_weeks)
                )
            return _merge_weeks(cache["weeks"]), warnings

    http = session or requests
    fetched: dict[str, list] = {}
    for week in weeks:
        url = CALENDAR_URLS[week]
        try:
            response = http.get(url, timeout=timeout)
            if week != "thisweek" and response.status_code == 404:
                warnings.append(f"経済指標カレンダー({week})未公開のため部分取得")
                continue
            response.raise_for_status()
            payload = json.loads(response.text)
            if not isinstance(payload, list):
                raise ValueError("calendar payload must be a list")
            issues = _calendar_payload_issues({week: payload})
            if issues:
                raise ValueError("calendar payload timestamp invalid: " + ",".join(issues[:3]))
            fetched[week] = payload
        except Exception as error:  # noqa: BLE001 - 外部フィード起因
            warnings.append(f"経済指標カレンダー({week})取得失敗: {error}")

    if fetched:
        missing_weeks = sorted(set(weeks) - set(fetched))
        if missing_weeks and not any("部分取得" in warning for warning in warnings):
            warnings.append("経済指標カレンダー部分取得: missing=" + ",".join(missing_weeks))
        if cache_file is not None:
            try:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(
                    json.dumps({"fetched_at": observed_at.isoformat(), "weeks": fetched}),
                    encoding="utf-8",
                )
            except OSError as error:
                warnings.append(f"カレンダーキャッシュ保存失敗: {error}")
        return _merge_weeks(fetched), warnings

    if cache is not None:
        age = round(_cache_age_minutes(cache, observed_at))
        warnings.append(f"カレンダー取得失敗かつ{age}分前の期限切れキャッシュを拒否")
    return [], warnings


def upcoming_events(
    events: Iterable[EconomicEvent],
    currencies: set[str] | None,
    now: datetime,
    hours_ahead: float = 48.0,
    min_impact: str = "low",
) -> list[EconomicEvent]:
    """now以降 hours_ahead 時間以内の対象通貨イベントを返す。"""
    horizon = now + timedelta(hours=hours_ahead)
    min_rank = IMPACT_ORDER.get(min_impact, 1)
    selected = [
        event
        for event in events
        if now <= event.when <= horizon
        and event.impact_rank >= min_rank
        and (currencies is None or event.currency in currencies)
    ]
    selected.sort(key=lambda e: e.when)
    return selected


def risk_windows(
    events: Iterable[EconomicEvent],
    currencies: set[str],
    minutes_before: int = DEFAULT_MINUTES_BEFORE,
    minutes_after: int = DEFAULT_MINUTES_AFTER,
    min_impact: str = DEFAULT_MIN_IMPACT,
) -> list[RiskWindow]:
    """対象通貨のイベント前後の警戒窓を返す。"""
    min_rank = IMPACT_ORDER.get(min_impact, 2)
    windows = [
        RiskWindow(
            event=event,
            start=event.when - timedelta(minutes=minutes_before),
            end=event.when + timedelta(minutes=minutes_after),
        )
        for event in events
        if event.impact_rank >= min_rank and event.currency in currencies
    ]
    windows.sort(key=lambda w: w.start)
    return windows


def active_and_next_window(
    windows: Sequence[RiskWindow], now: datetime
) -> tuple[RiskWindow | None, RiskWindow | None]:
    """現在アクティブな窓と、次に来る窓を返す。"""
    active = None
    upcoming = None
    for window in windows:
        if window.contains(now):
            if active is None or window.event.impact_rank > active.event.impact_rank:
                active = window
        elif window.start > now and upcoming is None:
            upcoming = window
    return active, upcoming


# research_pack/major_fx_events.csv と同じ列構成（fx_backtester の --events が読む形式）
EVENT_CSV_COLUMNS = [
    "timestamp",
    "currency",
    "symbol",
    "impact",
    "name",
    "category",
    "source_url",
    "notes",
]
# Append-only revision metadata.  ``effective_to`` is normally blank: the next
# visible revision closes the prior half-open interval during replay.
ARCHIVE_COLUMNS = [
    *EVENT_CSV_COLUMNS,
    "occurrence_id",
    "revision",
    "effective_from",
    "effective_to",
    "is_tombstone",
    "identity_quality",
    "recorded_at",
]


class CalendarArchiveSchemaError(OSError):
    """Raised when appending would corrupt or silently reinterpret an archive."""


def _event_csv_row(event: EconomicEvent) -> dict[str, str]:
    if event.when.tzinfo is None or event.when.utcoffset() is None:
        raise ValueError("economic event timestamp must be timezone-aware")
    return {
        "timestamp": event.when.astimezone(UTC).isoformat(),
        "currency": event.currency,
        "symbol": "",
        "impact": event.impact,
        "name": event.title,
        "category": "economic_calendar",
        "source_url": "https://www.forexfactory.com/calendar",
        "notes": f"forecast={event.forecast} previous={event.previous}".strip(),
    }


def _archive_key(row: Mapping[str, object]) -> tuple[str, ...]:
    """Content identity within one stable occurrence."""

    return tuple(
        str(row.get(field) or "").strip() for field in (*EVENT_CSV_COLUMNS, "is_tombstone")
    )


def _heuristic_occurrence_id(row: Mapping[str, object]) -> str:
    payload = "\x00".join(
        str(row.get(field) or "").strip().casefold() for field in ("currency", "name", "timestamp")
    )
    return "heuristic:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _matching_heuristic_occurrence(
    row: Mapping[str, object],
    latest: Mapping[str, Mapping[str, str]],
) -> str | None:
    """Reuse a local identity only when one nearby occurrence is unambiguous."""

    name = str(row.get("name") or "").strip().casefold()
    currency = str(row.get("currency") or "").strip().upper()
    timestamp = datetime.fromisoformat(str(row["timestamp"]))
    candidates: list[tuple[float, str]] = []
    for occurrence_id, prior in latest.items():
        if prior.get("identity_quality") != "heuristic":
            continue
        if prior.get("name", "").strip().casefold() != name:
            continue
        if prior.get("currency", "").strip().upper() != currency:
            continue
        prior_timestamp = datetime.fromisoformat(prior["timestamp"])
        distance = abs((timestamp - prior_timestamp).total_seconds())
        if distance <= timedelta(days=2).total_seconds():
            candidates.append((distance, occurrence_id))
    if not candidates:
        return None
    candidates.sort()
    if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
        return None
    return candidates[0][1]


def _validated_archive_rows(
    handle: TextIO,
) -> tuple[list[dict[str, str]], bool]:
    handle.seek(0)
    reader = csv.DictReader(handle)
    if reader.fieldnames is None:
        return [], False
    if reader.fieldnames != ARCHIVE_COLUMNS:
        raise CalendarArchiveSchemaError(
            "event archive has a legacy or unknown schema; preserve it and start a fresh "
            "PIT revision archive before appending"
        )
    rows = list(reader)
    latest: dict[str, int] = {}
    last_effective: dict[str, datetime] = {}
    last_recorded: dict[str, datetime] = {}
    archive_last_recorded: datetime | None = None
    seen: set[tuple[str, int]] = set()
    for position, row in enumerate(rows, start=2):
        occurrence_id = row.get("occurrence_id", "").strip()
        if not occurrence_id:
            raise CalendarArchiveSchemaError(f"event archive row {position} has no occurrence_id")
        try:
            revision = int(row.get("revision", ""))
            effective = datetime.fromisoformat(row.get("effective_from", ""))
            recorded = datetime.fromisoformat(row.get("recorded_at", ""))
        except ValueError as error:
            raise CalendarArchiveSchemaError(
                f"event archive row {position} has invalid revision metadata"
            ) from error
        if revision <= 0 or (occurrence_id, revision) in seen:
            raise CalendarArchiveSchemaError(
                f"event archive row {position} has duplicate/invalid revision"
            )
        if (
            effective.tzinfo is None
            or effective.utcoffset() is None
            or recorded.tzinfo is None
            or recorded.utcoffset() is None
            or effective < recorded
        ):
            raise CalendarArchiveSchemaError(
                f"event archive row {position} has invalid effective/recorded time"
            )
        expected = latest.get(occurrence_id, 0) + 1
        if revision != expected or (
            occurrence_id in last_effective and effective <= last_effective[occurrence_id]
        ):
            raise CalendarArchiveSchemaError(
                f"event archive row {position} has a non-contiguous revision chain"
            )
        if occurrence_id in last_recorded and recorded <= last_recorded[occurrence_id]:
            raise CalendarArchiveSchemaError(
                f"event archive row {position} has non-increasing recorded_at"
            )
        if archive_last_recorded is not None and recorded < archive_last_recorded:
            raise CalendarArchiveSchemaError(
                f"event archive row {position} has globally decreasing recorded_at"
            )
        if row.get("identity_quality") not in {"source", "heuristic"}:
            raise CalendarArchiveSchemaError(
                f"event archive row {position} has invalid identity_quality"
            )
        if row.get("is_tombstone", "").lower() not in {"true", "false"}:
            raise CalendarArchiveSchemaError(
                f"event archive row {position} has invalid tombstone state"
            )
        latest[occurrence_id] = revision
        last_effective[occurrence_id] = effective
        last_recorded[occurrence_id] = recorded
        archive_last_recorded = recorded
        seen.add((occurrence_id, revision))
    return rows, True


def _validate_archive_revision_contract(path: Path, rows: Sequence[Mapping[str, str]]) -> None:
    """Apply the backtester's complete PIT contract before mutating an archive."""

    if not rows:
        return
    as_of = max(datetime.fromisoformat(row["recorded_at"]) for row in rows)
    try:
        from fx_backtester.data import load_economic_events_csv

        load_economic_events_csv(
            path,
            as_of=as_of,
            require_point_in_time=True,
        )
    except (OSError, TypeError, ValueError) as error:
        raise CalendarArchiveSchemaError(
            f"event archive fails the shared PIT revision contract: {error}"
        ) from error


def export_events_csv(events: Iterable[EconomicEvent], path: str | Path) -> Path:
    """fx_backtesterの --events で読める形式でCSVに書き出す（毎回上書きのスナップショット）。

    research_pack/major_fx_events.csv と同じ列構成にして、
    バックテストとライブ運用が同じイベントデータを共有できるようにする。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=EVENT_CSV_COLUMNS)
        writer.writeheader()
        for event in events:
            writer.writerow(_event_csv_row(event))
    return target


def append_events_archive(
    events: Iterable[EconomicEvent],
    path: str | Path,
    now: datetime | None = None,
) -> tuple[Path, int]:
    """イベントを重複排除つきで追記し、カレンダー履歴を蓄積する。(path, 追記件数) を返す。

    upcoming_events.csv（毎回上書きのスナップショット）と違い、実行のたびに
    各版を stable occurrence_id、連番revision、effective_from、tombstone、
    recorded_at とともに残す。source IDがない場合は近接した同名イベントから
    heuristic IDを再利用するが、その履歴はpromotion不適格として扱われる。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    observed_at = now or datetime.now(UTC)
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("calendar archive timestamp must be timezone-aware")
    recorded_at = observed_at.astimezone(UTC).isoformat()

    appended = 0
    with exclusive_sidecar_lock(target):
        with target.open("a+", encoding="utf-8", newline="") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                rows, has_header = _validated_archive_rows(handle)
                if has_header:
                    _validate_archive_revision_contract(target, rows)
                existing_recorded = [
                    datetime.fromisoformat(row["recorded_at"]).astimezone(UTC) for row in rows
                ]
                if existing_recorded and observed_at.astimezone(UTC) < max(existing_recorded):
                    raise CalendarArchiveSchemaError(
                        "event archive observation time cannot precede the latest recorded_at"
                    )
                latest = {
                    occurrence_id: max(
                        (row for row in rows if row["occurrence_id"] == occurrence_id),
                        key=lambda row: int(row["revision"]),
                    )
                    for occurrence_id in {row["occurrence_id"] for row in rows}
                }
                handle.seek(0, os.SEEK_END)
                writer = csv.DictWriter(handle, fieldnames=ARCHIVE_COLUMNS)
                if not has_header:
                    writer.writeheader()
                for event in events:
                    row = _event_csv_row(event)
                    supplied_id = event.occurrence_id.strip()
                    occurrence_id = supplied_id or _matching_heuristic_occurrence(row, latest)
                    occurrence_id = occurrence_id or _heuristic_occurrence_id(row)
                    prior = latest.get(occurrence_id)
                    candidate = {**row, "is_tombstone": str(bool(event.cancelled))}
                    if prior is not None and _archive_key(prior) == _archive_key(candidate):
                        continue
                    revision = int(prior["revision"]) + 1 if prior is not None else 1
                    if prior is not None:
                        prior_effective = datetime.fromisoformat(prior["effective_from"])
                        if observed_at.astimezone(UTC) <= prior_effective:
                            raise CalendarArchiveSchemaError(
                                "event revision observation time must strictly increase"
                            )
                    archived = {
                        **row,
                        "occurrence_id": occurrence_id,
                        "revision": str(revision),
                        "effective_from": recorded_at,
                        "effective_to": "",
                        "is_tombstone": str(bool(event.cancelled)),
                        "identity_quality": "source" if supplied_id else "heuristic",
                        "recorded_at": recorded_at,
                    }
                    writer.writerow(archived)
                    latest[occurrence_id] = archived
                    appended += 1
                if appended or not has_header:
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return target, appended

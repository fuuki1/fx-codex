"""経済指標カレンダーの取得とイベントリスク窓の判定。

ForexFactoryの公開フィード(faireconomy.media)から今週・来週の
経済イベントを取得し、fx_backtesterの経済指標CSVと同じ考え方で
「イベント前後の新規エントリー禁止窓」を判定する。

research_pack/research_max_config.json のプリセットと同じ既定値
(前120分・後180分、影響度medium以上)を使う。
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, UTC
from pathlib import Path
from collections.abc import Iterable, Mapping, Sequence

import requests

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
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        events.append(
            EconomicEvent(
                title=str(item.get("title", "")).strip(),
                currency=str(item.get("country", "")).strip().upper(),
                when=when.astimezone(UTC),
                impact=str(item.get("impact", "")).strip().lower(),
                forecast=str(item.get("forecast", "")).strip(),
                previous=str(item.get("previous", "")).strip(),
            )
        )
    events.sort(key=lambda e: e.when)
    return events


def _load_cache(cache_path: Path | None) -> dict | None:
    if cache_path is None or not cache_path.exists():
        return None
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        datetime.fromisoformat(cache["fetched_at"])  # 形式検証
        return cache if isinstance(cache.get("weeks"), dict) else None
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def _cache_age_minutes(cache: dict, now: datetime) -> float:
    fetched_at = datetime.fromisoformat(cache["fetched_at"])
    return (now - fetched_at).total_seconds() / 60.0


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
) -> tuple[list[EconomicEvent], list[str]]:
    """今週(および来週)の経済イベントを取得する。

    フィードはレート制限(429)がかかるため、cache_path を渡すと
    成功時にキャッシュし、キャッシュが新鮮な間はネットワークを叩かず、
    取得失敗時は古いキャッシュでも代用する。
    nextweekフィードは週の前半は未公開(404)のことがあるため、
    週単位の失敗は警告として返し、取得できた分で継続する。
    戻り値は (イベント一覧, 警告一覧)。
    """
    cache_file = Path(cache_path) if cache_path else None
    cache = _load_cache(cache_file)
    now = datetime.now(UTC)
    if cache is not None and _cache_age_minutes(cache, now) <= cache_max_age_minutes:
        return _merge_weeks(cache["weeks"]), []

    http = session or requests
    warnings: list[str] = []
    fetched: dict[str, list] = {}
    for week in weeks:
        url = CALENDAR_URLS[week]
        try:
            response = http.get(url, timeout=timeout)
            if week != "thisweek" and response.status_code == 404:
                # nextweekは週の前半は未公開のことが多いので警告扱いにしない
                continue
            response.raise_for_status()
            fetched[week] = json.loads(response.text)
        except Exception as error:  # noqa: BLE001 - 外部フィード起因
            warnings.append(f"経済指標カレンダー({week})取得失敗: {error}")

    if fetched:
        if cache_file is not None:
            try:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(
                    json.dumps({"fetched_at": now.isoformat(), "weeks": fetched}),
                    encoding="utf-8",
                )
            except OSError as error:
                warnings.append(f"カレンダーキャッシュ保存失敗: {error}")
        return _merge_weeks(fetched), warnings

    if cache is not None:
        age = round(_cache_age_minutes(cache, now))
        warnings.append(f"カレンダー取得失敗のため{age}分前のキャッシュを使用")
        return _merge_weeks(cache["weeks"]), warnings
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
# 追記アーカイブは「いつ観測したか」を持つ。fx_backtester 側は未知の列を無視するので互換
ARCHIVE_COLUMNS = [*EVENT_CSV_COLUMNS, "recorded_at"]


def _event_csv_row(event: EconomicEvent) -> dict[str, str]:
    return {
        "timestamp": event.when.strftime("%Y-%m-%d %H:%M:%S"),
        "currency": event.currency,
        "symbol": "",
        "impact": event.impact,
        "name": event.title,
        "category": "economic_calendar",
        "source_url": "https://www.forexfactory.com/calendar",
        "notes": f"forecast={event.forecast} previous={event.previous}".strip(),
    }


def _archive_key(row: Mapping[str, str | None]) -> tuple[str, str, str, str, str]:
    """内容ベースの重複判定キー。時刻改定や forecast/previous の更新は別内容として扱う。"""
    return tuple(str(row.get(field) or "").strip() for field in ("timestamp", "currency", "name", "impact", "notes"))  # type: ignore[return-value]


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
    未観測のイベントだけが recorded_at 付きで追記される point-in-time 記録。
    同一イベントでも時刻・impact・forecast/previous が改定されたら別行として残し、
    「いつ・どの内容で観測したか」を再構成できるようにする。

    fx_backtester の --events はこのファイルをそのまま読める
    （recorded_at 列は無視され、同一イベントの改定行はマスク上同じ窓に畳まれる）。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    recorded_at = (now or datetime.now(UTC)).strftime("%Y-%m-%d %H:%M:%S")

    seen: set[tuple[str, str, str, str, str]] = set()
    has_header = target.exists() and target.stat().st_size > 0
    if has_header:
        with target.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                seen.add(_archive_key(row))

    appended = 0
    with target.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ARCHIVE_COLUMNS)
        if not has_header:
            writer.writeheader()
        for event in events:
            row = _event_csv_row(event)
            key = _archive_key(row)
            if key in seen:
                continue
            seen.add(key)
            row["recorded_at"] = recorded_at
            writer.writerow(row)
            appended += 1
    return target, appended

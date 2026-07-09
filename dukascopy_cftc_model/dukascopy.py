"""Dukascopy公開データフィードから tick を取得し、時間足OHLCVへ集計する。

Dukascopyは時間ごとに LZMA 圧縮された tick バイナリ(.bi5)を公開している:

    https://datafeed.dukascopy.com/datafeed/{SYMBOL}/{YYYY}/{MM0}/{DD}/{HH}h_ticks.bi5

- {MM0} は **0始まりの月**(1月=00, 12月=11)。ここが最頻の落とし穴。
- 各 tick は 20 バイト big-endian: `>iiiff`
    ms_offset(その時間内のミリ秒), ask_int, bid_int, ask_volume, bid_volume
  価格は整数で、point値で割って実価格に戻す(EURUSD等=1e5、JPYクロス=1e3)。
  point値は fx_backtester.models.instrument_for の pip_size から導く
  (pip_size=0.0001 → 1e5、pip_size=0.01 → 1e3。tickはpipの1/10刻み)。
- 空の時間(週末・祝日・流動性ゼロ)は 0 バイト or 404。ミッションクリティカル
  設計として「その時間はバー無し」で静かにスキップし、致命的にしない。

ミッションクリティカル設計:

- decode_bi5 はネットワーク非依存の純粋関数。テストはフィクスチャ
  (自作の tick バイト列)で完結する。
- TTLキャッシュ(1時間ぶん = 1ファイル)を logs/dcm_cache/duka/ に持ち、
  再取得を避ける。取得失敗はキャッシュ fallback に落ちる。
- mid価格(=(bid+ask)/2)で集計する。約定はbid/askだが、特徴量・ラベルには
  mid が素直(スプレッドはバックテストのコストモデル側で別途扱える)。
"""

from __future__ import annotations

import lzma
import struct
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, UTC
from pathlib import Path

import pandas as pd
import requests

from fx_backtester.models import instrument_for, normalize_symbol

DATAFEED_URL = (
    "https://datafeed.dukascopy.com/datafeed/{symbol}/{year:04d}/{month0:02d}/"
    "{day:02d}/{hour:02d}h_ticks.bi5"
)
USER_AGENT = "Mozilla/5.0 (compatible; fx-codex-dcm/1.0)"
TICK_STRUCT = struct.Struct(">iiiff")  # ms, ask_int, bid_int, ask_vol, bid_vol
TICK_SIZE = TICK_STRUCT.size  # 20 bytes

FETCH_ATTEMPTS = 3
FETCH_RETRY_WAIT_SECONDS = 1.0
FETCH_TIMEOUT_SECONDS = 30.0

# 集計する時間足 → pandas resample ルール
TIMEFRAME_RULES: dict[str, str] = {
    "H1": "1h",
    "H4": "4h",
    "D1": "1D",
}


@dataclass(frozen=True)
class Tick:
    """1 tick(UTC時刻・bid・ask・出来高)。"""

    when: datetime
    bid: float
    ask: float
    bid_volume: float
    ask_volume: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0


def point_value(symbol: str) -> float:
    """整数価格を実価格に戻す除数。pip=価格1/100とし、tickはその1/10。

    pip_size=0.0001(非JPY) → point=1e5、pip_size=0.01(JPY) → 1e3。
    """
    inst = instrument_for(symbol)
    # pip_size = 0.0001 → 1/pip_size = 10000、tick(1/10 pip) → ×10 = 1e5
    return round(1.0 / inst.pip_size * 10.0)


def decode_bi5(raw: bytes, hour_start: datetime, symbol: str) -> list[Tick]:
    """LZMA展開済みの生バイト列を Tick のリストへ復号する(純粋関数)。

    hour_start はその時間の先頭(UTC・分秒0)。各 tick の ms_offset を足して
    絶対時刻にする。長さが 20 の倍数でない末尾は無視する(壊れたブロック対策)。
    """
    if hour_start.tzinfo is None:
        hour_start = hour_start.replace(tzinfo=UTC)
    divisor = point_value(symbol)
    ticks: list[Tick] = []
    count = len(raw) // TICK_SIZE
    for i in range(count):
        ms, ask_int, bid_int, ask_vol, bid_vol = TICK_STRUCT.unpack_from(raw, i * TICK_SIZE)
        when = hour_start + timedelta(milliseconds=ms)
        ticks.append(
            Tick(
                when=when,
                bid=bid_int / divisor,
                ask=ask_int / divisor,
                bid_volume=float(bid_vol),
                ask_volume=float(ask_vol),
            )
        )
    return ticks


def decompress_bi5(compressed: bytes) -> bytes:
    """.bi5 の LZMA を展開。空(週末等)は空バイト列を返す。"""
    if not compressed:
        return b""
    return lzma.decompress(compressed)


# ---------------------------------------------------------------- キャッシュ付き取得


def _hour_cache_path(cache_dir: Path, symbol: str, hour_start: datetime) -> Path:
    return cache_dir / "duka" / symbol / f"{hour_start:%Y%m%d_%H}.bi5"


# _fetch_hour_raw の結果。ok=False は「その時間の取得に失敗(リトライ枯渇)」で、
# 空バイト列(=データ無しの時間)とは区別する。ミッションクリティカル設計として、
# 1時間の一時的失敗で全体を落とさず、失敗はカウントして呼び出し側に返す。
@dataclass(frozen=True)
class _HourOutcome:
    ok: bool
    raw: bytes  # ok=True のときのみ意味を持つ(空時間は ok=True, raw=b'')


def _fetch_hour_raw(
    symbol: str,
    hour_start: datetime,
    cache_dir: Path,
    session: requests.Session | None,
) -> _HourOutcome:
    """1時間ぶんの生 .bi5 バイト列を取得(キャッシュ優先)。

    - キャッシュ命中 / 200 / 404(空時間) → ok=True
    - ネットワーク失敗・429・5xx がリトライ枯渇 → ok=False(スキップ扱い)
    """
    cache_path = _hour_cache_path(cache_dir, symbol, hour_start)
    if cache_path.exists():
        return _HourOutcome(ok=True, raw=cache_path.read_bytes())

    url = DATAFEED_URL.format(
        symbol=symbol,
        year=hour_start.year,
        month0=hour_start.month - 1,  # 0始まりの月
        day=hour_start.day,
        hour=hour_start.hour,
    )
    http = session or requests
    for attempt in range(FETCH_ATTEMPTS):
        try:
            resp = http.get(url, timeout=FETCH_TIMEOUT_SECONDS, headers={"User-Agent": USER_AGENT})
        except requests.RequestException:  # ネットワーク層の失敗はリトライ
            time.sleep(FETCH_RETRY_WAIT_SECONDS * (attempt + 1))
            continue
        if resp.status_code == 404:
            # その時間はデータ無し(週末・上場前など)。空として確定キャッシュ。
            _write_cache(cache_path, b"")
            return _HourOutcome(ok=True, raw=b"")
        if resp.status_code == 200:
            _write_cache(cache_path, resp.content)
            return _HourOutcome(ok=True, raw=resp.content)
        # 429/5xx は待って再試行
        time.sleep(FETCH_RETRY_WAIT_SECONDS * (attempt + 1))
    # リトライ枯渇 → この時間はスキップ(キャッシュしない=次回再取得できる)
    return _HourOutcome(ok=False, raw=b"")


def _write_cache(path: Path, content: bytes) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    except OSError:
        pass  # キャッシュ書き込み失敗は致命的でない(次回再取得になるだけ)


def _iter_hours(start: datetime, end: datetime) -> Iterator[datetime]:
    cursor = start.replace(minute=0, second=0, microsecond=0, tzinfo=UTC)
    while cursor <= end:
        yield cursor
        cursor += timedelta(hours=1)


DEFAULT_MAX_WORKERS = 8  # 時間ごとの取得は独立I/O。並列で桁違いに速くなる。


def _build_session() -> requests.Session:
    """接続プールを持つ Session(HTTP keep-alive で往復を減らす)。"""
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=DEFAULT_MAX_WORKERS,
        pool_maxsize=DEFAULT_MAX_WORKERS,
        max_retries=0,  # リトライは _fetch_hour_raw 側で明示制御
    )
    session.mount("https://", adapter)
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def fetch_ticks(
    symbol: str,
    start: datetime,
    end: datetime,
    cache_dir: Path,
    session: requests.Session | None = None,
    progress: bool = False,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> list[Tick]:
    """[start, end] を1時間ずつ取得・復号して tick を連結する。

    時間ごとの取得は独立なので ThreadPoolExecutor で並列化する(I/Oバウンド)。
    一部の時間の取得に失敗しても全体は落とさない(スキップして継続)。
    復号後は必ず時刻昇順に整列する(並列で順序が乱れるため)。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    symbol = normalize_symbol(symbol)
    hours = list(_iter_hours(start, end))
    total = len(hours)
    owns_session = session is None
    session = session or _build_session()

    all_ticks: list[Tick] = []
    failed = 0
    done = 0
    try:
        workers = max(1, min(max_workers, total)) if total else 1
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_fetch_hour_raw, symbol, hour, cache_dir, session): hour
                for hour in hours
            }
            for fut in as_completed(futures):
                hour = futures[fut]
                outcome = fut.result()
                if not outcome.ok:
                    failed += 1
                elif outcome.raw:
                    all_ticks.extend(decode_bi5(decompress_bi5(outcome.raw), hour, symbol))
                done += 1
                if progress and (done % 1000 == 0 or done == total):
                    print(
                        f"  duka {symbol}: {done}/{total} hours, "
                        f"{len(all_ticks)} ticks, {failed} failed",
                        flush=True,
                    )
    finally:
        if owns_session:
            session.close()

    all_ticks.sort(key=lambda t: t.when)  # 並列取得で乱れた順序を整える
    if failed and failed / max(1, total) > 0.05:
        print(
            f"  ⚠ duka {symbol}: {failed}/{total} 時間の取得に失敗しました"
            "(再実行でキャッシュ未取得ぶんを埋められます)",
            flush=True,
        )
    return all_ticks


def ticks_to_ohlcv(ticks: list[Tick], timeframe: str) -> pd.DataFrame:
    """tick を指定時間足の mid-OHLCV へ集計する。

    volume は tick 件数(Dukascopyの生出来高は名目値でスケールが不定のため、
    件数のほうが安定した「活動量」の代理になる)。
    """
    rule = TIMEFRAME_RULES.get(timeframe.upper())
    if rule is None:
        raise ValueError(f"未対応の時間足: {timeframe}(対応: {sorted(TIMEFRAME_RULES)})")
    if not ticks:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"]).rename_axis(
            "timestamp"
        )

    frame = pd.DataFrame(
        {"timestamp": [t.when for t in ticks], "mid": [t.mid for t in ticks]}
    ).set_index("timestamp")
    frame.index = pd.DatetimeIndex(frame.index)
    ohlc = frame["mid"].resample(rule, label="left", closed="left").ohlc()
    volume = frame["mid"].resample(rule, label="left", closed="left").count()
    ohlc["volume"] = volume
    ohlc = ohlc.dropna(subset=["open", "high", "low", "close"])
    ohlc.index.name = "timestamp"
    return ohlc


def _iter_days(start: datetime, end: datetime) -> Iterator[tuple[datetime, datetime]]:
    """[start, end] を1日(UTC)ごとの (day_start, day_end) 区間に分割する。"""
    day = start.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=UTC)
    last = end.replace(tzinfo=UTC)
    while day <= last:
        day_end = day + timedelta(hours=23)
        yield (max(day, start.replace(tzinfo=UTC)), min(day_end, last))
        day += timedelta(days=1)


def fetch_prices(
    symbol: str,
    start: datetime,
    end: datetime,
    timeframe: str,
    cache_dir: Path,
    session: requests.Session | None = None,
    progress: bool = False,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> pd.DataFrame:
    """Dukascopy tick を取得し、指定時間足のOHLCV DataFrame を返す。

    メモリ効率のため**1日ずつ**取得→集計し、rawなtickは日ごとに破棄する
    (数年ぶんのtickを全部メモリに載せない)。H1/H4/D1 の足境界は日をまたがない
    ので、日単位で集計して連結しても結果は完全一致する。

    列: open/high/low/close/volume、index=timestamp(UTC・昇順)。
    fx_backtester.data.load_price_csv がそのまま読めるスキーマ。
    """
    owns_session = session is None
    session = session or _build_session()
    daily_frames: list[pd.DataFrame] = []
    days = list(_iter_days(start, end))
    try:
        for i, (day_start, day_end) in enumerate(days):
            ticks = fetch_ticks(
                symbol,
                day_start,
                day_end,
                cache_dir,
                session=session,
                progress=False,
                max_workers=max_workers,
            )
            if ticks:
                daily_frames.append(ticks_to_ohlcv(ticks, timeframe))
            del ticks  # rawなtickを日ごとに解放(メモリを平坦に保つ)
            if progress and (i % 30 == 0 or i == len(days) - 1):
                bars = sum(len(f) for f in daily_frames)
                print(f"  duka {symbol}: {i + 1}/{len(days)} days, {bars} bars", flush=True)
    finally:
        if owns_session:
            session.close()

    if not daily_frames:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"]).rename_axis(
            "timestamp"
        )
    combined = pd.concat(daily_frames).sort_index()
    combined = combined[~combined.index.duplicated(keep="first")]
    return combined

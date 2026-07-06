"""Dukascopy 実ティックデータ層 — .bi5 の取得・展開・バー集約・CSV出力・将来価格供給。

レポート(FX AI.md)第0段階の中核。個人開発者の実質標準である Dukascopy の
無料ティックフィード(ECN流動性プール、bid/ask込み、15年以上)を、追加の
サードパーティ依存ゼロ(標準ライブラリ + requests のみ)で取り込む。
dukascopy-node のような Node 依存を持ち込まず、macro.py と同じく Mac mini の
軽量venvへ rsync するだけで動く方針を維持する。

Dukascopy のデータ構造:

- URL   https://datafeed.dukascopy.com/datafeed/{SYMBOL}/{YYYY}/{MM-1}/{DD}/{HH}h_ticks.bi5
        月は 0 起点(1月=00, 12月=11)。時刻はUTC。1ファイル=1時間ぶんのティック。
- 中身  LZMA(.bi5)で圧縮された固定長20バイト構造体の連続。各レコードは
        >IIIff = (ms_offset, ask_points, bid_points, ask_volume, bid_volume)。
        価格は「その通貨ペアの point 数」の整数で、point値(1e-5 or 1e-3)を
        掛けて実価格へ戻す。ms_offset はそのファイル時刻(HH:00:00 UTC)からの
        経過ミリ秒。空ファイル(市場休場)は 0 バイトで、ティック無しを意味する。

ミッションクリティカル設計の原則(macro.py と同じ):

1. 劣化はしても死なない — 各時間ファイルは独立に失敗でき、失敗は warnings に
   記録される。1時間が欠けても他の時間の集約は続行する。
2. キャッシュ優先 — 生 .bi5 をローカルへ保存し、再取得を避ける(Dukascopy は
   IP制限があるため、同じ時間を二度取らない)。
3. パースは純粋関数 — parse_bi5 / ticks_to_bars / bars_to_csv_rows は
   ネットワーク非依存で、バイト列・辞書列からテストできる。
4. リーク防止 — バーは「足確定後の値」だけで構成し、集約は左閉右開区間で行う
   (data.py / engine.py が期待する足確定セマンティクスと一致)。

このモジュールはネットワーク取得(fetch_*)だけが requests に触れ、それ以外は
すべて純粋ロジックである。テストはネットワーク不要で完結する。
"""

from __future__ import annotations

import lzma
import struct
import time
from bisect import bisect_left
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, UTC
from pathlib import Path

import requests

USER_AGENT = "fx-codex-intel/1.0 (+https://github.com/fuuki1)"
FETCH_ATTEMPTS = 3
FETCH_RETRY_WAIT_SECONDS = 2.0
FETCH_TIMEOUT_SECONDS = 30.0

DATAFEED_URL = (
    "https://datafeed.dukascopy.com/datafeed/"
    "{symbol}/{year:04d}/{month0:02d}/{day:02d}/{hour:02d}h_ticks.bi5"
)

# 1レコード = >IIIff (ビッグエンディアン): ms_offset, ask_pts, bid_pts, ask_vol, bid_vol
_TICK_STRUCT = struct.Struct(">IIIff")
TICK_RECORD_SIZE = _TICK_STRUCT.size  # 20 bytes

# 通貨ペアごとの point 値。整数 point → 実価格への変換係数。
# JPYクロス等の3桁ペアは 1e-3、それ以外の5桁ペアは 1e-5。
POINT_VALUE_5DIGIT = 1e-5
POINT_VALUE_3DIGIT = 1e-3

# バー集約の対応時間足(分)。data.py / engine.py と同じ足確定セマンティクス。
TIMEFRAME_MINUTES: dict[str, int] = {
    "1m": 1,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "4h": 240,
    "1d": 1440,
}


def point_value(symbol: str) -> float:
    """通貨ペアの point 値を返す。JPYを含むペアは3桁(1e-3)、他は5桁(1e-5)。"""
    return POINT_VALUE_3DIGIT if "JPY" in symbol.upper() else POINT_VALUE_5DIGIT


# ---------------------------------------------------------------- データ型


@dataclass(frozen=True)
class Tick:
    """1ティック。時刻はUTCの絶対時刻に復元済み。価格は実価格(point変換後)。"""

    when: datetime
    bid: float
    ask: float
    bid_volume: float
    ask_volume: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass(frozen=True)
class Bar:
    """OHLCバー1本。始値時刻(足の開始=左端)を timestamp に持つ。

    価格は mid(bid/ask中値)ベース。spread は足内の平均スプレッド(実価格)で、
    data.py の spread_price 列へそのまま渡せる。
    """

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    spread: float


# ---------------------------------------------------------------- パース(純粋関数)


def parse_bi5(raw: bytes, base_time: datetime, symbol: str) -> list[Tick]:
    """1時間ぶんの .bi5(LZMA圧縮された固定長レコード列)をティックへ展開する。

    base_time はそのファイルの時刻(HH:00:00 UTC)。各レコードの ms_offset を
    base_time に足して絶対時刻へ戻す。空(0バイト)は休場でティック無し。
    壊れた末尾(20の倍数でない端数)は無視する。
    """
    if not raw:
        return []
    try:
        payload = lzma.decompress(raw)
    except lzma.LZMAError:
        return []
    pv = point_value(symbol)
    ticks: list[Tick] = []
    usable = len(payload) - (len(payload) % TICK_RECORD_SIZE)
    for offset in range(0, usable, TICK_RECORD_SIZE):
        ms, ask_pts, bid_pts, ask_vol, bid_vol = _TICK_STRUCT.unpack_from(payload, offset)
        ticks.append(
            Tick(
                when=base_time + timedelta(milliseconds=ms),
                bid=bid_pts * pv,
                ask=ask_pts * pv,
                bid_volume=float(bid_vol),
                ask_volume=float(ask_vol),
            )
        )
    return ticks


def _floor_to_timeframe(when: datetime, minutes: int) -> datetime:
    """時刻をその時間足の足開始(左端)へ切り下げる。

    1日足はUTCの日境界に、それ未満はエポックからの分グリッドに合わせる
    (60/240分は1日を割り切るのでUTC日内でも整合する)。
    """
    if minutes >= 1440:
        return when.replace(hour=0, minute=0, second=0, microsecond=0)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    total_minutes = int((when - epoch).total_seconds() // 60)
    floored = (total_minutes // minutes) * minutes
    return epoch + timedelta(minutes=floored)


def ticks_to_bars(ticks: Sequence[Tick], timeframe: str) -> list[Bar]:
    """ティック列を指定時間足のOHLCバーへ集約する(mid価格ベース、左閉右開区間)。

    リーク防止のため、各バーは自区間内のティックだけで構成する。ティックの
    無い足はスキップ(バーを捏造しない)。ティックは時刻昇順である前提だが、
    Dukascopy のファイル内順序は保証されるので、呼び出し側で連結した順を保つ。
    """
    minutes = TIMEFRAME_MINUTES.get(timeframe)
    if minutes is None:
        raise ValueError(f"未対応の時間足: {timeframe}(対応: {', '.join(TIMEFRAME_MINUTES)})")
    if not ticks:
        return []

    bars: list[Bar] = []
    bucket_start: datetime | None = None
    opens = highs = lows = closes = 0.0
    spread_sum = 0.0
    count = 0

    def _flush() -> None:
        nonlocal bucket_start, opens, highs, lows, closes, spread_sum, count
        if bucket_start is not None and count > 0:
            bars.append(
                Bar(
                    timestamp=bucket_start,
                    open=opens,
                    high=highs,
                    low=lows,
                    close=closes,
                    volume=float(count),
                    spread=spread_sum / count,
                )
            )

    for tick in ticks:
        start = _floor_to_timeframe(tick.when, minutes)
        mid = tick.mid
        if bucket_start is None or start != bucket_start:
            _flush()
            bucket_start = start
            opens = highs = lows = closes = mid
            spread_sum = tick.spread
            count = 1
        else:
            highs = max(highs, mid)
            lows = min(lows, mid)
            closes = mid
            spread_sum += tick.spread
            count += 1
    _flush()
    return bars


def bars_to_csv_rows(bars: Iterable[Bar], symbol: str, price_decimals: int = 5) -> list[str]:
    """バー列を data.py が読めるCSV行へ変換する(ヘッダ含む)。

    列は timestamp,symbol,open,high,low,close,volume,spread_price。
    spread_price は data.py がそのままスプレッドコストとして解釈できる実価格。
    JPYクロスは3桁、他は5桁で丸める(実ブローカーの気配刻みに合わせる)。
    """
    decimals = 3 if "JPY" in symbol.upper() else price_decimals
    rows = ["timestamp,symbol,open,high,low,close,volume,spread_price"]
    for bar in bars:
        stamp = bar.timestamp.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
        rows.append(
            f"{stamp},{symbol},"
            f"{bar.open:.{decimals}f},{bar.high:.{decimals}f},"
            f"{bar.low:.{decimals}f},{bar.close:.{decimals}f},"
            f"{bar.volume:.0f},{bar.spread:.{decimals}f}"
        )
    return rows


# ---------------------------------------------------------------- 取得(唯一のネット依存)


def _hour_slots(start: datetime, end: datetime) -> list[datetime]:
    """[start, end] を1時間刻みの取得スロット(各正時UTC)へ展開する。"""
    lo = start.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    hi = end.astimezone(UTC)
    slots: list[datetime] = []
    cursor = lo
    while cursor <= hi:
        slots.append(cursor)
        cursor += timedelta(hours=1)
    return slots


def _slot_url(symbol: str, slot: datetime) -> str:
    return DATAFEED_URL.format(
        symbol=symbol.upper(),
        year=slot.year,
        month0=slot.month - 1,  # Dukascopy は月を0起点で持つ
        day=slot.day,
        hour=slot.hour,
    )


def _cache_path(cache_dir: Path, symbol: str, slot: datetime) -> Path:
    return (
        cache_dir
        / symbol.upper()
        / f"{slot.year:04d}"
        / f"{slot.month:02d}"
        / f"{slot.day:02d}"
        / f"{slot.hour:02d}h_ticks.bi5"
    )


def _fetch_bytes(url: str, session: requests.Session | None = None) -> bytes | None:
    """再試行付きのバイナリ取得。404(その時間にファイル無し=休場等)は None。

    404 はデータが存在しないだけで異常ではないため、警告なしで空扱いにする。
    それ以外の失敗は最終試行後に例外を投げ、呼び出し側が warnings に降格する。
    """
    http = session or requests
    last_error: Exception | None = None
    for attempt in range(FETCH_ATTEMPTS):
        try:
            response = http.get(
                url, headers={"User-Agent": USER_AGENT}, timeout=FETCH_TIMEOUT_SECONDS
            )
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.content
        except Exception as error:  # noqa: BLE001 - 外部API起因
            last_error = error
            if attempt + 1 < FETCH_ATTEMPTS:
                time.sleep(FETCH_RETRY_WAIT_SECONDS)
    raise RuntimeError(f"{url} の取得に失敗: {last_error}")


def fetch_ticks(
    symbol: str,
    start: datetime,
    end: datetime,
    cache_dir: str | Path,
    warnings: list[str] | None = None,
    session: requests.Session | None = None,
    max_hours: int = 24 * 90,
) -> list[Tick]:
    """[start, end] のティックを Dukascopy から取得する(1時間ずつ、キャッシュ優先)。

    生 .bi5 を cache_dir 以下へ保存し、二度目以降はネットワークに触れない。
    1時間の取得失敗は warnings に記録して続行する(1時間欠けても全体は生きる)。
    max_hours は事故的な巨大レンジ取得を防ぐ安全弁(既定90日ぶん)。
    """
    warnings = warnings if warnings is not None else []
    cache_root = Path(cache_dir)
    slots = _hour_slots(start, end)
    if len(slots) > max_hours:
        raise ValueError(
            f"取得レンジが広すぎます({len(slots)}時間 > 上限{max_hours})。"
            "分割するか max_hours を明示してください。"
        )

    ticks: list[Tick] = []
    for slot in slots:
        cache_file = _cache_path(cache_root, symbol, slot)
        raw: bytes | None
        if cache_file.exists():
            raw = cache_file.read_bytes()
        else:
            try:
                raw = _fetch_bytes(_slot_url(symbol, slot), session=session)
            except RuntimeError as error:
                warnings.append(f"Dukascopy取得失敗({symbol} {slot:%Y-%m-%d %H}h): {error}")
                continue
            # 404(None)も「取得済みだが空」として空ファイルをキャッシュし再取得を避ける
            try:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_bytes(raw or b"")
            except OSError:
                pass  # キャッシュ失敗は致命的でない(次回再取得になるだけ)
        if raw:
            ticks.extend(parse_bi5(raw, slot, symbol))

    ticks.sort(key=lambda t: t.when)
    return ticks


@dataclass
class DownloadResult:
    symbol: str
    timeframe: str
    tick_count: int
    bar_count: int
    out_path: Path | None
    warnings: list[str] = field(default_factory=list)


def download_bars_csv(
    symbol: str,
    start: datetime,
    end: datetime,
    timeframe: str,
    out_path: str | Path,
    cache_dir: str | Path,
    session: requests.Session | None = None,
) -> DownloadResult:
    """ティックを取得→指定足へ集約→data.py 互換CSVを書き出す一気通貫の入口。

    戻り値の DownloadResult でティック数・バー数・警告・出力パスを返す。
    ティックが1件も取れなければCSVは書かず、警告付きの結果を返す。
    """
    warnings: list[str] = []
    ticks = fetch_ticks(symbol, start, end, cache_dir, warnings=warnings, session=session)
    bars = ticks_to_bars(ticks, timeframe)
    if not bars:
        warnings.append(f"{symbol} {timeframe}: 集約後のバーが0本(ティック{len(ticks)}件)")
        return DownloadResult(
            symbol=symbol,
            timeframe=timeframe,
            tick_count=len(ticks),
            bar_count=0,
            out_path=None,
            warnings=warnings,
        )
    rows = bars_to_csv_rows(bars, symbol)
    destination = Path(out_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return DownloadResult(
        symbol=symbol,
        timeframe=timeframe,
        tick_count=len(ticks),
        bar_count=len(bars),
        out_path=destination,
        warnings=warnings,
    )


# ---------------------------------------------------------------- 源B: 将来価格供給


def make_future_price_provider(
    cache_dir: str | Path,
    session: requests.Session | None = None,
) -> Callable[[str, str, datetime, float], float | None]:
    """price_history.py の FuturePriceProvider(源B)を Dukascopy で実装して返す。

    price_history は判断ジャーナルの後続行(源A)で将来価格を賄うが、後続行が
    まだ無い/実行が飛んだ区間は採点保留になる。この provider を注入すると、
    その穴を Dukascopy の確定ティックで埋められる(短い足の永久未採点を解消)。

    契約は (symbol, timeframe, target_time, tolerance_hours) -> float|None。
    target_time 近傍の tolerance_hours 幅のティックを取り、最も近い1件の mid を
    将来価格として返す。取得できなければ None(源Aで賄えなければ採点保留のまま)。
    ミッションクリティカル方針として、この源Bは失敗しても静かに None を返し、
    採点そのものを止めない(warnings は内部で破棄=判断ログを汚さない)。
    """
    tf_normalized = {"15m": "15m", "1h": "1h", "4h": "4h", "1d": "1d"}

    def provider(
        symbol: str, timeframe: str, target_time: datetime, tolerance_hours: float
    ) -> float | None:
        if timeframe not in tf_normalized and timeframe != "":
            return None
        target = target_time if target_time.tzinfo else target_time.replace(tzinfo=UTC)
        window = max(tolerance_hours, 1.0)
        start = target - timedelta(hours=window)
        end = target + timedelta(hours=window)
        sink: list[str] = []  # 源Bの取得警告は判断ログへ出さず内部で捨てる
        try:
            ticks = fetch_ticks(symbol, start, end, cache_dir, warnings=sink, session=session)
        except (ValueError, RuntimeError):
            return None
        return nearest_mid(ticks, target)

    return provider


def nearest_mid(ticks: Sequence[Tick], target: datetime) -> float | None:
    """ティック列の中で target 時刻に最も近い1件の mid を返す(昇順前提、二分探索)。"""
    if not ticks:
        return None
    times = [t.when for t in ticks]
    index = bisect_left(times, target)
    candidates = []
    if index < len(ticks):
        candidates.append(ticks[index])
    if index > 0:
        candidates.append(ticks[index - 1])
    best = min(candidates, key=lambda t: abs((t.when - target).total_seconds()))
    return best.mid

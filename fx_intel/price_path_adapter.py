"""Dukascopy tick から PIT 適格な確定足を作り、採点用の価格経路へ供給する。

## なぜ必要か

採点(`trade_outcome`)は TP/SL への到達を「価格経路の high/low」で判定する。
ところが実機の価格行は 5 分ごとの TradingView スナップショットで、
`ohlc_scope="forming_bar_snapshot"`(形成途中のバー)しか持たない。
形成中バーの high/low を信用すると未来情報の混入になるため、採点側は
`TRUSTED_POST_PREDICTION_OHLC_SCOPES` に無いスコープを棄却する。結果として
実機の採点は 100% が `close_only_path` に落ち、TP/SL 到達判定が機能しない
(実測 tp1_rate 0.15% / sl_rate 1.69% = 98% が時間切れ決済)。

このモジュールは `collect/log/quotes.jsonl`(Dukascopy の確定 tick)を
確定足に畳んで供給する。tick は `lag_hours` 遅れで取り込まれた**確定済み**の
観測なので、バー期間が終了した後にだけ書けば未来情報は混入しない。
したがって `ohlc_scope="lower_timeframe_closed_bar"` を名乗れる。

## PIT 上の約束

- 収集済み tick のみを読む。ネットワークアクセスはしない。
- バー期間が完全に終了した(`bar_end <= as_of`)バーだけを出力する。
  形成中のバーは書かない。
- `event_time` はバー終端(その価格が確定した時刻)、`available_time` は
  取り込み時刻(`received_at` の最大)。両者を分離することで、
  「いつ成立したか」と「いつ我々が知り得たか」を混同しない。
- `quality_state != "usable"` の tick は捨てる。
- 確定足とは「期間が終わった足」ではなく「その期間の tick が全部そろった足」。
  collector は 1 時間分を tick 1 行ずつ追記し、時間帯を書き終えてから
  `collect/state/dukascopy_history.json` の `completed_hours` を原子的に進める。
  追記途中の時間帯を読むと未完成の足を確定足として永続化してしまうため
  (実機 2026-09-16: EURUSD 09:30 足を 264 tick 中 185 tick で書いた)、
  launchd 経路では `completed_hours` を完成の唯一の合図として使う
  (`load_collector_completion` / `build_closed_bars(complete_through=...)`)。

## 既存経路との非干渉

`append_snapshot_entries` の自然キーは `(capture_slot, symbol, timeframe)` で、
`capture_slot` は 300 秒に量子化される。既存の 15m/1h/4h/1d 行と同じ
timeframe 名を使うと同一キーに別内容を書くことになり、writer が
`PriceHistoryWriteError` で落ちて**実機の収集を止める**。そのため専用の
timeframe 名(`ADAPTER_TIMEFRAME`)を使う。

採点側は価格系列を symbol だけでキー化し timeframe を見ない
(`trade_outcome._evaluate` の prices 構築)。したがって別 timeframe 名でも
同じ銘柄の価格経路として自然に取り込まれ、既存行には一切触れない。

バー幅を 5 分に揃えているのも同じ理由で、`capture_slot` の量子化単位より
細かいバー(例: 1 分足)を出すと 1 スロットに 5 本が衝突する。
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3

from .price_history import SNAPSHOT_CADENCE_SECONDS, SNAPSHOT_SCHEMA_VERSION, _content_hash

# 採点が信頼するスコープ。tick を畳んだ確定足はこれに該当する
# (trade_outcome.TRUSTED_POST_PREDICTION_OHLC_SCOPES と対応)。
ADAPTER_OHLC_SCOPE = "lower_timeframe_closed_bar"
# 既存 15m/1h/4h/1d と自然キーで衝突しないための専用 timeframe 名。
ADAPTER_TIMEFRAME = "5m_datafeed"
ADAPTER_SOURCE = "dukascopy_datafeed"
# 同じ tick から同じバーを再生成した場合、内容は完全に一致する。
# ところが `_same_snapshot` は writer_id が異なると内容を見る前に「競合する
# writer」と判定する。writer_id に PID を含めると再実行のたびに別 writer に
# なり、既に書いた行を再生成した時点で fail-closed に当たって全量が拒否される
# (実機で exit 74 になった原因)。このアダプタは「どの process が動かしても
# 同一の論理 writer」なので、PID を含めない固定 ID を名乗る。
ADAPTER_WRITER_ID = "price_path_adapter"
# capture_slot の量子化単位と一致させる(これより細かいと 1 スロットで衝突する)。
BAR_SECONDS = SNAPSHOT_CADENCE_SECONDS
USABLE_QUALITY_STATE = "usable"
# completed_hours が完成を保証するのはこの provider の tick だけ。
COLLECTOR_PROVIDER = "dukascopy"
# 既に書いた最新足より少し前から読み直し、境界の足を再検証する。
WINDOW_LOOKBACK = timedelta(hours=1)
_EVENT_TIME_MARKER = b'"provider_event_time": "'
# quote_tape_index (fx-quote-tape-offset-index-v1) の契約名。読み取り専用で参照する。
QUOTE_INDEX_CONTRACT = "fx-quote-tape-offset-index-v1"
# index の event_time 文字列比較に持たせる余裕(厳密な絞り込みは読み取り側で行う)。
INDEX_SEEK_MARGIN = timedelta(hours=1)


class PricePathAdapterError(RuntimeError):
    """Raised when the tick feed cannot be turned into a PIT-safe bar."""


@dataclass(frozen=True)
class TickQuote:
    """1 tick 分の観測。採点に必要な最小限だけを持つ。"""

    symbol: str
    event_time: datetime
    received_at: datetime
    mid: float
    bid: float | None
    ask: float | None
    provider: str = ""


@dataclass(frozen=True)
class ClosedBar:
    """期間が終了した確定足。high/low は実際に観測された値のみ。"""

    symbol: str
    bar_start: datetime
    bar_end: datetime
    open: float
    high: float
    low: float
    close: float
    bid: float | None
    ask: float | None
    tick_count: int
    available_time: datetime

    def to_row(self, *, run_id: str, writer_id: str) -> dict[str, object]:
        """`briefing_tf_prices.jsonl` schema_version 2 の行へ変換する。

        `ts`/`event_time` はバー終端。available_time は取り込み時刻で、
        両者は意図的に別の値になり得る(成立時刻と観測可能時刻の分離)。
        """
        stamp = self.bar_end.isoformat()
        row: dict[str, object] = {
            "ts": stamp,
            "symbol": self.symbol,
            "timeframe": ADAPTER_TIMEFRAME,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "event_time": stamp,
            "available_time": self.available_time.isoformat(),
            "ingested_time": self.available_time.isoformat(),
            "source": ADAPTER_SOURCE,
            "source_record_id": f"{self.symbol}:{ADAPTER_TIMEFRAME}:{stamp}",
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "run_id": run_id,
            "writer_id": writer_id,
            "capture_slot": self.bar_start.isoformat(),
            "ohlc_scope": ADAPTER_OHLC_SCOPE,
            "tick_count": self.tick_count,
        }
        flags: list[str] = []
        if self.bid is not None and self.ask is not None:
            row["bid"] = self.bid
            row["ask"] = self.ask
            row["spread"] = round(self.ask - self.bid, 10)
            if self.bid > self.ask:
                flags.append("crossed_quote")
        else:
            flags.append("bid_ask_unavailable")
        row["data_quality_flags"] = flags
        row["content_hash"] = _content_hash(row)
        return row


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def parse_tick(payload: Mapping[str, object]) -> TickQuote | None:
    """quotes.jsonl の 1 行を TickQuote にする。使えない行は None。

    `quality_state` が usable でない行は、理由を問わず採点に使わない
    (collector 側が既に隔離判断をしているものを再解釈しない)。
    """
    if str(payload.get("quality_state") or "") != USABLE_QUALITY_STATE:
        return None
    symbol = str(payload.get("instrument") or "").strip().upper()
    if not symbol:
        return None
    event_time = _parse_time(payload.get("provider_event_time"))
    received_at = _parse_time(payload.get("received_at"))
    if event_time is None or received_at is None:
        return None
    bid = _number(payload.get("bid"))
    ask = _number(payload.get("ask"))
    mid = _number(payload.get("mid"))
    if mid is None and bid is not None and ask is not None:
        mid = (bid + ask) / 2.0
    if mid is None:
        return None
    return TickQuote(
        symbol=symbol,
        event_time=event_time,
        received_at=received_at,
        mid=mid,
        bid=bid,
        ask=ask,
        provider=str(payload.get("provider") or ""),
    )


def iter_ticks(lines: Iterable[str]) -> Iterator[TickQuote]:
    """JSONL 行から使える tick だけを流す。壊れた行は黙って捨てる。

    collector は追記中のファイルを読む可能性があるため、末尾の切れた行で
    全体を失敗させない(次回実行で完全な行として読み直される)。
    """
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, Mapping):
            continue
        tick = parse_tick(payload)
        if tick is not None:
            yield tick


def _bar_start(moment: datetime) -> datetime:
    epoch = int(moment.timestamp())
    return datetime.fromtimestamp(epoch - epoch % BAR_SECONDS, tz=UTC)


def build_closed_bars(
    ticks: Iterable[TickQuote],
    *,
    as_of: datetime,
    complete_through: Mapping[str, datetime] | None = None,
) -> list[ClosedBar]:
    """tick を確定足へ畳む。期間が終了していないバーは返さない。

    `as_of` より後に終わるバーを落とすことが PIT 保護の本体で、これがないと
    形成中バーの high/low を確定値として書いてしまう。

    `complete_through`(銘柄→完了済み時間帯の終端)を渡すと、さらに
    collector が書き終えた時間帯の足だけを返す。このときは完成を保証できる
    `COLLECTOR_PROVIDER` の tick だけを使い、`as_of` より後に観測可能になった
    足(available_time > as_of)も返さない。
    """
    gated = complete_through is not None
    buckets: dict[tuple[str, datetime], list[TickQuote]] = {}
    for tick in ticks:
        if gated and tick.provider != COLLECTOR_PROVIDER:
            continue
        key = (tick.symbol, _bar_start(tick.event_time))
        buckets.setdefault(key, []).append(tick)

    bars: list[ClosedBar] = []
    for (symbol, start), group in buckets.items():
        end = start + timedelta(seconds=BAR_SECONDS)
        if end > as_of:
            # まだ期間が終わっていない = 確定足ではない
            continue
        if complete_through is not None:
            limit = complete_through.get(symbol)
            if limit is None or end > limit:
                # 期間は終わっていても collector がその時間帯を書き終えていない
                continue
            if max(tick.received_at for tick in group) > as_of:
                continue
        group.sort(key=lambda tick: tick.event_time)
        mids = [tick.mid for tick in group]
        last = group[-1]
        bars.append(
            ClosedBar(
                symbol=symbol,
                bar_start=start,
                bar_end=end,
                open=mids[0],
                high=max(mids),
                low=min(mids),
                close=mids[-1],
                bid=last.bid,
                ask=last.ask,
                tick_count=len(group),
                available_time=max(tick.received_at for tick in group),
            )
        )
    bars.sort(key=lambda bar: (bar.bar_end, bar.symbol))
    return bars


def build_rows(
    bars: Sequence[ClosedBar],
    *,
    run_id: str,
    writer_id: str = ADAPTER_WRITER_ID,
) -> list[dict[str, object]]:
    """確定足を追記可能な行へ変換する。

    writer_id は既定で固定値。process ごとに変えると、同じバーの再生成が
    `_same_snapshot` で「別 writer による競合」と判定され追記全体が失敗する。
    """
    return [bar.to_row(run_id=run_id, writer_id=writer_id) for bar in bars]


def load_quotes(path: str | Path) -> list[TickQuote]:
    """quotes.jsonl を読む。存在しなければ空(collector 未稼働は異常ではない)。"""
    target = Path(path)
    if not target.exists():
        return []
    try:
        with target.open(encoding="utf-8") as handle:
            return list(iter_ticks(handle))
    except OSError as error:
        raise PricePathAdapterError(f"quotes を読めません: {error}") from error


def load_collector_completion(path: str | Path) -> dict[str, datetime]:
    """collector state から銘柄ごとの「書き終えた時間帯の終端」を返す。

    `completed_hours[symbol]` は tick を全部追記し終えた最後の時間帯の開始時刻で、
    collector は追記の後にこの state を原子的に置き換える。したがって
    返す終端以前の足は、この state を読んだ後に quotes.jsonl を読めば
    必ず全 tick がそろっている。読めない・壊れている state は fail-closed。
    """
    target = Path(path)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PricePathAdapterError(f"collector state を読めません: {target}: {error}") from error
    completed = payload.get("completed_hours") if isinstance(payload, Mapping) else None
    if not isinstance(completed, Mapping):
        raise PricePathAdapterError(f"collector state に completed_hours がありません: {target}")
    output: dict[str, datetime] = {}
    for raw_symbol, raw_hour in completed.items():
        hour = _parse_time(raw_hour)
        if hour is None:
            raise PricePathAdapterError(
                f"completed_hours の時刻が不正です(timezone 必須): {raw_symbol}={raw_hour!r}"
            )
        symbol = str(raw_symbol).strip().upper()
        output[symbol] = hour + timedelta(hours=1)
    return output


def iter_quote_file(
    path: str | Path,
    *,
    end_offset: int,
    since: datetime | None = None,
    start_offset: int = 0,
) -> Iterator[TickQuote]:
    """quotes.jsonl を先頭から `end_offset` バイトまでだけ流す。

    run 開始時に固定したサイズより先(読んでいる間に追記された分)は読まない。
    `since` より前の tick は捨てる。全行を JSON にすると 9GB 級の tape で
    遅いため、provider_event_time の先頭 19 文字で 1 日の余裕をもって
    先に間引き、残りだけを厳密に判定する。全件をメモリに載せない。
    """
    target = Path(path)
    if end_offset <= 0 or not target.exists():
        return
    coarse: bytes | None = None
    if since is not None:
        coarse = (since - timedelta(days=1)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S").encode()
    if start_offset < 0 or start_offset > end_offset:
        raise PricePathAdapterError(f"読み取り開始位置が不正です: {start_offset}")
    try:
        with target.open("rb") as handle:
            if start_offset:
                handle.seek(start_offset - 1)
                if handle.read(1) != b"\n":
                    raise PricePathAdapterError(
                        f"読み取り開始位置が行頭ではありません: {start_offset}"
                    )
            remaining = end_offset - start_offset
            for raw in handle:
                if remaining <= 0:
                    break
                if len(raw) > remaining:
                    raw = raw[:remaining]
                remaining -= len(raw)
                if coarse is not None:
                    marker = raw.find(_EVENT_TIME_MARKER)
                    if marker >= 0:
                        start = marker + len(_EVENT_TIME_MARKER)
                        if raw[start : start + 19] < coarse:
                            continue
                try:
                    payload = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(payload, Mapping):
                    continue
                tick = parse_tick(payload)
                if tick is None:
                    continue
                if since is not None and tick.event_time < since:
                    continue
                yield tick
    except OSError as error:
        raise PricePathAdapterError(f"quotes を読めません: {error}") from error


def adapter_window_start(
    prices_path: str | Path,
    symbols: Iterable[str],
) -> datetime | None:
    """既に書いた足から、読み直しが必要な最古の時刻を返す。

    銘柄ごとの最新足の開始時刻の最小値から `WINDOW_LOOKBACK` 戻る。
    1 銘柄でも足が無ければ None(全量を読む)。collector は時間帯を
    古い順にしか完了させないため、これより前に新しい完成足は現れない。
    """
    wanted = {str(symbol).strip().upper() for symbol in symbols}
    if not wanted:
        return None
    latest: dict[str, datetime] = {}
    target = Path(prices_path)
    if not target.exists():
        return None
    marker = f'"timeframe": "{ADAPTER_TIMEFRAME}"'.encode()
    try:
        with target.open("rb") as handle:
            for raw in handle:
                if marker not in raw:
                    continue
                try:
                    row = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(row, Mapping) or row.get("timeframe") != ADAPTER_TIMEFRAME:
                    continue
                symbol = str(row.get("symbol") or "").upper()
                slot = _parse_time(row.get("capture_slot"))
                if symbol in wanted and slot is not None:
                    if symbol not in latest or slot > latest[symbol]:
                        latest[symbol] = slot
    except OSError as error:
        raise PricePathAdapterError(f"prices を読めません: {error}") from error
    if set(latest) != wanted:
        return None
    return min(latest.values()) - WINDOW_LOOKBACK


def quote_index_start_offset(
    quotes_path: str | Path,
    index_path: str | Path,
    *,
    symbols: Iterable[str],
    since: datetime | None,
    end_offset: int,
) -> tuple[int, str]:
    """quote index から、`since` 以降の tick を取りこぼさない最小の読み取り開始位置を返す。

    返す位置は「index 済みで event_time >= since - 余裕 の行の最小 start_offset」と
    「index 済みの末尾」の小さい方。index に未反映の末尾は必ず読むので、index が
    遅れていても取りこぼさない。index が同じファイルを指すと確認できない、
    index が除外した行がある、などの場合は 0(先頭から全量)に戻す。
    位置が変わっても読む tick の集合は変わらない(遅くなるか速くなるかだけ)。
    """
    if since is None or end_offset <= 0:
        return 0, "no_window"
    database = Path(index_path)
    if not database.is_file():
        return 0, "index_missing"
    source = Path(quotes_path).resolve()
    try:
        connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as error:
        return 0, f"index_error:{type(error).__name__}"
    try:
        connection.execute("PRAGMA query_only=ON")
        contract = connection.execute(
            "SELECT value FROM metadata WHERE key = 'contract'"
        ).fetchone()
        if contract is None or str(contract[0]) != QUOTE_INDEX_CONTRACT:
            return 0, "index_contract_mismatch"
        state = connection.execute("""
            SELECT source_path, inode, byte_offset, last_line_start_offset,
                   last_line_end_offset, last_line_sha256, rejected_lines
            FROM source_state WHERE singleton = 1
            """).fetchone()
        if state is None:
            return 0, "index_uninitialized"
        path, inode, byte_offset, line_start, line_end, line_sha, rejected = state
        if Path(str(path)).resolve() != source:
            return 0, "index_path_mismatch"
        stat = source.stat()
        if int(stat.st_ino) != int(inode):
            return 0, "index_inode_mismatch"
        if int(stat.st_size) < int(byte_offset):
            return 0, "index_ahead_of_source"
        if int(rejected) != 0:
            # index が採らなかった行を adapter が使う可能性がある。位置を詰めない。
            return 0, "index_has_rejected_lines"
        if int(line_end) > int(line_start):
            with source.open("rb") as handle:
                handle.seek(int(line_start))
                raw = handle.read(int(line_end) - int(line_start))
            if hashlib.sha256(raw).hexdigest() != str(line_sha):
                return 0, "index_last_line_mismatch"
        lower = (since - INDEX_SEEK_MARGIN).astimezone(UTC).isoformat()
        best: int | None = None
        for symbol in sorted({str(value).strip().upper() for value in symbols}):
            row = connection.execute(
                "SELECT MIN(start_offset) FROM quote_offsets"
                " WHERE instrument = ? AND event_time >= ?",
                (symbol, lower),
            ).fetchone()
            if row is not None and row[0] is not None:
                best = int(row[0]) if best is None else min(best, int(row[0]))
        start = int(byte_offset) if best is None else min(best, int(byte_offset))
        return min(start, end_offset), "index_seek"
    except (sqlite3.Error, OSError) as error:
        return 0, f"index_error:{type(error).__name__}"
    finally:
        connection.close()

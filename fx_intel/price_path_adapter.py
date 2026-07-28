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
import json
from pathlib import Path

from .price_history import SNAPSHOT_CADENCE_SECONDS, SNAPSHOT_SCHEMA_VERSION, _content_hash

# 採点が信頼するスコープ。tick を畳んだ確定足はこれに該当する
# (trade_outcome.TRUSTED_POST_PREDICTION_OHLC_SCOPES と対応)。
ADAPTER_OHLC_SCOPE = "lower_timeframe_closed_bar"
# 既存 15m/1h/4h/1d と自然キーで衝突しないための専用 timeframe 名。
ADAPTER_TIMEFRAME = "5m_datafeed"
ADAPTER_SOURCE = "dukascopy_datafeed"
# capture_slot の量子化単位と一致させる(これより細かいと 1 スロットで衝突する)。
BAR_SECONDS = SNAPSHOT_CADENCE_SECONDS
USABLE_QUALITY_STATE = "usable"


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
) -> list[ClosedBar]:
    """tick を確定足へ畳む。期間が終了していないバーは返さない。

    `as_of` より後に終わるバーを落とすことが PIT 保護の本体で、これがないと
    形成中バーの high/low を確定値として書いてしまう。
    """
    buckets: dict[tuple[str, datetime], list[TickQuote]] = {}
    for tick in ticks:
        key = (tick.symbol, _bar_start(tick.event_time))
        buckets.setdefault(key, []).append(tick)

    bars: list[ClosedBar] = []
    for (symbol, start), group in buckets.items():
        end = start + timedelta(seconds=BAR_SECONDS)
        if end > as_of:
            # まだ期間が終わっていない = 確定足ではない
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
    writer_id: str,
) -> list[dict[str, object]]:
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

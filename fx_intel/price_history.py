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
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta, UTC

from .market import WEEKEND_CLOSURE, open_hours_between

# 源B(外部履歴OHLC)の注入契約。
# (symbol, timeframe, target_time, tolerance_hours) -> 将来終値 or None
FuturePriceProvider = Callable[[str, str, datetime, float], float | None]


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
    stamp = now.isoformat()
    rows: list[dict] = []
    for symbol, intervals in closes_by_interval.items():
        for timeframe, snapshot in intervals.items():
            row = _snapshot_row(stamp, str(symbol), str(timeframe), snapshot)
            if row is not None:
                rows.append(row)
    return rows


def _snapshot_row(
    stamp: str,
    symbol: str,
    timeframe: str,
    snapshot: float | Mapping[str, object] | None,
) -> dict | None:
    if isinstance(snapshot, (int, float)) and not isinstance(snapshot, bool):
        return {
            "ts": stamp,
            "symbol": symbol,
            "timeframe": timeframe,
            "close": float(snapshot),
        }
    if not isinstance(snapshot, Mapping):
        return None
    close = _number(snapshot.get("close"))
    if close is None:
        return None
    row: dict[str, object] = {
        "ts": stamp,
        "symbol": symbol,
        "timeframe": timeframe,
        "close": close,
    }
    for key in ("open", "high", "low", "bid", "ask", "spread"):
        value = _number(snapshot.get(key))
        if value is not None:
            row[key] = value
    return row


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None

"""FX市場の週末クローズ判定。

FXは平日24時間・週末のみ休場: NYクローズ(金曜17:00 ET)からシドニー
再開(月曜早朝)まで取引がない。夏時間/冬時間で境界が1時間ずれるため、
閉場を広めに取る保守的な近似(金曜21:00 UTC〜日曜22:00 UTC)を使い、
どちらの季節でも「確実に開いている時間」だけを開場扱いにする。

用途:
- briefing: 休場中はTradingViewスキャナーが金曜クローズの価格を返し
  続けるため、新規の方向判断を止める(stale価格での判断防止)
- journal: 経過時間を「市場オープン時間」で数え、週末を跨いだ
  「価格が動きようがない区間」を的中率評価から除外する
"""

from __future__ import annotations

from datetime import datetime, timedelta, UTC

CLOSE_WEEKDAY = 4  # 金曜
CLOSE_HOUR_UTC = 21
REOPEN_HOUR_UTC = 22  # 日曜
WEEKEND_CLOSURE = timedelta(hours=49)  # 金曜21:00 UTC → 日曜22:00 UTC


def is_market_open(moment: datetime) -> bool:
    """momentがFX取引時間内かどうか(週末クローズの近似判定)。"""
    utc = moment.astimezone(UTC)
    weekday = utc.weekday()
    if weekday == CLOSE_WEEKDAY:
        return utc.hour < CLOSE_HOUR_UTC
    if weekday == 5:  # 土曜
        return False
    if weekday == 6:  # 日曜
        return utc.hour >= REOPEN_HOUR_UTC
    return True


def _closure_start_on_or_before(moment: datetime) -> datetime:
    """moment以前で最も近い週末クローズ開始(金曜21:00 UTC)を返す。"""
    anchor = moment.replace(hour=CLOSE_HOUR_UTC, minute=0, second=0, microsecond=0)
    anchor -= timedelta(days=(moment.weekday() - CLOSE_WEEKDAY) % 7)
    if anchor > moment:
        anchor -= timedelta(days=7)
    return anchor


def _weekend_closure_starts(start_utc: datetime, end_utc: datetime) -> list[datetime]:
    """[start_utc, end_utc] に重なり得る週末クローズ開始時刻を昇順で返す。

    列挙は open_hours_between の while ループと完全に同一(end 側から
    _closure_start_on_or_before で1つ取り、cursor + WEEKEND_CLOSURE > start_utc の
    間だけ7日ずつ遡る)。呼び出し側が早期 break できるよう昇順にして返す。
    """
    starts: list[datetime] = []
    cursor = _closure_start_on_or_before(end_utc)
    while cursor + WEEKEND_CLOSURE > start_utc:
        starts.append(cursor)
        cursor -= timedelta(days=7)
    starts.reverse()
    return starts


def _closed_hours(
    start_utc: datetime, end_utc: datetime, closure_starts: list[datetime]
) -> timedelta:
    """closure_starts(昇順)と [start_utc, end_utc] の重なり合計。"""
    closed = timedelta()
    for cursor in closure_starts:
        if cursor >= end_utc:
            break  # 昇順なので以降の窓は区間外
        overlap = min(cursor + WEEKEND_CLOSURE, end_utc) - max(cursor, start_utc)
        if overlap > timedelta():
            closed += overlap
    return closed


def open_hours_between(start: datetime, end: datetime) -> float:
    """start→endの経過時間から週末クローズ分を除いた「市場オープン時間」。"""
    if end <= start:
        return 0.0
    start_utc = start.astimezone(UTC)
    end_utc = end.astimezone(UTC)
    closed = _closed_hours(start_utc, end_utc, _weekend_closure_starts(start_utc, end_utc))
    return (end_utc - start_utc - closed).total_seconds() / 3600.0


class WeekendOpenHours:
    """固定 start から増加する end への open_hours を O(窓数) で繰り返し計算する。

    採点ループ(将来価格探索)は「固定した判断時刻 ts」と「価格系列を進む point.ts」
    の組で open_hours_between を系列長ぶん呼ぶため、そのままだと判断数×価格点数の
    二乗になる。この補助オブジェクトは start 固定で週末クローズ開始時刻を一度だけ
    列挙し、以降の各 end 呼び出しを窓の総和(通常1〜2個)+昇順早期 break にする。
    値は open_hours_between とビット単位で一致する。

    max_end は列挙する窓の上限を決めるための保守的な壁時計上限(将来探索窓の
    window_upper を渡す)。max_end を超える end を渡しても open_hours_between に
    フォールバックするため結果は正しい(高速路を外れるだけ)。
    """

    __slots__ = ("_start_utc", "_max_end_utc", "_closure_starts")

    def __init__(self, start: datetime, max_end: datetime) -> None:
        self._start_utc = start.astimezone(UTC)
        self._max_end_utc = max_end.astimezone(UTC)
        self._closure_starts = _weekend_closure_starts(self._start_utc, self._max_end_utc)

    def age(self, end: datetime) -> float:
        """start→end の市場オープン時間(open_hours_between(start, end) と同値)。"""
        end_utc = end.astimezone(UTC)
        if end_utc <= self._start_utc:
            return 0.0
        if end_utc > self._max_end_utc:
            # 事前列挙した窓の範囲外。素の実装にフォールバックする。
            return open_hours_between(self._start_utc, end_utc)
        closed = _closed_hours(self._start_utc, end_utc, self._closure_starts)
        return (end_utc - self._start_utc - closed).total_seconds() / 3600.0

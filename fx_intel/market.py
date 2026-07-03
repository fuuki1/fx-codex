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


def open_hours_between(start: datetime, end: datetime) -> float:
    """start→endの経過時間から週末クローズ分を除いた「市場オープン時間」。"""
    if end <= start:
        return 0.0
    start_utc = start.astimezone(UTC)
    end_utc = end.astimezone(UTC)
    closed = timedelta()
    cursor = _closure_start_on_or_before(end_utc)
    while cursor + WEEKEND_CLOSURE > start_utc:
        overlap = min(cursor + WEEKEND_CLOSURE, end_utc) - max(cursor, start_utc)
        if overlap > timedelta():
            closed += overlap
        cursor -= timedelta(days=7)
    return (end_utc - start_utc - closed).total_seconds() / 3600.0

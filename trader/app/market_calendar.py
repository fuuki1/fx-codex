"""市場休場カレンダー（祝日・半日取引）。依存は標準ライブラリのみ。

within_session（domain.py）の週末/時間帯判定に祝日を重ねるためのモジュール。
外部依存を持ち込まない方針（params_gate.py と同じ）なので、祝日は動的計算する:

- 固定日祝日は「振替（observed）」ルールで平日へ寄せる（米株: 土→前金曜, 日→翌月曜）。
- 計算祝日（MLK・大統領の日・メモリアルデー・レイバーデー・感謝祭など）は
  「第n週の曜日」「最終月曜」で導出する。
- 半日取引（早終い）は米株のみモデル化し、通常より早いクローズ時刻(分)を返す。

★フェイルセーフ設計:
  祝日テーブルには明示的な収録年レンジ（_MIN_YEAR.._MAX_YEAR）がある。
  レンジ外の日付は「祝日か不明」であり、ここで取引を止めると正当な取引を
  誤ってブロックしうる。そのため **レンジ外は「祝日でない」扱い**にし、
  週末/時間帯の既定判定に委ねる（呼び出し側は is_within_coverage() で
  収録切れを検知して運用者に更新を促せる）。逆に、収録済みの祝日は確実に閉じる。

日本の祝日は「ハッピーマンデー」「春分/秋分」「振替休日」を含むため近似が難しい。
確実性を優先し、収録済み固定リスト（_JP_HOLIDAYS）で管理する（毎年更新前提）。
"""

from __future__ import annotations

from datetime import date, timedelta

# ---------------------------------------------------------------------------
# 収録年レンジ。ここを外れる年は「祝日不明」としてフェイルセーフに開扱いする。
# 年次メンテナンスでレンジと _JP_HOLIDAYS を更新すること。
# ---------------------------------------------------------------------------
_MIN_YEAR = 2024
_MAX_YEAR = 2027


def is_within_coverage(d: date) -> bool:
    """祝日テーブルがこの日付をカバーしているか。False の年は要更新。"""
    return _MIN_YEAR <= d.year <= _MAX_YEAR


# ---------------------------------------------------------------------------
# 汎用ヘルパ
# ---------------------------------------------------------------------------
def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """その月の第n(1-based)・指定曜日(Mon=0..Sun=6)の日付。"""
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + (n - 1) * 7)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """その月の最終・指定曜日の日付。"""
    if month == 12:
        nxt = date(year + 1, 1, 1)
    else:
        nxt = date(year, month + 1, 1)
    last = nxt - timedelta(days=1)
    offset = (last.weekday() - weekday) % 7
    return last - timedelta(days=offset)


def _observed_us(d: date) -> date:
    """米国の振替ルール: 土曜→前金曜, 日曜→翌月曜。"""
    if d.weekday() == 5:  # 土
        return d - timedelta(days=1)
    if d.weekday() == 6:  # 日
        return d + timedelta(days=1)
    return d


# ---------------------------------------------------------------------------
# 米国株式（NYSE 準拠）
# ---------------------------------------------------------------------------
def _us_equity_holidays(year: int) -> set[date]:
    h: set[date] = set()
    h.add(_observed_us(date(year, 1, 1)))  # 元日
    h.add(_nth_weekday(year, 1, 0, 3))  # MLK: 1月第3月曜
    h.add(_nth_weekday(year, 2, 0, 3))  # 大統領の日: 2月第3月曜
    h.add(_good_friday(year))  # グッドフライデー
    h.add(_last_weekday(year, 5, 0))  # メモリアルデー: 5月最終月曜
    h.add(_observed_us(date(year, 6, 19)))  # ジューンティーンス
    h.add(_observed_us(date(year, 7, 4)))  # 独立記念日
    h.add(_nth_weekday(year, 9, 0, 1))  # レイバーデー: 9月第1月曜
    h.add(_nth_weekday(year, 11, 3, 4))  # 感謝祭: 11月第4木曜
    h.add(_observed_us(date(year, 12, 25)))  # クリスマス
    return h


def _us_equity_half_days(year: int) -> set[date]:
    """米株の早終い（13:00 ET クローズ）。全日休場の日は半日にしない（整合性のため除外）。"""
    days: set[date] = set()
    # 独立記念日の前日 7/3（平日のとき）
    jul3 = date(year, 7, 3)
    if jul3.weekday() < 5:
        days.add(jul3)
    # 感謝祭の翌日（ブラックフライデー）
    thanksgiving = _nth_weekday(year, 11, 3, 4)
    days.add(thanksgiving + timedelta(days=1))
    # クリスマスイブ 12/24（平日のとき）
    dec24 = date(year, 12, 24)
    if dec24.weekday() < 5:
        days.add(dec24)
    # 振替休日と重なった半日候補は「全日休場」を優先する
    return days - _us_equity_holidays(year)


def _good_friday(year: int) -> date:
    """復活祭（西方教会・Anonymous Gregorian algorithm）の2日前=グッドフライデー。"""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    hh = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ll = (32 + 2 * e + 2 * i - hh - k) % 7
    m = (a + 11 * hh + 22 * ll) // 451
    month = (hh + ll - 7 * m + 114) // 31
    day = ((hh + ll - 7 * m + 114) % 31) + 1
    easter = date(year, month, day)
    return easter - timedelta(days=2)


# ---------------------------------------------------------------------------
# 日本株式（東証）— 固定収録リスト（毎年更新）
# ---------------------------------------------------------------------------
_JP_HOLIDAYS: set[date] = {
    # 2024
    date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 8),
    date(2024, 2, 11), date(2024, 2, 12), date(2024, 2, 23), date(2024, 3, 20),
    date(2024, 4, 29), date(2024, 5, 3), date(2024, 5, 4), date(2024, 5, 5),
    date(2024, 5, 6), date(2024, 7, 15), date(2024, 8, 11), date(2024, 8, 12),
    date(2024, 9, 16), date(2024, 9, 22), date(2024, 9, 23), date(2024, 10, 14),
    date(2024, 11, 3), date(2024, 11, 4), date(2024, 11, 23), date(2024, 12, 31),
    # 2025
    date(2025, 1, 1), date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 13),
    date(2025, 2, 11), date(2025, 2, 23), date(2025, 2, 24), date(2025, 3, 20),
    date(2025, 4, 29), date(2025, 5, 3), date(2025, 5, 4), date(2025, 5, 5),
    date(2025, 5, 6), date(2025, 7, 21), date(2025, 8, 11), date(2025, 9, 15),
    date(2025, 9, 23), date(2025, 10, 13), date(2025, 11, 3), date(2025, 11, 23),
    date(2025, 11, 24), date(2025, 12, 31),
    # 2026
    date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3), date(2026, 1, 12),
    date(2026, 2, 11), date(2026, 2, 23), date(2026, 3, 20), date(2026, 4, 29),
    date(2026, 5, 3), date(2026, 5, 4), date(2026, 5, 5), date(2026, 5, 6),
    date(2026, 7, 20), date(2026, 8, 11), date(2026, 9, 21), date(2026, 9, 22),
    date(2026, 9, 23), date(2026, 10, 12), date(2026, 11, 3), date(2026, 11, 23),
    date(2026, 12, 31),
    # 2027
    date(2027, 1, 1), date(2027, 1, 2), date(2027, 1, 3), date(2027, 1, 11),
    date(2027, 2, 11), date(2027, 2, 23), date(2027, 3, 21), date(2027, 3, 22),
    date(2027, 4, 29), date(2027, 5, 3), date(2027, 5, 4), date(2027, 5, 5),
    date(2027, 7, 19), date(2027, 8, 11), date(2027, 9, 20), date(2027, 9, 23),
    date(2027, 10, 11), date(2027, 11, 3), date(2027, 11, 23), date(2027, 12, 31),
}


# ---------------------------------------------------------------------------
# FX（インターバンク）— 事実上の全世界休場のみ
# ---------------------------------------------------------------------------
def _fx_holidays(year: int) -> set[date]:
    """FX が実質停止する日（元日・クリスマス）。振替はしない（暦日で停止）。"""
    return {date(year, 1, 1), date(year, 12, 25)}


# ---------------------------------------------------------------------------
# 公開 API（domain.py から使う）
# ---------------------------------------------------------------------------
def is_us_equity_holiday(d: date) -> bool:
    if not is_within_coverage(d):
        return False
    return d in _us_equity_holidays(d.year)


def us_equity_early_close_minute(d: date) -> int | None:
    """早終いの日は 13:00 ET を分で返す。通常営業日は None。"""
    if not is_within_coverage(d):
        return None
    if d in _us_equity_half_days(d.year):
        return 13 * 60
    return None


def is_jp_equity_holiday(d: date) -> bool:
    if not is_within_coverage(d):
        return False
    return d in _JP_HOLIDAYS


def is_fx_holiday(d: date) -> bool:
    if not is_within_coverage(d):
        return False
    return d in _fx_holidays(d.year)

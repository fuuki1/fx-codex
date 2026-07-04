"""市場休場カレンダー（market_calendar.py）と within_session への統合の検証。"""
from __future__ import annotations

from datetime import UTC, date, datetime

import market_calendar as mc
from domain import within_session


def dt(y: int, mo: int, d: int, h: int, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


# ── 米株 祝日（NYSE 準拠の既知日） ──────────────────────────────────────────
def test_us_fixed_and_computed_holidays_2024() -> None:
    known = [
        date(2024, 1, 1),   # 元日
        date(2024, 1, 15),  # MLK
        date(2024, 2, 19),  # 大統領の日
        date(2024, 3, 29),  # グッドフライデー
        date(2024, 5, 27),  # メモリアルデー
        date(2024, 6, 19),  # ジューンティーンス
        date(2024, 7, 4),   # 独立記念日
        date(2024, 9, 2),   # レイバーデー
        date(2024, 11, 28), # 感謝祭
        date(2024, 12, 25), # クリスマス
    ]
    for d in known:
        assert mc.is_us_equity_holiday(d), d


def test_us_observed_shift_saturday_to_friday() -> None:
    # 2026-07-04 は土曜 → 前金曜 7/3 が振替休場
    assert not mc.is_us_equity_holiday(date(2026, 7, 4))  # 暦日(土)は営業日扱い
    assert mc.is_us_equity_holiday(date(2026, 7, 3))


def test_us_observed_shift_sunday_to_monday() -> None:
    # 2027-07-04 は日曜 → 翌月曜 7/5 が振替休場
    assert not mc.is_us_equity_holiday(date(2027, 7, 4))  # 暦日(日)は市場が元々休み
    assert mc.is_us_equity_holiday(date(2027, 7, 5))
    # 2027-12-25 は土曜 → 前金曜 12/24 が振替
    assert mc.is_us_equity_holiday(date(2027, 12, 24))


def test_us_half_day_early_close() -> None:
    # ブラックフライデー・クリスマスイブは 13:00 ET
    assert mc.us_equity_early_close_minute(date(2024, 11, 29)) == 13 * 60
    assert mc.us_equity_early_close_minute(date(2024, 12, 24)) == 13 * 60
    # 通常営業日は None
    assert mc.us_equity_early_close_minute(date(2024, 11, 27)) is None


def test_us_half_day_never_overlaps_full_holiday() -> None:
    # 2026-07-03 は振替休場。半日ではなく全日休場が優先される。
    assert mc.is_us_equity_holiday(date(2026, 7, 3))
    assert mc.us_equity_early_close_minute(date(2026, 7, 3)) is None


# ── 日本株 祝日（収録リスト） ────────────────────────────────────────────────
def test_jp_holidays_from_table() -> None:
    assert mc.is_jp_equity_holiday(date(2024, 1, 1))    # 元日
    assert mc.is_jp_equity_holiday(date(2025, 5, 5))    # こどもの日
    assert mc.is_jp_equity_holiday(date(2026, 12, 31))  # 大納会翌日(年末休場)
    assert not mc.is_jp_equity_holiday(date(2025, 6, 2))  # 平日


# ── FX 祝日 ──────────────────────────────────────────────────────────────────
def test_fx_holidays_only_newyear_and_christmas() -> None:
    assert mc.is_fx_holiday(date(2025, 1, 1))
    assert mc.is_fx_holiday(date(2025, 12, 25))
    assert not mc.is_fx_holiday(date(2025, 7, 4))  # 米独立記念日でもFXは動く


# ── フェイルセーフ: 収録レンジ外 ─────────────────────────────────────────────
def test_out_of_coverage_is_not_treated_as_holiday() -> None:
    assert not mc.is_within_coverage(date(2023, 12, 25))
    assert not mc.is_within_coverage(date(2028, 1, 1))
    # レンジ外は「祝日でない」= 週末/時間帯判定に委ねる（正当な取引を誤ブロックしない）
    assert not mc.is_us_equity_holiday(date(2023, 12, 25))
    assert not mc.is_jp_equity_holiday(date(2028, 1, 1))
    assert not mc.is_fx_holiday(date(2023, 1, 1))


# ── within_session への統合 ──────────────────────────────────────────────────
def test_within_session_blocks_us_holiday() -> None:
    # 2024-12-25 15:00 UTC = 10:00 ET（本来は場中）だが祝日で閉場
    assert within_session("us_stock", "AAPL", dt(2024, 12, 25, 15)) is False
    # 前営業日 12/24 は半日（13:00 ET クローズ）: 12:00 ET=17:00 UTC は開、14:00 ET=19:00 UTC は閉
    assert within_session("us_stock", "AAPL", dt(2024, 12, 24, 17)) is True
    assert within_session("us_stock", "AAPL", dt(2024, 12, 24, 19)) is False


def test_within_session_blocks_jp_holiday() -> None:
    # 2025-05-05(こどもの日) 01:00 UTC = 10:00 JST（本来は場中）だが祝日
    assert within_session("jp_stock", "7203", dt(2025, 5, 5, 1)) is False


def test_within_session_blocks_fx_holiday() -> None:
    # 2025-12-25(木) 12:00 UTC は平日だがクリスマスでFX停止
    assert within_session("fx", "USDJPY", dt(2025, 12, 25, 12)) is False
    # 前日 12-24(水) は通常どおり開
    assert within_session("fx", "USDJPY", dt(2025, 12, 24, 12)) is True

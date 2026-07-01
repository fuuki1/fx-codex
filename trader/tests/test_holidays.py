from __future__ import annotations

import json
import os
import time

import pytest


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch):
    """holidays.py はモジュールグローバルにキャッシュするので、テスト間で状態を分離する。"""
    import holidays

    monkeypatch.setattr(holidays, "_cache", {})
    monkeypatch.setattr(holidays, "_mtime", 0.0)
    yield


def _write(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_missing_file_returns_empty_calendar(tmp_path, monkeypatch):
    import holidays

    monkeypatch.setattr(holidays.settings, "market_holidays_file", str(tmp_path / "nope.json"))
    assert holidays.get_calendar() == {}


def test_loads_venue_lists_and_ignores_meta(tmp_path, monkeypatch):
    import holidays

    f = tmp_path / "market_holidays.json"
    _write(f, {"_meta": {"note": "x"}, "jp_stock": ["2024-01-08"], "fx": ["2024-12-25"]})
    monkeypatch.setattr(holidays.settings, "market_holidays_file", str(f))

    cal = holidays.get_calendar()
    assert cal["jp_stock"] == frozenset({"2024-01-08"})
    assert cal["fx"] == frozenset({"2024-12-25"})
    assert "_meta" not in cal


def test_reloads_on_mtime_change(tmp_path, monkeypatch):
    import holidays

    f = tmp_path / "market_holidays.json"
    _write(f, {"jp_stock": ["2024-01-08"]})
    monkeypatch.setattr(holidays.settings, "market_holidays_file", str(f))
    assert holidays.get_calendar()["jp_stock"] == frozenset({"2024-01-08"})

    _write(f, {"jp_stock": ["2024-01-08", "2024-02-12"]})
    os.utime(f, (time.time() + 5, time.time() + 5))  # mtime を確実に進める
    assert holidays.get_calendar()["jp_stock"] == frozenset({"2024-01-08", "2024-02-12"})


def test_malformed_file_keeps_previous_calendar(tmp_path, monkeypatch):
    import holidays

    f = tmp_path / "market_holidays.json"
    _write(f, {"jp_stock": ["2024-01-08"]})
    monkeypatch.setattr(holidays.settings, "market_holidays_file", str(f))
    first = holidays.get_calendar()
    assert first["jp_stock"] == frozenset({"2024-01-08"})

    f.write_text("{not valid json", encoding="utf-8")
    os.utime(f, (time.time() + 5, time.time() + 5))
    second = holidays.get_calendar()
    assert second == first  # 破損時は直前の値を維持

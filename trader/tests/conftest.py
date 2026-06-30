"""pytest 共通設定。

app モジュールを import 可能にし、テスト用の環境変数を *config import 前* に
セットする（config.settings は import 時に確定するため）。Redis は fakeredis に差し替える。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "optimize"))

# 環境変数（既存のシェル変数があればそちらを優先＝setdefault）
os.environ.setdefault("WEBHOOK_SECRET", "testsecret")
os.environ.setdefault("TV_ALLOWED_IPS", "")          # IP 検証を無効化
os.environ.setdefault("ENFORCE_SESSION", "0")         # セッション制御はテストでは無効
os.environ.setdefault("DISCORD_WEBHOOK_URL", "")
os.environ.setdefault("MAX_POSITION_QTY", "10000")
os.environ.setdefault("MAX_ORDERS_PER_MIN", "1000")
os.environ.setdefault("MAX_DAILY_LOSS_JPY", "50000")
os.environ.setdefault("TRADING_MODE", "paper")
# リスクエンジン: テストは既定で安全側（サイジング OFF・週次/通貨/連敗は無効寄り）。
# 個々のテストが必要に応じて settings を monkeypatch して有効化する。
os.environ.setdefault("RISK_SIZING_ENABLED", "0")
os.environ.setdefault("MAX_WEEKLY_LOSS_JPY", "0")
os.environ.setdefault("MAX_CURRENCY_EXPOSURE", "0")
os.environ.setdefault("ACCOUNT_EQUITY", "1000000")

import fakeredis  # noqa: E402
import pytest  # noqa: E402


@pytest.fixture
def fake_redis(monkeypatch):
    import common

    fake = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(common, "_redis", fake)
    return fake

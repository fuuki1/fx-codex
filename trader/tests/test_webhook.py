from __future__ import annotations

import json
import time

from fastapi.testclient import TestClient


def _client(monkeypatch, fake_redis):
    import common
    import webhook

    # DB 書き込み / 通知はモック（CI に DB / Discord は無い）
    monkeypatch.setattr(common, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(common, "notify", lambda *a, **k: None)
    return TestClient(webhook.app)


def _body(**over):
    b = {"secret": "testsecret", "symbol": "USDJPY", "side": "buy", "qty": 1000, "type": "market"}
    b.update(over)
    return b


# ---- 認証・正規化 ----------------------------------------------------------
def test_reject_without_secret(fake_redis, monkeypatch):
    client = _client(monkeypatch, fake_redis)
    r = client.post("/webhook", json={"symbol": "USDJPY", "side": "buy", "qty": 1000, "type": "market"})
    assert r.status_code == 401


def test_accept_then_dedupe(fake_redis, monkeypatch):
    client = _client(monkeypatch, fake_redis)
    body = _body(id="x1")
    r1 = client.post("/webhook", json=body)
    assert r1.status_code == 200
    assert r1.json()["status"] == "accepted"
    assert fake_redis.xlen("signals") == 1

    r2 = client.post("/webhook", json=body)
    assert r2.json()["status"] == "duplicate_ignored"
    assert fake_redis.xlen("signals") == 1  # 重複は配信しない


def test_invalid_signal_400(fake_redis, monkeypatch):
    client = _client(monkeypatch, fake_redis)
    r = client.post("/webhook", json={"secret": "testsecret", "side": "buy", "qty": 1})
    assert r.status_code == 400


# ---- Content-Type 非依存（TradingView は text/plain で送る）-----------------
def test_accept_text_plain_body(fake_redis, monkeypatch):
    """TradingView の Webhook は本文を text/plain で送る。422 にせず受理できること。"""
    client = _client(monkeypatch, fake_redis)
    raw = json.dumps(_body(id="tv1"))
    r = client.post("/webhook", content=raw, headers={"Content-Type": "text/plain; charset=utf-8"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "accepted"
    assert fake_redis.xlen("signals") == 1


def test_accept_without_content_type(fake_redis, monkeypatch):
    client = _client(monkeypatch, fake_redis)
    raw = json.dumps(_body(id="noct"))
    r = client.post("/webhook", content=raw)
    assert r.status_code == 200, r.text


def test_bad_json_400(fake_redis, monkeypatch):
    client = _client(monkeypatch, fake_redis)
    r = client.post("/webhook", content="not-json", headers={"Content-Type": "text/plain"})
    assert r.status_code == 400


def test_body_too_large_413(fake_redis, monkeypatch):
    import webhook

    monkeypatch.setattr(webhook.settings, "max_webhook_body_bytes", 32)
    client = _client(monkeypatch, fake_redis)
    raw = json.dumps(_body(id="big", note="x" * 200))
    r = client.post("/webhook", content=raw, headers={"Content-Type": "text/plain"})
    assert r.status_code == 413


# ---- 鮮度（リプレイ・遅延配信防止）----------------------------------------
def test_stale_signal_rejected_409(fake_redis, monkeypatch):
    import webhook

    monkeypatch.setattr(webhook.settings, "max_signal_age_sec", 60)
    client = _client(monkeypatch, fake_redis)
    # 1 時間前の時刻 -> 古すぎるので 409
    r = client.post("/webhook", json=_body(id="old", ts=time.time() - 3600))
    assert r.status_code == 409
    assert fake_redis.xlen("signals") == 0


def test_fresh_signal_accepted(fake_redis, monkeypatch):
    import webhook

    monkeypatch.setattr(webhook.settings, "max_signal_age_sec", 60)
    client = _client(monkeypatch, fake_redis)
    r = client.post("/webhook", json=_body(id="fresh", ts=time.time()))
    assert r.status_code == 200


# ---- IP 検証（XFF 偽装の迂回を防ぐ）---------------------------------------
def test_xff_spoof_rejected(fake_redis, monkeypatch):
    import webhook

    # 許可は実クライアント 10.0.0.9 のみ。プロキシ 1 段（右端が実クライアント）。
    monkeypatch.setattr(webhook.settings, "tv_allowed_ips", ["10.0.0.9"])
    monkeypatch.setattr(webhook.settings, "tv_trusted_proxy_hops", 1)
    client = _client(monkeypatch, fake_redis)
    # 攻撃者が偽の許可 IP を先頭に詰めても、プロキシが本物(1.2.3.4)を右端に足す想定。
    headers = {"X-Forwarded-For": "10.0.0.9, 1.2.3.4", "Content-Type": "application/json"}
    r = client.post("/webhook", json=_body(id="spoof"), headers=headers)
    assert r.status_code == 403


def test_xff_trusted_hop_accepted(fake_redis, monkeypatch):
    import webhook

    monkeypatch.setattr(webhook.settings, "tv_allowed_ips", ["1.2.3.4"])
    monkeypatch.setattr(webhook.settings, "tv_trusted_proxy_hops", 1)
    client = _client(monkeypatch, fake_redis)
    headers = {"X-Forwarded-For": "10.0.0.9, 1.2.3.4", "Content-Type": "application/json"}
    r = client.post("/webhook", json=_body(id="ok-hop"), headers=headers)
    assert r.status_code == 200, r.text


def test_no_xff_uses_client_host_rejected(fake_redis, monkeypatch):
    """XFF なしの直接アクセスは接続元（testclient）で判定され、許可外なので 403。"""
    import webhook

    monkeypatch.setattr(webhook.settings, "tv_allowed_ips", ["203.0.113.9"])
    client = _client(monkeypatch, fake_redis)
    r = client.post("/webhook", json=_body(id="no-xff"))
    assert r.status_code == 403


# ---- 配信失敗時の復旧（idem 解放 -> 再送可能）------------------------------
def test_publish_failure_releases_idem(fake_redis, monkeypatch):
    import common

    def _boom(*a, **k):
        raise RuntimeError("redis down")

    monkeypatch.setattr(common, "publish", _boom)
    client = _client(monkeypatch, fake_redis)
    r = client.post("/webhook", json=_body(id="pf1"))
    assert r.status_code == 503
    # idem が解放され、再送（publish 復旧後）で受理できること
    assert fake_redis.get("idem:pf1") is None
    monkeypatch.setattr(common, "publish", lambda stream, obj: "1-0")
    r2 = client.post("/webhook", json=_body(id="pf1"))
    assert r2.status_code == 200
    assert r2.json()["status"] == "accepted"

from __future__ import annotations

from fastapi.testclient import TestClient


def _client(monkeypatch, fake_redis):
    import common
    import webhook

    # DB 書き込みはモック（CI に DB は無い）
    monkeypatch.setattr(common, "log_event", lambda *a, **k: None)
    return TestClient(webhook.app)


def test_reject_without_secret(fake_redis, monkeypatch):
    client = _client(monkeypatch, fake_redis)
    r = client.post("/webhook", json={"symbol": "USDJPY", "side": "buy", "qty": 1000, "type": "market"})
    assert r.status_code == 401


def test_accept_then_dedupe(fake_redis, monkeypatch):
    client = _client(monkeypatch, fake_redis)
    body = {
        "secret": "testsecret",
        "symbol": "USDJPY",
        "side": "buy",
        "qty": 1000,
        "type": "market",
        "id": "x1",
    }
    r1 = client.post("/webhook", json=body)
    assert r1.status_code == 200
    assert r1.json()["status"] == "accepted"
    # 配信されたか
    assert fake_redis.xlen("signals") == 1

    r2 = client.post("/webhook", json=body)
    assert r2.json()["status"] == "duplicate_ignored"
    assert fake_redis.xlen("signals") == 1  # 重複は配信しない


def test_invalid_signal_400(fake_redis, monkeypatch):
    client = _client(monkeypatch, fake_redis)
    r = client.post("/webhook", json={"secret": "testsecret", "side": "buy", "qty": 1})
    assert r.status_code == 400

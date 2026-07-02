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


# ---- IP 検証（X-Forwarded-For）---------------------------------------------
TV_IP = "52.89.214.238"
_BODY = {"secret": "testsecret", "symbol": "USDJPY", "side": "buy", "qty": 1000, "id": "ip-test"}


def _client_with_allowlist(monkeypatch, fake_redis):
    import webhook

    client = _client(monkeypatch, fake_redis)
    monkeypatch.setattr(webhook.settings, "tv_allowed_ips", [TV_IP])
    return client


def test_spoofed_xff_rejected(fake_redis, monkeypatch):
    """攻撃者が許可 IP を X-Forwarded-For 先頭に偽装しても、ngrok が右端に追記した
    実接続元で判定される（= 旧実装の allowlist バイパスの回帰テスト）。"""
    client = _client_with_allowlist(monkeypatch, fake_redis)
    r = client.post(
        "/webhook", json=_BODY, headers={"X-Forwarded-For": f"{TV_IP}, 198.51.100.7"}
    )
    assert r.status_code == 403


def test_direct_spoofed_xff_rejected(fake_redis, monkeypatch):
    """プロキシを経ない直接アクセスで偽装 XFF 単体（右端＝偽装値でも、TestClient の
    接続元ではなく XFF を採らざるを得ないケース）。ngrok 前提の allowlist では
    右端が許可 IP のときのみ通る、を確認。"""
    client = _client_with_allowlist(monkeypatch, fake_redis)
    r = client.post("/webhook", json=_BODY, headers={"X-Forwarded-For": "203.0.113.9"})
    assert r.status_code == 403


def test_tradingview_ip_via_ngrok_accepted(fake_redis, monkeypatch):
    """TradingView → ngrok（XFF 右端に実 IP を追記）→ webhook の正常経路。"""
    client = _client_with_allowlist(monkeypatch, fake_redis)
    r = client.post("/webhook", json=_BODY, headers={"X-Forwarded-For": TV_IP})
    assert r.status_code == 200
    assert r.json()["status"] == "accepted"


def test_no_xff_uses_client_host_rejected(fake_redis, monkeypatch):
    """XFF なしの直接アクセスは接続元（testclient）で判定され、許可外なので 403。"""
    client = _client_with_allowlist(monkeypatch, fake_redis)
    r = client.post("/webhook", json=_BODY)
    assert r.status_code == 403


# ---- ボディサイズ上限 --------------------------------------------------------
def test_oversized_body_413(fake_redis, monkeypatch):
    client = _client(monkeypatch, fake_redis)
    r = client.post(
        "/webhook",
        content=b"x",
        headers={"Content-Type": "application/json", "Content-Length": str(1024 * 1024)},
    )
    assert r.status_code == 413


def test_invalid_content_length_400(fake_redis, monkeypatch):
    client = _client(monkeypatch, fake_redis)
    r = client.post(
        "/webhook",
        content=b"{}",
        headers={"Content-Type": "application/json", "Content-Length": "not-a-number"},
    )
    assert r.status_code == 400

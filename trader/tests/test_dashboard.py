from __future__ import annotations

from fastapi.testclient import TestClient


def test_index_serves_chart_page(monkeypatch):
    import dashboard

    monkeypatch.setattr(dashboard.settings, "oanda_api_token", "")   # ネットワークに触れない
    with TestClient(dashboard.app) as client:
        r = client.get("/")
        assert r.status_code == 200
        assert "アドバイザー" in r.text
        assert "lightweight-charts" in r.text          # TradingView 製の無料チャート


def test_api_state_and_health(monkeypatch):
    import dashboard

    monkeypatch.setattr(dashboard.settings, "oanda_api_token", "")
    with TestClient(dashboard.app) as client:
        s = client.get("/api/state")
        assert s.status_code == 200
        assert "ok" in s.json()
        # データが無い（トークン未設定）ので health は degraded=503
        h = client.get("/health")
        assert h.status_code == 503


def test_maybe_notify_only_on_state_change(monkeypatch):
    import common
    import dashboard

    sent: list[str] = []
    monkeypatch.setattr(common, "notify", lambda text, **k: sent.append(text))
    dashboard._last_alert_key = None

    buy = dashboard.analysis.Recommendation(
        action="BUY", strength="strong", last_price=150.0, entry=150.0, stop=149.7,
        take_profit=150.45, stop_distance=0.3, rr=1.5, trend_htf=1, signal_ltf=1,
        fresh_cross=1, session_open=True, reasons=["r"], ts=0.0,
    )
    dashboard._maybe_notify(buy)
    dashboard._maybe_notify(buy)          # 同じ状態は再通知しない
    assert len(sent) == 1
    wait = dashboard.analysis.Recommendation(
        action="WAIT", strength="none", last_price=150.0, entry=None, stop=None,
        take_profit=None, stop_distance=None, rr=None, trend_htf=0, signal_ltf=0,
        fresh_cross=0, session_open=True, reasons=["r"], ts=0.0,
    )
    dashboard._maybe_notify(wait)         # WAIT は通知しない
    assert len(sent) == 1

from __future__ import annotations

from dataclasses import dataclass, field

import requests

from fx_intel import discord_delivery


@dataclass
class FakeResponse:
    status_code: int
    json_payload: object = None
    headers: dict[str, str] = field(default_factory=dict)
    closed: bool = False

    def json(self) -> object:
        if isinstance(self.json_payload, Exception):
            raise self.json_payload
        return self.json_payload

    def close(self) -> None:
        self.closed = True


def test_success_closes_response(monkeypatch):
    response = FakeResponse(204)
    monkeypatch.setattr(discord_delivery.requests, "post", lambda *a, **k: response)
    discord_delivery.send_webhook("https://discord.com/api/webhooks/redacted", {"content": "ok"})
    assert response.closed is True


def test_retries_500_then_succeeds(monkeypatch):
    responses = [FakeResponse(500), FakeResponse(204)]
    sleeps: list[float] = []
    monkeypatch.setattr(
        discord_delivery.requests,
        "post",
        lambda *a, **k: responses.pop(0),
    )
    discord_delivery.send_webhook(
        "https://discord.com/api/webhooks/redacted",
        {"content": "ok"},
        sleep_fn=sleeps.append,
    )
    assert sleeps == [1.0]


def test_network_failure_does_not_leak_webhook(monkeypatch):
    secret_url = "https://discord.com/api/webhooks/secret-token"

    def fail(*args, **kwargs):
        raise requests.exceptions.SSLError(f"failed for {secret_url}")

    monkeypatch.setattr(discord_delivery.requests, "post", fail)
    try:
        discord_delivery.send_webhook(
            secret_url,
            {"content": "x"},
            attempts=2,
            sleep_fn=lambda _seconds: None,
        )
    except discord_delivery.DiscordDeliveryError as error:
        message = str(error)
    else:
        raise AssertionError("delivery must fail")
    assert secret_url not in message
    assert "secret-token" not in message
    assert "SSLError" in message


def test_budget_stops_retrying_slow_endpoint(monkeypatch):
    """実時間予算を超えたら、attemptsが残っていても再試行しない。"""
    posts = 0
    clock = {"now": 0.0}

    def slow_fail(*args, **kwargs):
        nonlocal posts
        posts += 1
        clock["now"] += 30.0  # SSL/timeout系の1試行を模す(接続10+読取20)
        raise requests.exceptions.ConnectTimeout("timed out")

    monkeypatch.setattr(discord_delivery.requests, "post", slow_fail)
    sleeps: list[float] = []

    def sleep_fn(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += seconds

    try:
        discord_delivery.send_webhook(
            "https://discord.com/api/webhooks/redacted",
            {"content": "x"},
            attempts=4,
            budget_seconds=45.0,
            sleep_fn=sleep_fn,
            monotonic_fn=lambda: clock["now"],
        )
    except discord_delivery.DiscordDeliveryError as error:
        message = str(error)
    else:
        raise AssertionError("delivery must fail")

    # 1回目で30秒消費。次のバックオフ1秒を待つ余裕はあるが、
    # 2回目でさらに30秒使い予算45秒を超えるため3回目には進まない
    assert posts == 2
    assert clock["now"] <= 90.0
    assert "budget exhausted" in message
    assert "attempts=2" in message  # 実際の試行回数を報告する


def test_budget_none_preserves_legacy_attempts(monkeypatch):
    """budget_seconds=Noneなら従来どおりattempts回まで試行する。"""
    posts = 0

    def fail(*args, **kwargs):
        nonlocal posts
        posts += 1
        raise requests.exceptions.ConnectTimeout("timed out")

    monkeypatch.setattr(discord_delivery.requests, "post", fail)
    try:
        discord_delivery.send_webhook(
            "https://discord.com/api/webhooks/redacted",
            {"content": "x"},
            attempts=4,
            budget_seconds=None,
            sleep_fn=lambda _seconds: None,
        )
    except discord_delivery.DiscordDeliveryError:
        pass
    else:
        raise AssertionError("delivery must fail")
    assert posts == 4


def test_budget_does_not_delay_non_retryable_404(monkeypatch):
    """404は再試行対象外なので1回で終わる(予算にも触れない)。"""
    posts = 0

    def not_found(*args, **kwargs):
        nonlocal posts
        posts += 1
        return FakeResponse(404)

    monkeypatch.setattr(discord_delivery.requests, "post", not_found)
    sleeps: list[float] = []
    try:
        discord_delivery.send_webhook(
            "https://discord.com/api/webhooks/redacted",
            {"content": "x"},
            attempts=4,
            sleep_fn=sleeps.append,
        )
    except discord_delivery.DiscordDeliveryError as error:
        message = str(error)
    else:
        raise AssertionError("delivery must fail")
    assert posts == 1
    assert sleeps == []
    assert "HTTP 404" in message


def test_rejects_non_positive_budget():
    for bad in (0.0, -1.0):
        try:
            discord_delivery.send_webhook(
                "https://discord.com/api/webhooks/redacted",
                {"content": "x"},
                budget_seconds=bad,
            )
        except ValueError:
            continue
        raise AssertionError(f"budget_seconds={bad} must be rejected")

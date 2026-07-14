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

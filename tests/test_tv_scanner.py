"""管理版 TradingView スキャナークライアントのテスト(ネットワーク不要)。

実機で確認済みの障害を回帰として固定する:
- 既定UA(tradingview_ta/<ver>)は HTTP 429 + 本文0byte を返される。
- 旧経路はステータスを検証せず空本文を json.loads して JSONDecodeError になる。

ここでは fake session で 200正常/429+Retry-After/空本文/HTML本文/部分成功/全失敗を
明示的に検証する。秘密情報・Webhook URL は一切扱わない(スキャナーは公開URL)。
"""

from __future__ import annotations

import json

import pytest

import fx_intel.tv_scanner as tv

# スキャナーが返す data 行の列順。tradingview_ta の指標定義 + 追加ATR。
_INDICATOR_KEYS = list(tv.TradingView.indicators)
_ADDITIONAL = ("ATR",)
_ALL_KEYS = _INDICATOR_KEYS + list(_ADDITIONAL)


class FakeResponse:
    """requests.Response の最小代役(status_code / text / headers のみ)。"""

    def __init__(self, status_code: int, text: str, headers: dict | None = None) -> None:
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


class FakeSession:
    """POSTに対しキューから応答を返す fake。呼び出しヘッダを記録する。

    responses が尽きたら最後の応答を繰り返す(全失敗系で使いやすいように)。
    """

    def __init__(self, responses: list[FakeResponse]) -> None:
        if not responses:
            raise ValueError("responses must not be empty")
        self._responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url, json=None, headers=None, timeout=None):  # noqa: A002 - requests互換
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]


def _row(symbol: str, **overrides: float) -> dict:
    """1銘柄ぶんの data 行({'s':..., 'd':[...]})を作る。overrides で列値を差し込む。"""
    values: list[object] = [None] * len(_ALL_KEYS)
    # レーティング計算が最低限動くだけの値。close は必須。
    defaults = {
        "Recommend.All": 0.5,
        "Recommend.Other": 0.3,
        "Recommend.MA": 0.4,
        "close": 150.10,
        "open": 150.00,
        "high": 150.30,
        "low": 149.90,
    }
    for key, value in {**defaults, **overrides}.items():
        values[_ALL_KEYS.index(key)] = value
    return {"s": symbol, "d": values}


def _scan_body(*symbols: str, **overrides: float) -> str:
    return json.dumps({"data": [_row(symbol, **overrides) for symbol in symbols]})


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """再試行のバックオフで実時間を消費しない(テストを速く・決定的にする)。"""
    monkeypatch.setattr(tv, "_sleep", lambda _seconds: None)


# ------------------------------------------------------------------ 200 正常JSON


def test_200_returns_analysis_and_sets_browser_headers() -> None:
    session = FakeSession([FakeResponse(200, _scan_body("OANDA:USDJPY", close=150.12))])

    result = tv.get_multiple_analysis("forex", "1h", ["OANDA:USDJPY"], _ADDITIONAL, session=session)

    assert set(result) == {"OANDA:USDJPY"}
    analysis = result["OANDA:USDJPY"]
    assert analysis is not None
    assert analysis.indicators["close"] == 150.12
    assert analysis.interval == "1h"
    # ヘッダは明示的にブラウザ互換UA + Accept/Content-Type
    headers = session.calls[0]["headers"]
    assert headers["User-Agent"] == tv.SCANNER_USER_AGENT
    assert "Mozilla" in headers["User-Agent"]
    assert headers["Accept"] == "application/json"
    assert headers["Content-Type"] == "application/json"
    # URLは screener を小文字化した scan エンドポイント
    assert session.calls[0]["url"] == "https://scanner.tradingview.com/forex/scan"
    # 1回で成功。無駄な再試行をしない
    assert len(session.calls) == 1


def test_user_agent_is_configured_in_one_place() -> None:
    """UAの単一設定点(SCANNER_USER_AGENT)を差し替えれば送出UAが変わる。"""
    session = FakeSession([FakeResponse(200, _scan_body("OANDA:USDJPY"))])

    tv.get_multiple_analysis("forex", "1h", ["OANDA:USDJPY"], session=session)

    # 既定UAは429を招く tradingview_ta/<ver> であってはならない
    assert not session.calls[0]["headers"]["User-Agent"].startswith("tradingview_ta/")


# ------------------------------------------------------------ 429 + Retry-After


def test_429_honors_retry_after_then_recovers() -> None:
    slept: list[float] = []
    tv_sleep_calls = slept.append
    session = FakeSession(
        [
            FakeResponse(429, "", {"Retry-After": "2"}),
            FakeResponse(200, _scan_body("OANDA:USDJPY")),
        ]
    )
    # このテストだけ実スリープ時間を観測する(autouseは差し替え済みなので上書き)
    import fx_intel.tv_scanner as module

    original = module._sleep
    module._sleep = tv_sleep_calls  # type: ignore[assignment]
    try:
        result = tv.get_multiple_analysis(
            "forex", "1h", ["OANDA:USDJPY"], session=session, max_attempts=4
        )
    finally:
        module._sleep = original  # type: ignore[assignment]

    assert result["OANDA:USDJPY"] is not None
    assert len(session.calls) == 2  # 429 → 200
    # Retry-After=2 を下限に待った(jitter加算のため2以上)
    assert len(slept) == 1
    assert slept[0] >= 2.0


def test_429_exhausts_bounded_retries_and_raises_rate_limited() -> None:
    session = FakeSession([FakeResponse(429, "", {"Retry-After": "1"})])

    with pytest.raises(tv.ScannerRateLimited) as excinfo:
        tv.get_multiple_analysis("forex", "1h", ["OANDA:USDJPY"], session=session, max_attempts=3)

    assert excinfo.value.status_code == 429
    assert excinfo.value.retry_after == 1.0
    # 無制限再試行はしない: ちょうど max_attempts 回で打ち切る
    assert len(session.calls) == 3


def test_retry_after_non_numeric_is_ignored_and_backoff_used() -> None:
    session = FakeSession([FakeResponse(429, "", {"Retry-After": "soon"})])

    with pytest.raises(tv.ScannerRateLimited) as excinfo:
        tv.get_multiple_analysis("forex", "1h", ["OANDA:USDJPY"], session=session, max_attempts=2)

    # 数値でないRetry-Afterはバックオフに委ねる(retry_after は None)
    assert excinfo.value.retry_after is None
    assert len(session.calls) == 2


# --------------------------------------------------------------- 空本文 / 非JSON


def test_empty_body_on_200_is_typed_failure_not_json_error() -> None:
    """429の空本文を200前提でdecodeする事故を防ぐ。空は明示的な typed failure。"""
    session = FakeSession([FakeResponse(200, "")])

    with pytest.raises(tv.ScannerEmptyBodyError):
        tv.get_multiple_analysis("forex", "1h", ["OANDA:USDJPY"], session=session)
    # 2xxの空本文はクライアント都合の再試行対象にしない(1回で打ち切り)
    assert len(session.calls) == 1


def test_whitespace_body_is_empty_body_failure() -> None:
    session = FakeSession([FakeResponse(200, "   \n\t ")])

    with pytest.raises(tv.ScannerEmptyBodyError):
        tv.get_multiple_analysis("forex", "1h", ["OANDA:USDJPY"], session=session)


def test_html_body_is_typed_non_json_failure() -> None:
    html = "<!DOCTYPE html><html><head><title>Blocked</title></head><body>bot</body></html>"
    session = FakeSession([FakeResponse(200, html)])

    with pytest.raises(tv.ScannerNonJSONError):
        tv.get_multiple_analysis("forex", "1h", ["OANDA:USDJPY"], session=session)


def test_non_json_garbage_body_is_typed_failure() -> None:
    session = FakeSession([FakeResponse(200, "not json at all {oops")])

    with pytest.raises(tv.ScannerNonJSONError):
        tv.get_multiple_analysis("forex", "1h", ["OANDA:USDJPY"], session=session)


def test_json_without_data_field_is_typed_failure() -> None:
    session = FakeSession([FakeResponse(200, json.dumps({"totalCount": 0}))])

    with pytest.raises(tv.ScannerNonJSONError):
        tv.get_multiple_analysis("forex", "1h", ["OANDA:USDJPY"], session=session)


# ------------------------------------------------------------ HTTPステータス各種


def test_500_retries_then_raises_http_error() -> None:
    session = FakeSession([FakeResponse(500, "server exploded")])

    with pytest.raises(tv.ScannerHTTPError) as excinfo:
        tv.get_multiple_analysis("forex", "1h", ["OANDA:USDJPY"], session=session, max_attempts=2)

    assert excinfo.value.status_code == 500
    assert len(session.calls) == 2  # 5xxは再試行対象


def test_403_client_error_does_not_retry() -> None:
    session = FakeSession([FakeResponse(403, "forbidden")])

    with pytest.raises(tv.ScannerHTTPError) as excinfo:
        tv.get_multiple_analysis("forex", "1h", ["OANDA:USDJPY"], session=session, max_attempts=4)

    assert excinfo.value.status_code == 403
    # 4xx(429以外)は恒久的なので再試行しない
    assert len(session.calls) == 1


def test_status_is_checked_before_json_decode() -> None:
    """429の本文が壊れたJSONでも、decodeより先にステータスで弾く(旧バグの核心)。"""
    session = FakeSession([FakeResponse(429, "", {"Retry-After": "1"})])

    # JSONDecodeError ではなく ScannerRateLimited になることを確認
    with pytest.raises(tv.ScannerRateLimited):
        tv.get_multiple_analysis("forex", "1h", ["OANDA:USDJPY"], session=session, max_attempts=1)


# -------------------------------------------------------------------- 部分成功


def test_partial_success_returns_present_symbols_and_none_for_missing() -> None:
    """一部銘柄しか data に無くても、取れた銘柄は返し欠けた銘柄は None にする。"""
    body = _scan_body("OANDA:USDJPY")  # EURUSD は含めない
    session = FakeSession([FakeResponse(200, body)])

    result = tv.get_multiple_analysis(
        "forex", "1h", ["OANDA:USDJPY", "OANDA:EURUSD"], session=session
    )

    assert result["OANDA:USDJPY"] is not None
    assert result["OANDA:EURUSD"] is None


def test_malformed_row_is_skipped_without_failing_whole_batch() -> None:
    """行単位の欠損(s/d 欠落)は全体を落とさず、その銘柄だけ None にする。"""
    good = _row("OANDA:USDJPY")
    body = json.dumps({"data": [good, {"s": "OANDA:EURUSD"}]})  # EURUSDはd欠落
    session = FakeSession([FakeResponse(200, body)])

    result = tv.get_multiple_analysis(
        "forex", "1h", ["OANDA:USDJPY", "OANDA:EURUSD"], session=session
    )

    assert result["OANDA:USDJPY"] is not None
    assert result["OANDA:EURUSD"] is None


# ---------------------------------------------------------- ネットワーク層失敗


def test_network_exception_retries_then_raises_scanner_error() -> None:
    import requests

    class BoomSession:
        def __init__(self) -> None:
            self.calls = 0

        def post(self, *args, **kwargs):
            self.calls += 1
            raise requests.ConnectionError("dns down")

    session = BoomSession()

    with pytest.raises(tv.ScannerError):
        tv.get_multiple_analysis("forex", "1h", ["OANDA:USDJPY"], session=session, max_attempts=3)

    assert session.calls == 3  # ネットワーク層失敗も上限付きで再試行


# ---------------------------------------------------------------- 引数バリデーション


def test_invalid_symbol_format_raises_value_error() -> None:
    session = FakeSession([FakeResponse(200, _scan_body("OANDA:USDJPY"))])

    with pytest.raises(ValueError):
        tv.get_multiple_analysis("forex", "1h", ["USDJPY"], session=session)  # 取引所欠落


def test_empty_symbols_raises_value_error() -> None:
    session = FakeSession([FakeResponse(200, "{}")])

    with pytest.raises(ValueError):
        tv.get_multiple_analysis("forex", "1h", [], session=session)


def test_max_attempts_below_one_raises_value_error() -> None:
    session = FakeSession([FakeResponse(200, _scan_body("OANDA:USDJPY"))])

    with pytest.raises(ValueError):
        tv.get_multiple_analysis("forex", "1h", ["OANDA:USDJPY"], session=session, max_attempts=0)


# ------------------------------------------------------------- バックオフ内部関数


def test_backoff_delay_is_bounded_and_uses_retry_after_floor() -> None:
    # retry_after が大きくても RETRY_AFTER_CAP_SECONDS で丸める
    delay = tv._backoff_delay(0, retry_after=10_000.0)
    assert delay <= tv.RETRY_AFTER_CAP_SECONDS + tv.JITTER_SECONDS
    # 指数項も BACKOFF_MAX_SECONDS で頭打ち
    capped = tv._backoff_delay(20, retry_after=None)
    assert capped <= tv.BACKOFF_MAX_SECONDS + tv.JITTER_SECONDS


def test_parse_retry_after_rejects_negative_and_garbage() -> None:
    assert tv._parse_retry_after("5") == 5.0
    assert tv._parse_retry_after("-3") is None
    assert tv._parse_retry_after("later") is None
    assert tv._parse_retry_after(None) is None

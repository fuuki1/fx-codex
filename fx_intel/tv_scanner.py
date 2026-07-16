"""管理された TradingView スキャナーHTTPクライアント。

`tradingview_ta` の `get_multiple_analysis` は
`scanner.tradingview.com/<screener>/scan` へ `User-Agent: tradingview_ta/<ver>`
でPOSTするが、この既定UAは実機で **HTTP 429 + 本文0byte** を返され、
ライブラリはHTTPステータスを検証せず本文をそのまま `json.loads` するため
`JSONDecodeError: Expecting value` として観測される(採点用価格が取れない)。

本モジュールはその輸送層だけを置き換える。site-packages を編集せず、
ペイロード生成(`TradingView.data`)・指標計算(`calculate`)・戻り値型
(`Analysis`)は **ハッシュ固定済みの `tradingview_ta` から再利用** して、
テクニカル計算やインジケータ順序を再実装しない。輸送層でやることは:

- ブラウザ互換 User-Agent を **一箇所** で設定(`SCANNER_USER_AGENT`)。
  `Accept` / `Content-Type` も明示する。
- JSON decode の **前に** HTTPステータスを検証する。
- 429 は `Retry-After` を尊重し、指数バックオフ + jitter で再試行する。
  再試行回数は上限付き(無制限再試行はしない)。
- 空本文・HTML本文・非JSON本文を、暗黙のクラッシュではなく
  明示的な typed failure(`ScannerError` サブクラス)にする。

`get_multiple_analysis` は `tradingview_ta` と同じ
`{"EXCHANGE:SYMBOL": Analysis | None}` を返すので、呼び出し側は import を
差し替えるだけでよい。秘密情報は扱わない(スキャナーは公開エンドポイント)。
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import requests
from tradingview_ta import Analysis
from tradingview_ta.main import TradingView, calculate

# --- User-Agent はここ一箇所だけで設定する ------------------------------------
# 既定の `tradingview_ta/<ver>` は 429 を返されるため、実機で 200 を返した
# ブラウザ互換UAを使う。UAを変えたいときはこの定数だけ触ればよい。
SCANNER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
SCANNER_ACCEPT = "application/json"
SCANNER_CONTENT_TYPE = "application/json"

DEFAULT_TIMEOUT_SECONDS = 15.0

# 再試行は上限付き。無制限再試行は禁止。
# 全体で MAX_ATTEMPTS 回まで試し、それでも駄目なら typed failure を送出する。
MAX_ATTEMPTS = 4
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 30.0
# Retry-After が桁外れに大きくても待ちすぎないよう上限で丸める(5分ループを塞がない)。
RETRY_AFTER_CAP_SECONDS = 60.0
JITTER_SECONDS = 0.5

# HTMLブロックページ/チャレンジを非JSONとして早期に弾くための素朴な判定。
_HTML_MARKERS = ("<!doctype html", "<html", "<head", "<body", "<!--")


class ScannerError(RuntimeError):
    """スキャナー取得の基底例外(すべて typed failure)。"""


class ScannerHTTPError(ScannerError):
    """2xx以外のHTTPステータス。`status_code` を保持する。"""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class ScannerRateLimited(ScannerHTTPError):
    """HTTP 429。再試行を使い切った後に送出する。"""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(429, message)
        self.retry_after = retry_after


class ScannerEmptyBodyError(ScannerError):
    """ステータスは2xxだが本文が空(429の空本文を200前提でdecodeする事故を防ぐ)。"""


class ScannerNonJSONError(ScannerError):
    """本文がJSONでない(HTMLのブロックページ等)。"""


@dataclass(frozen=True)
class _RequestOutcome:
    """1回のHTTP試行の結果(再試行判断のための中間表現)。"""

    status_code: int
    text: str
    retry_after: float | None


def _sleep(seconds: float) -> None:
    """`time.sleep` の薄いラッパ(テストで差し替え可能にする)。"""

    if seconds > 0:
        time.sleep(seconds)


def _parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """`Retry-After`をdelta-secondsまたはHTTP-dateとしてUTC秒へ変換する。"""

    if not value:
        return None
    try:
        seconds = float(value.strip())
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(value.strip())
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        reference = now or datetime.now(UTC)
        if reference.tzinfo is None:
            raise ValueError("Retry-After reference time must be timezone-aware")
        seconds = (retry_at.astimezone(UTC) - reference.astimezone(UTC)).total_seconds()
    if seconds < 0:
        return None
    return seconds


def _backoff_delay(attempt: int, retry_after: float | None) -> float:
    """次の再試行までの待ち時間(秒)。指数バックオフ + jitter。

    `retry_after`(429のRetry-After)が指定されていればそれを下限に使う。
    attempt は0始まり(0=初回失敗後の待ち)。上限で丸める。
    """

    exponential = BACKOFF_BASE_SECONDS * (2**attempt)
    delay = min(exponential, BACKOFF_MAX_SECONDS)
    if retry_after is not None:
        delay = max(delay, min(retry_after, RETRY_AFTER_CAP_SECONDS))
    return delay + random.uniform(0.0, JITTER_SECONDS)


def _looks_like_html(text: str) -> bool:
    head = text.lstrip()[:512].lower()
    return any(marker in head for marker in _HTML_MARKERS)


def _post_once(
    url: str,
    payload: dict,
    *,
    session: requests.Session | None,
    timeout: float,
) -> _RequestOutcome:
    """スキャナーへ1回だけPOSTし、ステータス/本文/Retry-Afterを取り出す。

    ネットワーク例外は `ScannerError` に正規化して呼び出し側の再試行に委ねる。
    """

    # news.py / macro.py と同じく session 未指定なら module-level requests を使う。
    http = session or requests
    headers = {
        "User-Agent": SCANNER_USER_AGENT,
        "Accept": SCANNER_ACCEPT,
        "Content-Type": SCANNER_CONTENT_TYPE,
    }
    try:
        response = http.post(url, json=payload, headers=headers, timeout=timeout)
    except requests.RequestException as error:
        raise ScannerError(f"scanner request failed: {error}") from error
    retry_after = _parse_retry_after(response.headers.get("Retry-After"))
    return _RequestOutcome(
        status_code=response.status_code,
        text=response.text,
        retry_after=retry_after,
    )


def _decode_scan_rows(text: str) -> list[dict]:
    """2xx本文をJSON decodeし `data` 配列を返す。

    空本文・HTML・非JSON・`data` 欠落を明示的な typed failure にする。
    JSON decode の前に呼び出し側でステータス検証済みであることが前提。
    """

    if not text.strip():
        raise ScannerEmptyBodyError("scanner returned an empty body on a 2xx response")
    if _looks_like_html(text):
        raise ScannerNonJSONError("scanner returned an HTML body instead of JSON")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as error:
        raise ScannerNonJSONError(f"scanner body is not valid JSON: {error}") from error
    if not isinstance(parsed, dict) or "data" not in parsed:
        raise ScannerNonJSONError("scanner JSON is missing the 'data' field")
    data = parsed["data"]
    if not isinstance(data, list):
        raise ScannerNonJSONError("scanner 'data' field is not a list")
    return data


def fetch_scan(
    screener: str,
    interval: str,
    symbols: Sequence[str],
    additional_indicators: Sequence[str] = (),
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    session: requests.Session | None = None,
    max_attempts: int = MAX_ATTEMPTS,
) -> tuple[list[dict], list[str]]:
    """スキャナーへPOSTし `data` 行の生リストと使用したインジケータkeyを返す。

    - User-Agent/Accept/Content-Type を明示。
    - HTTPステータスを JSON decode の前に検証。
    - 429 は Retry-After 尊重 + 指数バックオフ + jitter で `max_attempts` まで再試行。
    - 空/HTML/非JSON本文は typed failure(`ScannerError` サブクラス)。

    戻り値は (data行のリスト, indicators_key)。呼び出し側で `calculate` に渡す。
    """

    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    indicators_key = list(TradingView.indicators)
    if additional_indicators:
        indicators_key += list(additional_indicators)
    payload = TradingView.data(list(symbols), interval, indicators_key)
    url = f"{TradingView.scan_url}{screener.lower()}/scan"

    last_error: ScannerError | None = None
    for attempt in range(max_attempts):
        try:
            outcome = _post_once(url, payload, session=session, timeout=timeout)
        except ScannerError as error:
            # ネットワーク層の失敗も上限付きで再試行する。
            last_error = error
            if attempt + 1 < max_attempts:
                _sleep(_backoff_delay(attempt, None))
                continue
            raise

        if outcome.status_code == 429:
            last_error = ScannerRateLimited(
                f"scanner rate limited (HTTP 429) on {interval}",
                retry_after=outcome.retry_after,
            )
            if attempt + 1 < max_attempts:
                _sleep(_backoff_delay(attempt, outcome.retry_after))
                continue
            raise last_error

        if outcome.status_code >= 500:
            # サーバ側一時障害。上限付きで再試行。
            last_error = ScannerHTTPError(
                outcome.status_code,
                f"scanner server error (HTTP {outcome.status_code}) on {interval}",
            )
            if attempt + 1 < max_attempts:
                _sleep(_backoff_delay(attempt, outcome.retry_after))
                continue
            raise last_error

        if not 200 <= outcome.status_code < 300:
            # 4xx(429以外)はクライアント側の恒久的問題なので再試行しない。
            raise ScannerHTTPError(
                outcome.status_code,
                f"scanner returned HTTP {outcome.status_code} on {interval}",
            )

        # ステータス検証を通過してから初めてJSON decodeする。
        data = _decode_scan_rows(outcome.text)
        return data, indicators_key

    # ループはraise/returnで抜けるためここには到達しない。防御的に送出する。
    raise last_error or ScannerError(f"scanner fetch failed on {interval}")


def get_multiple_analysis(
    screener: str,
    interval: str,
    symbols: Sequence[str],
    additional_indicators: Sequence[str] = (),
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    session: requests.Session | None = None,
    max_attempts: int = MAX_ATTEMPTS,
) -> dict[str, Analysis | None]:
    """`tradingview_ta.get_multiple_analysis` 互換の管理版。

    戻り値は `{"EXCHANGE:SYMBOL": Analysis | None}`。取得できなかった銘柄は None。
    輸送層(UA/ステータス検証/429バックオフ/typed failure)だけを差し替え、
    指標計算は `tradingview_ta.calculate` を再利用する。
    """

    if not screener or not isinstance(screener, str):
        raise ValueError("screener must be a non-empty string")
    requested = list(symbols)
    if not requested:
        raise ValueError("symbols must be a non-empty list")
    for symbol in requested:
        parts = symbol.split(":")
        if len(parts) != 2 or "" in parts:
            raise ValueError(
                "each symbol must be 'EXCHANGE:SYMBOL' (e.g. 'OANDA:USDJPY'); " f"got {symbol!r}"
            )

    data, indicators_key = fetch_scan(
        screener,
        interval,
        requested,
        additional_indicators,
        timeout=timeout,
        session=session,
        max_attempts=max_attempts,
    )

    final: dict[str, Analysis | None] = {}
    for row in data:
        symbol_key = row.get("s")
        values = row.get("d")
        if not isinstance(symbol_key, str) or not isinstance(values, list):
            # 個別行の欠損は全体を落とさず None 扱いにする(部分成功維持)。
            continue
        indicators = {
            indicators_key[i]: values[i] for i in range(min(len(values), len(indicators_key)))
        }
        exchange, _, symbol = symbol_key.partition(":")
        final[symbol_key] = calculate(
            indicators=indicators,
            indicators_key=indicators_key,
            screener=screener,
            symbol=symbol,
            exchange=exchange,
            interval=interval,
        )

    for symbol in requested:
        if symbol.upper() not in final:
            final[symbol.upper()] = None
    return final

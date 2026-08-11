"""収集済み Dukascopy bid/ask を判断時 quote 行へ変換する(読み取り専用)。

``tools/fx_datafeed_collector.py`` が ``collect/log/quotes.jsonl`` へ追記した
実測 bid/ask を読み、``fx_briefing`` の ``broker_quotes`` と同じ形へ整える。

## このモジュールが主張しないこと

Dukascopy の収集は ``collection_mode="historical_download"`` であり、
collector 自身が ``tradable: false`` と申告する。すなわち
「その瞬間に約定できたはずの気配」ではない。したがってここでは:

- ``role`` は ``decision_quote`` だが ``tradeable`` は **False** を明示する
- ``source`` は ``dukascopy_historical_datafeed`` に固定し、
  ``executable_cost_model_id_for_source`` からは**解決されない**
- spread は実測値なので net-R の計算には使える(そこが導入の目的)

ネットワークアクセスは持たない。collector が書いた行を読むだけなので、
発注経路とは構造的に無関係である。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime, timedelta, UTC
import hashlib
import json
from pathlib import Path

from .evaluation_labels import DUKASCOPY_HISTORICAL_QUOTE_SOURCE

# collector の出力先(repository root からの相対)。
DEFAULT_QUOTE_LOG_PATH = Path("collect/log/quotes.jsonl")
# 判断時刻からこれ以上古い気配は使わない。historical download は確定済みの
# 過去1時間を取り込むため、既定は余裕を持たせつつ「古すぎる」を弾く。
DEFAULT_MAX_AGE = timedelta(hours=3)
# 末尾からこのバイト数だけ走査する。ファイルは数百MBに達するため全読みしない。
# collectorはsymbolごとに大きな連続ブロックで書くため、窓が小さいと
# 最後のsymbolしか入らない(実機で2MBではGBPUSDのみ・3通貨には約40MB必要)。
# 余裕を見て64MBとし、全symbolが窓に入ることをテストで固定する。
DEFAULT_TAIL_BYTES = 64_000_000


def _normalize_symbol(value: object) -> str:
    symbol = str(value or "").upper()
    for separator in ("/", "_", "-"):
        symbol = symbol.replace(separator, "")
    return symbol


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _aware(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _content_hash(row: Mapping[str, object]) -> str:
    canonical = json.dumps(
        {key: value for key, value in row.items() if key != "content_hash"},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _tail_lines(path: Path, tail_bytes: int) -> Iterator[str]:
    """末尾 ``tail_bytes`` 分だけを行として返す(先頭の欠けた行は捨てる)."""

    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        start = max(0, size - tail_bytes)
        handle.seek(start)
        chunk = handle.read()
    text = chunk.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if start > 0 and lines:
        lines = lines[1:]  # 途中で切れた先頭行は破棄
    return iter(lines)


def parse_decision_quotes(
    lines: Sequence[str] | Iterator[str],
    symbols: Sequence[str],
    *,
    now: datetime,
    max_age: timedelta = DEFAULT_MAX_AGE,
) -> tuple[dict[str, dict[str, object]], list[str]]:
    """収集行から symbol ごとの最新 quote を組み立てる。

    同一 symbol は ``provider_event_time`` が最も新しい行を採用する。時刻は
    ``provider_event_time <= received_at <= now`` の aware UTC を必須とし、
    一行でも違反したsymbolは古い行へfallbackせず判断対象から除外する。
    """

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if max_age < timedelta(0):
        raise ValueError("max_age must not be negative")
    now_utc = now.astimezone(UTC)
    wanted = {_normalize_symbol(symbol) for symbol in symbols}
    latest: dict[str, tuple[datetime, Mapping[str, object]]] = {}
    invalid_timestamp_symbols: set[str] = set()
    warnings: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or "dukascopy" not in stripped:
            continue
        try:
            row = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, Mapping) or row.get("provider") != "dukascopy":
            continue
        symbol = _normalize_symbol(row.get("instrument"))
        if symbol not in wanted:
            continue
        if symbol in invalid_timestamp_symbols:
            continue
        observed = _aware(row.get("provider_event_time"))
        received = _aware(row.get("received_at"))
        if observed is None or received is None:
            invalid_timestamp_symbols.add(symbol)
            latest.pop(symbol, None)
            warnings.append(
                f"{symbol}: dukascopy気配のprovider_event_time/received_atは"
                "timezone付きISO-8601が必須です"
            )
            continue
        if observed > now_utc or received > now_utc:
            invalid_timestamp_symbols.add(symbol)
            latest.pop(symbol, None)
            warnings.append(f"{symbol}: dukascopy気配に判断時点より未来の時刻があります")
            continue
        if observed > received:
            invalid_timestamp_symbols.add(symbol)
            latest.pop(symbol, None)
            warnings.append(f"{symbol}: dukascopy気配のreceived_atが観測時刻より前です")
            continue
        previous = latest.get(symbol)
        if previous is None or observed > previous[0]:
            latest[symbol] = (observed, row)

    output: dict[str, dict[str, object]] = {}
    for symbol in sorted(wanted):
        if symbol in invalid_timestamp_symbols:
            continue
        entry = latest.get(symbol)
        if entry is None:
            warnings.append(f"{symbol}: dukascopy気配が見つかりません")
            continue
        observed, row = entry
        bid = _number(row.get("bid"))
        ask = _number(row.get("ask"))
        if bid is None or ask is None or bid <= 0 or ask < bid:
            warnings.append(f"{symbol}: dukascopy気配のbid/askが不正です")
            continue
        age = now_utc - observed
        if age > max_age:
            warnings.append(
                f"{symbol}: dukascopy気配が古すぎます({int(age.total_seconds() // 60)}分前)"
            )
            continue
        if str(row.get("quality_state") or "") != "usable":
            warnings.append(f"{symbol}: dukascopy気配のquality_stateがusableではありません")
            continue
        # 取り込み完了時刻をavailabilityとする。上の入力境界で
        # observed <= received <= now を検証済みなので補完やmax()は不要。
        received = _aware(row.get("received_at"))
        if received is None:  # guarded above; retain fail-closed behavior if code changes
            warnings.append(f"{symbol}: dukascopy気配のreceived_atを再検証できません")
            continue
        quote: dict[str, object] = {
            "schema_version": 1,
            "symbol": symbol,
            "bid": bid,
            "ask": ask,
            "spread": ask - bid,
            "observed_at": observed.isoformat(),
            "available_time": received.isoformat(),
            "ingested_time": received.isoformat(),
            "source": DUKASCOPY_HISTORICAL_QUOTE_SOURCE,
            "role": "decision_quote",
            "source_record_id": f"{symbol}:dukascopy:{observed.isoformat()}",
            # collector の自己申告を保持する。live quote ではないという事実を
            # 下流へそのまま伝えるための最重要フィールド。
            "tradeable": False,
            "status": str(row.get("quality_state") or ""),
        }
        quote["content_hash"] = _content_hash(quote)
        output[symbol] = quote
    return output, warnings


def fetch_decision_quotes(
    symbols: Sequence[str],
    *,
    now: datetime,
    quote_log_path: Path,
    max_age: timedelta = DEFAULT_MAX_AGE,
    tail_bytes: int = DEFAULT_TAIL_BYTES,
) -> tuple[dict[str, dict[str, object]], list[str]]:
    """collector が書いた quote log の末尾から判断時 quote を読む。"""

    path = Path(quote_log_path)
    if not path.exists():
        return {}, [f"dukascopy quote logが存在しません: {path}"]
    try:
        lines = _tail_lines(path, tail_bytes)
    except OSError as error:
        return {}, [f"dukascopy quote logを読めません: {error}"]
    return parse_decision_quotes(lines, symbols, now=now, max_age=max_age)

#!/usr/bin/env python3
"""Dukascopy tick の確定足を採点用の価格経路へ供給する(launchd entrypoint)。

採点は TP/SL 到達を価格経路の high/low で判定するが、実機の価格行は
形成中バーのスナップショットしか持たず、PIT 保護により high/low が棄却される
(実測 100% が close_only_path)。このツールは収集済み tick を確定足に畳んで
`logs/briefing_tf_prices.jsonl` へ追記し、採点に信頼できる経路を与える。

この process は構造的に発注できない: 読むのは収集済みの JSONL だけで、
ネットワークアクセスも broker 経路も持たない。

## 安全性

- **既定は dry-run**。実際に書くには `--write` を明示する。
- 専用 timeframe (`5m_datafeed`) で書くため、既存の 15m/1h/4h/1d 行とは
  自然キーが衝突しない。既存の 5 分スナップショット writer には触れない。
- 期間が終了し、かつ collector がその時間帯を書き終えた足だけを書く。
  順序は「collector state を読む → quotes のサイズを固定する → as_of を取る」。
  state が完了を示す時間帯の tick は state 更新より前に追記済みなので、
  固定したサイズまでに必ず全部含まれ、その received_at は as_of 以前になる。
- 追記は `append_snapshot_entries` の advisory lock 下で行う。既存行と内容が
  食い違う足は書かず(既存行も書き換えない)、衝突として
  `logs/price_path_adapter_conflicts.jsonl` に追記記録し、他の足の追記は続ける。

Fail-closed:
- 入力(collector state / quotes)を読めない -> exit 66 (EX_NOINPUT)
- 書き込み失敗、または新しい衝突を記録した -> exit 74 (EX_IOERR)
  (衝突した足以外は追記済み。記録済みの同一衝突は次回以降 exit 0)
- 想定外 -> exit 70 (EX_SOFTWARE)
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fx_intel.price_history import (  # noqa: E402
    PriceHistoryWriteError,
    append_snapshot_entries,
)
from fx_intel.price_path_adapter import (  # noqa: E402
    ADAPTER_TIMEFRAME,
    ADAPTER_WRITER_ID,
    PricePathAdapterError,
    TickQuote,
    adapter_window_start,
    build_closed_bars,
    build_rows,
    iter_quote_file,
    load_collector_completion,
    quote_index_start_offset,
)

EX_NOINPUT = 66
EX_SOFTWARE = 70
EX_IOERR = 74

DEFAULT_QUOTES = REPO_ROOT / "collect" / "log" / "quotes.jsonl"
DEFAULT_PRICES = REPO_ROOT / "logs" / "briefing_tf_prices.jsonl"
DEFAULT_COLLECTOR_STATE = REPO_ROOT / "collect" / "state" / "dukascopy_history.json"
DEFAULT_CONFLICT_LEDGER = REPO_ROOT / "logs" / "price_path_adapter_conflicts.jsonl"
DEFAULT_QUOTE_INDEX = REPO_ROOT / "collect" / "index" / "quotes.sqlite3"
CONFLICT_LEDGER_CONTRACT = "fx-price-path-adapter-conflict-v1"
_CONFLICT_FIELDS = ("open", "high", "low", "close", "bid", "ask", "spread", "tick_count")


def _run_id(now: datetime) -> str:
    stamp = now.strftime("%Y%m%dT%H%M%S%f")
    return f"pricepath-{stamp}Z-{os.getpid()}"


def _writer_id() -> str:
    """このアダプタの論理 writer ID。

    PID を含めてはいけない。`_same_snapshot` は writer_id 不一致を内容比較より
    先に「競合」と判定するため、process ごとに変えると再実行が必ず失敗する。
    """
    return ADAPTER_WRITER_ID


class _Counted:
    """tick を流しながら件数だけ数える(全件をメモリに載せない)。"""

    def __init__(self, ticks: Iterable[TickQuote]) -> None:
        self._ticks = ticks
        self.count = 0

    def __iter__(self) -> Iterator[TickQuote]:
        for tick in self._ticks:
            self.count += 1
            yield tick


def _conflict_signature(conflict: Mapping[str, object]) -> str:
    existing = conflict.get("existing")
    candidate = conflict.get("candidate")
    existing_hash = existing.get("content_hash") if isinstance(existing, Mapping) else None
    values = (
        [candidate.get(field) for field in _CONFLICT_FIELDS]
        if isinstance(candidate, Mapping)
        else []
    )
    return json.dumps(
        [conflict.get("natural_key"), existing_hash, values],
        sort_keys=True,
        separators=(",", ":"),
    )


def _load_known_conflicts(path: Path) -> set[str]:
    if not path.exists():
        return set()
    known: set[str] = set()
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            record = json.loads(line)
            signature = record.get("signature") if isinstance(record, Mapping) else None
            if isinstance(signature, str):
                known.add(signature)
    return known


def _record_new_conflicts(
    path: Path,
    conflicts: Sequence[Mapping[str, object]],
    *,
    run_id: str,
    now: datetime,
) -> list[dict[str, object]]:
    """まだ記録していない衝突だけを追記し、それを返す。"""
    known = _load_known_conflicts(path)
    fresh: list[dict[str, object]] = []
    for conflict in conflicts:
        signature = _conflict_signature(conflict)
        if signature in known:
            continue
        known.add(signature)
        existing = conflict.get("existing")
        candidate = conflict.get("candidate")
        fresh.append(
            {
                "contract": CONFLICT_LEDGER_CONTRACT,
                "recorded_at": now.isoformat(),
                "run_id": run_id,
                "natural_key": conflict.get("natural_key"),
                "signature": signature,
                "resolution": "kept_existing_row_rejected_candidate",
                "existing": dict(existing) if isinstance(existing, Mapping) else None,
                "candidate": dict(candidate) if isinstance(candidate, Mapping) else None,
            }
        )
    if fresh:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for record in fresh:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    return fresh


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quotes", default=str(DEFAULT_QUOTES))
    parser.add_argument("--prices", default=str(DEFAULT_PRICES))
    parser.add_argument("--collector-state", default=str(DEFAULT_COLLECTOR_STATE))
    parser.add_argument("--conflict-ledger", default=str(DEFAULT_CONFLICT_LEDGER))
    parser.add_argument(
        "--quote-index",
        default=str(DEFAULT_QUOTE_INDEX),
        help="読み取り開始位置を決めるための quote index(使えなければ先頭から読む)",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="実際に追記する(既定は dry-run で件数と内容だけ報告)",
    )
    parser.add_argument("--limit", type=int, default=0, help="出力するバー数の上限(検証用)")
    args = parser.parse_args(argv)

    # 順序が PIT の本体: state → quotes のサイズ → as_of。
    try:
        complete_through = load_collector_completion(args.collector_state)
    except PricePathAdapterError as error:
        print(f"入力を読めません: {error}", file=sys.stderr)
        return EX_NOINPUT
    quotes_path = Path(args.quotes)
    try:
        quotes_end_offset = quotes_path.stat().st_size if quotes_path.exists() else 0
    except OSError as error:
        print(f"入力を読めません: {error}", file=sys.stderr)
        return EX_NOINPUT
    now = datetime.now(UTC)

    try:
        window_start = adapter_window_start(args.prices, complete_through)
        read_start, seek_reason = quote_index_start_offset(
            quotes_path,
            args.quote_index,
            symbols=complete_through,
            since=window_start,
            end_offset=quotes_end_offset,
        )
        ticks = _Counted(
            iter_quote_file(
                quotes_path,
                end_offset=quotes_end_offset,
                since=window_start,
                start_offset=read_start,
            )
        )
        bars = build_closed_bars(ticks, as_of=now, complete_through=complete_through)
    except PricePathAdapterError as error:
        print(f"入力を読めません: {error}", file=sys.stderr)
        return EX_NOINPUT
    if args.limit > 0:
        bars = bars[-args.limit :]
    run_id = _run_id(now)
    rows = build_rows(bars, run_id=run_id, writer_id=_writer_id())

    symbols: dict[str, int] = {}
    for bar in bars:
        symbols[bar.symbol] = symbols.get(bar.symbol, 0) + 1

    report: dict[str, object] = {
        "mode": "write" if args.write else "dry-run",
        "as_of": now.isoformat(),
        "collector_complete_through": {
            symbol: moment.isoformat() for symbol, moment in sorted(complete_through.items())
        },
        "quotes_read_offset_bytes": quotes_end_offset,
        "quotes_read_start_offset_bytes": read_start,
        "quote_index_seek": seek_reason,
        "window_start": window_start.isoformat() if window_start else None,
        "ticks_usable": ticks.count,
        "closed_bars": len(bars),
        "by_symbol": symbols,
        "timeframe": ADAPTER_TIMEFRAME,
        "first_bar": bars[0].bar_end.isoformat() if bars else None,
        "last_bar": bars[-1].bar_end.isoformat() if bars else None,
    }

    if not args.write:
        report["sample_row"] = rows[-1] if rows else None
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    conflicts: list[dict[str, object]] = []
    try:
        appended = append_snapshot_entries(args.prices, rows, conflicts=conflicts)
        fresh = _record_new_conflicts(Path(args.conflict_ledger), conflicts, run_id=run_id, now=now)
    except PriceHistoryWriteError as error:
        print(f"追記に失敗しました: {error}", file=sys.stderr)
        return EX_IOERR
    except (OSError, ValueError) as error:
        print(f"I/O 失敗: {error}", file=sys.stderr)
        return EX_IOERR

    report["appended"] = appended
    report["skipped_duplicates"] = len(rows) - appended - len(conflicts)
    report["conflicts_total"] = len(conflicts)
    report["conflicts_new"] = len(fresh)
    report["conflict_keys"] = [conflict.get("natural_key") for conflict in conflicts]
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if fresh:
        keys = ", ".join(str(record.get("natural_key")) for record in fresh)
        print(
            f"既存行と食い違う足を書かずに記録しました({args.conflict_ledger}): {keys}",
            file=sys.stderr,
        )
        return EX_IOERR
    return 0


if __name__ == "__main__":  # pragma: no cover
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(EX_SOFTWARE) from None

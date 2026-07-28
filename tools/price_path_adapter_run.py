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
- 期間が終了したバーだけを書く(形成中バーは書かない)。
- 追記は `append_snapshot_entries` の advisory lock 下で行い、同一キーに
  別内容が来た場合は writer が fail-closed で拒否する。

Fail-closed:
- 入力を読めない -> exit 66 (EX_NOINPUT)
- 書き込み失敗/競合 -> exit 74 (EX_IOERR)
- 想定外 -> exit 70 (EX_SOFTWARE)
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import socket
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
    PricePathAdapterError,
    build_closed_bars,
    build_rows,
    load_quotes,
)

EX_NOINPUT = 66
EX_SOFTWARE = 70
EX_IOERR = 74

DEFAULT_QUOTES = REPO_ROOT / "collect" / "log" / "quotes.jsonl"
DEFAULT_PRICES = REPO_ROOT / "logs" / "briefing_tf_prices.jsonl"


def _run_id(now: datetime) -> str:
    stamp = now.strftime("%Y%m%dT%H%M%S%f")
    return f"pricepath-{stamp}Z-{os.getpid()}"


def _writer_id() -> str:
    return f"{socket.gethostname().split('.')[0]}:{os.getpid()}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quotes", default=str(DEFAULT_QUOTES))
    parser.add_argument("--prices", default=str(DEFAULT_PRICES))
    parser.add_argument(
        "--write",
        action="store_true",
        help="実際に追記する(既定は dry-run で件数と内容だけ報告)",
    )
    parser.add_argument("--limit", type=int, default=0, help="出力するバー数の上限(検証用)")
    args = parser.parse_args(argv)

    now = datetime.now(UTC)
    try:
        ticks = load_quotes(args.quotes)
    except PricePathAdapterError as error:
        print(f"入力を読めません: {error}", file=sys.stderr)
        return EX_NOINPUT

    bars = build_closed_bars(ticks, as_of=now)
    if args.limit > 0:
        bars = bars[-args.limit :]
    rows = build_rows(bars, run_id=_run_id(now), writer_id=_writer_id())

    symbols: dict[str, int] = {}
    for bar in bars:
        symbols[bar.symbol] = symbols.get(bar.symbol, 0) + 1

    report = {
        "mode": "write" if args.write else "dry-run",
        "as_of": now.isoformat(),
        "ticks_usable": len(ticks),
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

    try:
        appended = append_snapshot_entries(args.prices, rows)
    except PriceHistoryWriteError as error:
        print(f"追記に失敗しました(既存行とは競合していない前提が崩れた): {error}", file=sys.stderr)
        return EX_IOERR
    except OSError as error:
        print(f"I/O 失敗: {error}", file=sys.stderr)
        return EX_IOERR

    report["appended"] = appended
    report["skipped_duplicates"] = len(rows) - appended
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(EX_SOFTWARE) from None

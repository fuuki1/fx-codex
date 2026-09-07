#!/usr/bin/env python3
"""Read-only FX bid/ask collector for credential-free datafeeds (launchd entrypoint).

OANDA の read-only pricing 資格情報が用意できるまでの間、実 bid/ask を
供給するための収集器。Dukascopy の公開 tick datafeed を、OANDA collector と
**同一の raw-first 経路**(``data_platform.collect.raw_first.ingest_payload``)へ
流し込む。

この process は構造的に発注できない:
``data_platform.collect`` しか import せず、executor も order endpoint も持たない。
資格情報も不要で、参照するのは公開 datafeed のみ。

## OANDA へ切り替えるとき

収集器は provider ごとに分かれているが、**下流は完全に共通**である。

    dukascopy.parse_hour_payload ─┐
                                   ├─→ CollectedQuote ─→ ingest_payload ─→ raw store + QuoteLog
    oanda.parse_price_line       ─┘

したがって切り替えは「どの launchd service を有効にするか」だけで済む。

    # 今                                    # OANDA が使えるようになったら
    com.fx-codex.datafeed-collector  →      com.fx-codex.quote-collector
    (このスクリプト)                          (tools/fx_quote_collector.py)

同時稼働も可能で、その場合 ``CollectedQuote.provider`` が
``"dukascopy"`` / ``"oanda"`` で区別されるため、後から
どちらを正準とするかを選べる(片方を落とす必要はない)。

## この収集器の限界(誇張しない)

- ``collection_mode = "historical_download"``、``tradable = False``。
  live stream ではなく**確定済みの過去 1 時間**を取り込む
- したがって「その瞬間に約定できたはずの気配」ではない。
  spread の実測値としては使えるが、live quote の代替ではない
- 最新の未確定時間は取得しない(確定するまで待つ)

Fail-closed:
- 取得失敗 -> exit 69 (EX_UNAVAILABLE)
- lock 保持済み -> exit 75 (EX_TEMPFAIL)
- I/O 失敗 -> exit 74 (EX_IOERR)
- 想定外 -> exit 70 (EX_SOFTWARE)
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import socket
import sys
import traceback

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from data_platform.collect.dukascopy import (  # noqa: E402
    DukascopyFetchError,
    ingest_hour,
    requests_fetcher,
)
from data_platform.collect.raw_first import QuoteLog  # noqa: E402
from data_platform.raw.immutable_store import ImmutableRawStore  # noqa: E402
from tools.run_exclusive import ExclusiveLock  # noqa: E402

EX_OK = 0
EX_UNAVAILABLE = 69
EX_SOFTWARE = 70
EX_IOERR = 74
EX_TEMPFAIL = 75

DEFAULT_INSTRUMENTS = ("USDJPY", "EURUSD", "GBPUSD")
# 直近の時間はまだ確定していないので取りに行かない。
# 2 時間前を既定にして、provider 側の確定を待つ。
DEFAULT_LAG_HOURS = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument(
        "--instruments",
        nargs="+",
        default=list(DEFAULT_INSTRUMENTS),
        help="収集する通貨ペア(既定: USDJPY EURUSD GBPUSD)",
    )
    parser.add_argument(
        "--lag-hours",
        type=int,
        default=DEFAULT_LAG_HOURS,
        help="何時間前の確定済みデータを取り込むか(既定: 2)",
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=1,
        help="1 回の実行で遡って取り込む時間数(既定: 1)",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _writer_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _write_state(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output_root: Path = args.output_root
    started = datetime.now(UTC)

    plan = {
        "provider": "dukascopy",
        "instruments": list(args.instruments),
        "lag_hours": args.lag_hours,
        "hours": args.hours,
        "output_root": str(output_root),
        "credentials_required": False,
        "collection_mode": "historical_download",
        "tradable": False,
    }

    if args.dry_run:
        print("[datafeed] plan: " + json.dumps(plan, sort_keys=True))
        print("[datafeed] no credentials required; public datafeed only")
        return EX_OK

    output_root.mkdir(parents=True, exist_ok=True)
    lock = ExclusiveLock("fx-datafeed-collector", locks_dir=output_root / "locks")
    if not lock.acquire():
        print("[datafeed] another collector holds the lock", file=sys.stderr)
        return EX_TEMPFAIL

    results: list[dict[str, object]] = []
    exit_code = EX_OK
    try:
        store = ImmutableRawStore(output_root / "raw")
        log = QuoteLog(output_root / "log")
        connection_id = f"datafeed-{started:%Y%m%dT%H%M%SZ}"

        base_hour = (started - timedelta(hours=args.lag_hours)).replace(
            minute=0, second=0, microsecond=0
        )
        for back in range(args.hours):
            hour = base_hour - timedelta(hours=back)
            for instrument in args.instruments:
                entry: dict[str, object] = {
                    "instrument": instrument,
                    "hour": hour.isoformat(),
                }
                try:
                    result = ingest_hour(
                        instrument,
                        hour,
                        fetcher=requests_fetcher,
                        store=store,
                        log=log,
                        connection_id=connection_id,
                    )
                except DukascopyFetchError as error:
                    entry["status"] = "fetch_failed"
                    entry["error"] = str(error)[:200]
                    exit_code = EX_UNAVAILABLE
                except OSError as error:
                    entry["status"] = "io_error"
                    entry["error"] = str(error)[:200]
                    exit_code = EX_IOERR
                else:
                    if result is None:
                        entry["status"] = "empty_hour"
                    else:
                        entry["status"] = "ingested"
                        entry["quotes"] = result.accepted_count
                        entry["quarantined"] = len(result.quarantined)
                        entry["raw_sha256"] = result.raw_sha256
                results.append(entry)

        _write_state(
            output_root / "state" / "datafeed-collector.json",
            {
                "schema_version": 1,
                "provider": "dukascopy",
                "started_at": started.isoformat(),
                "completed_at": datetime.now(UTC).isoformat(),
                "writer_id": _writer_id(),
                "plan": plan,
                "results": results,
                "exit_code": exit_code,
            },
        )
        ingested = sum(1 for r in results if r.get("status") == "ingested")
        print(f"[datafeed] hours processed={len(results)} ingested={ingested}")
        return exit_code
    except Exception:  # noqa: BLE001 - launchd へ終了コードで伝える
        traceback.print_exc()
        return EX_SOFTWARE
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())

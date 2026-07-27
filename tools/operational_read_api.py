#!/usr/bin/env python3
"""Serve the compact SQLite read model on loopback only."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fx_intel.operational_read_api import (  # noqa: E402
    OperationalReadHTTPServer,
    make_handler,
)
from fx_intel.operational_read_model import read_model_metadata  # noqa: E402
from fx_intel.operational_store import (  # noqa: E402
    OperationalStoreError,
    open_operational_reader,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="SQLite operational read modelをloopback HTTPで公開する"
    )
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8770)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.port <= 65_535:
        return _error("port must be between 1 and 65535")
    try:
        with open_operational_reader(args.db) as store:
            metadata = read_model_metadata(store)
        server = OperationalReadHTTPServer(
            ("127.0.0.1", args.port),
            make_handler(args.db),
        )
    except (OperationalStoreError, OSError, ValueError) as error:
        return _error(str(error))
    print(
        json.dumps(
            {
                "ok": True,
                "listen": f"http://127.0.0.1:{args.port}",
                "database": str(args.db.resolve()),
                "metadata": metadata,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _error(message: str) -> int:
    print(json.dumps({"ok": False, "error": message}, ensure_ascii=False), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

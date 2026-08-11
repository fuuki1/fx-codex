#!/usr/bin/env python3
"""Incrementally index the append-only historical quote JSONL."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fx_intel.quote_tape_index import QuoteIndexError, sync_quote_index  # noqa: E402


def _atomic_write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Update a SQLite byte-offset index without mutating raw quote history"
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--status-output", type=Path)
    parser.add_argument("--batch-size", type=int, default=10_000)
    args = parser.parse_args()
    try:
        result = sync_quote_index(
            args.source,
            args.db,
            now=datetime.now(UTC),
            batch_size=args.batch_size,
        )
    except QuoteIndexError as error:
        print(
            json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 1
    if args.status_output is not None:
        _atomic_write(args.status_output.expanduser().resolve(), result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

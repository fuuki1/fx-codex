#!/usr/bin/env python3
"""Score decision JSONL with completed bid/ask JSONL and persist canonical outcomes."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fx_intel.canonical_outcome_pipeline import (  # noqa: E402
    score_and_store_decision_events,
)
from fx_intel.canonical_outcome_store import (  # noqa: E402
    export_canonical_outcomes,
    verify_canonical_outcome_store,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Offline canonical outcome scorer. Inputs must be immutable decision "
            "events and completed executable bid/ask bars."
        )
    )
    parser.add_argument("--decisions", required=True, type=Path)
    parser.add_argument("--prices", required=True, type=Path)
    parser.add_argument("--store", required=True, type=Path)
    parser.add_argument("--export", type=Path)
    parser.add_argument("--report", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    decisions = _read_jsonl(args.decisions)
    prices = _read_jsonl(args.prices)
    result = score_and_store_decision_events(decisions, prices, args.store)
    payload = result.to_dict()
    if args.store.exists():
        audit = verify_canonical_outcome_store(args.store)
        payload["store_audit"] = audit.to_dict()
        if args.export is not None:
            export_canonical_outcomes(args.store, args.export)
            payload["export"] = str(args.export)
    elif args.export is not None:
        payload["export"] = None
        payload["export_reason"] = "no_persistable_outcomes"
    if args.report is not None:
        _atomic_write_json(args.report, payload)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise RuntimeError(f"cannot read required JSONL: {path}") from error
    rows: list[dict[str, object]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, ValueError) as error:
            raise RuntimeError(f"malformed JSONL at {path}:{line_number}") from error
        if not isinstance(value, dict):
            raise RuntimeError(f"non-object JSONL row at {path}:{line_number}")
        rows.append(value)
    return rows


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


if __name__ == "__main__":
    raise SystemExit(main())

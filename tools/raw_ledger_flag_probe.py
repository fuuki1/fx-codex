#!/usr/bin/env python3
"""Safely report whether the private collector env explicitly enables Raw Ledger."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from data_platform.collect.collector_env import (  # noqa: E402
    CollectorEnvironmentError,
    load_private_collector_env,
)

RAW_LEDGER_FLAG = "FX_RAW_LEDGER_ENABLED"
ALLOWED_KEYS = frozenset(
    {
        "FX_OANDA_API_TOKEN",
        "FX_OANDA_ACCOUNT_ID",
        "FX_OANDA_ENV",
        RAW_LEDGER_FLAG,
    }
)
TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("env_file", type=Path)
    args = parser.parse_args(argv)
    try:
        environment = load_private_collector_env(args.env_file, allowed_keys=ALLOWED_KEYS)
    except CollectorEnvironmentError as error:
        print(f"[collector-flag-probe] configuration rejected: {error}", file=sys.stderr)
        return 78
    enabled = environment.get(RAW_LEDGER_FLAG, "").strip().lower() in TRUE_VALUES
    return 0 if enabled else 1


if __name__ == "__main__":
    raise SystemExit(main())

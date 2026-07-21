#!/usr/bin/env python3
"""2020–2025のbid/askチャートを固定期間分割でshadow学習する。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import cast

from fx_intel import historical_chart


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bars-root", type=Path, default=Path("data/historical_training/histdata/bars_m5")
    )
    parser.add_argument(
        "--artifact",
        type=Path,
        default=Path("data/historical_training/artifacts/historical_chart_model.json"),
    )
    parser.add_argument(
        "--dashboard-report", type=Path, default=Path("logs/historical_chart_training.json")
    )
    args = parser.parse_args(argv)
    payload = historical_chart.train_historical_models(args.bars_root)
    historical_chart.save_artifact(payload, args.artifact)
    cells = cast(list[dict[str, object]], payload["cells"])
    report = dict(payload)
    report["artifact_path"] = str(args.artifact)
    report["cells"] = [
        {key: value for key, value in cell.items() if key != "model"} for cell in cells
    ]
    historical_chart.save_artifact(report, args.dashboard_report)
    summary = {
        "trained_at": payload["trained_at"],
        "cells": len(cells),
        "beats_baseline": sum(
            bool(cast(dict[str, object], cell["metrics"])["beats_baseline"]) for cell in cells
        ),
        "artifact": str(args.artifact),
        "report": str(args.dashboard_report),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

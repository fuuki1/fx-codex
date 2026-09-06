#!/usr/bin/env python3
"""Build one immutable-input, atomic dashboard state cache."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import gc
import hashlib
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.ai_learning_dashboard.server import (  # noqa: E402
    DEFAULT_LOG_DIR,
    DEFAULT_VIRTUAL_PORTFOLIO_API_URL,
    DEFAULT_VIRTUAL_PORTFOLIO_DB,
    build_state,
)

DEFAULT_OUTPUT = DEFAULT_LOG_DIR / "dashboard_state_cache.json"


def _atomic_write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build one read-only dashboard state cache")
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--portfolio-db", type=Path, default=DEFAULT_VIRTUAL_PORTFOLIO_DB)
    parser.add_argument(
        "--portfolio-api-url",
        default=DEFAULT_VIRTUAL_PORTFOLIO_API_URL,
    )
    parser.add_argument(
        "--direct-portfolio-db",
        action="store_true",
        help="read the portfolio DB directly instead of waiting on the loopback API",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    generated = datetime.now(UTC)
    # 読み込み中は巡回GCを止める。JSONの解析結果は参照循環を作らないので、
    # 参照カウントだけで解放され、循環回収は取りこぼしを生まない。
    #
    # 実機のsample採取では、走行中のworkerの 5862/5869 サンプル(99.9%)が
    # gc_collect_main -> list_traverse -> visit_decref に居た。世代別GCが
    # 生存中の大量のdictを繰り返し走査するためで、オブジェクトが増えるほど
    # 1回のgen2回収が重くなる。最大のジャーナル1本だけでも実測で
    # 12.6-14.2秒 -> 6.4秒 (約2.1倍) になる。
    #
    # このプロセスは書き出して即終了するため、循環が残っても終了時に解放される。
    gc.disable()
    try:
        state = build_state(
            args.log_dir.expanduser().resolve(),
            portfolio_db=args.portfolio_db.expanduser().resolve(),
            portfolio_api_url=None if args.direct_portfolio_db else args.portfolio_api_url,
            now=generated,
        )
    finally:
        gc.enable()
    state.pop("log_dir", None)
    canonical = json.dumps(
        state,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    state["dashboard_cache"] = {
        "contract": "fx-dashboard-state-cache-v1",
        "generated_at": generated.isoformat(),
        "source_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "read_only": True,
    }
    _atomic_write(args.output.expanduser().resolve(), state)
    print(
        json.dumps(
            {
                "ok": True,
                "output": str(args.output.expanduser().resolve()),
                "generated_at": generated.isoformat(),
                "source_sha256": state["dashboard_cache"]["source_sha256"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

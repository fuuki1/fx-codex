#!/usr/bin/env python3
"""Create one Canonical v2 ledger by replaying the committed decision history.

This is analysis-only. It reads immutable decision and Dukascopy bid/ask evidence,
never contacts a broker, and refuses to replace an existing ledger or manifest.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fx_intel import decision_commit, decision_log  # noqa: E402
from fx_intel.virtual_portfolio import (  # noqa: E402
    DEFAULT_WRITER_ID,
    VirtualPortfolioPolicy,
    open_virtual_portfolio_writer,
)
from fx_intel.virtual_portfolio_replay import run_replay_cycle  # noqa: E402

CONTRACT = "canonical-virtual-portfolio-full-replay-v2"
DEFAULT_DECISIONS = REPO_ROOT / "logs" / "briefing_decisions.jsonl"
DEFAULT_QUOTES = REPO_ROOT / "collect" / "log" / "quotes.jsonl"
DEFAULT_DB = REPO_ROOT / "logs" / "fx_virtual_portfolio_canonical_v2.sqlite3"


def _aware(value: str) -> datetime:
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must be timezone-aware")
    return moment.astimezone(UTC)


def _file_identity(path: Path) -> dict[str, object]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return {"path": str(path.resolve()), "size_bytes": size, "sha256": digest.hexdigest()}


def _git_identity(root: Path) -> dict[str, object]:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return {"commit": commit, "dirty": bool(status.strip())}


def _canonical_decision(event: Mapping[str, object]) -> dict[str, object]:
    nested = event.get("decision")
    decision = dict(nested) if isinstance(nested, Mapping) else {}
    for key in (
        "decision_id",
        "ts",
        "prediction_time",
        "source_cutoff",
        "max_feature_available_time",
        "input_context_id",
        "source_record_ids",
        "pit_eligible",
        "pit_contract",
        "producer",
        "producer_version",
        "mode",
        "symbol",
        "timeframe",
    ):
        if key in event:
            decision[key] = event[key]
    return decision


def _decision_time(decision: Mapping[str, object]) -> datetime:
    raw = decision.get("prediction_time") or decision.get("ts")
    try:
        moment = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError) as error:
        raise ValueError("decision history contains an invalid prediction timestamp") from error
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("decision history contains a naive prediction timestamp")
    return moment.astimezone(UTC)


def _write_create_only(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with path.open("x", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()


def run_full_replay(
    *,
    decisions: Path,
    decision_commits: Path,
    quotes: Path,
    quote_index: Path | None,
    database: Path,
    manifest: Path,
    as_of: datetime,
    writer_id: str,
    repository_root: Path = REPO_ROOT,
    require_clean_git: bool = True,
) -> dict[str, object]:
    for output in (database, manifest):
        if output.exists():
            raise FileExistsError(f"create-only output already exists: {output}")
    if not decisions.is_file() or not quotes.is_file():
        raise FileNotFoundError("decision history and quote log are both required")

    git = _git_identity(repository_root)
    if require_clean_git and git["dirty"] is True:
        raise RuntimeError("formal Canonical v2 replay requires a clean worktree")

    raw_events = list(decision_log.read_decision_events(decisions, commit_path=decision_commits))
    canonical = [_canonical_decision(event) for event in raw_events if isinstance(event, Mapping)]
    if not canonical:
        raise RuntimeError("committed decision history is empty")
    ordered = sorted(canonical, key=lambda row: (_decision_time(row), str(row.get("decision_id"))))
    earliest = _decision_time(ordered[0])
    if as_of < earliest:
        raise ValueError("replay as_of precedes the first committed decision")

    inputs: dict[str, object] = {
        "decisions": _file_identity(decisions),
        "quotes": _file_identity(quotes),
    }
    if decision_commits.is_file():
        inputs["decision_commits"] = _file_identity(decision_commits)
    if quote_index is not None and quote_index.is_file():
        inputs["quote_index"] = _file_identity(quote_index)
    batch_sha = hashlib.sha256(
        json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    database.parent.mkdir(parents=True, exist_ok=True)
    conflicts: list[dict[str, str]] = []
    queued = 0
    with open_virtual_portfolio_writer(database, writer_id=writer_id) as store:
        store.initialize_portfolio(
            policy=VirtualPortfolioPolicy(),
            created_at=earliest - timedelta(microseconds=1),
        )
        for decision in ordered:
            identity = str(decision.get("decision_id") or "unknown")
            try:
                result = store.queue_decision(
                    decision,
                    observed_at=_decision_time(decision),
                    source=str(decisions.resolve()),
                    batch_id=f"full-history-sha256:{batch_sha}",
                )
                queued += int(result["inserted"] is True)
            except Exception as error:
                conflicts.append({"record_id": identity, "error": str(error)})
        if conflicts:
            raise RuntimeError(f"decision intake conflicts: {len(conflicts)}")
        replay = run_replay_cycle(
            store,
            quote_log=quotes,
            quote_index=quote_index,
            as_of=as_of,
        )
        audit = store.audit_virtual_portfolio_v2()
        snapshot = store.snapshot(now=as_of)

    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    database_identity = _file_identity(database)
    replay_conflicts = replay.get("conflicts")
    replay_conflict_count = len(replay_conflicts) if isinstance(replay_conflicts, list) else 1
    eligible = bool(
        replay.get("status") in {"ok", "idle"}
        and int(replay.get("waiting", 0)) == 0
        and replay_conflict_count == 0
        and audit.get("passed") is True
        and integrity == "ok"
    )
    report: dict[str, object] = {
        "contract": CONTRACT,
        "generated_at": datetime.now(UTC).isoformat(),
        "as_of": as_of.isoformat(),
        "git": git,
        "writer_id": writer_id,
        "inputs": inputs,
        "input_bundle_sha256": batch_sha,
        "committed_event_count": len(raw_events),
        "canonical_decision_count": len(ordered),
        "queued_count": queued,
        "replay": replay,
        "audit": audit,
        "sqlite_integrity_check": integrity,
        "database": database_identity,
        "snapshot": snapshot,
        "eligible_to_resume_shadow": eligible,
        "broker_connected": False,
        "execution_mode": "offline_historical_bid_ask_replay",
    }
    _write_create_only(manifest, report)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--decisions", type=Path, default=DEFAULT_DECISIONS)
    parser.add_argument("--decision-commits", type=Path)
    parser.add_argument("--quotes", type=Path, default=DEFAULT_QUOTES)
    parser.add_argument("--quote-index", type=Path)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--as-of", type=_aware, required=True)
    parser.add_argument("--writer-id", default=DEFAULT_WRITER_ID)
    return parser


def main() -> int:
    args = _parser().parse_args()
    commits = args.decision_commits or decision_commit.commit_path_for(args.decisions)
    manifest = args.manifest or args.db.with_suffix(".replay-manifest.json")
    try:
        report = run_full_replay(
            decisions=args.decisions.resolve(),
            decision_commits=commits.resolve(),
            quotes=args.quotes.resolve(),
            quote_index=args.quote_index.resolve() if args.quote_index else None,
            database=args.db.resolve(),
            manifest=manifest.resolve(),
            as_of=args.as_of,
            writer_id=args.writer_id,
        )
    except (OSError, RuntimeError, ValueError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["eligible_to_resume_shadow"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())

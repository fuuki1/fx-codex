#!/usr/bin/env python3
"""Create an immutable weekly challenger-validation gate artifact."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fx_intel.virtual_portfolio import (  # noqa: E402
    open_virtual_portfolio_reader,
)
from fx_intel.multi_axis_learning import build_multi_axis_shadow_model  # noqa: E402
from fx_intel.virtual_portfolio_learning import (  # noqa: E402
    build_challenger_manifest,
    build_outcome_memory,
    build_promotion_audit,
    build_validation_evidence,
)

DEFAULT_DB = REPO_ROOT / "logs" / "fx_virtual_portfolio_canonical_v2.sqlite3"
DEFAULT_RUN_ROOT = REPO_ROOT / "runs" / "virtual_portfolio_learning"


def _create_artifacts(
    run_root: Path,
    manifest: dict[str, object],
    memory: dict[str, object],
    evidence: dict[str, object],
    promotion_audit: dict[str, object],
    multi_axis_model: dict[str, object],
) -> tuple[Path, Path, Path, Path, Path]:
    generated = datetime.fromisoformat(str(manifest["generated_at"]))
    dataset_hash = str(manifest["source_dataset_sha256"])
    run_id = f"{generated.strftime('%Y%m%dT%H%M%SZ')}-{dataset_hash[:12]}"
    root = run_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    run_dir = root / run_id
    if run_dir.exists():
        raise FileExistsError(f"immutable learning run already exists: {run_dir}")
    staging = Path(tempfile.mkdtemp(prefix=f".{run_id}.staging-", dir=root))
    payloads = {
        "challenger_gate.json": manifest,
        "outcome_memory.json": memory,
        "validation_evidence.json": evidence,
        "promotion_audit.json": promotion_audit,
        "multi_axis_model.json": multi_axis_model,
    }
    file_hashes: dict[str, str] = {}
    for name, payload in payloads.items():
        encoded = (
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode()
        descriptor = os.open(
            staging / name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o444,
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        file_hashes[name] = hashlib.sha256(encoded).hexdigest()
    completion = {
        "contract": "virtual-portfolio-learning-run-completion-v1",
        "run_id": run_id,
        "files": file_hashes,
    }
    completion_bytes = (
        json.dumps(completion, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()
    descriptor = os.open(
        staging / "completion.json",
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o444,
    )
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(completion_bytes)
        handle.flush()
        os.fsync(handle.fileno())
    os.rename(staging, run_dir)
    root_descriptor = os.open(root, os.O_RDONLY)
    try:
        os.fsync(root_descriptor)
    finally:
        os.close(root_descriptor)
    target = run_dir / "challenger_gate.json"
    memory_target = run_dir / "outcome_memory.json"
    evidence_target = run_dir / "validation_evidence.json"
    promotion_target = run_dir / "promotion_audit.json"
    multi_axis_target = run_dir / "multi_axis_model.json"
    return target, memory_target, evidence_target, promotion_target, multi_axis_target


def _git_binding(root: Path) -> tuple[str | None, bool | None]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout.strip()
        )
    except (OSError, subprocess.SubprocessError):
        return None, None
    return commit or None, dirty


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a fail-closed weekly virtual-portfolio challenger gate"
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    parser.add_argument("--minimum-rows", type=int, default=150)
    parser.add_argument("--embargo-hours", type=int, default=24)
    args = parser.parse_args()
    try:
        now = datetime.now(UTC)
        with open_virtual_portfolio_reader(args.db) as store:
            learning = store.export_learning_rows(as_of=now)
        manifest = build_challenger_manifest(
            learning,
            as_of=now,
            minimum_rows=args.minimum_rows,
            embargo_hours=args.embargo_hours,
        )
        memory = build_outcome_memory(learning, as_of=now)
        evidence = build_validation_evidence(learning, manifest, as_of=now)
        git_commit, dirty_worktree = _git_binding(REPO_ROOT)
        promotion_audit = build_promotion_audit(
            learning,
            manifest,
            evidence,
            git_commit=git_commit,
            dirty_worktree=dirty_worktree,
            as_of=now,
        )
        multi_axis_model = build_multi_axis_shadow_model(
            learning,
            manifest,
            as_of=now,
        )
        (
            target,
            memory_target,
            evidence_target,
            promotion_target,
            multi_axis_target,
        ) = _create_artifacts(
            args.run_root,
            manifest,
            memory,
            evidence,
            promotion_audit,
            multi_axis_model,
        )
    except (OSError, RuntimeError, ValueError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 2
    print(
        json.dumps(
            {
                "ok": True,
                "artifact": str(target),
                "outcome_memory": str(memory_target),
                "validation_evidence": str(evidence_target),
                "promotion_audit": str(promotion_target),
                "multi_axis_model": str(multi_axis_target),
                "multi_axis_status": multi_axis_model["status"],
                "status": manifest["status"],
                "eligible_for_candidate_training": manifest["eligible_for_candidate_training"],
                "promotion_eligible": promotion_audit["promotion_eligible"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

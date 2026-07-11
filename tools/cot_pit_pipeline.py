#!/usr/bin/env python3
"""Explicit operator CLI for the research-only CFTC COT PIT boundary.

This command is intentionally not wired into launchd, the Mac mini, or model
promotion.  Every mutating command requires explicit paths; audit and as-of are
read-only.  Release evidence remains a locally bound operator attestation.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fx_backtester.pit_dataset import PITDatasetError  # noqa: E402
from fx_backtester.point_in_time import PointInTimeError  # noqa: E402
from fx_intel import cot_pit  # noqa: E402
from fx_intel.macro import COT_CONTRACT_CODES  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="CFTC Legacy COTのresearch-only PIT取得・証跡・監査・as-of読込"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    capture = commands.add_parser("capture", help="CFTC filtered datasetを完全pagination取得")
    capture.add_argument("--capture-root", type=Path, required=True)
    capture.add_argument("--page-size", type=_positive_int, default=cot_pit.COT_PAGE_SIZE)
    capture.add_argument("--run-id")
    capture.add_argument("--writer-id")

    attest = commands.add_parser("attest", help="ローカルrelease evidence sidecarを作成")
    attest.add_argument("--output", type=Path, required=True)
    attest.add_argument("--evidence", type=Path, required=True)
    attest.add_argument("--report-date", type=_iso_date, required=True)
    attest.add_argument(
        "--basis",
        choices=("scheduled", "actual_release_notice"),
        required=True,
    )
    attest.add_argument("--released-at", type=_aware_datetime, required=True)
    attest.add_argument("--evidence-uri", required=True)
    attest.add_argument("--run-id")
    attest.add_argument("--writer-id")

    materialize = commands.add_parser("materialize", help="明示raw入力からPIT artifactを作成")
    materialize.add_argument("--root", type=Path, required=True)
    materialize.add_argument("--capture", type=Path, action="append", default=[])
    materialize.add_argument(
        "--release",
        type=Path,
        nargs=2,
        action="append",
        default=[],
        metavar=("SIDECAR", "EVIDENCE"),
    )
    materialize.add_argument("--previous-dataset", type=Path)

    audit = commands.add_parser("audit", help="artifactをrawから再構成して監査")
    audit.add_argument("dataset", type=Path)

    as_of = commands.add_parser("as-of", help="prediction time時点のtyped COT stateを読込")
    as_of.add_argument("dataset", type=Path)
    as_of.add_argument("--prediction-time", type=_aware_datetime, required=True)
    as_of.add_argument(
        "--required-currencies",
        choices=tuple(sorted(COT_CONTRACT_CODES)),
        nargs="+",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "capture":
            payload, exit_code = _capture(args)
        elif args.command == "attest":
            payload, exit_code = _attest(args)
        elif args.command == "materialize":
            payload, exit_code = _materialize(args)
        elif args.command == "audit":
            payload, exit_code = _audit(args)
        elif args.command == "as-of":
            payload, exit_code = _as_of(args)
        else:  # pragma: no cover - argparse requires a registered command
            parser.error(f"unsupported command: {args.command}")
            return 2
    except (
        cot_pit.COTPITError,
        PITDatasetError,
        PointInTimeError,
        OSError,
        subprocess.SubprocessError,
        ValueError,
    ) as error:
        _write_json(
            {
                "command": args.command,
                "error": str(error),
                "error_type": type(error).__name__,
                "status": "error",
            },
            stream=sys.stderr,
        )
        return 1
    _write_json(payload, stream=sys.stdout)
    return exit_code


def _capture(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    capture = cot_pit.fetch_cot_capture(
        args.capture_root,
        page_size=args.page_size,
        run_id=args.run_id,
        writer_id=args.writer_id,
    )
    return (
        {
            "acquired_at": capture.acquired_at.isoformat(),
            "capture_id": capture.capture_id,
            "capture_path": str(capture.path),
            "command": "capture",
            "promotion_eligible": False,
            "research_only": True,
            "run_id": capture.run_id,
            "status": "ok",
            "validated_at": capture.validated_at.isoformat(),
            "writer_id": capture.writer_id,
        },
        0,
    )


def _attest(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    output = _operator_path(args.output)
    evidence = _operator_path(args.evidence)
    if output == evidence:
        raise cot_pit.COTPITError("attestation output and release evidence must be different files")
    captured_at = _now_utc()
    run_id = args.run_id or f"cftc-release-{args.report_date}-{captured_at:%Y%m%dT%H%M%S%fZ}"
    writer_id = args.writer_id or f"{socket.gethostname()}:{os.getpid()}"
    attestation = cot_pit.write_cot_release_attestation(
        output,
        evidence,
        report_date=args.report_date,
        basis=args.basis,
        released_at=args.released_at,
        evidence_uri=args.evidence_uri,
        evidence_captured_at=captured_at,
        run_id=run_id,
        writer_id=writer_id,
    )
    return (
        {
            "attestation_path": str(attestation.attestation_path),
            "basis": attestation.basis,
            "command": "attest",
            "evidence_captured_at": attestation.evidence_captured_at.isoformat(),
            "evidence_path": str(attestation.evidence_path),
            "evidence_sha256": attestation.evidence_sha256,
            "evidence_uri": attestation.evidence_uri,
            "promotion_eligible": False,
            "released_at": attestation.released_at.isoformat(),
            "report_date": attestation.report_date.isoformat(),
            "research_only": True,
            "run_id": attestation.run_id,
            "status": "ok",
            "writer_id": attestation.writer_id,
        },
        0,
    )


def _materialize(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    previous = args.previous_dataset
    if previous is not None:
        prior_audit = cot_pit.audit_cot_pit_dataset(previous)
        if not prior_audit.passed:
            raise cot_pit.COTPITError(
                "previous COT PIT dataset failed domain audit: " + "; ".join(prior_audit.errors)
            )
    captures = [cot_pit.COTCapture(path) for path in args.capture]
    attestations = [
        cot_pit.COTReleaseAttestation(sidecar, evidence) for sidecar, evidence in args.release
    ]
    commit, dirty = _git_provenance(REPO_ROOT)
    artifact = cot_pit.materialize_cot_pit_dataset(
        args.root,
        captures,
        release_attestations=attestations,
        previous_dataset=previous,
        created_at=_now_utc(),
        code_commit=commit,
        dirty_worktree=dirty,
    )
    audit = cot_pit.audit_cot_pit_dataset(artifact.directory)
    if not audit.passed:  # materializer already checks; keep CLI output fail-closed
        raise cot_pit.COTPITError("new COT PIT dataset failed audit: " + "; ".join(audit.errors))
    return (
        {
            "audit": _audit_payload(audit),
            "code_commit": commit,
            "command": "materialize",
            "dataset_dir": str(artifact.directory),
            "dataset_id": artifact.dataset_id,
            "dirty_worktree": dirty,
            "promotion_eligible": False,
            "research_only": True,
            "status": "ok",
        },
        0,
    )


def _audit(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    result = cot_pit.audit_cot_pit_dataset(args.dataset)
    payload = {
        "command": "audit",
        "dataset": str(args.dataset.expanduser().resolve()),
        **_audit_payload(result),
    }
    return payload, 0 if result.passed else 1


def _as_of(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    result = cot_pit.load_cot_as_of(
        args.dataset,
        args.prediction_time,
        required_currencies=args.required_currencies,
    )
    reports: dict[str, dict[str, object]] = {}
    for currency, report in result.reports.items():
        reports[currency] = {
            "available_time": report.available_time.isoformat() if report.available_time else None,
            "content_hash": report.content_hash,
            "data_quality_flags": list(report.data_quality_flags),
            "dataset_id": report.dataset_id,
            "net_position": report.net_position,
            "open_interest": report.open_interest,
            "prev_net_position": report.prev_net_position,
            "report_date": report.report_date.isoformat(),
            "source_record_id": report.source_record_id,
        }
    payload = {
        **result.to_dict(),
        "command": "as-of",
        "dataset": str(args.dataset.expanduser().resolve()),
        "reports": reports,
    }
    return payload, 0 if result.usable else 1


def _audit_payload(result: cot_pit.COTPITAudit) -> dict[str, object]:
    return {
        "errors": list(result.errors),
        "observation_count": result.observation_count,
        "passed": result.passed,
        "promotion_eligible": False,
        "release_attestation_count": result.release_attestation_count,
        "research_only": True,
        "status": "passed" if result.passed else "failed",
        "warnings": list(result.warnings),
    }


def _git_provenance(repository_root: Path) -> tuple[str, bool]:
    commit = _git_output(repository_root, "rev-parse", "HEAD")
    status = _git_output(repository_root, "status", "--porcelain", "--untracked-files=normal")
    if len(commit) not in (40, 64) or any(
        character not in "0123456789abcdef" for character in commit
    ):
        raise cot_pit.COTPITError("Git HEAD is not a full lowercase object ID")
    return commit, bool(status)


def _git_output(repository_root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _operator_path(value: Path) -> Path:
    return Path(os.path.abspath(os.fspath(value.expanduser())))


def _aware_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an ISO-8601 datetime") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("must be timezone-aware")
    return parsed.astimezone(UTC)


def _iso_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an ISO date (YYYY-MM-DD)") from error
    if parsed.isoformat() != value:
        raise argparse.ArgumentTypeError("must be a canonical ISO date (YYYY-MM-DD)")
    return parsed


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _write_json(payload: Mapping[str, object], *, stream: Any) -> None:
    json.dump(
        dict(payload),
        stream,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    stream.write("\n")


if __name__ == "__main__":
    raise SystemExit(main())

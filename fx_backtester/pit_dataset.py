"""Content-addressed point-in-time dataset artifacts.

The artifact produced here is intentionally narrow: it preserves raw inputs and
canonical :class:`~fx_backtester.point_in_time.PointInTimeRecord` rows in a
create-only directory, then provides a full local audit.  This makes accidental
rewrites and ordinary tampering detectable.  It is not an external signature,
object lock, or trusted timestamp, so every artifact remains research-only and
cannot by itself satisfy a promotion gate.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fx_backtester.point_in_time import PointInTimeError, PointInTimeRecord, utc_datetime


class PITDatasetError(ValueError):
    """Raised when a PIT dataset cannot be materialized or trusted."""


VerificationStatus = Literal["unverified", "research_only", "verified"]
DatasetClass = Literal["synthetic", "research_only"]

_VERIFICATION_STATUSES = frozenset({"unverified", "research_only", "verified"})
_DATASET_CLASSES = frozenset({"synthetic", "research_only"})
_SHA256 = re.compile(r"[0-9a-f]{64}")
_GIT_COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_MANIFEST_FILES = frozenset({"manifest.json", "manifest.sha256", "records.jsonl", "raw"})


@dataclass(frozen=True)
class SourceLineage:
    """Human-reviewed source, contract, and licence evidence."""

    source: str
    upstream_uri: str
    source_version: str
    contract_status: VerificationStatus
    license_status: VerificationStatus
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("source", "upstream_uri", "source_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise PITDatasetError(f"source lineage {name} must be a non-empty string")
            value = value.strip()
            object.__setattr__(self, name, value)
        for name in ("contract_status", "license_status"):
            value = str(getattr(self, name))
            if value not in _VERIFICATION_STATUSES:
                raise PITDatasetError(f"unsupported {name}: {value}")
        if not isinstance(self.limitations, (list, tuple)) or any(
            not isinstance(item, str) for item in self.limitations
        ):
            raise PITDatasetError("source lineage limitations must contain only strings")
        limitations = tuple(sorted({item.strip() for item in self.limitations if item.strip()}))
        if any(not item for item in limitations):
            raise PITDatasetError("source lineage limitations must be non-empty strings")
        object.__setattr__(self, "limitations", limitations)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "upstream_uri": self.upstream_uri,
            "source_version": self.source_version,
            "contract_status": self.contract_status,
            "license_status": self.license_status,
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True)
class RawInput:
    """A raw file captured before the PIT transformation."""

    source: str
    role: str
    path: Path
    acquired_at: datetime

    def __post_init__(self) -> None:
        for name in ("source", "role"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise PITDatasetError(f"raw input {name} must be a non-empty string")
            value = value.strip()
            object.__setattr__(self, name, value)
        path = Path(self.path).expanduser().resolve()
        if not path.is_file():
            raise PITDatasetError(f"raw input is not a regular file: {path}")
        object.__setattr__(self, "path", path)
        object.__setattr__(
            self,
            "acquired_at",
            _utc(self.acquired_at, field_name="raw_input.acquired_at"),
        )


@dataclass(frozen=True)
class PITDatasetArtifact:
    """Paths and identity for a successfully materialized dataset."""

    dataset_id: str
    directory: Path
    manifest_path: Path
    records_path: Path
    raw_directory: Path


@dataclass(frozen=True)
class PITDatasetAudit:
    """Non-throwing audit result for a local PIT dataset directory."""

    passed: bool
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    manifest: Mapping[str, Any] = field(default_factory=dict)


def materialize_pit_dataset(
    root: str | Path,
    records: Iterable[PointInTimeRecord],
    *,
    source_lineage: Sequence[SourceLineage],
    raw_inputs: Sequence[RawInput],
    transform_name: str,
    transform_version: str,
    dataset_class: DatasetClass,
    description: str,
    created_at: datetime,
    code_commit: str,
    dirty_worktree: bool,
) -> PITDatasetArtifact:
    """Preserve and bind raw inputs plus canonical PIT records.

    The destination is ``root/<dataset_id>``.  A valid artifact with identical
    content is returned idempotently.  Any incomplete or corrupt collision is
    rejected and never overwritten.
    """

    created = _validated_creation_time(created_at, field_name="created_at")
    transform_name = _required_text(transform_name, "transform_name")
    transform_version = _required_text(transform_version, "transform_version")
    description = _required_text(description, "description")
    if dataset_class not in _DATASET_CLASSES:
        raise PITDatasetError(f"unsupported dataset_class: {dataset_class}")
    if not isinstance(dirty_worktree, bool):
        raise PITDatasetError("dirty_worktree must be a bool")
    if not isinstance(code_commit, str) or not _GIT_COMMIT.fullmatch(code_commit):
        raise PITDatasetError("code_commit must be a full lowercase Git object ID")

    canonical_records, record_summary, records_bytes = _prepare_records(records, created)
    lineage_rows = _prepare_lineage(source_lineage)
    raw_rows = _prepare_raw_inputs(raw_inputs, created)
    _validate_source_coverage(canonical_records, lineage_rows, raw_rows)

    identity = _build_identity(
        created_at=created,
        dataset_class=dataset_class,
        description=description,
        transform_name=transform_name,
        transform_version=transform_version,
        code_commit=code_commit,
        dirty_worktree=dirty_worktree,
        lineage_rows=lineage_rows,
        raw_rows=[row for row, _raw in raw_rows],
        record_summary=record_summary,
    )
    dataset_id = _digest(_canonical_bytes(identity))
    manifest = _build_manifest(dataset_id, identity)
    manifest_bytes = _canonical_bytes(manifest) + b"\n"
    manifest_digest = _digest(manifest_bytes)

    root_path = Path(root).expanduser().resolve()
    root_path.mkdir(parents=True, exist_ok=True)
    if not root_path.is_dir():
        raise PITDatasetError(f"dataset root is not a directory: {root_path}")
    destination = root_path / dataset_id
    artifact = _artifact_paths(destination, dataset_id)

    if destination.exists():
        return _existing_artifact_or_raise(artifact)
    try:
        destination.mkdir()
    except FileExistsError:
        return _existing_artifact_or_raise(artifact)

    incomplete = destination / ".incomplete"
    _exclusive_write(incomplete, b"materialization in progress\n")
    artifact.raw_directory.mkdir()
    _fsync_directory(destination)

    # Copy every distinct raw object into the artifact and verify that it did not
    # change between initial fingerprinting and preservation.
    copied: set[str] = set()
    for row, raw in raw_rows:
        stored_path = str(row["stored_path"])
        if stored_path in copied:
            continue
        _copy_and_verify_raw(raw.path, destination / stored_path, row)
        copied.add(stored_path)
    _fsync_directory(artifact.raw_directory)

    _exclusive_write(artifact.records_path, records_bytes)
    _exclusive_write(artifact.manifest_path, manifest_bytes)
    _exclusive_write(destination / "manifest.sha256", f"{manifest_digest}\n".encode("ascii"))
    incomplete.unlink()
    _fsync_directory(destination)
    _fsync_directory(root_path)

    audit = audit_pit_dataset(destination)
    if not audit.passed:
        # Do not delete or overwrite evidence of a partial/corrupt write.
        _exclusive_write(incomplete, b"materialized dataset failed self-audit\n")
        raise PITDatasetError(f"materialized dataset failed self-audit: {'; '.join(audit.errors)}")
    return artifact


def audit_pit_dataset(dataset_dir: str | Path) -> PITDatasetAudit:
    """Recompute the complete artifact identity and report every local failure."""

    errors: list[str] = []
    warnings: list[str] = []
    manifest: dict[str, Any] = {}

    try:
        requested_directory = Path(dataset_dir).expanduser().absolute()
        if requested_directory.is_symlink():
            errors.append("dataset directory must not be a symbolic link")
        directory = requested_directory.resolve()
    except OSError as error:
        return PITDatasetAudit(False, (f"cannot resolve dataset directory: {error}",), (), {})

    if not directory.is_dir():
        return PITDatasetAudit(False, (f"dataset directory not found: {directory}",), (), {})
    try:
        entries = tuple(directory.iterdir())
    except OSError as error:
        return PITDatasetAudit(False, (f"cannot list dataset directory: {error}",), (), {})
    actual_entries = {item.name for item in entries}
    symbolic_links = sorted(item.name for item in entries if item.is_symlink())
    if symbolic_links:
        errors.append(f"artifact entries must not be symbolic links: {symbolic_links}")
    if ".incomplete" in actual_entries:
        errors.append("dataset has an incomplete materialization marker")
    missing = sorted(_MANIFEST_FILES - actual_entries)
    unexpected = sorted(actual_entries - _MANIFEST_FILES - {".incomplete"})
    if missing:
        errors.append(f"missing artifact entries: {missing}")
    if unexpected:
        errors.append(f"unexpected artifact entries: {unexpected}")

    manifest_path = directory / "manifest.json"
    digest_path = directory / "manifest.sha256"
    records_path = directory / "records.jsonl"
    raw_directory = directory / "raw"
    for required_file in (manifest_path, digest_path, records_path):
        if required_file.exists() and (required_file.is_symlink() or not required_file.is_file()):
            errors.append(f"artifact entry must be a regular file: {required_file.name}")
    if raw_directory.exists() and (raw_directory.is_symlink() or not raw_directory.is_dir()):
        errors.append("artifact entry must be a directory: raw")
    manifest_bytes = b""

    if manifest_path.is_file():
        try:
            manifest_bytes = manifest_path.read_bytes()
            parsed = json.loads(manifest_bytes.decode("utf-8"))
            if not isinstance(parsed, dict):
                raise PITDatasetError("manifest must be a JSON object")
            manifest = parsed
            if _canonical_bytes(manifest) + b"\n" != manifest_bytes:
                errors.append("manifest.json is not canonical JSON")
        except (
            OSError,
            UnicodeDecodeError,
            ValueError,
            RecursionError,
            PITDatasetError,
        ) as error:
            errors.append(f"cannot parse manifest.json: {error}")
    if digest_path.is_file() and manifest_bytes:
        try:
            expected_digest_file = f"{_digest(manifest_bytes)}\n".encode("ascii")
            if digest_path.read_bytes() != expected_digest_file:
                errors.append("manifest.sha256 does not match manifest.json")
        except OSError as error:
            errors.append(f"cannot read manifest.sha256: {error}")

    identity = manifest.get("identity") if isinstance(manifest, dict) else None
    if not isinstance(identity, dict):
        if manifest:
            errors.append("manifest identity is missing or invalid")
        return PITDatasetAudit(False, tuple(dict.fromkeys(errors)), tuple(warnings), manifest)

    rebuilt_identity: dict[str, Any] | None = None
    try:
        # Future-time rejection belongs to materialization. Re-evaluating it on
        # audit would make an immutable verdict depend on the auditor host clock.
        created = _utc(identity["created_at"], field_name="identity.created_at")
        dataset_class = str(identity["dataset_class"])
        if dataset_class not in _DATASET_CLASSES:
            raise PITDatasetError(f"unsupported dataset_class: {dataset_class}")
        description = _required_text(identity["description"], "identity.description")

        transform = _exact_mapping(
            identity["transform"], {"name", "version"}, field_name="identity.transform"
        )
        code = _exact_mapping(
            identity["code"], {"commit", "dirty_worktree"}, field_name="identity.code"
        )
        transform_name = _required_text(transform["name"], "identity.transform.name")
        transform_version = _required_text(transform["version"], "identity.transform.version")
        code_commit = str(code["commit"])
        dirty_worktree = code["dirty_worktree"]
        if not _GIT_COMMIT.fullmatch(code_commit):
            raise PITDatasetError("identity.code.commit is not a full lowercase Git object ID")
        if not isinstance(dirty_worktree, bool):
            raise PITDatasetError("identity.code.dirty_worktree must be a bool")

        lineage_rows = _lineage_from_manifest(identity["source_lineage"])
        records = _records_from_artifact(records_path)
        canonical_records, record_summary, expected_records = _prepare_records(records, created)
        if records_path.is_file() and records_path.read_bytes() != expected_records:
            errors.append("records.jsonl content, ordering, or serialization is not canonical")

        raw_rows = _raw_rows_from_manifest(identity["raw_inputs"], created)
        _validate_preserved_raw(raw_directory, raw_rows)
        _validate_source_coverage(
            canonical_records,
            lineage_rows,
            [(row, None) for row in raw_rows],
        )
        rebuilt_identity = _build_identity(
            created_at=created,
            dataset_class=dataset_class,
            description=description,
            transform_name=transform_name,
            transform_version=transform_version,
            code_commit=code_commit,
            dirty_worktree=dirty_worktree,
            lineage_rows=lineage_rows,
            raw_rows=raw_rows,
            record_summary=record_summary,
        )
    except (
        KeyError,
        TypeError,
        ValueError,
        OSError,
        RecursionError,
        PointInTimeError,
        PITDatasetError,
    ) as error:
        errors.append(f"artifact identity cannot be reconstructed: {error}")

    if rebuilt_identity is not None:
        if rebuilt_identity != identity:
            errors.append("manifest identity does not match preserved content")
        rebuilt_id = _digest(_canonical_bytes(rebuilt_identity))
        if manifest.get("dataset_id") != rebuilt_id:
            errors.append("dataset_id does not match reconstructed identity")
        if directory.name != rebuilt_id:
            errors.append("dataset directory name does not match dataset_id")
        expected_manifest = _build_manifest(rebuilt_id, rebuilt_identity)
        if manifest != expected_manifest:
            errors.append("manifest claims do not match reconstructed evidence")

    warnings.extend(
        [
            "domain-specific QA is not evaluated by the PIT artifact layer",
            "local content addressing is tamper-evident, not an external immutable store",
            "artifact is research-only and not promotion eligible",
        ]
    )
    return PITDatasetAudit(
        not errors,
        tuple(dict.fromkeys(errors)),
        tuple(dict.fromkeys(warnings)),
        manifest,
    )


def dataset_hash_from_manifest(dataset_dir: str | Path) -> str:
    """Return the verified dataset identity or fail closed."""

    audit = audit_pit_dataset(dataset_dir)
    if not audit.passed:
        raise PITDatasetError(f"PIT dataset audit failed: {'; '.join(audit.errors)}")
    dataset_id = audit.manifest.get("dataset_id")
    if not isinstance(dataset_id, str) or not _SHA256.fullmatch(dataset_id):
        raise PITDatasetError("audited manifest is missing a valid dataset_id")
    return dataset_id


def load_pit_dataset_records(dataset_dir: str | Path) -> tuple[PointInTimeRecord, ...]:
    """Return records from a fully re-audited PIT artifact.

    The audit is deliberately repeated at the read boundary.  The records file is
    then read once, checked against the audited manifest digest, and parsed from
    those exact bytes so a caller cannot accidentally consume an unaudited copy.
    This is still a local-filesystem trust boundary, not an external signature or
    immutable object-store guarantee.
    """

    audit = audit_pit_dataset(dataset_dir)
    if not audit.passed:
        raise PITDatasetError(f"PIT dataset audit failed: {'; '.join(audit.errors)}")
    directory = Path(dataset_dir).expanduser().resolve()
    records_path = directory / "records.jsonl"
    try:
        raw = records_path.read_bytes()
        expected = audit.manifest["structural_integrity"]["records_sha256"]
    except (OSError, KeyError, TypeError) as error:
        raise PITDatasetError(f"cannot load audited records: {error}") from error
    if not isinstance(expected, str) or _digest(raw) != expected:
        raise PITDatasetError("records.jsonl changed after PIT dataset audit")
    return _records_from_bytes(raw)


def _prepare_records(
    records: Iterable[PointInTimeRecord], created_at: datetime
) -> tuple[tuple[PointInTimeRecord, ...], dict[str, Any], bytes]:
    values = tuple(records)
    if not values:
        raise PITDatasetError("at least one PointInTimeRecord is required")
    if any(not isinstance(record, PointInTimeRecord) for record in values):
        raise PITDatasetError("records must contain only PointInTimeRecord instances")

    canonical_rows: list[tuple[tuple[str, ...], PointInTimeRecord, bytes]] = []
    exact_rows: set[bytes] = set()
    natural_times: set[tuple[str, str, str]] = set()
    grouped: defaultdict[tuple[str, str], list[PointInTimeRecord]] = defaultdict(list)
    flag_counts: defaultdict[str, int] = defaultdict(int)

    for record in values:
        if not record.run_id.strip() or not record.writer_id.strip():
            raise PITDatasetError("every record requires non-empty run_id and writer_id")
        for name in ("available_time", "ingested_time", "validated_time"):
            value = getattr(record, name)
            if value is not None and value > created_at:
                raise PITDatasetError(f"record {record.source_record_id} has future {name}")
        encoded = _canonical_bytes(record.to_dict())
        if encoded in exact_rows:
            raise PITDatasetError("duplicate PointInTimeRecord detected")
        exact_rows.add(encoded)
        natural_time = (record.source, record.source_record_id, record.available_time.isoformat())
        if natural_time in natural_times:
            raise PITDatasetError("conflicting source record at the same available_time")
        natural_times.add(natural_time)
        grouped[(record.source, record.source_record_id)].append(record)
        for flag in record.data_quality_flags:
            flag_counts[flag] += 1
        sort_key = (
            record.source,
            record.source_record_id,
            record.available_time.isoformat(),
            record.event_time.isoformat(),
            record.content_hash,
            encoded.decode("utf-8"),
        )
        canonical_rows.append((sort_key, record, encoded))

    for source_key, revisions in grouped.items():
        ordered = sorted(revisions, key=lambda item: item.available_time)
        for previous, current in zip(ordered, ordered[1:], strict=False):
            if current.content_hash == previous.content_hash:
                raise PITDatasetError(f"duplicate payload in revision chain: {source_key}")
            if current.revision_time is None:
                raise PITDatasetError(f"changed source record lacks revision_time: {source_key}")
            if (
                previous.revision_time is not None
                and current.revision_time <= previous.revision_time
            ):
                raise PITDatasetError(f"revision_time is not strictly monotonic: {source_key}")
            if current.available_time <= previous.available_time:
                raise PITDatasetError(f"available_time is not strictly monotonic: {source_key}")

    canonical_rows.sort(key=lambda item: item[0])
    ordered_records = tuple(row[1] for row in canonical_rows)
    records_bytes = b"".join(row[2] + b"\n" for row in canonical_rows)
    record_summary = {
        "path": "records.jsonl",
        "sha256": _digest(records_bytes),
        "bytes": len(records_bytes),
        "record_count": len(ordered_records),
        "source_count": len({record.source for record in ordered_records}),
        "source_record_count": len(grouped),
        "revision_count": sum(record.revision_time is not None for record in ordered_records),
        "event_time_min": min(record.event_time for record in ordered_records).isoformat(),
        "event_time_max": max(record.event_time for record in ordered_records).isoformat(),
        "available_time_min": min(record.available_time for record in ordered_records).isoformat(),
        "available_time_max": max(record.available_time for record in ordered_records).isoformat(),
        "run_ids": sorted({record.run_id for record in ordered_records}),
        "writer_ids": sorted({record.writer_id for record in ordered_records}),
        "data_quality_flag_counts": dict(sorted(flag_counts.items())),
        "envelope_temporal_violations": 0,
    }
    return ordered_records, record_summary, records_bytes


def _prepare_lineage(source_lineage: Sequence[SourceLineage]) -> list[dict[str, Any]]:
    if not source_lineage:
        raise PITDatasetError("source_lineage must be non-empty")
    if any(not isinstance(item, SourceLineage) for item in source_lineage):
        raise PITDatasetError("source_lineage contains an invalid item")
    sources = [item.source for item in source_lineage]
    if len(sources) != len(set(sources)):
        raise PITDatasetError("source_lineage contains duplicate sources")
    return sorted((item.to_dict() for item in source_lineage), key=lambda row: str(row["source"]))


def _prepare_raw_inputs(
    raw_inputs: Sequence[RawInput], created_at: datetime
) -> list[tuple[dict[str, Any], RawInput]]:
    if not raw_inputs:
        raise PITDatasetError("raw_inputs must be non-empty")
    if any(not isinstance(item, RawInput) for item in raw_inputs):
        raise PITDatasetError("raw_inputs contains an invalid item")
    rows: list[tuple[dict[str, Any], RawInput]] = []
    identities: set[tuple[str, ...]] = set()
    for raw in raw_inputs:
        if raw.acquired_at > created_at:
            raise PITDatasetError(f"raw input has a future acquired_at: {raw.path}")
        digest, size = _file_digest(raw.path)
        row = {
            "source": raw.source,
            "role": raw.role,
            "acquired_at": raw.acquired_at.isoformat(),
            "original_name": raw.path.name,
            "stored_path": f"raw/{digest}",
            "sha256": digest,
            "bytes": size,
        }
        identity = tuple(str(row[key]) for key in sorted(row))
        if identity in identities:
            raise PITDatasetError("duplicate raw input detected")
        identities.add(identity)
        rows.append((row, raw))
    rows.sort(
        key=lambda item: (
            str(item[0]["source"]),
            str(item[0]["role"]),
            str(item[0]["acquired_at"]),
            str(item[0]["sha256"]),
            str(item[0]["original_name"]),
        )
    )
    return rows


def _validate_source_coverage(
    records: Sequence[PointInTimeRecord],
    lineage_rows: Sequence[Mapping[str, Any]],
    raw_rows: Sequence[tuple[Mapping[str, Any], RawInput | None]],
) -> None:
    record_sources = {record.source for record in records}
    lineage_sources = {str(row["source"]) for row in lineage_rows}
    raw_sources = {str(row["source"]) for row, _raw in raw_rows}
    if record_sources != lineage_sources:
        raise PITDatasetError(
            "source lineage coverage mismatch: "
            f"records={sorted(record_sources)} lineage={sorted(lineage_sources)}"
        )
    if not record_sources <= raw_sources:
        raise PITDatasetError(
            f"raw input coverage missing sources: {sorted(record_sources - raw_sources)}"
        )
    if not raw_sources <= lineage_sources:
        raise PITDatasetError(
            f"raw inputs reference unknown sources: {sorted(raw_sources - lineage_sources)}"
        )


def _build_identity(
    *,
    created_at: datetime,
    dataset_class: str,
    description: str,
    transform_name: str,
    transform_version: str,
    code_commit: str,
    dirty_worktree: bool,
    lineage_rows: Sequence[Mapping[str, Any]],
    raw_rows: Sequence[Mapping[str, Any]],
    record_summary: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "artifact_kind": "point_in_time_dataset",
        "created_at": created_at.isoformat(),
        "dataset_class": dataset_class,
        "description": description,
        "transform": {"name": transform_name, "version": transform_version},
        "code": {"commit": code_commit, "dirty_worktree": dirty_worktree},
        "source_lineage": [dict(row) for row in lineage_rows],
        "raw_inputs": [dict(row) for row in raw_rows],
        "records": dict(record_summary),
    }


def _build_manifest(dataset_id: str, identity: Mapping[str, Any]) -> dict[str, Any]:
    lineage = identity["source_lineage"]
    records = identity["records"]
    all_contracts_verified = all(row["contract_status"] == "verified" for row in lineage)
    all_licenses_verified = all(row["license_status"] == "verified" for row in lineage)
    blockers = [
        "research_only_artifact_scope",
        "domain_qa_not_evaluated",
        "point_in_time_join_not_evaluated",
    ]
    if identity["dataset_class"] == "synthetic":
        blockers.append("synthetic_data")
    if not all_contracts_verified:
        blockers.append("source_contract_not_verified")
    if not all_licenses_verified:
        blockers.append("source_license_not_verified")
    return {
        "schema_version": 1,
        "artifact_kind": "point_in_time_dataset_manifest",
        "dataset_id": dataset_id,
        "identity": dict(identity),
        "structural_integrity": {
            "status": "passed",
            "envelope_temporal_violations": records["envelope_temporal_violations"],
            "records_sha256": records["sha256"],
            "tamper_model": "create-only local content addressing; no external signature or object lock",
        },
        "domain_qa": {
            "status": "not_evaluated",
            "as_of_join_status": "not_evaluated",
            "data_quality_flag_counts": records["data_quality_flag_counts"],
        },
        "source_admissibility": {
            "status": (
                "declared_verified"
                if all_contracts_verified and all_licenses_verified
                else "research_only"
            ),
            "evidence_scope": "source declarations bound locally; no external attestation",
            "all_contracts_verified": all_contracts_verified,
            "all_licenses_verified": all_licenses_verified,
        },
        "promotion_eligible": False,
        "promotion_blockers": blockers,
    }


def _records_from_artifact(path: Path) -> tuple[PointInTimeRecord, ...]:
    if not path.is_file():
        raise PITDatasetError("records.jsonl is missing")
    return _records_from_bytes(path.read_bytes())


def _records_from_bytes(raw: bytes) -> tuple[PointInTimeRecord, ...]:
    if not raw:
        raise PITDatasetError("records.jsonl is empty")
    if not raw.endswith(b"\n"):
        raise PITDatasetError("records.jsonl must end with a newline")
    records: list[PointInTimeRecord] = []
    for line_number, line in enumerate(raw.splitlines(), start=1):
        if not line:
            raise PITDatasetError(f"records.jsonl contains a blank line at {line_number}")
        try:
            row = json.loads(line.decode("utf-8"))
            if not isinstance(row, dict):
                raise PITDatasetError("record must be a JSON object")
            record = PointInTimeRecord(**row)
        except (
            UnicodeDecodeError,
            ValueError,
            TypeError,
            RecursionError,
            PointInTimeError,
        ) as error:
            raise PITDatasetError(f"invalid record at line {line_number}: {error}") from error
        if _canonical_bytes(record.to_dict()) != line:
            raise PITDatasetError(f"record at line {line_number} is not canonical")
        records.append(record)
    return tuple(records)


def _lineage_from_manifest(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise PITDatasetError("identity.source_lineage must be a list")
    items: list[SourceLineage] = []
    expected = {
        "source",
        "upstream_uri",
        "source_version",
        "contract_status",
        "license_status",
        "limitations",
    }
    for row in value:
        mapping = _exact_mapping(row, expected, field_name="source_lineage row")
        limitations = mapping["limitations"]
        if not isinstance(limitations, list):
            raise PITDatasetError("source lineage limitations must be a list")
        items.append(
            SourceLineage(
                source=mapping["source"],
                upstream_uri=mapping["upstream_uri"],
                source_version=mapping["source_version"],
                contract_status=mapping["contract_status"],
                license_status=mapping["license_status"],
                limitations=tuple(limitations),
            )
        )
    rows = _prepare_lineage(items)
    if rows != value:
        raise PITDatasetError("source_lineage is not canonical")
    return rows


def _raw_rows_from_manifest(value: Any, created_at: datetime) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise PITDatasetError("identity.raw_inputs must be a non-empty list")
    expected_keys = {
        "source",
        "role",
        "acquired_at",
        "original_name",
        "stored_path",
        "sha256",
        "bytes",
    }
    rows: list[dict[str, Any]] = []
    for item in value:
        row = _exact_mapping(item, expected_keys, field_name="raw input row")
        source = _required_text(row["source"], "raw_input.source")
        role = _required_text(row["role"], "raw_input.role")
        acquired = _utc(row["acquired_at"], field_name="raw_input.acquired_at")
        if acquired > created_at:
            raise PITDatasetError("raw input acquired_at is after dataset creation")
        original_name = _required_text(row["original_name"], "raw_input.original_name")
        if Path(original_name).name != original_name:
            raise PITDatasetError("raw input original_name must be a basename")
        digest = str(row["sha256"])
        if not _SHA256.fullmatch(digest):
            raise PITDatasetError("raw input sha256 is invalid")
        stored_path = str(row["stored_path"])
        if stored_path != f"raw/{digest}":
            raise PITDatasetError("raw input stored_path is not content-addressed")
        size = row["bytes"]
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise PITDatasetError("raw input bytes must be a non-negative integer")
        rows.append(
            {
                "source": source,
                "role": role,
                "acquired_at": acquired.isoformat(),
                "original_name": original_name,
                "stored_path": stored_path,
                "sha256": digest,
                "bytes": size,
            }
        )
    rows.sort(
        key=lambda row: (
            row["source"],
            row["role"],
            row["acquired_at"],
            row["sha256"],
            row["original_name"],
        )
    )
    if rows != value:
        raise PITDatasetError("raw_inputs is not canonical")
    if len({_canonical_bytes(row) for row in rows}) != len(rows):
        raise PITDatasetError("duplicate raw input metadata")
    return rows


def _validate_preserved_raw(raw_directory: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    if raw_directory.is_symlink() or not raw_directory.is_dir():
        raise PITDatasetError("raw artifact directory is missing")
    expected_names = {str(row["sha256"]) for row in rows}
    entries = tuple(raw_directory.iterdir())
    symbolic_links = sorted(item.name for item in entries if item.is_symlink())
    if symbolic_links:
        raise PITDatasetError(f"preserved raw objects must not be symbolic links: {symbolic_links}")
    actual_entries = {item.name for item in entries}
    if actual_entries != expected_names:
        raise PITDatasetError(
            f"preserved raw object set mismatch: expected={sorted(expected_names)} "
            f"actual={sorted(actual_entries)}"
        )
    for digest in expected_names:
        path = raw_directory / digest
        if not path.is_file():
            raise PITDatasetError(f"preserved raw object is not a regular file: {digest}")
        actual_digest, actual_size = _file_digest(path)
        expected_sizes = {int(row["bytes"]) for row in rows if row["sha256"] == digest}
        if actual_digest != digest or expected_sizes != {actual_size}:
            raise PITDatasetError(f"preserved raw object hash/size mismatch: {digest}")


def _copy_and_verify_raw(source: Path, destination: Path, row: Mapping[str, Any]) -> None:
    digest = hashlib.sha256()
    size = 0
    try:
        with source.open("rb") as reader, destination.open("xb") as writer:
            for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
    except OSError as error:
        raise PITDatasetError(f"cannot preserve raw input {source}: {error}") from error
    if digest.hexdigest() != row["sha256"] or size != row["bytes"]:
        raise PITDatasetError(f"raw input changed during materialization: {source}")


def _existing_artifact_or_raise(artifact: PITDatasetArtifact) -> PITDatasetArtifact:
    audit = audit_pit_dataset(artifact.directory)
    if audit.passed and audit.manifest.get("dataset_id") == artifact.dataset_id:
        return artifact
    detail = "; ".join(audit.errors) or "identity collision"
    raise PITDatasetError(f"existing dataset is not a valid identical artifact: {detail}")


def _artifact_paths(directory: Path, dataset_id: str) -> PITDatasetArtifact:
    return PITDatasetArtifact(
        dataset_id=dataset_id,
        directory=directory,
        manifest_path=directory / "manifest.json",
        records_path=directory / "records.jsonl",
        raw_directory=directory / "raw",
    )


def _file_digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
    except OSError as error:
        raise PITDatasetError(f"cannot read raw input {path}: {error}") from error
    return digest.hexdigest(), size


def _exclusive_write(path: Path, content: bytes) -> None:
    try:
        with path.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as error:
        raise PITDatasetError(f"create-only write failed for {path}: {error}") from error


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise PITDatasetError(f"cannot fsync directory {path}: {error}") from error


def _exact_mapping(value: Any, keys: set[str], *, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PITDatasetError(f"{field_name} must be an object")
    if set(value) != keys:
        raise PITDatasetError(
            f"{field_name} keys mismatch: expected={sorted(keys)} actual={sorted(value)}"
        )
    return value


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PITDatasetError(f"{field_name} must be a non-empty string")
    return value.strip()


def _utc(value: object, *, field_name: str) -> datetime:
    try:
        return utc_datetime(value, field_name=field_name)
    except PointInTimeError as error:
        raise PITDatasetError(str(error)) from error


def _validated_creation_time(value: object, *, field_name: str) -> datetime:
    timestamp = _utc(value, field_name=field_name)
    if timestamp > datetime.now(UTC):
        raise PITDatasetError(f"{field_name} must not be in the future")
    return timestamp


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise PITDatasetError(f"value is not canonical JSON: {error}") from error


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


__all__ = [
    "PITDatasetArtifact",
    "PITDatasetAudit",
    "PITDatasetError",
    "RawInput",
    "SourceLineage",
    "audit_pit_dataset",
    "dataset_hash_from_manifest",
    "load_pit_dataset_records",
    "materialize_pit_dataset",
]

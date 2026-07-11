from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fx_backtester.pit_dataset import (
    PITDatasetError,
    RawInput,
    SourceLineage,
    audit_pit_dataset,
    dataset_hash_from_manifest,
    materialize_pit_dataset,
)
from fx_backtester.point_in_time import PointInTimeRecord

CREATED_AT = datetime(2024, 2, 1, tzinfo=UTC)
COMMIT = "a" * 40


def _record(
    source_record_id: str = "observation-1",
    *,
    payload_value: float = 1.0,
    available_at: datetime | None = None,
    ingested_at: datetime | None = None,
    revision_at: datetime | None = None,
    validated_at: datetime | None = None,
    source: str = "vendor",
) -> PointInTimeRecord:
    available = available_at or datetime(2024, 1, 2, tzinfo=UTC)
    ingested = ingested_at or available
    return PointInTimeRecord(
        event_time=datetime(2024, 1, 1, tzinfo=UTC),
        available_time=available,
        ingested_time=ingested,
        revision_time=revision_at,
        validated_time=validated_at,
        source=source,
        source_record_id=source_record_id,
        payload={"value": payload_value},
        run_id="ingest-run-1",
        writer_id="writer-1",
    )


def _lineage(
    *,
    source: str = "vendor",
    contract_status: str = "research_only",
    license_status: str = "research_only",
) -> SourceLineage:
    return SourceLineage(
        source=source,
        upstream_uri="https://example.invalid/data",
        source_version="snapshot-2024-01",
        contract_status=contract_status,  # type: ignore[arg-type]
        license_status=license_status,  # type: ignore[arg-type]
        limitations=("historical first-seen time not vendor-signed",),
    )


def _raw(tmp_path: Path, *, source: str = "vendor", name: str = "raw.csv") -> RawInput:
    path = tmp_path / name
    path.write_bytes(b"timestamp,value\n2024-01-01T00:00:00Z,1.0\n")
    return RawInput(
        source=source,
        role="observations",
        path=path,
        acquired_at=datetime(2024, 1, 3, tzinfo=UTC),
    )


def _materialize(
    tmp_path: Path,
    *,
    root_name: str = "datasets",
    records: list[PointInTimeRecord] | None = None,
    lineage: list[SourceLineage] | None = None,
    raw_inputs: list[RawInput] | None = None,
    dataset_class: str = "research_only",
):
    raw_values = raw_inputs or [_raw(tmp_path)]
    return materialize_pit_dataset(
        tmp_path / root_name,
        records or [_record()],
        source_lineage=lineage or [_lineage()],
        raw_inputs=raw_values,
        transform_name="canonical-pit-envelope",
        transform_version="1.0.0",
        dataset_class=dataset_class,  # type: ignore[arg-type]
        description="PIT artifact test fixture",
        created_at=CREATED_AT,
        code_commit=COMMIT,
        dirty_worktree=False,
    )


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def test_materialization_is_order_independent_and_content_addressed(tmp_path: Path) -> None:
    raw = _raw(tmp_path)
    records = [_record("second", payload_value=2.0), _record("first")]

    first = _materialize(tmp_path, root_name="one", records=records, raw_inputs=[raw])
    second = _materialize(
        tmp_path,
        root_name="two",
        records=list(reversed(records)),
        raw_inputs=[raw],
    )

    assert first.dataset_id == second.dataset_id
    assert first.directory.name == first.dataset_id
    assert first.records_path.read_bytes() == second.records_path.read_bytes()
    assert first.manifest_path.read_bytes() == second.manifest_path.read_bytes()
    assert dataset_hash_from_manifest(first.directory) == first.dataset_id


def test_manifest_binds_raw_lineage_transform_and_time_bounds(tmp_path: Path) -> None:
    raw = _raw(tmp_path)
    artifact = _materialize(tmp_path, raw_inputs=[raw])
    manifest = json.loads(artifact.manifest_path.read_text(encoding="utf-8"))
    raw_row = manifest["identity"]["raw_inputs"][0]

    assert raw_row["sha256"] == hashlib.sha256(raw.path.read_bytes()).hexdigest()
    assert (artifact.directory / raw_row["stored_path"]).read_bytes() == raw.path.read_bytes()
    assert manifest["identity"]["transform"] == {
        "name": "canonical-pit-envelope",
        "version": "1.0.0",
    }
    assert manifest["identity"]["records"]["event_time_min"] == "2024-01-01T00:00:00+00:00"
    assert manifest["identity"]["source_lineage"][0]["limitations"]
    assert manifest["promotion_eligible"] is False
    assert {
        "research_only_artifact_scope",
        "domain_qa_not_evaluated",
        "point_in_time_join_not_evaluated",
    }.issubset(manifest["promotion_blockers"])
    assert manifest["domain_qa"]["as_of_join_status"] == "not_evaluated"
    assert "point_in_time_violations" not in manifest["structural_integrity"]


def test_existing_valid_dataset_is_idempotent(tmp_path: Path) -> None:
    raw = _raw(tmp_path)
    first = _materialize(tmp_path, raw_inputs=[raw])
    second = _materialize(tmp_path, raw_inputs=[raw])

    assert second == first
    assert audit_pit_dataset(second.directory).passed


def test_existing_corrupt_dataset_is_never_overwritten(tmp_path: Path) -> None:
    raw = _raw(tmp_path)
    artifact = _materialize(tmp_path, raw_inputs=[raw])
    corrupted = artifact.records_path.read_bytes() + b"{}\n"
    artifact.records_path.write_bytes(corrupted)

    with pytest.raises(PITDatasetError, match="existing dataset is not a valid"):
        _materialize(tmp_path, raw_inputs=[raw])

    assert artifact.records_path.read_bytes() == corrupted


def test_audit_detects_record_raw_and_manifest_tampering(tmp_path: Path) -> None:
    records_artifact = _materialize(tmp_path, root_name="records")
    record_row = json.loads(records_artifact.records_path.read_text(encoding="utf-8").strip())
    record_row["payload"]["value"] = 99.0
    records_artifact.records_path.write_bytes(_canonical_json(record_row) + b"\n")
    assert not audit_pit_dataset(records_artifact.directory).passed

    raw_artifact = _materialize(tmp_path, root_name="raw")
    preserved_raw = next(raw_artifact.raw_directory.iterdir())
    preserved_raw.write_bytes(b"tampered\n")
    raw_audit = audit_pit_dataset(raw_artifact.directory)
    assert not raw_audit.passed
    assert any("raw object" in error for error in raw_audit.errors)

    manifest_artifact = _materialize(tmp_path, root_name="manifest")
    manifest = json.loads(manifest_artifact.manifest_path.read_text(encoding="utf-8"))
    manifest["identity"]["description"] = "forged description"
    manifest_bytes = _canonical_json(manifest) + b"\n"
    manifest_artifact.manifest_path.write_bytes(manifest_bytes)
    (manifest_artifact.directory / "manifest.sha256").write_text(
        f"{hashlib.sha256(manifest_bytes).hexdigest()}\n",
        encoding="ascii",
    )
    manifest_audit = audit_pit_dataset(manifest_artifact.directory)
    assert not manifest_audit.passed
    assert any(
        "dataset_id" in error or "manifest claims" in error for error in manifest_audit.errors
    )


@pytest.mark.parametrize("future_field", ["ingested", "validated"])
def test_future_ingestion_or_validation_fails_closed(tmp_path: Path, future_field: str) -> None:
    future = CREATED_AT + timedelta(seconds=1)
    kwargs = {"ingested_at": future} if future_field == "ingested" else {"validated_at": future}

    with pytest.raises(PITDatasetError, match=f"future (available_time|{future_field}_time)"):
        _materialize(tmp_path, records=[_record(**kwargs)])


def test_duplicate_and_ambiguous_revision_fail_closed(tmp_path: Path) -> None:
    raw = _raw(tmp_path)
    record = _record()
    with pytest.raises(PITDatasetError, match="duplicate PointInTimeRecord"):
        _materialize(tmp_path, records=[record, record], raw_inputs=[raw])

    changed_without_revision = _record(
        payload_value=2.0,
        available_at=datetime(2024, 1, 3, tzinfo=UTC),
    )
    with pytest.raises(PITDatasetError, match="lacks revision_time"):
        _materialize(
            tmp_path,
            root_name="ambiguous",
            records=[record, changed_without_revision],
            raw_inputs=[raw],
        )


def test_distinct_monotonic_revisions_are_retained(tmp_path: Path) -> None:
    raw = _raw(tmp_path)
    original = _record()
    revised = _record(
        payload_value=2.0,
        available_at=datetime(2024, 1, 3, tzinfo=UTC),
        ingested_at=datetime(2024, 1, 3, 0, 1, tzinfo=UTC),
        revision_at=datetime(2024, 1, 3, tzinfo=UTC),
    )

    artifact = _materialize(tmp_path, records=[revised, original], raw_inputs=[raw])
    manifest = json.loads(artifact.manifest_path.read_text(encoding="utf-8"))

    assert manifest["identity"]["records"]["record_count"] == 2
    assert manifest["identity"]["records"]["revision_count"] == 1
    assert audit_pit_dataset(artifact.directory).passed


def test_revision_source_clock_is_not_compared_to_delayed_ingestion_clock(
    tmp_path: Path,
) -> None:
    raw = _raw(tmp_path)
    original = _record(
        available_at=datetime(2024, 1, 1, tzinfo=UTC),
        ingested_at=datetime(2024, 1, 10, tzinfo=UTC),
    )
    revised = _record(
        payload_value=2.0,
        available_at=datetime(2024, 1, 5, tzinfo=UTC),
        ingested_at=datetime(2024, 1, 11, tzinfo=UTC),
        revision_at=datetime(2024, 1, 5, tzinfo=UTC),
    )

    artifact = _materialize(
        tmp_path,
        root_name="delayed-vintage",
        records=[revised, original],
        raw_inputs=[raw],
    )

    assert audit_pit_dataset(artifact.directory).passed


def test_unverified_source_and_synthetic_data_are_explicitly_blocked(tmp_path: Path) -> None:
    artifact = _materialize(
        tmp_path,
        lineage=[_lineage(contract_status="unverified", license_status="unverified")],
        dataset_class="synthetic",
    )
    manifest = json.loads(artifact.manifest_path.read_text(encoding="utf-8"))

    assert manifest["source_admissibility"]["status"] == "research_only"
    assert {
        "synthetic_data",
        "source_contract_not_verified",
        "source_license_not_verified",
    }.issubset(manifest["promotion_blockers"])
    assert manifest["promotion_eligible"] is False


def test_missing_provenance_and_source_coverage_fail_closed(tmp_path: Path) -> None:
    raw = _raw(tmp_path)
    record_without_writer = PointInTimeRecord(
        event_time=datetime(2024, 1, 1, tzinfo=UTC),
        available_time=datetime(2024, 1, 2, tzinfo=UTC),
        ingested_time=datetime(2024, 1, 2, tzinfo=UTC),
        source="vendor",
        source_record_id="missing-writer",
        run_id="run",
    )
    with pytest.raises(PITDatasetError, match="run_id and writer_id"):
        _materialize(tmp_path, records=[record_without_writer], raw_inputs=[raw])

    with pytest.raises(PITDatasetError, match="coverage mismatch"):
        _materialize(
            tmp_path,
            root_name="coverage",
            records=[_record(source="other")],
            raw_inputs=[raw],
        )


def test_future_creation_time_and_artifact_symlinks_fail_closed(tmp_path: Path) -> None:
    raw = _raw(tmp_path)
    with pytest.raises(PITDatasetError, match="created_at must not be in the future"):
        materialize_pit_dataset(
            tmp_path / "future",
            [_record()],
            source_lineage=[_lineage()],
            raw_inputs=[raw],
            transform_name="canonical-pit-envelope",
            transform_version="1.0.0",
            dataset_class="research_only",
            description="future fixture",
            created_at=datetime.now(UTC) + timedelta(days=1),
            code_commit=COMMIT,
            dirty_worktree=False,
        )

    artifact = _materialize(tmp_path, root_name="symlink", raw_inputs=[raw])
    target = next(artifact.raw_directory.iterdir())
    target.unlink()
    target.symlink_to(raw.path)
    audit = audit_pit_dataset(artifact.directory)

    assert not audit.passed
    assert any("symbolic link" in error for error in audit.errors)


def test_audit_rejects_wrong_entry_types_and_malformed_manifest_without_raising(
    tmp_path: Path,
) -> None:
    digest_artifact = _materialize(tmp_path, root_name="digest-type")
    digest_path = digest_artifact.directory / "manifest.sha256"
    digest_path.unlink()
    digest_path.mkdir()
    digest_audit = audit_pit_dataset(digest_artifact.directory)

    assert not digest_audit.passed
    assert any("regular file" in error for error in digest_audit.errors)

    malformed_artifact = _materialize(tmp_path, root_name="malformed")
    malformed_artifact.manifest_path.write_text(
        '{"x":' + "9" * 5000 + "}",
        encoding="utf-8",
    )
    malformed_audit = audit_pit_dataset(malformed_artifact.directory)

    assert not malformed_audit.passed
    assert any("cannot parse manifest" in error for error in malformed_audit.errors)

    nested_artifact = _materialize(tmp_path, root_name="nested-record")
    nested_artifact.records_path.write_bytes(b"[" * 1500 + b"0" + b"]" * 1500 + b"\n")
    nested_audit = audit_pit_dataset(nested_artifact.directory)

    assert not nested_audit.passed
    assert any("cannot be reconstructed" in error for error in nested_audit.errors)

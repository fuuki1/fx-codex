"""Dataset lineage manifests and an append-only registry.

A :class:`DatasetManifest` pins everything needed to reproduce a materialised
dataset: the ordered source lineage (each source id + its raw content hash + the
``available_at`` cutoff it was built under) and the resulting content hash. The
:class:`DatasetRegistry` stores manifests append-only and lets a later run prove
byte-for-byte determinism by recomputing the dataset and comparing hashes.

This is the platform-level record of *what data produced what dataset*; it
complements ``fx_backtester.pit_dataset`` (which materialises the dataset
artifact itself) by giving a durable, queryable index keyed by dataset id.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from data_platform.contracts.pit_record import canonical_json_sha256

REGISTRY_SCHEMA_VERSION = 1


class DatasetRegistryError(RuntimeError):
    """Raised when the dataset registry's invariants would be violated."""


@dataclass(frozen=True)
class SourceLineageEntry:
    """One source's contribution to a dataset, with its PIT cutoff."""

    source_id: str
    raw_sha256: str
    available_at_cutoff: datetime
    record_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "raw_sha256": self.raw_sha256,
            "available_at_cutoff": self.available_at_cutoff.astimezone(UTC).isoformat(),
            "record_count": self.record_count,
        }


@dataclass(frozen=True)
class DatasetManifest:
    """Reproducibility manifest for one materialised dataset."""

    dataset_id: str
    instrument: str
    as_of: datetime
    sources: tuple[SourceLineageEntry, ...]
    dataset_sha256: str
    writer_id: str
    schema_version: int = REGISTRY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.dataset_id.strip():
            raise DatasetRegistryError("dataset_id is required")
        if not self.sources:
            raise DatasetRegistryError("a dataset manifest must record at least one source")

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "instrument": self.instrument,
            "as_of": self.as_of.astimezone(UTC).isoformat(),
            "sources": [source.to_dict() for source in self.sources],
            "dataset_sha256": self.dataset_sha256,
            "writer_id": self.writer_id,
            "schema_version": self.schema_version,
        }

    def lineage_sha256(self) -> str:
        """Deterministic hash of the identity + source lineage (not payload)."""

        return canonical_json_sha256(
            {
                "dataset_id": self.dataset_id,
                "instrument": self.instrument,
                "as_of": self.as_of.astimezone(UTC).isoformat(),
                "sources": [source.to_dict() for source in self.sources],
                "dataset_sha256": self.dataset_sha256,
            }
        )


class DatasetRegistry:
    """Append-only registry of dataset manifests, keyed by dataset id.

    Registering the same dataset id twice with an *identical* manifest is an
    idempotent no-op; registering it with a *different* manifest is rejected, so
    a dataset's lineage can never be silently rewritten.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def register(self, manifest: DatasetManifest) -> str:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existing = self._read()
        lineage_hash = manifest.lineage_sha256()
        if manifest.dataset_id in existing:
            prior = existing[manifest.dataset_id]
            if prior["lineage_sha256"] != lineage_hash:
                raise DatasetRegistryError(
                    f"dataset {manifest.dataset_id} is already registered with different lineage; "
                    "lineage cannot be rewritten"
                )
            return lineage_hash
        record = {
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "registered_at": datetime.now(UTC).isoformat(),
            "lineage_sha256": lineage_hash,
            "manifest": manifest.to_dict(),
        }
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, ensure_ascii=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return lineage_hash

    def get(self, dataset_id: str) -> dict[str, Any]:
        existing = self._read()
        if dataset_id not in existing:
            raise DatasetRegistryError(f"dataset {dataset_id} is not registered")
        return existing[dataset_id]

    def verify_replay(self, dataset_id: str, recomputed_dataset_sha256: str) -> bool:
        """True iff a recomputed dataset hash matches the registered one.

        The determinism check the platform relies on: re-materialise the dataset,
        hash it, and confirm it equals what was registered. A mismatch means the
        dataset drifted after registration and is not reproducible.
        """

        registered = self.get(dataset_id)["manifest"]["dataset_sha256"]
        return registered == recomputed_dataset_sha256

    def _read(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        index: dict[str, dict[str, Any]] = {}
        for line_number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines()):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError as error:
                raise DatasetRegistryError(
                    f"dataset registry line {line_number} is not valid JSON"
                ) from error
            index[record["manifest"]["dataset_id"]] = record
        return index

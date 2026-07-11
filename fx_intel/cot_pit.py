"""Research-only PIT adapter for CFTC Legacy Futures Only COT data.

The CFTC report date describes Tuesday positions; it is not a publication time.
This module preserves complete paginated responses, records first local ingestion,
models observed row changes as revisions, and joins observations to versioned local
release attestations at read time.  It never substitutes ``report_date + 3 days``.

All evidence remains local and promotion-ineligible.  COT is a public futures-
positioning proxy, not spot-FX customer flow, dealer order flow, or a proprietary
information advantage.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import socket
from types import MappingProxyType
from typing import Any, Literal
from urllib.parse import urlsplit

import requests

from fx_backtester.pit_dataset import (
    PITDatasetArtifact,
    PITDatasetError,
    RawInput,
    SourceLineage,
    audit_pit_dataset,
    dataset_hash_from_manifest,
    load_pit_dataset_records,
    materialize_pit_dataset,
)
from fx_backtester.point_in_time import (
    PointInTimeError,
    PointInTimeRecord,
    canonical_content_hash,
    utc_datetime,
)

from .macro import CFTC_COT_URL, COT_CONTRACT_CODES, COT_STALE_DAYS, CotReport

COT_DATA_SOURCE = "cftc_legacy_futures_only_6dca_aqww"
COT_RELEASE_SOURCE = "cftc_cot_release_attestation"
COT_DATASET_ID = "6dca-aqww"
COT_DATA_SOURCE_VERSION = "6dca-aqww:legacy-futures-only:v2"
COT_RELEASE_SOURCE_VERSION = "cftc-release-attestation:v1"
COT_TRANSFORM_NAME = "cftc_legacy_cot_pit"
COT_TRANSFORM_VERSION = "2"
COT_REPORT_TYPE = "FutOnly"
COT_PAGE_SIZE = 5_000
COT_MAX_PAGES = 100
COT_FETCH_TIMEOUT_SECONDS = 20.0
COT_USER_AGENT = "fx-codex-cot-pit/2.0 (+https://github.com/fuuki1)"
COT_RELEASE_SCHEDULE_URI = (
    "https://www.cftc.gov/MarketReports/CommitmentsofTraders/ReleaseSchedule/index.htm"
)
COT_DATASET_DESCRIPTION = (
    "Count-bounded paginated configured-code CFTC Legacy Futures Only captures, "
    "locally versioned release attestations, and replayed row revisions; "
    "public positioning proxy only"
)

CaptureRole = Literal["count_start", "page", "count_end"]
ReleaseBasis = Literal["scheduled", "actual_release_notice"]
LoadStatus = Literal["ok", "unavailable", "invalid", "incomplete", "stale"]

_CODE_TO_CURRENCY = {code: currency for currency, code in COT_CONTRACT_CODES.items()}
_SHA256_HEX = frozenset("0123456789abcdef")
_CAPTURE_ROLE = "cftc_paginated_capture_bundle"
_ATTESTATION_ROLE = "cftc_release_attestation_sidecar"
_RELEASE_EVIDENCE_ROLE = "cftc_release_evidence"
_ATTESTED_FLAG = "publication_time_attested_locally"
_OBSERVATION_FLAGS = (
    "cftc_legacy_futures_only",
    "publication_join_required",
    "public_futures_positioning_proxy_not_spot_fx_order_flow",
    "report_date_only_event_time",
    "revision_detection_limited_to_stable_cftc_row_id",
)


class COTPITError(ValueError):
    """Raised when COT evidence cannot cross the PIT boundary safely."""


@dataclass(frozen=True)
class _NormalizedCOTRow:
    source_record_id: str
    currency: str
    report_date: date
    contract_market_code: str
    noncommercial_long: int
    noncommercial_short: int
    open_interest: int
    raw_row_hash: str
    page_body_sha256: str
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class _CaptureBundle:
    path: Path
    capture_id: str
    acquired_at: datetime
    validated_at: datetime
    run_id: str
    writer_id: str
    rows: tuple[_NormalizedCOTRow, ...]


@dataclass(frozen=True)
class COTCapture:
    """A validated, immutable bundle containing count and every response page."""

    path: Path
    capture_id: str = field(init=False)
    acquired_at: datetime = field(init=False)
    validated_at: datetime = field(init=False)
    run_id: str = field(init=False)
    writer_id: str = field(init=False)

    def __post_init__(self) -> None:
        path = _absolute_path(self.path)
        bundle = _load_capture_bundle(path)
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "capture_id", bundle.capture_id)
        object.__setattr__(self, "acquired_at", bundle.acquired_at)
        object.__setattr__(self, "validated_at", bundle.validated_at)
        object.__setattr__(self, "run_id", bundle.run_id)
        object.__setattr__(self, "writer_id", bundle.writer_id)


@dataclass(frozen=True)
class COTReleaseAttestation:
    """Canonical sidecar plus the exact locally supplied evidence bytes it references."""

    attestation_path: Path
    evidence_path: Path
    report_date: date = field(init=False)
    basis: ReleaseBasis = field(init=False)
    released_at: datetime = field(init=False)
    evidence_uri: str = field(init=False)
    evidence_sha256: str = field(init=False)
    evidence_captured_at: datetime = field(init=False)
    run_id: str = field(init=False)
    writer_id: str = field(init=False)

    def __post_init__(self) -> None:
        attestation_path = _absolute_path(self.attestation_path)
        evidence_path = _absolute_path(self.evidence_path)
        if not _regular_file(attestation_path):
            raise COTPITError(f"release attestation must be a regular file: {attestation_path}")
        if not _regular_file(evidence_path):
            raise COTPITError(f"release evidence must be a regular file: {evidence_path}")
        payload = _load_canonical_attestation(attestation_path)
        evidence_digest = _file_sha256(evidence_path)
        if payload["evidence_sha256"] != evidence_digest:
            raise COTPITError("release attestation evidence hash does not match evidence bytes")
        report_date = _parse_iso_date(payload["report_date"], "attestation.report_date")
        basis = str(payload["basis"])
        if basis not in ("scheduled", "actual_release_notice"):
            raise COTPITError(f"unsupported release attestation basis: {basis}")
        released_at = _utc(payload["released_at"], "attestation.released_at")
        captured_at = _utc(payload["evidence_captured_at"], "attestation.evidence_captured_at")
        if released_at.date() < report_date:
            raise COTPITError("release time cannot precede the COT report date")
        if basis == "actual_release_notice" and captured_at < released_at:
            raise COTPITError("actual release evidence cannot be captured before actual release")
        object.__setattr__(self, "attestation_path", attestation_path)
        object.__setattr__(self, "evidence_path", evidence_path)
        object.__setattr__(self, "report_date", report_date)
        object.__setattr__(self, "basis", basis)
        object.__setattr__(self, "released_at", released_at)
        object.__setattr__(
            self,
            "evidence_uri",
            _official_cftc_uri(payload["evidence_uri"], "attestation.evidence_uri"),
        )
        object.__setattr__(self, "evidence_sha256", evidence_digest)
        object.__setattr__(self, "evidence_captured_at", captured_at)
        object.__setattr__(self, "run_id", str(payload["run_id"]))
        object.__setattr__(self, "writer_id", str(payload["writer_id"]))

    def payload(self) -> dict[str, Any]:
        return _load_canonical_attestation(self.attestation_path)


@dataclass(frozen=True)
class COTPITAudit:
    """Non-throwing result of generic plus source-specific reconstruction."""

    passed: bool
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    observation_count: int = 0
    release_attestation_count: int = 0


@dataclass(frozen=True)
class COTLoadResult:
    """Typed COT feature result; an empty mapping is never an ambiguous success."""

    status: LoadStatus
    prediction_time: datetime
    dataset_id: str | None
    reports: Mapping[str, CotReport]
    max_available_time: datetime | None
    record_hashes: tuple[str, ...]
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "prediction_time": self.prediction_time.isoformat(),
            "dataset_id": self.dataset_id,
            "report_currencies": sorted(self.reports),
            "max_available_time": (
                self.max_available_time.isoformat() if self.max_available_time else None
            ),
            "record_hashes": list(self.record_hashes),
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "usable": self.usable,
        }


def cot_count_query_url() -> str:
    """Return the count query used to prove page completeness."""

    where = _encoded_contract_filter()
    return f"{CFTC_COT_URL}?$select=count(*)%20as%20row_count&$where={where}"


def cot_query_url(*, limit: int = COT_PAGE_SIZE, offset: int = 0) -> str:
    """Return a stable, explicitly paginated official Socrata query."""

    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise COTPITError("COT page limit must be a positive integer")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise COTPITError("COT page offset must be a non-negative integer")
    where = _encoded_contract_filter()
    return (
        f"{CFTC_COT_URL}?$where={where}"
        "&$order=report_date_as_yyyy_mm_dd%20DESC,id%20ASC"
        f"&$limit={limit}&$offset={offset}"
    )


def fetch_cot_capture(
    capture_root: str | Path,
    *,
    session: requests.Session | None = None,
    clock: Callable[[], datetime] | None = None,
    run_id: str | None = None,
    writer_id: str | None = None,
    page_size: int = COT_PAGE_SIZE,
) -> COTCapture:
    """Fetch all pages, preserve exact response bytes, and return an admitted bundle.

    A non-2xx, changing count, invalid JSON, missing page, duplicate row, or schema
    failure is written as a quarantine bundle and then rejected.  Acquisition time
    is the completion of the final response, never the run start.
    """

    if not isinstance(page_size, int) or isinstance(page_size, bool) or page_size < 1:
        raise COTPITError("page_size must be a positive integer")
    now = clock or (lambda: datetime.now(UTC))
    http = session or requests
    first_started = _utc(now(), "request_started_at")
    resolved_writer = writer_id or f"{socket.gethostname()}:{os.getpid()}"
    resolved_run = run_id or f"cftc-cot-{first_started.strftime('%Y%m%dT%H%M%S%fZ')}"
    responses: list[dict[str, Any]] = []
    errors: list[str] = []
    expected_count: int | None = None

    try:
        count_start = _request_response(
            http,
            cot_count_query_url(),
            role="count_start",
            clock=now,
            request_started_at=first_started,
        )
        responses.append(count_start)
        _require_http_success(count_start)
        expected_count = _count_from_response(count_start)
        offset = 0
        for _page_number in range(COT_MAX_PAGES):
            page = _request_response(
                http,
                cot_query_url(limit=page_size, offset=offset),
                role="page",
                clock=now,
                offset=offset,
                limit=page_size,
            )
            responses.append(page)
            _require_http_success(page)
            rows = _json_array_from_response(page)
            if len(rows) < page_size:
                break
            offset += page_size
        else:
            raise COTPITError("COT pagination exceeded the configured page limit")

        count_end = _request_response(
            http,
            cot_count_query_url(),
            role="count_end",
            clock=now,
        )
        responses.append(count_end)
        _require_http_success(count_end)
        ending_count = _count_from_response(count_end)
        if ending_count != expected_count:
            raise COTPITError(
                f"COT row count changed during pagination: {expected_count} -> {ending_count}"
            )
        captured_rows = _validate_response_sequence(responses, page_size, expected_count)
        preliminary_acquired = _latest_response_completion(
            responses,
            fallback=_utc(now(), "preliminary_acquired_at"),
        )
        # Validate the source schema before marking the bundle admitted. The real
        # capture ID is the final bundle digest; a fixed valid digest is sufficient
        # for this pre-admission pass and is replaced during replay.
        _normalize_capture_rows(captured_rows, preliminary_acquired, "0" * 64)
    except COTPITError as error:
        errors.append(str(error))

    acquired_at = _latest_response_completion(responses, fallback=_utc(now(), "acquired_at"))
    validated_at = _utc(now(), "validated_at")
    payload = {
        "schema_version": 1,
        "artifact_kind": "cftc_cot_paginated_capture_bundle",
        "dataset_id": COT_DATASET_ID,
        "admitted": not errors,
        "run_id": resolved_run,
        "writer_id": resolved_writer,
        "acquired_at": acquired_at.isoformat(),
        "validated_at": validated_at.isoformat(),
        "page_size": page_size,
        "expected_row_count": expected_count,
        "responses": responses,
        "errors": errors,
    }
    path = _write_capture_bundle(capture_root, payload)
    if errors:
        raise COTPITError(f"COT response quarantined at {path}: {'; '.join(errors)}")
    return COTCapture(path)


def write_cot_release_attestation(
    attestation_path: str | Path,
    evidence_path: str | Path,
    *,
    report_date: date,
    basis: ReleaseBasis,
    released_at: datetime,
    evidence_uri: str,
    evidence_captured_at: datetime,
    run_id: str,
    writer_id: str,
) -> COTReleaseAttestation:
    """Create a canonical local release sidecar and bind exact caller-supplied bytes."""

    if not isinstance(report_date, date) or isinstance(report_date, datetime):
        raise COTPITError("report_date must be a date")
    if basis not in ("scheduled", "actual_release_notice"):
        raise COTPITError(f"unsupported release basis: {basis}")
    released = _utc(released_at, "released_at")
    captured = _utc(evidence_captured_at, "evidence_captured_at")
    if released.date() < report_date:
        raise COTPITError("release time cannot precede the COT report date")
    if basis == "actual_release_notice" and captured < released:
        raise COTPITError("actual release evidence cannot be captured before actual release")
    evidence = _absolute_path(evidence_path)
    if not _regular_file(evidence):
        raise COTPITError(f"release evidence must be a regular file: {evidence}")
    payload = {
        "schema_version": 1,
        "record_kind": "cot_release_attestation",
        "report_date": report_date.isoformat(),
        "basis": basis,
        "released_at": released.isoformat(),
        "evidence_uri": _official_cftc_uri(evidence_uri, "evidence_uri"),
        "evidence_sha256": _file_sha256(evidence),
        "evidence_captured_at": captured.isoformat(),
        "parser_version": COT_RELEASE_SOURCE_VERSION,
        "run_id": _required_text(run_id, "run_id"),
        "writer_id": _required_text(writer_id, "writer_id"),
    }
    target = _absolute_path(attestation_path)
    _exclusive_write(target, _canonical_bytes(payload) + b"\n")
    return COTReleaseAttestation(target, evidence)


def materialize_cot_pit_dataset(
    root: str | Path,
    captures: Sequence[COTCapture] = (),
    *,
    release_attestations: Sequence[COTReleaseAttestation] = (),
    previous_dataset: str | Path | None = None,
    created_at: datetime,
    code_commit: str,
    dirty_worktree: bool,
) -> PITDatasetArtifact:
    """Rebuild a cumulative COT artifact entirely from preserved raw inputs."""

    if any(not isinstance(item, COTCapture) for item in captures):
        raise COTPITError("captures must contain only COTCapture instances")
    if any(not isinstance(item, COTReleaseAttestation) for item in release_attestations):
        raise COTPITError("release_attestations contains an invalid item")
    created = _utc(created_at, "created_at")
    raw_inputs = _previous_raw_inputs(previous_dataset)
    for capture in captures:
        raw_inputs.append(
            RawInput(COT_DATA_SOURCE, _CAPTURE_ROLE, capture.path, capture.acquired_at)
        )
    for attestation in release_attestations:
        raw_inputs.extend(
            [
                RawInput(
                    COT_RELEASE_SOURCE,
                    _ATTESTATION_ROLE,
                    attestation.attestation_path,
                    attestation.evidence_captured_at,
                ),
                RawInput(
                    COT_RELEASE_SOURCE,
                    _RELEASE_EVIDENCE_ROLE,
                    attestation.evidence_path,
                    attestation.evidence_captured_at,
                ),
            ]
        )
    raw_inputs = _deduplicate_raw_inputs(raw_inputs)
    if not any(item.source == COT_DATA_SOURCE for item in raw_inputs):
        raise COTPITError("at least one admitted COT capture bundle is required")

    bundles = _capture_bundles_from_raw_inputs(raw_inputs)
    attestations = _release_attestations_from_raw_inputs(raw_inputs)
    records = [*_build_observation_records(bundles), *_build_release_records(attestations)]
    lineage = [_data_source_lineage()]
    if any(item.source == COT_RELEASE_SOURCE for item in raw_inputs):
        lineage.append(_release_source_lineage())
    artifact = materialize_pit_dataset(
        root,
        records,
        source_lineage=lineage,
        raw_inputs=raw_inputs,
        transform_name=COT_TRANSFORM_NAME,
        transform_version=COT_TRANSFORM_VERSION,
        dataset_class="research_only",
        description=COT_DATASET_DESCRIPTION,
        created_at=created,
        code_commit=code_commit,
        dirty_worktree=dirty_worktree,
    )
    audit = audit_cot_pit_dataset(artifact.directory)
    if not audit.passed:
        raise COTPITError(f"materialized COT artifact failed domain audit: {audit.errors}")
    return artifact


def audit_cot_pit_dataset(dataset_dir: str | Path) -> COTPITAudit:
    """Replay every capture and sidecar, then compare the exact record set."""

    errors: list[str] = []
    warnings: list[str] = []
    observation_count = 0
    release_count = 0
    try:
        generic = audit_pit_dataset(dataset_dir)
        if not generic.passed:
            return COTPITAudit(False, generic.errors, generic.warnings)
        identity = generic.manifest.get("identity")
        if not isinstance(identity, Mapping):
            raise COTPITError("manifest identity is missing")
        transform = identity.get("transform")
        if not isinstance(transform, Mapping) or transform.get("name") != COT_TRANSFORM_NAME:
            raise COTPITError("artifact is not a CFTC Legacy COT PIT dataset")
        if transform.get("version") != COT_TRANSFORM_VERSION:
            raise COTPITError("unsupported COT PIT transform version")
        if identity.get("dataset_class") != "research_only":
            raise COTPITError("COT PIT dataset_class must remain research_only")
        if identity.get("description") != COT_DATASET_DESCRIPTION:
            raise COTPITError("COT PIT dataset description does not match the adapter contract")
        if generic.manifest.get("promotion_eligible") is not False:
            raise COTPITError("COT artifact must remain promotion-ineligible")

        directory = Path(dataset_dir).expanduser().resolve()
        raw_inputs = _raw_inputs_from_manifest(directory, identity)
        expected_lineage = [_data_source_lineage().to_dict()]
        if any(item.source == COT_RELEASE_SOURCE for item in raw_inputs):
            expected_lineage.append(_release_source_lineage().to_dict())
        expected_lineage.sort(key=lambda row: str(row["source"]))
        if identity.get("source_lineage") != expected_lineage:
            raise COTPITError("COT source lineage does not match the adapter contract")
        bundles = _capture_bundles_from_raw_inputs(raw_inputs)
        attestations = _release_attestations_from_raw_inputs(raw_inputs)
        expected = [*_build_observation_records(bundles), *_build_release_records(attestations)]
        actual = list(load_pit_dataset_records(directory))
        if _canonical_record_set(actual) != _canonical_record_set(expected):
            raise COTPITError("COT records do not exactly replay from preserved raw inputs")
        observation_count = sum(record.source == COT_DATA_SOURCE for record in actual)
        release_count = sum(record.source == COT_RELEASE_SOURCE for record in actual)
        if release_count == 0:
            warnings.append("no release attestation is bound; COT rows are unavailable to features")
        warnings.extend(
            [
                "release sidecars are locally bound, not externally signed or independently timed",
                "count-bounded pagination cannot exclude same-count upstream mutation mid-capture",
                "COT is a public cross-contract futures-positioning proxy, not spot order flow",
                "COT success does not attest FRED, prices, features, or system-wide PIT integrity",
                "artifact remains research-only and promotion-ineligible",
            ]
        )
    except (
        COTPITError,
        PITDatasetError,
        PointInTimeError,
        KeyError,
        TypeError,
        ValueError,
        OSError,
        UnicodeDecodeError,
        RecursionError,
        OverflowError,
    ) as error:
        errors.append(str(error))
    return COTPITAudit(
        not errors,
        tuple(dict.fromkeys(errors)),
        tuple(dict.fromkeys(warnings)),
        observation_count,
        release_count,
    )


def load_cot_as_of(
    dataset_dir: str | Path,
    prediction_time: datetime,
    *,
    required_currencies: Sequence[str] | None = None,
) -> COTLoadResult:
    """Return a typed, release-joined COT state as known at prediction time."""

    prediction = _utc(prediction_time, "prediction_time")
    if required_currencies is None:
        required = tuple(sorted(COT_CONTRACT_CODES))
    else:
        if isinstance(required_currencies, (str, bytes)) or any(
            not isinstance(currency, str) for currency in required_currencies
        ):
            raise COTPITError("required_currencies must contain configured currency codes")
        required = tuple(sorted(set(required_currencies)))
        if not required:
            raise COTPITError("required_currencies cannot be empty")
        if unknown := sorted(set(required) - set(COT_CONTRACT_CODES)):
            raise COTPITError(f"unknown required COT currencies: {unknown}")
    audit = audit_cot_pit_dataset(dataset_dir)
    if not audit.passed:
        return COTLoadResult(
            "invalid",
            prediction,
            None,
            MappingProxyType({}),
            None,
            (),
            errors=audit.errors,
            warnings=audit.warnings,
        )
    try:
        dataset_id = dataset_hash_from_manifest(dataset_dir)
        records = load_pit_dataset_records(dataset_dir)
        releases = _latest_release_records_as_of(records, prediction)
        observations = _latest_observation_records_as_of(records, prediction)
        eligible_release_dates = {
            report_date
            for report_date, release in releases.items()
            if max(
                release.available_time,
                _utc(release.payload.get("released_at"), "release.payload.released_at"),
            )
            <= prediction
        }
        latest_eligible_report_date = max(eligible_release_dates, default=None)
        joined: dict[
            str, list[tuple[_NormalizedCOTRow, PointInTimeRecord, PointInTimeRecord, datetime]]
        ] = {}
        natural_keys: dict[tuple[str, date], str] = {}
        for observation in observations.values():
            row = _normalized_row_from_record(observation)
            release = releases.get(row.report_date)
            if release is None:
                continue
            declared_release = _utc(
                release.payload.get("released_at"), "release.payload.released_at"
            )
            effective = max(
                observation.available_time,
                release.available_time,
                declared_release,
            )
            if effective > prediction:
                continue
            natural = (row.currency, row.report_date)
            owner = natural_keys.get(natural)
            if owner is not None and owner != observation.source_record_id:
                raise COTPITError(f"ambiguous COT observations for {natural}: {owner}")
            natural_keys[natural] = observation.source_record_id
            joined.setdefault(row.currency, []).append((row, observation, release, effective))

        reports: dict[str, CotReport] = {}
        used_hashes: set[str] = set()
        release_bases: set[str] = set()
        for currency, rows in joined.items():
            rows.sort(key=lambda item: (item[0].report_date, item[3]))
            row, observation, release, effective = rows[-1]
            earlier = [item for item in rows[:-1] if item[0].report_date < row.report_date]
            previous_net = None
            previous_records: tuple[PointInTimeRecord, PointInTimeRecord] | None = None
            if earlier:
                previous, previous_observation, previous_release, previous_effective = earlier[-1]
                previous_net = previous.noncommercial_long - previous.noncommercial_short
                effective = max(effective, previous_effective)
                previous_records = (previous_observation, previous_release)
            release_payload = release.payload
            basis = str(release_payload.get("basis", ""))
            release_bases.add(basis)
            flags = tuple(
                sorted(
                    {
                        *observation.data_quality_flags,
                        _ATTESTED_FLAG,
                        f"release_basis_{basis}",
                        "release_attestation_is_local_not_independent_custody",
                    }
                    - {"publication_join_required"}
                )
            )
            reports[currency] = CotReport(
                currency=currency,
                report_date=row.report_date,
                net_position=row.noncommercial_long - row.noncommercial_short,
                open_interest=row.open_interest,
                prev_net_position=previous_net,
                available_time=effective,
                source_record_id=observation.source_record_id,
                content_hash=observation.content_hash,
                dataset_id=dataset_id,
                data_quality_flags=flags,
            )
            used_hashes.update((observation.content_hash, release.content_hash))
            if previous_records is not None:
                used_hashes.update(record.content_hash for record in previous_records)

        max_available = max(
            (report.available_time for report in reports.values() if report.available_time),
            default=None,
        )
        warnings = list(audit.warnings)
        if "scheduled" in release_bases:
            warnings.append(
                "scheduled release evidence is tentative; local ingestion remains a boundary"
            )
        if not reports:
            status: LoadStatus = "unavailable"
        elif missing := sorted(set(required) - set(reports)):
            status = "incomplete"
            warnings.append(f"missing required COT currencies: {missing}")
        elif latest_eligible_report_date is not None and (
            misaligned := {
                currency: reports[currency].report_date.isoformat()
                for currency in required
                if reports[currency].report_date != latest_eligible_report_date
            }
        ):
            status = "incomplete"
            warnings.append(
                "required COT currencies are not aligned to latest eligible report date "
                f"{latest_eligible_report_date}: {dict(sorted(misaligned.items()))}"
            )
        elif stale := sorted(
            currency
            for currency in required
            if (prediction.date() - reports[currency].report_date).days > COT_STALE_DAYS
        ):
            status = "stale"
            warnings.append(f"stale COT currencies: {stale}")
        else:
            status = "ok"
        return COTLoadResult(
            status,
            prediction,
            dataset_id,
            MappingProxyType(dict(sorted(reports.items()))),
            max_available,
            tuple(sorted(used_hashes)),
            warnings=tuple(dict.fromkeys(warnings)),
        )
    except (
        COTPITError,
        PITDatasetError,
        PointInTimeError,
        KeyError,
        TypeError,
        ValueError,
        OSError,
        RecursionError,
        OverflowError,
    ) as error:
        return COTLoadResult(
            "invalid",
            prediction,
            None,
            MappingProxyType({}),
            None,
            (),
            errors=(str(error),),
            warnings=audit.warnings,
        )


def load_cot_reports_as_of(
    dataset_dir: str | Path,
    prediction_time: datetime,
) -> dict[str, CotReport]:
    """Compatibility wrapper; non-usable typed states return no feature rows."""

    result = load_cot_as_of(dataset_dir, prediction_time)
    if result.status == "invalid":
        raise COTPITError(f"COT PIT dataset is invalid: {'; '.join(result.errors)}")
    return dict(result.reports) if result.usable else {}


def _request_response(
    http: Any,
    url: str,
    *,
    role: CaptureRole,
    clock: Callable[[], datetime],
    request_started_at: datetime | None = None,
    offset: int | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    started = request_started_at or _utc(clock(), "request_started_at")
    try:
        response = http.get(
            url,
            headers={"User-Agent": COT_USER_AGENT},
            timeout=COT_FETCH_TIMEOUT_SECONDS,
        )
    except requests.RequestException as error:
        raise COTPITError(f"CFTC request failed before a response was captured: {error}") from error
    completed = _utc(clock(), "response_completed_at")
    if completed < started:
        raise COTPITError("response completion cannot precede request start")
    body = bytes(response.content)
    response_headers = getattr(response, "headers", {})
    selected_headers: dict[str, str] = {}
    if isinstance(response_headers, Mapping):
        lowered = {str(key).lower(): str(value) for key, value in response_headers.items()}
        for name in ("content-type", "etag", "last-modified", "date"):
            if name in lowered:
                selected_headers[name] = lowered[name]
    row: dict[str, Any] = {
        "role": role,
        "request_uri": url,
        "request_started_at": started.isoformat(),
        "response_completed_at": completed.isoformat(),
        "status_code": int(getattr(response, "status_code", 0)),
        "headers": selected_headers,
        "body_sha256": hashlib.sha256(body).hexdigest(),
        "body_bytes": len(body),
        "body_base64": base64.b64encode(body).decode("ascii"),
    }
    if role == "page":
        row["offset"] = offset
        row["limit"] = limit
    return row


def _require_http_success(response: Mapping[str, Any]) -> None:
    status = response.get("status_code")
    if not isinstance(status, int) or isinstance(status, bool) or not 200 <= status < 300:
        raise COTPITError(f"CFTC HTTP status is not successful: {status}")


def _count_from_response(response: Mapping[str, Any]) -> int:
    rows = _json_array_from_response(response)
    if len(rows) != 1 or not isinstance(rows[0], Mapping):
        raise COTPITError("CFTC count response must contain one object")
    return _nonnegative_integer(rows[0].get("row_count"), "row_count")


def _json_array_from_response(response: Mapping[str, Any]) -> list[Any]:
    raw = _response_body(response)
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            parse_constant=_reject_nonstandard_json_constant,
            object_pairs_hook=_unique_json_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as error:
        raise COTPITError(f"CFTC response is not valid UTF-8 JSON: {error}") from error
    if not isinstance(payload, list):
        raise COTPITError("CFTC response must be a JSON array")
    return payload


def _reject_nonstandard_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant is forbidden: {value}")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key is forbidden: {key}")
        result[key] = value
    return result


def _response_body(response: Mapping[str, Any]) -> bytes:
    encoded = response.get("body_base64")
    if not isinstance(encoded, str):
        raise COTPITError("capture response body_base64 is missing")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except ValueError as error:
        raise COTPITError("capture response body is not valid base64") from error
    expected_hash = response.get("body_sha256")
    expected_bytes = response.get("body_bytes")
    if hashlib.sha256(raw).hexdigest() != expected_hash or len(raw) != expected_bytes:
        raise COTPITError("capture response body hash or size mismatch")
    return raw


def _validate_response_sequence(
    responses: Sequence[Mapping[str, Any]], page_size: int, expected_count: int
) -> list[tuple[Mapping[str, Any], str]]:
    if len(responses) < 3:
        raise COTPITError("capture must contain start count, pages, and end count")
    _validate_response_envelope(
        responses[0],
        expected_role="count_start",
        expected_uri=cot_count_query_url(),
    )
    _validate_response_envelope(
        responses[-1],
        expected_role="count_end",
        expected_uri=cot_count_query_url(),
    )
    if _count_from_response(responses[0]) != expected_count:
        raise COTPITError("capture start count does not match expected_row_count")
    if _count_from_response(responses[-1]) != expected_count:
        raise COTPITError("capture end count does not match expected_row_count")
    rows: list[tuple[Mapping[str, Any], str]] = []
    expected_offset = 0
    page_responses = responses[1:-1]
    for index, response in enumerate(page_responses):
        if response.get("offset") != expected_offset or response.get("limit") != page_size:
            raise COTPITError("COT pagination has a missing, overlapping, or reordered page")
        _validate_response_envelope(
            response,
            expected_role="page",
            expected_uri=cot_query_url(limit=page_size, offset=expected_offset),
        )
        page = _json_array_from_response(response)
        body_hash = str(response.get("body_sha256", ""))
        for item in page:
            if not isinstance(item, Mapping):
                raise COTPITError("COT page contains a non-object row")
            rows.append((item, body_hash))
        if index < len(page_responses) - 1 and len(page) != page_size:
            raise COTPITError("a short COT page was followed by another page")
        expected_offset += page_size
    final_page = _json_array_from_response(page_responses[-1])
    if len(final_page) >= page_size:
        raise COTPITError("a full final COT page must be followed by a terminal short page")
    if len(rows) != expected_count:
        raise COTPITError(
            f"paginated COT row count mismatch: expected {expected_count}, got {len(rows)}"
        )
    _validate_response_timeline(responses)
    return rows


def _validate_response_envelope(
    response: Mapping[str, Any],
    *,
    expected_role: CaptureRole,
    expected_uri: str,
) -> None:
    expected_keys = {
        "role",
        "request_uri",
        "request_started_at",
        "response_completed_at",
        "status_code",
        "headers",
        "body_sha256",
        "body_bytes",
        "body_base64",
    }
    if expected_role == "page":
        expected_keys.update(("offset", "limit"))
    if set(response) != expected_keys:
        raise COTPITError(f"COT {expected_role} response envelope schema is invalid")
    if response.get("role") != expected_role:
        raise COTPITError(f"unexpected COT response role: {response.get('role')}")
    if response.get("request_uri") != expected_uri:
        raise COTPITError(f"COT {expected_role} request URI does not match the adapter query")
    started = _utc(response.get("request_started_at"), "request_started_at")
    completed = _utc(response.get("response_completed_at"), "response_completed_at")
    if completed < started:
        raise COTPITError("response completion cannot precede request start")
    status = response.get("status_code")
    if not isinstance(status, int) or isinstance(status, bool):
        raise COTPITError("COT response status_code must be an integer")
    headers = response.get("headers")
    allowed_headers = {"content-type", "etag", "last-modified", "date"}
    if (
        not isinstance(headers, Mapping)
        or not set(headers) <= allowed_headers
        or any(
            not isinstance(key, str) or not isinstance(value, str) for key, value in headers.items()
        )
    ):
        raise COTPITError("COT response selected headers are invalid")
    _required_sha256(response.get("body_sha256"), "response.body_sha256")
    body_bytes = response.get("body_bytes")
    if not isinstance(body_bytes, int) or isinstance(body_bytes, bool) or body_bytes < 0:
        raise COTPITError("COT response body_bytes must be a non-negative integer")
    _response_body(response)


def _validate_response_timeline(responses: Sequence[Mapping[str, Any]]) -> None:
    prior_completion: datetime | None = None
    for response in responses:
        started = _utc(response.get("request_started_at"), "request_started_at")
        completed = _utc(response.get("response_completed_at"), "response_completed_at")
        if prior_completion is not None and started < prior_completion:
            raise COTPITError("COT response timeline overlaps or moves backward")
        prior_completion = completed


def _write_capture_bundle(root: str | Path, payload: Mapping[str, Any]) -> Path:
    content = _canonical_bytes(payload) + b"\n"
    digest = hashlib.sha256(content).hexdigest()
    acquired = _utc(payload.get("acquired_at"), "bundle.acquired_at")
    directory = Path(root).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"cftc-cot-{acquired.strftime('%Y%m%dT%H%M%S%fZ')}-{digest}.json"
    _exclusive_write(path, content)
    return path


def _load_capture_bundle(path: Path) -> _CaptureBundle:
    if not _regular_file(path):
        raise COTPITError(f"COT capture bundle must be a regular file: {path}")
    raw = path.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise COTPITError(f"cannot parse COT capture bundle: {error}") from error
    if not isinstance(payload, dict) or _canonical_bytes(payload) + b"\n" != raw:
        raise COTPITError("COT capture bundle is not canonical JSON")
    expected_keys = {
        "schema_version",
        "artifact_kind",
        "dataset_id",
        "admitted",
        "run_id",
        "writer_id",
        "acquired_at",
        "validated_at",
        "page_size",
        "expected_row_count",
        "responses",
        "errors",
    }
    if set(payload) != expected_keys:
        raise COTPITError("COT capture bundle schema keys are invalid")
    if payload["schema_version"] != 1 or payload["dataset_id"] != COT_DATASET_ID:
        raise COTPITError("unsupported COT capture bundle version or dataset")
    if payload["artifact_kind"] != "cftc_cot_paginated_capture_bundle":
        raise COTPITError("unexpected COT capture artifact kind")
    if payload["admitted"] is not True or payload["errors"] != []:
        raise COTPITError("quarantined COT capture cannot be admitted")
    page_size = payload["page_size"]
    expected_count = payload["expected_row_count"]
    if not isinstance(page_size, int) or isinstance(page_size, bool) or page_size < 1:
        raise COTPITError("invalid COT capture page_size")
    if (
        not isinstance(expected_count, int)
        or isinstance(expected_count, bool)
        or expected_count < 1
    ):
        raise COTPITError("invalid COT expected_row_count")
    responses = payload["responses"]
    if not isinstance(responses, list) or any(not isinstance(item, Mapping) for item in responses):
        raise COTPITError("invalid COT capture responses")
    for response in responses:
        _require_http_success(response)
    raw_rows = _validate_response_sequence(responses, page_size, expected_count)
    acquired = _utc(payload["acquired_at"], "bundle.acquired_at")
    validated = _utc(payload["validated_at"], "bundle.validated_at")
    if validated < acquired:
        raise COTPITError("bundle validation precedes acquisition")
    latest_completion = _latest_response_completion(responses, fallback=acquired)
    if acquired != latest_completion:
        raise COTPITError("bundle acquired_at is not the final response completion")
    capture_id = hashlib.sha256(raw).hexdigest()
    rows = _normalize_capture_rows(raw_rows, acquired, capture_id)
    return _CaptureBundle(
        path,
        capture_id,
        acquired,
        validated,
        _required_text(payload["run_id"], "bundle.run_id"),
        _required_text(payload["writer_id"], "bundle.writer_id"),
        tuple(rows),
    )


def _normalize_capture_rows(
    raw_rows: Sequence[tuple[Mapping[str, Any], str]],
    acquired_at: datetime,
    capture_id: str,
) -> list[_NormalizedCOTRow]:
    rows: list[_NormalizedCOTRow] = []
    source_ids: set[str] = set()
    natural_keys: set[tuple[str, date]] = set()
    prior_source_order: tuple[date, str] | None = None
    for index, (raw, body_sha) in enumerate(raw_rows):
        raw_code = raw.get("cftc_contract_market_code")
        code = _required_text(raw_code, f"row {index}.code")
        if raw_code != code:
            raise COTPITError(f"row {index}.code must be a canonical source string")
        currency = _CODE_TO_CURRENCY.get(code)
        if currency is None:
            raise COTPITError(f"capture contains an unexpected contract code: {code}")
        raw_source_id = raw.get("id")
        source_id = _required_text(raw_source_id, f"row {index}.id")
        if raw_source_id != source_id:
            raise COTPITError(f"row {index}.id must be a canonical source string")
        if source_id in source_ids:
            raise COTPITError(f"duplicate CFTC row ID in capture: {source_id}")
        source_ids.add(source_id)
        if raw.get("futonly_or_combined") != COT_REPORT_TYPE:
            raise COTPITError(f"row {source_id} is not Legacy Futures Only")
        report_date = _parse_cftc_report_date(
            raw.get("report_date_as_yyyy_mm_dd"), f"row {source_id}.date"
        )
        if report_date > acquired_at.date():
            raise COTPITError(f"row {source_id} has a future report date")
        if prior_source_order is not None:
            prior_date, prior_id = prior_source_order
            if report_date > prior_date or (report_date == prior_date and source_id < prior_id):
                raise COTPITError(
                    "COT rows violate the requested report-date-descending/ID-ascending order"
                )
        prior_source_order = (report_date, source_id)
        natural = (currency, report_date)
        if natural in natural_keys:
            raise COTPITError(f"duplicate COT currency/report date in capture: {natural}")
        natural_keys.add(natural)
        long = _nonnegative_integer(
            raw.get("noncomm_positions_long_all"), f"row {source_id}.noncommercial_long"
        )
        short = _nonnegative_integer(
            raw.get("noncomm_positions_short_all"), f"row {source_id}.noncommercial_short"
        )
        open_interest = _nonnegative_integer(
            raw.get("open_interest_all"), f"row {source_id}.open_interest"
        )
        if open_interest <= 0:
            raise COTPITError(f"row {source_id} has non-positive open interest")
        raw_row = dict(raw)
        try:
            raw_hash = canonical_content_hash(raw_row)
        except (TypeError, ValueError, RecursionError, OverflowError) as error:
            raise COTPITError(f"row {source_id} is not canonical JSON: {error}") from error
        payload: dict[str, Any] = {
            "adapter_schema_version": 2,
            "record_kind": "cot_observation",
            "dataset_id": COT_DATASET_ID,
            "report_type": "Legacy Futures Only",
            "source_row_id": source_id,
            "currency": currency,
            "contract_market_code": code,
            "report_date": report_date.isoformat(),
            "noncommercial_long": long,
            "noncommercial_short": short,
            "open_interest": open_interest,
            "canonical_raw_row_hash": raw_hash,
            "first_seen_capture_id": capture_id,
            "first_seen_body_sha256": body_sha,
            "raw_row": raw_row,
        }
        rows.append(
            _NormalizedCOTRow(
                source_id,
                currency,
                report_date,
                code,
                long,
                short,
                open_interest,
                raw_hash,
                body_sha,
                payload,
            )
        )
    if not rows:
        raise COTPITError("COT capture contains no configured currency rows")
    rows.sort(key=lambda item: (item.report_date, item.currency, item.source_record_id))
    return rows


def _build_observation_records(bundles: Sequence[_CaptureBundle]) -> list[PointInTimeRecord]:
    ordered = sorted(bundles, key=lambda item: (item.acquired_at, item.capture_id))
    if len({item.acquired_at for item in ordered}) != len(ordered):
        raise COTPITError("admitted COT capture timestamps must be unique")
    latest_hash: dict[str, str] = {}
    identities: dict[str, tuple[str, date, str]] = {}
    natural_owners: dict[tuple[str, date], str] = {}
    records: list[PointInTimeRecord] = []
    prior_capture_ids: set[str] | None = None
    for bundle in ordered:
        current_capture_ids = {row.source_record_id for row in bundle.rows}
        if prior_capture_ids is not None and (missing := prior_capture_ids - current_capture_ids):
            preview = sorted(missing)[:5]
            raise COTPITError(
                "previously observed CFTC row IDs disappeared from a later complete capture: "
                f"{preview}" + (" ..." if len(missing) > len(preview) else "")
            )
        for row in bundle.rows:
            identity = (row.currency, row.report_date, row.contract_market_code)
            if prior_identity := identities.get(row.source_record_id):
                if prior_identity != identity:
                    raise COTPITError(f"CFTC row ID changed identity: {row.source_record_id}")
            identities[row.source_record_id] = identity
            natural = (row.currency, row.report_date)
            owner = natural_owners.get(natural)
            if owner is not None and owner != row.source_record_id:
                raise COTPITError(
                    f"multiple CFTC IDs claim {natural}: {owner}, {row.source_record_id}"
                )
            natural_owners[natural] = row.source_record_id
            prior_hash = latest_hash.get(row.source_record_id)
            if prior_hash == row.raw_row_hash:
                continue
            record = PointInTimeRecord(
                event_time=datetime.combine(row.report_date, datetime.min.time(), tzinfo=UTC),
                available_time=bundle.acquired_at,
                ingested_time=bundle.acquired_at,
                revision_time=bundle.acquired_at if prior_hash is not None else None,
                validated_time=bundle.validated_at,
                source=COT_DATA_SOURCE,
                source_record_id=row.source_record_id,
                payload=row.payload,
                schema_version=2,
                run_id=bundle.run_id,
                writer_id=bundle.writer_id,
                data_quality_flags=(
                    *_OBSERVATION_FLAGS,
                    *(("source_revision_observed_at_ingestion",) if prior_hash is not None else ()),
                ),
            )
            records.append(record)
            latest_hash[row.source_record_id] = row.raw_row_hash
        prior_capture_ids = current_capture_ids
    return records


def _build_release_records(
    attestations: Sequence[COTReleaseAttestation],
) -> list[PointInTimeRecord]:
    ordered = sorted(
        attestations,
        key=lambda item: (
            item.report_date,
            item.evidence_captured_at,
            item.released_at,
            str(item.attestation_path),
        ),
    )
    latest_hash: dict[date, str] = {}
    latest_evidence_capture: dict[date, datetime] = {}
    records: list[PointInTimeRecord] = []
    for item in ordered:
        payload = item.payload()
        payload_hash = canonical_content_hash(payload)
        prior_hash = latest_hash.get(item.report_date)
        if prior_hash == payload_hash:
            continue
        prior_capture = latest_evidence_capture.get(item.report_date)
        if prior_capture is not None and item.evidence_captured_at <= prior_capture:
            raise COTPITError(
                f"release attestation revision is not strictly later: {item.report_date}"
            )
        record = PointInTimeRecord(
            event_time=datetime.combine(item.report_date, datetime.min.time(), tzinfo=UTC),
            available_time=item.evidence_captured_at,
            ingested_time=item.evidence_captured_at,
            # A schedule is known when its evidence is captured, but is not an
            # actual publication. The declared release boundary remains in the
            # payload and is applied during the observation/release as-of join.
            published_time=(item.released_at if item.basis == "actual_release_notice" else None),
            revision_time=item.evidence_captured_at if prior_hash is not None else None,
            validated_time=item.evidence_captured_at,
            source=COT_RELEASE_SOURCE,
            source_record_id=item.report_date.isoformat(),
            payload=payload,
            schema_version=1,
            run_id=item.run_id,
            writer_id=item.writer_id,
            data_quality_flags=(
                f"release_basis_{item.basis}",
                "release_attestation_is_local_not_independent_custody",
            ),
        )
        records.append(record)
        latest_hash[item.report_date] = payload_hash
        latest_evidence_capture[item.report_date] = item.evidence_captured_at
    return records


def _normalized_row_from_record(record: PointInTimeRecord) -> _NormalizedCOTRow:
    if record.source != COT_DATA_SOURCE or record.payload.get("record_kind") != "cot_observation":
        raise COTPITError(f"not a COT observation record: {record.source_record_id}")
    payload = record.payload
    source_id = _required_text(payload.get("source_row_id"), "payload.source_row_id")
    if source_id != record.source_record_id:
        raise COTPITError("normalized source_row_id does not match record source ID")
    raw = payload.get("raw_row")
    if not isinstance(raw, Mapping):
        raise COTPITError(f"COT raw_row is missing: {source_id}")
    if str(raw.get("id", "")) != source_id:
        raise COTPITError(f"raw CFTC ID does not match normalized ID: {source_id}")
    raw_hash = canonical_content_hash(raw)
    if payload.get("canonical_raw_row_hash") != raw_hash:
        raise COTPITError(f"raw CFTC row hash does not match normalized row: {source_id}")
    currency = _required_text(payload.get("currency"), "payload.currency")
    code = _required_text(payload.get("contract_market_code"), "payload.contract_market_code")
    if _CODE_TO_CURRENCY.get(code) != currency or str(raw.get("cftc_contract_market_code")) != code:
        raise COTPITError(f"COT currency/contract mismatch: {source_id}")
    report_date = _parse_iso_date(payload.get("report_date"), "payload.report_date")
    if (
        _parse_cftc_report_date(raw.get("report_date_as_yyyy_mm_dd"), "raw report date")
        != report_date
    ):
        raise COTPITError(f"raw and normalized COT dates differ: {source_id}")
    long = _nonnegative_integer(payload.get("noncommercial_long"), "payload.long")
    short = _nonnegative_integer(payload.get("noncommercial_short"), "payload.short")
    open_interest = _nonnegative_integer(payload.get("open_interest"), "payload.open_interest")
    if long != _nonnegative_integer(raw.get("noncomm_positions_long_all"), "raw.long"):
        raise COTPITError(f"raw and normalized COT long positions differ: {source_id}")
    if short != _nonnegative_integer(raw.get("noncomm_positions_short_all"), "raw.short"):
        raise COTPITError(f"raw and normalized COT short positions differ: {source_id}")
    if open_interest != _nonnegative_integer(raw.get("open_interest_all"), "raw.open_interest"):
        raise COTPITError(f"raw and normalized COT open interest differ: {source_id}")
    if open_interest <= 0:
        raise COTPITError(f"non-positive COT open interest: {source_id}")
    return _NormalizedCOTRow(
        source_id,
        currency,
        report_date,
        code,
        long,
        short,
        open_interest,
        raw_hash,
        _required_sha256(payload.get("first_seen_body_sha256"), "first_seen_body_sha256"),
        payload,
    )


def _latest_observation_records_as_of(
    records: Sequence[PointInTimeRecord], prediction: datetime
) -> dict[str, PointInTimeRecord]:
    result: dict[str, PointInTimeRecord] = {}
    for record in records:
        if record.source != COT_DATA_SOURCE or record.available_time > prediction:
            continue
        prior = result.get(record.source_record_id)
        if prior is None or record.available_time > prior.available_time:
            result[record.source_record_id] = record
    return result


def _latest_release_records_as_of(
    records: Sequence[PointInTimeRecord], prediction: datetime
) -> dict[date, PointInTimeRecord]:
    result: dict[date, PointInTimeRecord] = {}
    for record in records:
        if record.source != COT_RELEASE_SOURCE or record.available_time > prediction:
            continue
        report_date = _parse_iso_date(record.payload.get("report_date"), "release.report_date")
        prior = result.get(report_date)
        if prior is None or record.available_time > prior.available_time:
            result[report_date] = record
    return result


def _previous_raw_inputs(previous_dataset: str | Path | None) -> list[RawInput]:
    if previous_dataset is None:
        return []
    directory = Path(previous_dataset).expanduser().resolve()
    audit = audit_pit_dataset(directory)
    if not audit.passed:
        raise COTPITError(f"previous PIT dataset audit failed: {'; '.join(audit.errors)}")
    identity = audit.manifest.get("identity")
    if not isinstance(identity, Mapping):
        raise COTPITError("previous PIT dataset identity is missing")
    transform = identity.get("transform")
    if not isinstance(transform, Mapping) or transform.get("name") != COT_TRANSFORM_NAME:
        raise COTPITError("previous dataset is not a COT PIT artifact")
    return _raw_inputs_from_manifest(directory, identity)


def _raw_inputs_from_manifest(directory: Path, identity: Mapping[str, Any]) -> list[RawInput]:
    rows = identity.get("raw_inputs")
    if not isinstance(rows, list) or not rows:
        raise COTPITError("COT manifest raw_inputs are missing")
    allowed = {
        (COT_DATA_SOURCE, _CAPTURE_ROLE),
        (COT_RELEASE_SOURCE, _ATTESTATION_ROLE),
        (COT_RELEASE_SOURCE, _RELEASE_EVIDENCE_ROLE),
    }
    result: list[RawInput] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise COTPITError("COT manifest contains a non-object raw input")
        source = str(row.get("source", ""))
        role = str(row.get("role", ""))
        if (source, role) not in allowed:
            raise COTPITError(f"unexpected COT raw source/role: {(source, role)}")
        stored = str(row.get("stored_path", ""))
        if not stored.startswith("raw/"):
            raise COTPITError("COT raw input has an invalid stored path")
        result.append(
            RawInput(
                source,
                role,
                directory / stored,
                _utc(row.get("acquired_at"), "raw_input.acquired_at"),
            )
        )
    return result


def _capture_bundles_from_raw_inputs(values: Sequence[RawInput]) -> list[_CaptureBundle]:
    bundles = [
        _load_capture_bundle(item.path)
        for item in values
        if item.source == COT_DATA_SOURCE and item.role == _CAPTURE_ROLE
    ]
    for item, bundle in zip(
        [row for row in values if row.source == COT_DATA_SOURCE and row.role == _CAPTURE_ROLE],
        bundles,
        strict=True,
    ):
        if item.acquired_at != bundle.acquired_at:
            raise COTPITError("raw manifest acquisition time does not match capture bundle")
    if not bundles:
        raise COTPITError("COT artifact has no capture bundles")
    return bundles


def _release_attestations_from_raw_inputs(
    values: Sequence[RawInput],
) -> list[COTReleaseAttestation]:
    sidecars = [
        item
        for item in values
        if item.source == COT_RELEASE_SOURCE and item.role == _ATTESTATION_ROLE
    ]
    evidence = [
        item
        for item in values
        if item.source == COT_RELEASE_SOURCE and item.role == _RELEASE_EVIDENCE_ROLE
    ]
    evidence_by_hash: dict[str, list[RawInput]] = {}
    for item in evidence:
        evidence_by_hash.setdefault(_file_sha256(item.path), []).append(item)
    result: list[COTReleaseAttestation] = []
    used_evidence: set[tuple[Path, datetime]] = set()
    for sidecar in sidecars:
        payload = _load_canonical_attestation(sidecar.path)
        digest = str(payload["evidence_sha256"])
        captured = _utc(payload["evidence_captured_at"], "attestation.evidence_captured_at")
        matches = [
            item for item in evidence_by_hash.get(digest, []) if item.acquired_at == captured
        ]
        if not matches:
            raise COTPITError("release sidecar has no matching preserved evidence bytes")
        selected = sorted(matches, key=lambda item: str(item.path))[0]
        if sidecar.acquired_at != captured:
            raise COTPITError("release sidecar manifest time does not match its payload")
        result.append(COTReleaseAttestation(sidecar.path, selected.path))
        used_evidence.add((selected.path, selected.acquired_at))
    unused = {(item.path, item.acquired_at) for item in evidence} - used_evidence
    if unused:
        raise COTPITError("preserved release evidence is not referenced by an attestation sidecar")
    return result


def _load_canonical_attestation(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise COTPITError(f"cannot parse release attestation: {error}") from error
    expected_keys = {
        "schema_version",
        "record_kind",
        "report_date",
        "basis",
        "released_at",
        "evidence_uri",
        "evidence_sha256",
        "evidence_captured_at",
        "parser_version",
        "run_id",
        "writer_id",
    }
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        raise COTPITError("release attestation schema is invalid")
    if _canonical_bytes(payload) + b"\n" != raw:
        raise COTPITError("release attestation is not canonical JSON")
    if payload["schema_version"] != 1 or payload["record_kind"] != "cot_release_attestation":
        raise COTPITError("unsupported release attestation version")
    if payload["parser_version"] != COT_RELEASE_SOURCE_VERSION:
        raise COTPITError("unsupported release attestation parser version")
    _official_cftc_uri(payload["evidence_uri"], "attestation.evidence_uri")
    _required_sha256(payload["evidence_sha256"], "attestation.evidence_sha256")
    _required_text(payload["run_id"], "attestation.run_id")
    _required_text(payload["writer_id"], "attestation.writer_id")
    return payload


def _data_source_lineage() -> SourceLineage:
    return SourceLineage(
        source=COT_DATA_SOURCE,
        upstream_uri=CFTC_COT_URL,
        source_version=COT_DATA_SOURCE_VERSION,
        contract_status="research_only",
        license_status="research_only",
        limitations=(
            "capture count can change during pagination and is rejected rather than reconciled",
            "source-row disappearance has no tombstone contract and fails materialization",
            "row-ID revisions are observed only across retained local captures",
            "historical backfill is available no earlier than first local ingestion",
            "Legacy noncommercial futures positions are not spot FX customer or dealer flow",
            "cross-contract pair subtraction requires separate empirical validation",
        ),
    )


def _release_source_lineage() -> SourceLineage:
    return SourceLineage(
        source=COT_RELEASE_SOURCE,
        upstream_uri=COT_RELEASE_SCHEDULE_URI,
        source_version=COT_RELEASE_SOURCE_VERSION,
        contract_status="research_only",
        license_status="research_only",
        limitations=(
            "sidecars are local declarations without external signature or trusted timestamp",
            "scheduled release evidence is tentative and can be revised",
            "actual-release notices require evidence captured no earlier than the claimed release",
        ),
    )


def _canonical_record_set(records: Sequence[PointInTimeRecord]) -> tuple[bytes, ...]:
    return tuple(sorted(_canonical_bytes(record.to_dict()) for record in records))


def _deduplicate_raw_inputs(values: Sequence[RawInput]) -> list[RawInput]:
    result: list[RawInput] = []
    seen: set[tuple[str, str, datetime, str]] = set()
    for item in values:
        # A previous artifact stores the same bytes under their digest rather than
        # the original filename. Deduplicate by evidence identity so explicitly
        # re-supplying the same capture/sidecar remains idempotent across extension.
        key = (item.source, item.role, item.acquired_at, _file_sha256(item.path))
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _latest_response_completion(
    responses: Sequence[Mapping[str, Any]], *, fallback: datetime
) -> datetime:
    values = [
        _utc(response.get("response_completed_at"), "response_completed_at")
        for response in responses
    ]
    return max(values, default=fallback)


def _encoded_contract_filter() -> str:
    codes = ",".join(f"'{code}'" for code in sorted(COT_CONTRACT_CODES.values()))
    return requests.utils.quote(f"cftc_contract_market_code in({codes})")


def _parse_iso_date(value: object, field_name: str) -> date:
    if not isinstance(value, str):
        raise COTPITError(f"{field_name} is not a valid ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise COTPITError(f"{field_name} is not a valid ISO date") from error
    if parsed.isoformat() != value:
        raise COTPITError(f"{field_name} is not a canonical ISO date")
    return parsed


def _parse_cftc_report_date(value: object, field_name: str) -> date:
    if not isinstance(value, str) or not value.strip():
        raise COTPITError(f"{field_name} is not a valid CFTC report date")
    text = value.strip()
    if len(text) == 10:
        return _parse_iso_date(text, field_name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise COTPITError(f"{field_name} is not a valid CFTC report timestamp") from error
    if parsed.timetz().replace(tzinfo=None) != datetime.min.time():
        raise COTPITError(f"{field_name} CFTC report timestamp must be midnight")
    return parsed.date()


def _official_cftc_uri(value: object, field_name: str) -> str:
    text = _required_text(value, field_name)
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError as error:
        raise COTPITError(f"{field_name} must be an official HTTPS CFTC URI") from error
    hostname = (parsed.hostname or "").lower()
    if (
        parsed.scheme.lower() != "https"
        or not hostname
        or not (hostname == "cftc.gov" or hostname.endswith(".cftc.gov"))
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or bool(parsed.fragment)
    ):
        raise COTPITError(f"{field_name} must be an official HTTPS CFTC URI")
    return text


def _nonnegative_integer(value: object, field_name: str) -> int:
    if isinstance(value, bool) or value is None:
        raise COTPITError(f"{field_name} must be a non-negative integer")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise COTPITError(f"{field_name} must be a non-negative integer") from error
    if not parsed.is_finite() or parsed < 0 or parsed != parsed.to_integral_value():
        raise COTPITError(f"{field_name} must be a non-negative integer")
    return int(parsed)


def _utc(value: object, field_name: str) -> datetime:
    try:
        return utc_datetime(value, field_name=field_name)
    except PointInTimeError as error:
        raise COTPITError(str(error)) from error


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise COTPITError(f"{field_name} must be a non-empty string")
    return value.strip()


def _required_sha256(value: object, field_name: str) -> str:
    text = _required_text(value, field_name)
    if len(text) != 64 or any(character not in _SHA256_HEX for character in text):
        raise COTPITError(f"{field_name} must be a lowercase SHA-256 digest")
    return text


def _regular_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def _absolute_path(value: str | Path) -> Path:
    """Return an absolute lexical path without following a leaf symlink."""

    return Path(os.path.abspath(os.fspath(Path(value).expanduser())))


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise COTPITError(f"value is not canonical JSON: {error}") from error


def _exclusive_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        if not _regular_file(path) or path.read_bytes() != content:
            raise COTPITError(f"immutable file collision: {path}")
    try:
        descriptor = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "COT_DATASET_DESCRIPTION",
    "COT_DATA_SOURCE",
    "COT_DATA_SOURCE_VERSION",
    "COT_PAGE_SIZE",
    "COT_RELEASE_SCHEDULE_URI",
    "COT_RELEASE_SOURCE",
    "COT_RELEASE_SOURCE_VERSION",
    "COT_TRANSFORM_NAME",
    "COT_TRANSFORM_VERSION",
    "COTCapture",
    "COTLoadResult",
    "COTPITAudit",
    "COTPITError",
    "COTReleaseAttestation",
    "ReleaseBasis",
    "audit_cot_pit_dataset",
    "cot_count_query_url",
    "cot_query_url",
    "fetch_cot_capture",
    "load_cot_as_of",
    "load_cot_reports_as_of",
    "materialize_cot_pit_dataset",
    "write_cot_release_attestation",
]

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
import json
from pathlib import Path
import re

import pytest

import fx_briefing
from fx_backtester.pit_dataset import (
    RawInput,
    SourceLineage,
    audit_pit_dataset,
    load_pit_dataset_records,
    materialize_pit_dataset,
)
from fx_intel import cot_pit
from fx_intel.macro import COT_CONTRACT_CODES, MacroSnapshot

COMMIT = "b" * 40
REPORT_1 = date(2026, 6, 30)
REPORT_2 = date(2026, 7, 7)
RELEASE_1 = datetime(2026, 7, 6, 19, 30, tzinfo=UTC)  # holiday-delayed Monday
RELEASE_2 = datetime(2026, 7, 10, 19, 30, tzinfo=UTC)
CREATED_AT = datetime(2026, 7, 11, 1, 0, tzinfo=UTC)


class _StepClock:
    def __init__(self, start: datetime) -> None:
        self.current = start

    def __call__(self) -> datetime:
        value = self.current
        self.current += timedelta(seconds=1)
        return value


class _Response:
    def __init__(self, payload: object, *, status: int = 200) -> None:
        self.status_code = status
        self.content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Date": "Fri, 10 Jul 2026 19:31:00 GMT",
            "ETag": '"fixture"',
        }


class _Session:
    def __init__(
        self,
        rows: list[dict[str, object]],
        *,
        first_status: int = 200,
        ending_count_delta: int = 0,
    ) -> None:
        self.rows = rows
        self.first_status = first_status
        self.ending_count_delta = ending_count_delta
        self.count_calls = 0
        self.urls: list[str] = []

    def get(self, url: str, **_kwargs: object) -> _Response:
        self.urls.append(url)
        if "$select=count" in url:
            self.count_calls += 1
            count = len(self.rows)
            if self.count_calls > 1:
                count += self.ending_count_delta
            status = self.first_status if self.count_calls == 1 else 200
            return _Response([{"row_count": str(count)}], status=status)
        offset_match = re.search(r"\$offset=(\d+)", url)
        limit_match = re.search(r"\$limit=(\d+)", url)
        assert offset_match and limit_match
        offset = int(offset_match.group(1))
        limit = int(limit_match.group(1))
        return _Response(self.rows[offset : offset + limit])


def _source_row(
    currency: str,
    report_date: date,
    *,
    long: int | None = None,
    source_id: str | None = None,
) -> dict[str, object]:
    code = COT_CONTRACT_CODES[currency]
    ordinal = sorted(COT_CONTRACT_CODES).index(currency)
    report_index = 1 if report_date == REPORT_1 else 2
    resolved_long = long if long is not None else 10_000 + report_index * 1_000 + ordinal * 10
    return {
        "id": source_id or f"{report_date:%y%m%d}{code}F",
        "cftc_contract_market_code": code,
        "market_and_exchange_names": f"{currency} TEST FUTURE",
        "report_date_as_yyyy_mm_dd": f"{report_date.isoformat()}T00:00:00.000",
        "futonly_or_combined": "FutOnly",
        "noncomm_positions_long_all": str(resolved_long),
        "noncomm_positions_short_all": str(7_000 + ordinal),
        "open_interest_all": str(100_000 + ordinal * 100),
    }


def _all_rows(*report_dates: date) -> list[dict[str, object]]:
    rows = [
        _source_row(currency, report_date)
        for report_date in sorted(report_dates, reverse=True)
        for currency in COT_CONTRACT_CODES
    ]
    return sorted(
        rows,
        key=lambda row: (
            -date.fromisoformat(str(row["report_date_as_yyyy_mm_dd"])[:10]).toordinal(),
            str(row["id"]),
        ),
    )


def _capture(
    root: Path,
    rows: list[dict[str, object]],
    *,
    start: datetime,
    page_size: int = 5,
    session: _Session | None = None,
) -> cot_pit.COTCapture:
    return cot_pit.fetch_cot_capture(
        root,
        session=session or _Session(rows),  # type: ignore[arg-type]
        clock=_StepClock(start),
        run_id=f"run-{start:%Y%m%d%H%M%S}",
        writer_id="test-writer",
        page_size=page_size,
    )


def _attestation(
    root: Path,
    report_date: date,
    released_at: datetime,
    *,
    basis: cot_pit.ReleaseBasis = "actual_release_notice",
    captured_at: datetime | None = None,
    suffix: str = "",
) -> cot_pit.COTReleaseAttestation:
    evidence = root / f"release-evidence-{report_date}{suffix}.html"
    evidence.write_text(
        f"official CFTC evidence for {report_date} released {released_at.isoformat()}",
        encoding="utf-8",
    )
    return cot_pit.write_cot_release_attestation(
        root / f"release-attestation-{report_date}{suffix}.json",
        evidence,
        report_date=report_date,
        basis=basis,
        released_at=released_at,
        evidence_uri=cot_pit.COT_RELEASE_SCHEDULE_URI,
        evidence_captured_at=captured_at or released_at + timedelta(minutes=5),
        run_id=f"release-{report_date}{suffix}",
        writer_id="test-reviewer",
    )


def _artifact(
    tmp_path: Path,
    captures: list[cot_pit.COTCapture],
    attestations: list[cot_pit.COTReleaseAttestation],
    *,
    previous: Path | None = None,
    name: str = "artifacts",
) -> Path:
    artifact = cot_pit.materialize_cot_pit_dataset(
        tmp_path / name,
        captures,
        release_attestations=attestations,
        previous_dataset=previous,
        created_at=CREATED_AT,
        code_commit=COMMIT,
        dirty_worktree=True,
    )
    return artifact.directory


def test_fetch_uses_response_completion_and_proves_pagination(tmp_path: Path) -> None:
    rows = _all_rows(REPORT_1)
    start = datetime(2026, 7, 6, 20, 0, tzinfo=UTC)
    session = _Session(rows)
    capture = _capture(tmp_path / "captures", rows, start=start, page_size=4, session=session)

    assert capture.acquired_at > start
    assert capture.validated_at > capture.acquired_at
    assert session.urls[0] == cot_pit.cot_count_query_url()
    assert session.urls[-1] == cot_pit.cot_count_query_url()
    page_urls = [url for url in session.urls if "$offset=" in url]
    assert len(page_urls) == 3  # 8 rows: full 4 + full 4 + terminal empty page
    assert "$offset=8" in page_urls[-1]


def test_non_2xx_and_schema_failure_are_preserved_but_quarantined(tmp_path: Path) -> None:
    bad_http = _Session(_all_rows(REPORT_1), first_status=503)
    with pytest.raises(cot_pit.COTPITError, match="quarantined"):
        _capture(
            tmp_path / "http",
            bad_http.rows,
            start=datetime(2026, 7, 6, 20, 0, tzinfo=UTC),
            session=bad_http,
        )
    quarantined = list((tmp_path / "http").glob("*.json"))
    assert len(quarantined) == 1
    assert json.loads(quarantined[0].read_text(encoding="utf-8"))["admitted"] is False

    missing_id = _all_rows(REPORT_1)
    missing_id[0] = {key: value for key, value in missing_id[0].items() if key != "id"}
    with pytest.raises(cot_pit.COTPITError, match="quarantined"):
        _capture(
            tmp_path / "schema",
            missing_id,
            start=datetime(2026, 7, 6, 20, 0, tzinfo=UTC),
        )
    schema_bundle = next((tmp_path / "schema").glob("*.json"))
    assert json.loads(schema_bundle.read_text(encoding="utf-8"))["admitted"] is False

    nonstandard_json = _all_rows(REPORT_1)
    nonstandard_json[0]["unexpected_nonfinite"] = float("nan")
    with pytest.raises(cot_pit.COTPITError, match="quarantined"):
        _capture(
            tmp_path / "nonstandard-json",
            nonstandard_json,
            start=datetime(2026, 7, 6, 20, 0, tzinfo=UTC),
        )
    nonstandard_bundle = next((tmp_path / "nonstandard-json").glob("*.json"))
    errors = json.loads(nonstandard_bundle.read_text(encoding="utf-8"))["errors"]
    assert any("non-standard JSON constant" in error for error in errors)


def test_capture_rejects_changing_count_and_missing_terminal_page(tmp_path: Path) -> None:
    rows = _all_rows(REPORT_1)
    changing = _Session(rows, ending_count_delta=1)
    with pytest.raises(cot_pit.COTPITError, match="row count changed"):
        _capture(
            tmp_path / "changing",
            rows,
            start=datetime(2026, 7, 6, 20, 0, tzinfo=UTC),
            session=changing,
        )

    capture = _capture(
        tmp_path / "complete",
        rows,
        start=datetime(2026, 7, 6, 20, 0, tzinfo=UTC),
        page_size=len(rows),
    )
    payload = json.loads(capture.path.read_text(encoding="utf-8"))
    del payload["responses"][-2]  # remove required empty terminal page
    broken = tmp_path / "missing-terminal.json"
    broken.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(cot_pit.COTPITError, match="full final COT page"):
        cot_pit.COTCapture(broken)


def test_capture_rejects_rows_that_violate_requested_stable_order(tmp_path: Path) -> None:
    rows = _all_rows(REPORT_2, REPORT_1)
    rows[0], rows[-1] = rows[-1], rows[0]

    with pytest.raises(cot_pit.COTPITError, match="report-date-descending"):
        _capture(
            tmp_path / "out-of-order",
            rows,
            start=datetime(2026, 7, 10, 20, 0, tzinfo=UTC),
        )

    quarantined = next((tmp_path / "out-of-order").glob("*.json"))
    assert json.loads(quarantined.read_text(encoding="utf-8"))["admitted"] is False


def test_identical_recapture_preserves_raw_bundles_without_extra_versions(
    tmp_path: Path,
) -> None:
    rows = _all_rows(REPORT_1)
    first = _capture(tmp_path / "c1", rows, start=datetime(2026, 7, 6, 20, 0, tzinfo=UTC))
    second = _capture(tmp_path / "c2", rows, start=datetime(2026, 7, 7, 20, 0, tzinfo=UTC))
    dataset = _artifact(tmp_path, [first, second], [])
    audit = cot_pit.audit_cot_pit_dataset(dataset)
    manifest = json.loads((dataset / "manifest.json").read_text(encoding="utf-8"))

    assert audit.passed
    assert audit.observation_count == len(rows)
    capture_inputs = [
        row
        for row in manifest["identity"]["raw_inputs"]
        if row["role"] == "cftc_paginated_capture_bundle"
    ]
    assert len(capture_inputs) == 2


def test_changed_same_cftc_id_is_visible_only_after_revision_capture(tmp_path: Path) -> None:
    original = _all_rows(REPORT_1)
    corrected = [dict(row) for row in original]
    jpy_id = _source_row("JPY", REPORT_1)["id"]
    target = next(row for row in corrected if row["id"] == jpy_id)
    original_long = int(str(target["noncomm_positions_long_all"]))
    target["noncomm_positions_long_all"] = str(original_long + 5_000)
    first = _capture(tmp_path / "c1", original, start=datetime(2026, 7, 6, 20, 0, tzinfo=UTC))
    second = _capture(tmp_path / "c2", corrected, start=datetime(2026, 7, 8, 20, 0, tzinfo=UTC))
    release = _attestation(tmp_path, REPORT_1, RELEASE_1)
    dataset = _artifact(tmp_path, [first, second], [release])

    before = cot_pit.load_cot_as_of(
        dataset, first.acquired_at + timedelta(minutes=1), required_currencies=("JPY",)
    )
    after = cot_pit.load_cot_as_of(
        dataset, second.validated_at + timedelta(minutes=1), required_currencies=("JPY",)
    )
    assert before.usable and after.usable
    assert after.reports["JPY"].net_position - before.reports["JPY"].net_position == 5_000
    assert cot_pit.audit_cot_pit_dataset(dataset).observation_count == len(original) + 1


def test_historical_backfill_and_holiday_release_do_not_backdate_availability(
    tmp_path: Path,
) -> None:
    first = _capture(
        tmp_path / "c1",
        _all_rows(REPORT_1),
        start=datetime(2026, 7, 6, 20, 0, tzinfo=UTC),
    )
    release = _attestation(tmp_path, REPORT_1, RELEASE_1)
    dataset = _artifact(tmp_path, [first], [release])

    friday_plus_three = datetime(2026, 7, 3, 23, 59, tzinfo=UTC)
    after_release_before_ingestion = RELEASE_1 + timedelta(minutes=1)
    after_ingestion = first.validated_at + timedelta(minutes=1)
    assert cot_pit.load_cot_as_of(dataset, friday_plus_three).status == "unavailable"
    assert cot_pit.load_cot_as_of(dataset, after_release_before_ingestion).status == "unavailable"
    loaded = cot_pit.load_cot_as_of(dataset, after_ingestion)
    assert loaded.usable
    assert loaded.reports["JPY"].available_time == first.validated_at


def test_asof_previous_position_uses_only_rows_visible_at_that_time(tmp_path: Path) -> None:
    first = _capture(
        tmp_path / "c1",
        _all_rows(REPORT_1),
        start=datetime(2026, 7, 6, 20, 0, tzinfo=UTC),
    )
    second = _capture(
        tmp_path / "c2",
        _all_rows(REPORT_2, REPORT_1),
        start=datetime(2026, 7, 10, 20, 0, tzinfo=UTC),
    )
    dataset = _artifact(
        tmp_path,
        [first, second],
        [
            _attestation(tmp_path, REPORT_1, RELEASE_1, suffix="-1"),
            _attestation(tmp_path, REPORT_2, RELEASE_2, suffix="-2"),
        ],
    )

    early = cot_pit.load_cot_as_of(dataset, first.validated_at + timedelta(minutes=1))
    late = cot_pit.load_cot_as_of(dataset, second.validated_at + timedelta(minutes=1))
    assert early.usable and late.usable
    assert early.reports["JPY"].report_date == REPORT_1
    assert early.reports["JPY"].prev_net_position is None
    assert late.reports["JPY"].report_date == REPORT_2
    expected_previous = early.reports["JPY"].net_position
    assert late.reports["JPY"].prev_net_position == expected_previous


def test_late_prior_week_correction_updates_availability_and_provenance(tmp_path: Path) -> None:
    report_1_rows = _all_rows(REPORT_1)
    report_2_rows = _all_rows(REPORT_2, REPORT_1)
    corrected_rows = [dict(row) for row in report_2_rows]
    prior_jpy = next(
        row for row in corrected_rows if row["id"] == _source_row("JPY", REPORT_1)["id"]
    )
    prior_jpy["noncomm_positions_long_all"] = str(
        int(str(prior_jpy["noncomm_positions_long_all"])) + 5_000
    )
    first = _capture(
        tmp_path / "prior-original",
        report_1_rows,
        start=datetime(2026, 7, 6, 20, 0, tzinfo=UTC),
    )
    current = _capture(
        tmp_path / "current",
        report_2_rows,
        start=datetime(2026, 7, 10, 20, 0, tzinfo=UTC),
    )
    correction = _capture(
        tmp_path / "prior-correction",
        corrected_rows,
        start=datetime(2026, 7, 11, 0, 0, tzinfo=UTC),
    )
    dataset = _artifact(
        tmp_path,
        [first, current, correction],
        [
            _attestation(tmp_path, REPORT_1, RELEASE_1, suffix="-prior-correction-1"),
            _attestation(tmp_path, REPORT_2, RELEASE_2, suffix="-prior-correction-2"),
        ],
    )
    before = cot_pit.load_cot_as_of(
        dataset,
        current.validated_at + timedelta(minutes=1),
        required_currencies=("JPY",),
    )
    after = cot_pit.load_cot_as_of(
        dataset,
        correction.validated_at + timedelta(minutes=1),
        required_currencies=("JPY",),
    )

    assert before.usable and after.usable
    assert after.reports["JPY"].prev_net_position == before.reports["JPY"].prev_net_position + 5_000
    assert after.reports["JPY"].available_time == correction.validated_at
    assert after.max_available_time == correction.validated_at
    assert after.record_hashes != before.record_hashes
    assert set(after.record_hashes) - set(before.record_hashes)


def test_required_currencies_must_align_to_latest_eligible_report_date(
    tmp_path: Path,
) -> None:
    rows = [
        row
        for row in _all_rows(REPORT_2, REPORT_1)
        if not (
            row["cftc_contract_market_code"] == COT_CONTRACT_CODES["JPY"]
            and str(row["report_date_as_yyyy_mm_dd"]).startswith(REPORT_2.isoformat())
        )
    ]
    capture = _capture(
        tmp_path / "misaligned",
        rows,
        start=datetime(2026, 7, 10, 20, 0, tzinfo=UTC),
    )
    dataset = _artifact(
        tmp_path,
        [capture],
        [
            _attestation(tmp_path, REPORT_1, RELEASE_1, suffix="-misaligned-1"),
            _attestation(tmp_path, REPORT_2, RELEASE_2, suffix="-misaligned-2"),
        ],
    )
    result = cot_pit.load_cot_as_of(
        dataset,
        capture.validated_at + timedelta(minutes=1),
        required_currencies=("JPY", "USD"),
    )

    assert result.status == "incomplete"
    assert result.reports["JPY"].report_date == REPORT_1
    assert result.reports["USD"].report_date == REPORT_2
    assert any("not aligned" in warning for warning in result.warnings)


def test_later_complete_capture_cannot_silently_drop_observed_row_ids(tmp_path: Path) -> None:
    complete = _all_rows(REPORT_1)
    missing_jpy = [
        row for row in complete if row["cftc_contract_market_code"] != COT_CONTRACT_CODES["JPY"]
    ]
    first = _capture(
        tmp_path / "complete-first",
        complete,
        start=datetime(2026, 7, 6, 20, 0, tzinfo=UTC),
    )
    second = _capture(
        tmp_path / "missing-later",
        missing_jpy,
        start=datetime(2026, 7, 7, 20, 0, tzinfo=UTC),
    )

    with pytest.raises(cot_pit.COTPITError, match="disappeared"):
        _artifact(tmp_path, [first, second], [], name="dropped-source-row")


def test_later_attestation_enables_unchanged_rows_without_rewriting_observations(
    tmp_path: Path,
) -> None:
    capture = _capture(
        tmp_path / "capture",
        _all_rows(REPORT_1),
        start=datetime(2026, 7, 6, 20, 0, tzinfo=UTC),
    )
    unbound = _artifact(tmp_path, [capture], [], name="unbound")
    assert cot_pit.load_cot_as_of(unbound, datetime(2026, 7, 7, tzinfo=UTC)).status == "unavailable"

    release = _attestation(tmp_path, REPORT_1, RELEASE_1)
    bound = _artifact(tmp_path, [], [release], previous=unbound, name="bound")
    result = cot_pit.load_cot_as_of(bound, datetime(2026, 7, 7, tzinfo=UTC))
    assert result.usable
    assert (
        cot_pit.audit_cot_pit_dataset(unbound).observation_count
        == cot_pit.audit_cot_pit_dataset(bound).observation_count
    )


def test_extending_with_exact_same_raw_inputs_is_idempotent(tmp_path: Path) -> None:
    capture = _capture(
        tmp_path / "capture-idempotent",
        _all_rows(REPORT_1),
        start=datetime(2026, 7, 6, 20, 0, tzinfo=UTC),
    )
    release = _attestation(tmp_path, REPORT_1, RELEASE_1, suffix="-idempotent")
    first = _artifact(tmp_path, [capture], [release], name="first-idempotent")
    second = _artifact(
        tmp_path,
        [capture],
        [release],
        previous=first,
        name="second-idempotent",
    )
    first_audit = cot_pit.audit_cot_pit_dataset(first)
    second_audit = cot_pit.audit_cot_pit_dataset(second)
    second_manifest = json.loads((second / "manifest.json").read_text(encoding="utf-8"))

    assert first_audit.passed and second_audit.passed
    assert first_audit.observation_count == second_audit.observation_count
    assert first_audit.release_attestation_count == second_audit.release_attestation_count
    assert len(second_manifest["identity"]["raw_inputs"]) == 3


def test_release_attestation_is_versioned_and_basis_rules_fail_closed(tmp_path: Path) -> None:
    report = REPORT_1
    scheduled_release = datetime(2026, 7, 6, 19, 30, tzinfo=UTC)
    scheduled = _attestation(
        tmp_path,
        report,
        scheduled_release,
        basis="scheduled",
        captured_at=datetime(2026, 7, 1, tzinfo=UTC),
        suffix="-scheduled",
    )
    assert scheduled.evidence_captured_at < scheduled.released_at

    with pytest.raises(cot_pit.COTPITError, match="before actual release"):
        _attestation(
            tmp_path,
            report,
            scheduled_release,
            basis="actual_release_notice",
            captured_at=datetime(2026, 7, 1, tzinfo=UTC),
            suffix="-invalid-actual",
        )

    capture = _capture(
        tmp_path / "capture",
        _all_rows(report),
        start=datetime(2026, 7, 6, 19, 35, tzinfo=UTC),
    )
    actual = _attestation(
        tmp_path,
        report,
        datetime(2026, 7, 6, 20, 30, tzinfo=UTC),
        basis="actual_release_notice",
        captured_at=datetime(2026, 7, 6, 20, 31, tzinfo=UTC),
        suffix="-actual",
    )
    dataset = _artifact(tmp_path, [capture], [scheduled, actual])
    before_actual = cot_pit.load_cot_as_of(
        dataset,
        datetime(2026, 7, 6, 20, 0, tzinfo=UTC),
        required_currencies=("JPY",),
    )
    after_actual = cot_pit.load_cot_as_of(
        dataset,
        datetime(2026, 7, 6, 21, 0, tzinfo=UTC),
        required_currencies=("JPY",),
    )
    assert (
        before_actual.usable
        and "release_basis_scheduled" in before_actual.reports["JPY"].data_quality_flags
    )
    assert (
        after_actual.usable
        and "release_basis_actual_release_notice" in after_actual.reports["JPY"].data_quality_flags
    )


def test_known_schedule_delay_supersedes_old_time_before_new_release(tmp_path: Path) -> None:
    original = _attestation(
        tmp_path,
        REPORT_1,
        datetime(2026, 7, 6, 19, 30, tzinfo=UTC),
        basis="scheduled",
        captured_at=datetime(2026, 7, 1, tzinfo=UTC),
        suffix="-original-schedule",
    )
    delayed = _attestation(
        tmp_path,
        REPORT_1,
        datetime(2026, 7, 6, 20, 30, tzinfo=UTC),
        basis="scheduled",
        captured_at=datetime(2026, 7, 6, 18, 0, tzinfo=UTC),
        suffix="-delayed-schedule",
    )
    capture = _capture(
        tmp_path / "capture-delayed",
        _all_rows(REPORT_1),
        start=datetime(2026, 7, 6, 19, 35, tzinfo=UTC),
    )
    artifact = cot_pit.materialize_cot_pit_dataset(
        tmp_path / "delayed-artifact",
        [capture],
        release_attestations=[original, delayed],
        created_at=datetime(2026, 7, 6, 19, 50, tzinfo=UTC),
        code_commit=COMMIT,
        dirty_worktree=True,
    )
    records = load_pit_dataset_records(artifact.directory)
    scheduled_records = [
        record for record in records if record.source == cot_pit.COT_RELEASE_SOURCE
    ]
    assert len(scheduled_records) == 2
    assert all(record.published_time is None for record in scheduled_records)

    before_delay_release = cot_pit.load_cot_as_of(
        artifact.directory,
        datetime(2026, 7, 6, 20, 0, tzinfo=UTC),
        required_currencies=("JPY",),
    )
    after_delay_release = cot_pit.load_cot_as_of(
        artifact.directory,
        datetime(2026, 7, 6, 21, 0, tzinfo=UTC),
        required_currencies=("JPY",),
    )
    assert before_delay_release.status == "unavailable"
    assert after_delay_release.usable
    assert after_delay_release.reports["JPY"].available_time == datetime(
        2026, 7, 6, 20, 30, tzinfo=UTC
    )


def test_domain_audit_replays_raw_and_rejects_generic_valid_omission(tmp_path: Path) -> None:
    capture = _capture(
        tmp_path / "capture",
        _all_rows(REPORT_1),
        start=datetime(2026, 7, 6, 20, 0, tzinfo=UTC),
    )
    release = _attestation(tmp_path, REPORT_1, RELEASE_1)
    valid = _artifact(tmp_path, [capture], [release], name="valid")
    manifest = json.loads((valid / "manifest.json").read_text(encoding="utf-8"))
    identity = manifest["identity"]
    records = list(load_pit_dataset_records(valid))
    omitted = next(record for record in records if record.source == cot_pit.COT_DATA_SOURCE)
    records.remove(omitted)
    lineage = [
        SourceLineage(
            source=row["source"],
            upstream_uri=row["upstream_uri"],
            source_version=row["source_version"],
            contract_status=row["contract_status"],
            license_status=row["license_status"],
            limitations=tuple(row["limitations"]),
        )
        for row in identity["source_lineage"]
    ]
    raw_inputs = [
        RawInput(
            row["source"],
            row["role"],
            valid / row["stored_path"],
            datetime.fromisoformat(row["acquired_at"]),
        )
        for row in identity["raw_inputs"]
    ]
    fabricated = materialize_pit_dataset(
        tmp_path / "generic-valid",
        records,
        source_lineage=lineage,
        raw_inputs=raw_inputs,
        transform_name=cot_pit.COT_TRANSFORM_NAME,
        transform_version=cot_pit.COT_TRANSFORM_VERSION,
        dataset_class="research_only",
        description=cot_pit.COT_DATASET_DESCRIPTION,
        created_at=CREATED_AT,
        code_commit="c" * 40,
        dirty_worktree=True,
    )
    assert audit_pit_dataset(fabricated.directory).passed
    domain = cot_pit.audit_cot_pit_dataset(fabricated.directory)
    assert not domain.passed
    assert "exactly replay" in " ".join(domain.errors)


def test_capture_replay_binds_exact_request_uri_and_timeline(tmp_path: Path) -> None:
    capture = _capture(
        tmp_path / "capture",
        _all_rows(REPORT_1),
        start=datetime(2026, 7, 6, 20, 0, tzinfo=UTC),
    )
    payload = json.loads(capture.path.read_text(encoding="utf-8"))
    payload["responses"][1]["request_uri"] = "https://publicreporting.cftc.gov/other"
    altered_uri = tmp_path / "altered-uri.json"
    altered_uri.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(cot_pit.COTPITError, match="request URI"):
        cot_pit.COTCapture(altered_uri)

    payload = json.loads(capture.path.read_text(encoding="utf-8"))
    payload["responses"][1]["request_started_at"] = payload["responses"][0]["request_started_at"]
    overlapping = tmp_path / "overlapping.json"
    overlapping.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(cot_pit.COTPITError, match="overlaps"):
        cot_pit.COTCapture(overlapping)


@pytest.mark.parametrize(
    "uri",
    (
        "http://www.cftc.gov/release",
        "https://evil-cftc.gov/release",
        "https://www.cftc.gov.evil.example/release",
        "https://user@www.cftc.gov/release",
        "https://www.cftc.gov:443/release",
        "https://www.cftc.gov/release#fragment",
    ),
)
def test_release_attestation_requires_official_cftc_uri(tmp_path: Path, uri: str) -> None:
    evidence = tmp_path / "evidence.html"
    evidence.write_text("official release evidence", encoding="utf-8")
    with pytest.raises(cot_pit.COTPITError, match="official HTTPS CFTC URI"):
        cot_pit.write_cot_release_attestation(
            tmp_path / "attestation.json",
            evidence,
            report_date=REPORT_1,
            basis="scheduled",
            released_at=RELEASE_1,
            evidence_uri=uri,
            evidence_captured_at=datetime(2026, 7, 1, tzinfo=UTC),
            run_id="release-uri-test",
            writer_id="test-reviewer",
        )


def test_release_time_and_sidecar_report_date_fail_closed(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.html"
    evidence.write_text("official release evidence", encoding="utf-8")
    with pytest.raises(cot_pit.COTPITError, match="precede the COT report date"):
        cot_pit.write_cot_release_attestation(
            tmp_path / "too-early.json",
            evidence,
            report_date=REPORT_1,
            basis="scheduled",
            released_at=datetime(2026, 6, 29, 19, 30, tzinfo=UTC),
            evidence_uri=cot_pit.COT_RELEASE_SCHEDULE_URI,
            evidence_captured_at=datetime(2026, 6, 28, tzinfo=UTC),
            run_id="release-too-early",
            writer_id="test-reviewer",
        )

    valid = _attestation(tmp_path, REPORT_1, RELEASE_1, suffix="-strict-date")
    payload = json.loads(valid.attestation_path.read_text(encoding="utf-8"))
    payload["report_date"] = f"{REPORT_1.isoformat()}garbage"
    malformed = tmp_path / "malformed-date.json"
    malformed.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(cot_pit.COTPITError, match="valid ISO date"):
        cot_pit.COTReleaseAttestation(malformed, valid.evidence_path)

    payload = json.loads(valid.attestation_path.read_text(encoding="utf-8"))
    payload["parser_version"] = "unreviewed-parser"
    wrong_parser = tmp_path / "wrong-parser.json"
    wrong_parser.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(cot_pit.COTPITError, match="parser version"):
        cot_pit.COTReleaseAttestation(wrong_parser, valid.evidence_path)


def test_domain_audit_rejects_generic_valid_lineage_overclaim(tmp_path: Path) -> None:
    capture = _capture(
        tmp_path / "capture",
        _all_rows(REPORT_1),
        start=datetime(2026, 7, 6, 20, 0, tzinfo=UTC),
    )
    valid = _artifact(tmp_path, [capture], [], name="valid-lineage")
    manifest = json.loads((valid / "manifest.json").read_text(encoding="utf-8"))
    identity = manifest["identity"]
    records = load_pit_dataset_records(valid)
    raw_inputs = [
        RawInput(
            row["source"],
            row["role"],
            valid / row["stored_path"],
            datetime.fromisoformat(row["acquired_at"]),
        )
        for row in identity["raw_inputs"]
    ]
    overclaimed = SourceLineage(
        source=cot_pit.COT_DATA_SOURCE,
        upstream_uri="https://example.invalid/not-cftc",
        source_version=cot_pit.COT_DATA_SOURCE_VERSION,
        contract_status="verified",
        license_status="verified",
        limitations=(),
    )
    fabricated = materialize_pit_dataset(
        tmp_path / "generic-lineage",
        records,
        source_lineage=[overclaimed],
        raw_inputs=raw_inputs,
        transform_name=cot_pit.COT_TRANSFORM_NAME,
        transform_version=cot_pit.COT_TRANSFORM_VERSION,
        dataset_class="research_only",
        description=cot_pit.COT_DATASET_DESCRIPTION,
        created_at=CREATED_AT,
        code_commit="c" * 40,
        dirty_worktree=True,
    )
    assert audit_pit_dataset(fabricated.directory).passed
    domain = cot_pit.audit_cot_pit_dataset(fabricated.directory)
    assert not domain.passed
    assert "source lineage" in " ".join(domain.errors)


def test_domain_audit_and_loader_reject_tampered_artifact(tmp_path: Path) -> None:
    capture = _capture(
        tmp_path / "capture",
        _all_rows(REPORT_1),
        start=datetime(2026, 7, 6, 20, 0, tzinfo=UTC),
    )
    dataset = _artifact(tmp_path, [capture], [_attestation(tmp_path, REPORT_1, RELEASE_1)])
    raw_file = next((dataset / "raw").iterdir())
    raw_file.write_bytes(raw_file.read_bytes() + b"tamper")

    assert not cot_pit.audit_cot_pit_dataset(dataset).passed
    result = cot_pit.load_cot_as_of(dataset, datetime(2026, 7, 7, tzinfo=UTC))
    assert result.status == "invalid"
    assert not result.reports


def test_capture_sidecar_and_evidence_symlinks_are_rejected(tmp_path: Path) -> None:
    capture = _capture(
        tmp_path / "capture-real",
        _all_rows(REPORT_1),
        start=datetime(2026, 7, 6, 20, 0, tzinfo=UTC),
    )
    capture_link = tmp_path / "capture-link.json"
    capture_link.symlink_to(capture.path)
    with pytest.raises(cot_pit.COTPITError, match="regular file"):
        cot_pit.COTCapture(capture_link)

    release = _attestation(tmp_path, REPORT_1, RELEASE_1, suffix="-symlink")
    sidecar_link = tmp_path / "sidecar-link.json"
    sidecar_link.symlink_to(release.attestation_path)
    with pytest.raises(cot_pit.COTPITError, match="regular file"):
        cot_pit.COTReleaseAttestation(sidecar_link, release.evidence_path)

    evidence_link = tmp_path / "evidence-link.html"
    evidence_link.symlink_to(release.evidence_path)
    with pytest.raises(cot_pit.COTPITError, match="regular file"):
        cot_pit.write_cot_release_attestation(
            tmp_path / "linked-evidence-sidecar.json",
            evidence_link,
            report_date=REPORT_1,
            basis="scheduled",
            released_at=RELEASE_1,
            evidence_uri=cot_pit.COT_RELEASE_SCHEDULE_URI,
            evidence_captured_at=datetime(2026, 7, 1, tzinfo=UTC),
            run_id="release-symlink",
            writer_id="test-reviewer",
        )


def test_briefing_uses_positive_typed_evidence_and_never_legacy_fallback(tmp_path: Path) -> None:
    now = datetime(2026, 7, 7, tzinfo=UTC)
    disabled = MacroSnapshot(fetched_at=now)
    fx_briefing._attach_cot_pit_dataset(disabled, None, prediction_time=now)
    assert disabled.cot == {}
    assert disabled.cot_evidence == {
        "status": "disabled",
        "prediction_time": now.isoformat(),
        "usable": False,
    }

    capture = _capture(
        tmp_path / "capture",
        _all_rows(REPORT_1),
        start=datetime(2026, 7, 6, 20, 0, tzinfo=UTC),
    )
    dataset = _artifact(tmp_path, [capture], [_attestation(tmp_path, REPORT_1, RELEASE_1)])
    enabled = MacroSnapshot(fetched_at=now)
    fx_briefing._attach_cot_pit_dataset(enabled, dataset, prediction_time=now)
    assert enabled.cot_evidence is not None and enabled.cot_evidence["status"] == "ok"
    assert enabled.fresh_cot("JPY") is not None

    invalid = MacroSnapshot(fetched_at=now)
    fx_briefing._attach_cot_pit_dataset(invalid, tmp_path / "missing", prediction_time=now)
    assert invalid.cot == {}
    assert invalid.cot_evidence is not None and invalid.cot_evidence["status"] == "invalid"


def test_naive_prediction_time_and_semantic_identity_changes_are_rejected(tmp_path: Path) -> None:
    capture = _capture(
        tmp_path / "capture",
        _all_rows(REPORT_1),
        start=datetime(2026, 7, 6, 20, 0, tzinfo=UTC),
    )
    dataset = _artifact(tmp_path, [capture], [_attestation(tmp_path, REPORT_1, RELEASE_1)])
    with pytest.raises(cot_pit.COTPITError, match="timezone-aware"):
        cot_pit.load_cot_as_of(dataset, datetime(2026, 7, 7))
    with pytest.raises(cot_pit.COTPITError, match="cannot be empty"):
        cot_pit.load_cot_as_of(dataset, datetime(2026, 7, 7, tzinfo=UTC), required_currencies=())
    with pytest.raises(cot_pit.COTPITError, match="unknown required"):
        cot_pit.load_cot_as_of(
            dataset,
            datetime(2026, 7, 7, tzinfo=UTC),
            required_currencies=("XYZ",),
        )

    changed = _all_rows(REPORT_1)
    jpy = next(
        row for row in changed if row["cftc_contract_market_code"] == COT_CONTRACT_CODES["JPY"]
    )
    jpy["report_date_as_yyyy_mm_dd"] = f"{REPORT_2.isoformat()}T00:00:00.000"
    changed.sort(
        key=lambda row: (
            -date.fromisoformat(str(row["report_date_as_yyyy_mm_dd"])[:10]).toordinal(),
            str(row["id"]),
        )
    )
    second = _capture(
        tmp_path / "changed",
        changed,
        start=datetime(2026, 7, 10, 20, 0, tzinfo=UTC),
    )
    with pytest.raises(cot_pit.COTPITError, match="changed identity"):
        _artifact(tmp_path, [capture, second], [], name="identity-change")


def test_artifact_remains_research_only_and_not_systemwide_pit_claim(tmp_path: Path) -> None:
    capture = _capture(
        tmp_path / "capture",
        _all_rows(REPORT_1),
        start=datetime(2026, 7, 6, 20, 0, tzinfo=UTC),
    )
    dataset = _artifact(tmp_path, [capture], [_attestation(tmp_path, REPORT_1, RELEASE_1)])
    manifest = json.loads((dataset / "manifest.json").read_text(encoding="utf-8"))
    domain = cot_pit.audit_cot_pit_dataset(dataset)

    assert manifest["promotion_eligible"] is False
    assert manifest["domain_qa"]["as_of_join_status"] == "not_evaluated"
    assert domain.passed
    assert any("does not attest FRED" in warning for warning in domain.warnings)

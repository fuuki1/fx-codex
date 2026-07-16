from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import re
from types import SimpleNamespace

import pytest

from fx_intel import cot_pit
from tools import cot_pit_pipeline


class _Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        result = self.value
        self.value += timedelta(seconds=1)
        return result


class _Response:
    def __init__(self, payload: object) -> None:
        self.status_code = 200
        self.content = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.headers = {"Content-Type": "application/json", "ETag": '"fixture"'}


class _Session:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def get(self, url: str, **_kwargs: object) -> _Response:
        if "$select=count" in url:
            return _Response([{"row_count": str(len(self.rows))}])
        offset = int(re.search(r"\$offset=(\d+)", url).group(1))  # type: ignore[union-attr]
        limit = int(re.search(r"\$limit=(\d+)", url).group(1))  # type: ignore[union-attr]
        return _Response(self.rows[offset : offset + limit])


def _row() -> dict[str, object]:
    return {
        "id": "260707097741F",
        "cftc_contract_market_code": "097741",
        "market_and_exchange_names": "JAPANESE YEN TEST FUTURE",
        "report_date_as_yyyy_mm_dd": "2026-07-07T00:00:00.000",
        "futonly_or_combined": "FutOnly",
        "noncomm_positions_long_all": "12000",
        "noncomm_positions_short_all": "7000",
        "open_interest_all": "100000",
    }


def _json_stdout(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.count("\n") == 1
    payload = json.loads(captured.out)
    assert isinstance(payload, dict)
    return payload


def test_parser_rejects_missing_command_naive_time_and_unknown_currency() -> None:
    with pytest.raises(SystemExit, match="2"):
        cot_pit_pipeline.main([])
    with pytest.raises(SystemExit, match="2"):
        cot_pit_pipeline.main(["as-of", "dataset", "--prediction-time", "2026-07-11T00:00:00"])
    with pytest.raises(SystemExit, match="2"):
        cot_pit_pipeline.main(
            [
                "as-of",
                "dataset",
                "--prediction-time",
                "2026-07-11T00:00:00Z",
                "--required-currencies",
                "XYZ",
            ]
        )


def test_capture_outputs_one_json_document_and_errors_are_structured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured_args: dict[str, object] = {}

    def fake_capture(root: Path, **kwargs: object) -> SimpleNamespace:
        captured_args.update({"root": root, **kwargs})
        return SimpleNamespace(
            path=(tmp_path / "capture.json").resolve(),
            capture_id="a" * 64,
            acquired_at=datetime(2026, 7, 10, 20, 0, tzinfo=UTC),
            validated_at=datetime(2026, 7, 10, 20, 1, tzinfo=UTC),
            run_id="run-explicit",
            writer_id="writer-explicit",
        )

    monkeypatch.setattr(cot_pit_pipeline.cot_pit, "fetch_cot_capture", fake_capture)
    exit_code = cot_pit_pipeline.main(
        [
            "capture",
            "--capture-root",
            str(tmp_path / "captures"),
            "--page-size",
            "17",
            "--run-id",
            "run-explicit",
            "--writer-id",
            "writer-explicit",
        ]
    )
    payload = _json_stdout(capsys)
    assert exit_code == 0
    assert payload["capture_id"] == "a" * 64
    assert payload["promotion_eligible"] is False
    assert captured_args["page_size"] == 17

    def fail_capture(*_args: object, **_kwargs: object) -> None:
        raise cot_pit.COTPITError("upstream response quarantined")

    monkeypatch.setattr(cot_pit_pipeline.cot_pit, "fetch_cot_capture", fail_capture)
    assert cot_pit_pipeline.main(["capture", "--capture-root", str(tmp_path / "failed")]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)
    assert error["status"] == "error"
    assert "quarantined" in error["error"]
    assert "Traceback" not in captured.err


def test_attest_uses_invocation_time_and_never_overwrites(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    now = datetime(2026, 7, 10, 20, 5, tzinfo=UTC)
    monkeypatch.setattr(cot_pit_pipeline, "_now_utc", lambda: now)
    evidence = tmp_path / "release.html"
    evidence.write_bytes(b"official CFTC release evidence\n")
    output = tmp_path / "release.json"
    arguments = [
        "attest",
        "--output",
        str(output),
        "--evidence",
        str(evidence),
        "--report-date",
        "2026-07-07",
        "--basis",
        "actual_release_notice",
        "--released-at",
        "2026-07-10T19:30:00Z",
        "--evidence-uri",
        cot_pit.COT_RELEASE_SCHEDULE_URI,
    ]
    assert cot_pit_pipeline.main(arguments) == 0
    payload = _json_stdout(capsys)
    assert payload["evidence_captured_at"] == now.isoformat()
    assert cot_pit.COTReleaseAttestation(output, evidence).evidence_captured_at == now
    original = output.read_bytes()

    assert cot_pit_pipeline.main(arguments) == 0  # exact content is idempotent
    _json_stdout(capsys)
    assert output.read_bytes() == original

    assert (
        cot_pit_pipeline.main(
            [
                "attest",
                "--output",
                str(evidence),
                "--evidence",
                str(evidence),
                "--report-date",
                "2026-07-07",
                "--basis",
                "scheduled",
                "--released-at",
                "2026-07-10T19:30:00Z",
                "--evidence-uri",
                cot_pit.COT_RELEASE_SCHEDULE_URI,
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "different files" in json.loads(captured.err)["error"]


def test_materialize_audit_and_asof_cli_end_to_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    capture = cot_pit.fetch_cot_capture(
        tmp_path / "captures",
        session=_Session([_row()]),  # type: ignore[arg-type]
        clock=_Clock(datetime(2026, 7, 10, 20, 0, tzinfo=UTC)),
        run_id="capture-run",
        writer_id="capture-writer",
        page_size=2,
    )
    evidence = tmp_path / "release.html"
    evidence.write_text("official CFTC release evidence", encoding="utf-8")
    attestation = cot_pit.write_cot_release_attestation(
        tmp_path / "release.json",
        evidence,
        report_date=datetime(2026, 7, 7, tzinfo=UTC).date(),
        basis="actual_release_notice",
        released_at=datetime(2026, 7, 10, 19, 30, tzinfo=UTC),
        evidence_uri=cot_pit.COT_RELEASE_SCHEDULE_URI,
        evidence_captured_at=datetime(2026, 7, 10, 20, 5, tzinfo=UTC),
        run_id="release-run",
        writer_id="release-writer",
    )
    monkeypatch.setattr(cot_pit_pipeline, "_git_provenance", lambda _root: ("b" * 40, True))
    monkeypatch.setattr(
        cot_pit_pipeline,
        "_now_utc",
        lambda: datetime(2026, 7, 11, 1, 0, tzinfo=UTC),
    )
    root = tmp_path / "artifacts"
    assert (
        cot_pit_pipeline.main(
            [
                "materialize",
                "--root",
                str(root),
                "--capture",
                str(capture.path),
                "--release",
                str(attestation.attestation_path),
                str(attestation.evidence_path),
            ]
        )
        == 0
    )
    materialized = _json_stdout(capsys)
    dataset = Path(str(materialized["dataset_dir"]))
    manifest = json.loads((dataset / "manifest.json").read_text(encoding="utf-8"))
    assert materialized["promotion_eligible"] is False
    assert manifest["identity"]["code"] == {
        "commit": "b" * 40,
        "dirty_worktree": True,
    }

    assert cot_pit_pipeline.main(["audit", str(dataset)]) == 0
    audited = _json_stdout(capsys)
    assert audited["passed"] is True
    assert audited["observation_count"] == 1

    assert (
        cot_pit_pipeline.main(
            [
                "as-of",
                str(dataset),
                "--prediction-time",
                "2026-07-11T00:00:00Z",
                "--required-currencies",
                "JPY",
            ]
        )
        == 0
    )
    loaded = _json_stdout(capsys)
    assert loaded["status"] == "ok"
    assert loaded["reports"]["JPY"]["source_record_id"] == "260707097741F"  # type: ignore[index]
    assert len(loaded["record_hashes"]) == 2  # type: ignore[arg-type]

    assert (
        cot_pit_pipeline.main(
            [
                "as-of",
                str(dataset),
                "--prediction-time",
                "2026-07-10T19:31:00Z",
                "--required-currencies",
                "JPY",
            ]
        )
        == 1
    )
    unavailable = _json_stdout(capsys)
    assert unavailable["status"] == "unavailable"
    assert unavailable["usable"] is False

    raw = next((dataset / "raw").iterdir())
    raw.write_bytes(raw.read_bytes() + b"tamper")
    assert cot_pit_pipeline.main(["audit", str(dataset)]) == 1
    failed = _json_stdout(capsys)
    assert failed["passed"] is False
    assert raw.read_bytes().endswith(b"tamper")


def test_materialize_fails_closed_when_git_provenance_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def unavailable(_root: Path) -> tuple[str, bool]:
        raise cot_pit.COTPITError("Git provenance unavailable")

    monkeypatch.setattr(cot_pit_pipeline, "_git_provenance", unavailable)
    output = tmp_path / "artifacts"
    assert cot_pit_pipeline.main(["materialize", "--root", str(output)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Git provenance unavailable" in json.loads(captured.err)["error"]
    assert not output.exists()

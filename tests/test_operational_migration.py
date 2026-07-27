from __future__ import annotations

from datetime import datetime, timedelta, UTC
import hashlib
import json
from pathlib import Path

import pytest

from fx_intel import decision_commit, decision_log, journal
from fx_intel import operational_contracts as contracts
from fx_intel import operational_migration as migration
from fx_intel import operational_store as store

NOW = datetime(2026, 7, 27, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def safe_sqlite_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store.sqlite3, "sqlite_version_info", (3, 53, 3))
    monkeypatch.setattr(store.sqlite3, "sqlite_version", "3.53.3")


def _content_hash(row: dict[str, object]) -> str:
    payload = {key: value for key, value in row.items() if key != "content_hash"}
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def _decision(identifier: str, *, pit: bool) -> dict[str, object]:
    row: dict[str, object] = {
        "schema": 3,
        "event_type": "chart_decision",
        "decision_id": identifier,
        "ts": NOW.isoformat(),
        "source": "fx_briefing",
        "mode": "per_timeframe",
        "symbol": "USDJPY",
        "timeframe": "15m",
        "horizon_hours": 0.25,
        "decision": {"direction": "neutral"},
    }
    if pit:
        row.update(
            {
                "prediction_time": NOW.isoformat(),
                "source_cutoff": (NOW - timedelta(seconds=2)).isoformat(),
                "max_feature_available_time": (NOW - timedelta(seconds=1)).isoformat(),
                "pit_eligible": True,
                "pit_contract": contracts.DECISION_JOURNAL_PIT_CONTRACT,
                "producer": contracts.TIMEFRAME_PRODUCER,
                "producer_version": contracts.TIMEFRAME_PRODUCER_VERSION,
                "input_context_id": f"context-{identifier}",
                "source_record_ids": [f"source-{identifier}"],
            }
        )
    return row


def _price(identifier: str, *, event_time: bool = True) -> dict[str, object]:
    row: dict[str, object] = {
        "schema_version": 3,
        "ts": NOW.isoformat(),
        "available_time": NOW.isoformat(),
        "ingested_time": NOW.isoformat(),
        "source": "oanda",
        "source_record_id": identifier,
        "symbol": "USDJPY",
        "timeframe": "15m",
        "ohlc_scope": "completed_bar",
        "close": 150.0,
    }
    if event_time:
        row["event_time"] = NOW.isoformat()
    row["content_hash"] = _content_hash(row)
    return row


def _write_jsonl(path: Path, rows: list[object]) -> str:
    path.write_text(
        "".join(f"{json.dumps(row, ensure_ascii=False)}\n" for row in rows),
        encoding="utf-8",
    )
    return store.file_sha256(path)


def _write_decision_jsonl(path: Path, rows: list[dict[str, object]]) -> str:
    path.write_text("", encoding="utf-8")
    compact_path = path.parent / "briefing_tf_journal.jsonl"
    compact_path.write_text("", encoding="utf-8")
    legacy = [row for row in rows if row.get("pit_eligible") is not True]
    if legacy:
        with path.open("a", encoding="utf-8") as handle:
            for row in legacy:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    current = [row for row in rows if row.get("pit_eligible") is True]
    if current:
        decision_ids = [str(row["decision_id"]) for row in current]
        transaction_id = decision_commit.transaction_id_for(decision_ids)
        full_batch = decision_log.append_decision_events(
            path,
            current,
            transaction_id=transaction_id,
        )
        compact_batch = journal._append_journal_batch(  # noqa: SLF001
            compact_path,
            current,
            decision_transaction_id=transaction_id,
        )
        decision_commit.append_commit(
            path,
            decision_ids=decision_ids,
            full_batch_sha256=str(full_batch["batch_sha256"]),
            full_batch_line_start_offset=int(full_batch["line_start_offset"]),
            full_batch_line_sha256=str(full_batch["line_sha256"]),
            compact_batch_sha256=str(compact_batch["batch_sha256"]),
            mode="per_timeframe",
            committed_at=NOW,
        )
    return store.file_sha256(path)


def test_candidate_keeps_legacy_audit_but_only_queues_pit_prediction(tmp_path: Path) -> None:
    decisions = tmp_path / "decisions.jsonl"
    prices = tmp_path / "prices.jsonl"
    decision_hash = _write_decision_jsonl(
        decisions,
        [_decision("legacy", pit=False), _decision("current", pit=True)],
    )
    price_hash = _write_jsonl(prices, [_price("price-1")])

    with store.open_operational_writer(
        tmp_path / "candidate.sqlite3",
        writer_id="migration-test",
    ) as opened:
        report = migration.migrate_jsonl_candidate(
            opened,
            decision_path=decisions,
            decision_sha256=decision_hash,
            price_path=prices,
            price_sha256=price_hash,
            run_id="migration-1",
            now=NOW,
        )

    assert report.decisions.audit_events_inserted == 2
    assert report.decisions.pit_eligible == 1
    assert report.decisions.pit_ineligible == 1
    assert report.database.audit_events == 2
    assert report.database.predictions == 1
    assert report.database.price_points == 1
    assert report.verdict == "parity_with_exclusions"
    assert report.decisions.exclusion_reasons == {"pit_flag_not_true": 1}
    assert [Path(source.path).name for source in report.decision_support_sources] == [
        "briefing_tf_journal.jsonl"
    ]


def test_no_pit_history_is_explicitly_research_only(tmp_path: Path) -> None:
    decisions = tmp_path / "decisions.jsonl"
    prices = tmp_path / "prices.jsonl"
    decision_hash = _write_decision_jsonl(decisions, [_decision("legacy", pit=False)])
    price_hash = _write_jsonl(prices, [_price("price-1")])

    with store.open_operational_writer(
        tmp_path / "candidate.sqlite3",
        writer_id="migration-test",
    ) as opened:
        report = migration.migrate_jsonl_candidate(
            opened,
            decision_path=decisions,
            decision_sha256=decision_hash,
            price_path=prices,
            price_sha256=price_hash,
            run_id="migration-1",
            now=NOW,
        )

    assert report.verdict == "research_only"
    assert "no_pit_eligible_predictions" in report.limitations


def test_missing_market_event_time_is_excluded_without_coercion(tmp_path: Path) -> None:
    decisions = tmp_path / "decisions.jsonl"
    prices = tmp_path / "prices.jsonl"
    decision_hash = _write_decision_jsonl(decisions, [_decision("current", pit=True)])
    price_hash = _write_jsonl(prices, [_price("legacy-price", event_time=False)])

    with store.open_operational_writer(
        tmp_path / "candidate.sqlite3",
        writer_id="migration-test",
    ) as opened:
        report = migration.migrate_jsonl_candidate(
            opened,
            decision_path=decisions,
            decision_sha256=decision_hash,
            price_path=prices,
            price_sha256=price_hash,
            run_id="migration-1",
            now=NOW,
        )

    assert report.database.price_points == 0
    assert report.prices.excluded == 1
    assert report.prices.exclusion_reasons == {"missing_event_time": 1}
    assert report.verdict == "parity_with_exclusions"


def test_source_hash_mismatch_fails_before_database_rows(tmp_path: Path) -> None:
    decisions = tmp_path / "decisions.jsonl"
    prices = tmp_path / "prices.jsonl"
    _write_decision_jsonl(decisions, [_decision("current", pit=True)])
    price_hash = _write_jsonl(prices, [_price("price-1")])

    with store.open_operational_writer(
        tmp_path / "candidate.sqlite3",
        writer_id="migration-test",
    ) as opened:
        with pytest.raises(migration.SourceFingerprintMismatch):
            migration.migrate_jsonl_candidate(
                opened,
                decision_path=decisions,
                decision_sha256="0" * 64,
                price_path=prices,
                price_sha256=price_hash,
                run_id="migration-1",
                now=NOW,
            )
        assert opened.audit().audit_events == 0


def test_content_hash_mismatch_is_reported_and_excluded(tmp_path: Path) -> None:
    decisions = tmp_path / "decisions.jsonl"
    prices = tmp_path / "prices.jsonl"
    decision_hash = _write_decision_jsonl(decisions, [_decision("current", pit=True)])
    tampered = _price("price-1")
    tampered["close"] = 151.0
    price_hash = _write_jsonl(prices, [tampered])

    with store.open_operational_writer(
        tmp_path / "candidate.sqlite3",
        writer_id="migration-test",
    ) as opened:
        report = migration.migrate_jsonl_candidate(
            opened,
            decision_path=decisions,
            decision_sha256=decision_hash,
            price_path=prices,
            price_sha256=price_hash,
            run_id="migration-1",
            now=NOW,
        )

    assert report.prices.exclusion_reasons == {"content_hash_mismatch": 1}
    assert report.database.price_points == 0


def test_market_open_horizon_skips_weekend() -> None:
    friday = datetime(2026, 7, 24, 20, 0, tzinfo=UTC)
    due = migration.add_market_open_hours(friday, 4.0)

    assert contracts.open_hours_between(friday, due) == pytest.approx(4.0)
    assert due > friday + timedelta(days=2)


def test_exact_candidate_payload_parity_is_verified(tmp_path: Path) -> None:
    decisions = tmp_path / "decisions.jsonl"
    prices = tmp_path / "prices.jsonl"
    decision_hash = _write_decision_jsonl(
        decisions,
        [_decision("legacy", pit=False), _decision("current", pit=True)],
    )
    price_hash = _write_jsonl(
        prices,
        [_price("price-1"), _price("legacy-price", event_time=False)],
    )
    database = tmp_path / "candidate.sqlite3"
    with store.open_operational_writer(database, writer_id="migration-test") as opened:
        migration.migrate_jsonl_candidate(
            opened,
            decision_path=decisions,
            decision_sha256=decision_hash,
            price_path=prices,
            price_sha256=price_hash,
            run_id="migration-1",
            now=NOW,
        )
    with store.open_operational_reader(database) as opened:
        parity = migration.verify_candidate_parity(
            opened,
            decision_path=decisions,
            decision_sha256=decision_hash,
            price_path=prices,
            price_sha256=price_hash,
        )

    assert parity.parity_verdict == "parity_verified"
    assert parity.data_usability == "usable_with_exclusions"
    assert parity.audit_payload_mismatches == 0
    assert parity.prediction_payload_mismatches == 0
    assert parity.price_payload_mismatches == 0
    assert parity.exclusion_reasons == {
        "decision:pit_flag_not_true": 1,
        "price:missing_event_time": 1,
    }


def test_parity_detects_source_payload_changed_after_migration(tmp_path: Path) -> None:
    decisions = tmp_path / "decisions.jsonl"
    prices = tmp_path / "prices.jsonl"
    original = _decision("current", pit=True)
    decision_hash = _write_decision_jsonl(decisions, [original])
    price_hash = _write_jsonl(prices, [_price("price-1")])
    database = tmp_path / "candidate.sqlite3"
    with store.open_operational_writer(database, writer_id="migration-test") as opened:
        migration.migrate_jsonl_candidate(
            opened,
            decision_path=decisions,
            decision_sha256=decision_hash,
            price_path=prices,
            price_sha256=price_hash,
            run_id="migration-1",
            now=NOW,
        )

    changed = {**original, "decision": {"direction": "long"}}
    changed_hash = _write_decision_jsonl(decisions, [changed])
    with store.open_operational_reader(database) as opened:
        parity = migration.verify_candidate_parity(
            opened,
            decision_path=decisions,
            decision_sha256=changed_hash,
            price_path=prices,
            price_sha256=price_hash,
        )

    assert parity.parity_verdict == "parity_failed"
    assert parity.audit_payload_mismatches == 1
    assert parity.prediction_payload_mismatches == 1

"""本番書き込みを1関数へ統合した `append_decision_transaction` の契約テスト。

ネットワーク不要。実機の書き込み経路(full log / compact journal / commit)が
1トランザクションとして成立すること、および途中で落ちた場合に部分状態が
可視化されないことを固定する。
"""

from __future__ import annotations

from datetime import datetime, UTC
from pathlib import Path

import pytest

from fx_intel import decision_commit, decision_log, journal

NOW = datetime(2026, 7, 27, 7, 0, tzinfo=UTC)

# ⚠️ 他のテストモジュールから import しない。clean clone では
# `tests` が package として解決されず ModuleNotFoundError になる
# (実際に踏んだ)。fixture はこのファイル内で完結させる。


def _pit_envelope(decision_id: str) -> dict[str, object]:
    return {
        "ts": NOW.isoformat(),
        "prediction_time": NOW.isoformat(),
        "source_cutoff": NOW.isoformat(),
        "max_feature_available_time": NOW.isoformat(),
        "pit_eligible": True,
        "pit_contract": journal.DECISION_JOURNAL_PIT_CONTRACT,
        "decision_id": decision_id,
        "mode": "per_timeframe",
        "producer": journal.TIMEFRAME_PRODUCER,
        "producer_version": journal.TIMEFRAME_PRODUCER_VERSION,
        "input_context_id": f"context:{decision_id}",
        "source_record_ids": [f"source:{decision_id}"],
    }


def _full_event(decision_id: str) -> dict[str, object]:
    return {
        "schema": 1,
        "event_type": decision_log.EVENT_TYPE,
        "symbol": "USDJPY",
        "timeframe": "1h",
        "horizon_hours": 1.0,
        "source": "fx_briefing",
        "decision": {"direction": "long", "close": 150.0},
        **_pit_envelope(decision_id),
    }


def _compact_row(decision_id: str) -> dict[str, object]:
    return {
        "symbol": "USDJPY",
        "timeframe": "1h",
        "horizon_hours": 1.0,
        "direction": "long",
        "close": 150.0,
        **_pit_envelope(decision_id),
    }


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    return (
        tmp_path / decision_commit.DEFAULT_COMMIT_FILENAME,
        tmp_path / "briefing_tf_journal.jsonl",
    )


def test_one_call_publishes_full_and_compact_together(tmp_path) -> None:
    """1回の呼び出しで full/compact 両方が可視になる。"""
    full_path, compact_path = _paths(tmp_path)
    decision_id = "unified-1"

    result = decision_log.append_decision_transaction(
        [_full_event(decision_id)],
        [_compact_row(decision_id)],
        mode="per_timeframe",
        full_path=full_path,
        compact_path=compact_path,
    )

    assert result["decision_ids"] == [decision_id]
    assert [row["decision_id"] for row in decision_log.read_decision_events(full_path)] == [
        decision_id
    ]
    assert [row["decision_id"] for row in journal.read_entries(compact_path)] == [decision_id]


def test_partial_write_stays_invisible_without_the_commit(tmp_path) -> None:
    """commit が無い限り、full も compact も見えない(fail-closed)。

    統合関数を使わずに個別へ書いた状態 = 途中でクラッシュした状態を再現する。
    """
    full_path, compact_path = _paths(tmp_path)
    decision_id = "crash-1"
    transaction_id = decision_commit.transaction_id_for([decision_id])

    decision_log.append_decision_events(
        full_path,
        [_full_event(decision_id)],
        transaction_id=transaction_id,
    )
    assert list(decision_log.read_decision_events(full_path)) == []

    journal._append_journal_batch(  # noqa: SLF001
        compact_path,
        [_compact_row(decision_id)],
        decision_transaction_id=transaction_id,
    )
    assert list(journal.read_entries(compact_path)) == []
    assert list(decision_log.read_decision_events(full_path)) == []


def test_full_and_compact_must_describe_the_same_decisions(tmp_path) -> None:
    """full と compact の decision_id が食い違う入力は受け付けない。

    これを許すと、commit は片方だけを指す不整合な取引になる。
    """
    full_path, compact_path = _paths(tmp_path)

    with pytest.raises(ValueError, match="same decision ids"):
        decision_log.append_decision_transaction(
            [_full_event("left")],
            [_compact_row("right")],
            mode="per_timeframe",
            full_path=full_path,
            compact_path=compact_path,
        )
    # 不正入力では full にも compact にも何も書かれていない
    assert not compact_path.exists()
    assert list(decision_log.read_decision_events(full_path)) == []


def test_counts_must_match_one_to_one(tmp_path) -> None:
    full_path, compact_path = _paths(tmp_path)

    with pytest.raises(ValueError, match="one-to-one"):
        decision_log.append_decision_transaction(
            [_full_event("a"), _full_event("b")],
            [_compact_row("a")],
            mode="per_timeframe",
            full_path=full_path,
            compact_path=compact_path,
        )


def test_empty_transaction_is_rejected(tmp_path) -> None:
    full_path, compact_path = _paths(tmp_path)

    with pytest.raises(ValueError, match="at least one decision event"):
        decision_log.append_decision_transaction(
            [],
            [],
            mode="per_timeframe",
            full_path=full_path,
            compact_path=compact_path,
        )


def test_resending_the_same_decision_fails_closed_instead_of_double_counting(
    tmp_path,
) -> None:
    """⚠️同じ decision_id を作り直して再送すると、両方が不可視になる。

    トランザクション全体を再実行すると full/compact とも**新しいオフセット**へ
    書かれるため、同一 transaction_id に対して内容の異なる commit が2つ並ぶ。
    `load_commits` はこれを衝突として扱い、**両方を破棄**する(fail-closed)。

    これは望ましい安全側の挙動である。二重計上を防ぐ代わりに、
    「同じ判断を安易に再送してはいけない」という運用契約になる。
    途中でクラッシュした場合の復旧は、トランザクション全体をやり直すのではなく
    `decision_commit.append_commit` を同じバッチ記述子で呼び直すこと
    (そちらは冪等で、元の committed_at を返す)。
    """
    full_path, compact_path = _paths(tmp_path)
    decision_id = "retry-1"

    decision_log.append_decision_transaction(
        [_full_event(decision_id)],
        [_compact_row(decision_id)],
        mode="per_timeframe",
        full_path=full_path,
        compact_path=compact_path,
    )
    assert [row["decision_id"] for row in decision_log.read_decision_events(full_path)] == [
        decision_id
    ]

    decision_log.append_decision_transaction(
        [_full_event(decision_id)],
        [_compact_row(decision_id)],
        mode="per_timeframe",
        full_path=full_path,
        compact_path=compact_path,
    )

    # 衝突した transaction は両方とも隠れる(二重には決して見えない)
    assert list(decision_log.read_decision_events(full_path)) == []
    assert list(journal.read_entries(compact_path)) == []


def test_crash_before_commit_is_recovered_by_replaying_the_commit(tmp_path) -> None:
    """commit だけが欠けた状態は、同じ記述子で commit を打ち直せば復旧する。"""
    full_path, compact_path = _paths(tmp_path)
    decision_id = "recover-1"
    transaction_id = decision_commit.transaction_id_for([decision_id])

    full_batch = decision_log.append_decision_events(
        full_path,
        [_full_event(decision_id)],
        transaction_id=transaction_id,
    )
    compact_batch = journal._append_journal_batch(  # noqa: SLF001
        compact_path,
        [_compact_row(decision_id)],
        decision_transaction_id=transaction_id,
    )
    assert list(decision_log.read_decision_events(full_path)) == []
    assert full_batch is not None

    decision_commit.append_commit(
        full_path,
        decision_ids=[decision_id],
        full_batch_sha256=str(full_batch["batch_sha256"]),
        full_batch_line_start_offset=int(full_batch["line_start_offset"]),
        full_batch_line_sha256=str(full_batch["line_sha256"]),
        compact_batch_sha256=str(compact_batch["batch_sha256"]),
        compact_batch_line_start_offset=int(compact_batch["line_start_offset"]),
        compact_batch_byte_length=int(compact_batch["byte_length"]),
        compact_batch_payload_sha256=str(compact_batch["payload_sha256"]),
        mode="per_timeframe",
    )

    assert [row["decision_id"] for row in decision_log.read_decision_events(full_path)] == [
        decision_id
    ]
    assert [row["decision_id"] for row in journal.read_entries(compact_path)] == [decision_id]

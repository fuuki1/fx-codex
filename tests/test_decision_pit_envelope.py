"""判断イベントへのPIT封筒付与が operational store の適格判定と一致することを固定する。

2026-07-27 の実機で `audit_events` 20,758 件すべてが `pit_flag_not_true` となり
`predictions` が 0 件のままだった。原因は封筒生成 (`journal.pit_metadata_for_plan`)
自体ではなく、`briefing_decisions.jsonl` へ書く判断イベントへ封筒を付ける
`attach_decision_pit_metadata` が当時の本番HEADに存在しなかったこと。

ここでは次の2点を回帰として固定する。

1. 封筒を付けた判断イベントが operational store の gate を通ること
   (writer 側と store 側の契約が一致していること)
2. 根拠が欠けた判断は fail-closed で不適格に留まること
   (`pit_eligible` を無条件に true にしてはいけない)
"""

from __future__ import annotations

from datetime import datetime, timedelta, UTC

import pytest

import fx_briefing
from fx_intel import journal
from fx_intel.operational_migration import decision_pit_failure_reason

NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
SOURCE_CUTOFF = NOW - timedelta(minutes=5)


class _Plan:
    """判断プラン最小形。source_record_ids は input_context から導出される。"""

    def __init__(self, *, input_context_id: str = "context:sha256:abc", **context):
        self.symbol = "USDJPY"
        self.timeframe = "1h"
        self.direction = "long"
        self.input_context_id = input_context_id
        self.input_context = context or {
            "macro": {"values": {"dgs10": {"source_record_id": "DGS10:2026-07-27"}}},
            "liquidity": {"quote": {"source_record_id": "oanda:USDJPY:2026-07-27T12:00Z"}},
        }


def _event(decision_id: str = "d1", mode: str = "per_timeframe") -> dict[str, object]:
    return {
        "schema": 1,
        "event_type": "chart_decision",
        "decision_id": decision_id,
        "mode": mode,
        "ts": NOW.isoformat(),
        "symbol": "USDJPY",
        "timeframe": "1h",
    }


def _attach(event: dict[str, object], plan: object, *, mode: str = "per_timeframe") -> None:
    fx_briefing.attach_decision_pit_metadata(
        [event],
        [plan],
        prediction_time=NOW,
        source_cutoff=SOURCE_CUTOFF,
        max_feature_available_time=SOURCE_CUTOFF,
        mode=mode,
    )


@pytest.mark.parametrize("mode", ["per_timeframe", "fusion"])
def test_attached_envelope_passes_operational_store_gate(mode: str) -> None:
    """封筒付き判断は store 側 gate を通り、predictions へ昇格できる。"""

    event = _event(mode=mode)
    _attach(event, _Plan(), mode=mode)

    # writer 側と store 側が同一契約であること
    assert event["pit_contract"] == journal.DECISION_JOURNAL_PIT_CONTRACT
    assert event["pit_eligible"] is True
    assert journal.is_pit_eligible_entry(event) is True
    assert decision_pit_failure_reason(event) == ""


def test_envelope_carries_every_field_the_store_requires() -> None:
    """store が参照する項目が欠けていないこと(欠落が今回の障害の直接原因)。"""

    event = _event()
    _attach(event, _Plan())

    for field in (
        "pit_eligible",
        "pit_contract",
        "producer",
        "producer_version",
        "decision_id",
        "input_context_id",
        "source_record_ids",
        "prediction_time",
        "source_cutoff",
        "max_feature_available_time",
    ):
        assert field in event, f"PIT契約フィールドが欠落: {field}"

    assert event["source_record_ids"], "source_record_ids が空だと不適格になる"


def test_unenveloped_event_is_rejected_exactly_as_production_was() -> None:
    """封筒を付けないと、実機で観測された理由と同じ理由で不適格になる。"""

    event = _event()
    assert decision_pit_failure_reason(event) == "pit_flag_not_true"
    assert journal.is_pit_eligible_entry(event) is False


@pytest.mark.parametrize(
    "context, missing",
    [
        ({"macro": {}, "liquidity": {}}, "source_record_ids"),
        ({"macro": {"values": {}}, "liquidity": {"quote": {}}}, "source_record_ids"),
    ],
)
def test_missing_provenance_stays_fail_closed(context, missing) -> None:
    """根拠が無い判断を適格にしてはいけない(fail-closed の維持)。"""

    event = _event()
    _attach(event, _Plan(**context))

    assert event["source_record_ids"] == []
    assert event["pit_eligible"] is False
    assert decision_pit_failure_reason(event) == "pit_flag_not_true"


def test_missing_input_context_id_stays_fail_closed() -> None:
    """入力コンテキスト識別子が無い判断も不適格に留まる。"""

    event = _event()
    _attach(event, _Plan(input_context_id=""))

    assert event["pit_eligible"] is False
    assert journal.is_pit_eligible_entry(event) is False


def test_attach_rejects_mismatched_plan_count() -> None:
    """イベントとプランの対応が崩れたら黙って進めず失敗する。"""

    with pytest.raises(journal.PointInTimeError):
        fx_briefing.attach_decision_pit_metadata(
            [_event("d1"), _event("d2")],
            [_Plan()],
            prediction_time=NOW,
            source_cutoff=SOURCE_CUTOFF,
            max_feature_available_time=SOURCE_CUTOFF,
            mode="per_timeframe",
        )

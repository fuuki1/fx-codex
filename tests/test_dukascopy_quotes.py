"""収集済みdukascopy気配→判断時quoteの変換テスト。

fixtureは実機 ``collect/log/quotes.jsonl`` の実行を写したもの。
"""

from __future__ import annotations

from datetime import datetime, UTC
import json

import pytest

from fx_intel.dukascopy_quotes import (
    DEFAULT_MAX_AGE,
    DEFAULT_TAIL_BYTES,
    fetch_decision_quotes,
    parse_decision_quotes,
)
from fx_intel.evaluation_labels import (
    DUKASCOPY_HISTORICAL_QUOTE_SOURCE,
    executable_cost_model_id_for_source,
    measured_cost_model_id_for_source,
)
from fx_intel import liquidity

NOW = datetime(2026, 7, 30, 4, 10, tzinfo=UTC)


def _row(**overrides: object) -> str:
    # 実機の1行を写したもの(2026-07-30 GBPUSD)
    payload: dict[str, object] = {
        "account_environment": "datafeed",
        "ask": 1.33494,
        "bid": 1.33485,
        "collection_mode": "historical_download",
        "instrument": "GBPUSD",
        "mid": 1.334895,
        "provider": "dukascopy",
        "provider_event_time": "2026-07-30T02:59:59.612000+00:00",
        "quality_flags": ["provider_does_not_supply_sequence_id"],
        "quality_state": "usable",
        "received_at": "2026-07-30T04:07:20.802031+00:00",
        "schema_version": 1,
        "spread": 8.999999999992347e-05,
        "tradable": False,
        "writer_id": "trader-mini:13867",
    }
    payload.update(overrides)
    return json.dumps(payload)


def test_parses_real_production_row() -> None:
    quotes, warnings = parse_decision_quotes([_row()], ["GBPUSD"], now=NOW)
    assert warnings == []
    quote = quotes["GBPUSD"]
    assert quote["bid"] == 1.33485
    assert quote["ask"] == 1.33494
    assert quote["source"] == DUKASCOPY_HISTORICAL_QUOTE_SOURCE
    assert quote["role"] == "decision_quote"


def test_tradeable_is_false_so_downstream_cannot_claim_executability() -> None:
    """collectorのtradable:falseを英綴りtradeableへ確実に引き継ぐ。

    liquidity.quote_from_mapping が読むのは英綴りなので、ここで橋渡ししないと
    「約定可能」と誤認される。この変換こそが本モジュールの要点。
    """

    quotes, _ = parse_decision_quotes([_row()], ["GBPUSD"], now=NOW)
    assert quotes["GBPUSD"]["tradeable"] is False


def test_liquidity_separates_not_tradable_from_malformed() -> None:
    """not_tradeableは「壊れている」ではなく「約定を主張しない」を意味する。

    価格自体は実測なのでnet-Rは計算できる。両者をinvalidに潰すと測定不能に
    なるため、statusを分ける。フラグは消さずに伝播させる。
    """

    quotes, _ = parse_decision_quotes([_row()], ["GBPUSD"], now=NOW)
    snapshot = liquidity.quote_from_mapping(quotes["GBPUSD"])
    assert snapshot is not None
    assert "not_tradeable" in snapshot.quality_flags
    assert snapshot.quality_status == "measured_not_tradable"


def test_malformed_price_still_reports_invalid() -> None:
    """価格の欠陥は従来どおりinvalid(緩めすぎていないことの確認)。"""

    snapshot = liquidity.quote_from_mapping(
        {"bid": 1.4, "ask": 1.3, "observed_at": "2026-07-30T03:00:00+00:00", "tradeable": False}
    )
    assert snapshot is not None
    assert snapshot.quality_status == "invalid"


def test_tradable_quote_keeps_measured_status() -> None:
    """執行可能な気配のmeasuredは一切変えない(退行防止)。"""

    snapshot = liquidity.quote_from_mapping(
        {"bid": 1.3, "ask": 1.4, "observed_at": "2026-07-30T03:00:00+00:00", "tradeable": True}
    )
    assert snapshot is not None
    assert snapshot.quality_status == "measured"
    assert snapshot.quality_flags == ()


def test_source_never_resolves_to_executable_cost_model() -> None:
    quotes, _ = parse_decision_quotes([_row()], ["GBPUSD"], now=NOW)
    source = str(quotes["GBPUSD"]["source"])
    assert executable_cost_model_id_for_source(source) is None
    assert measured_cost_model_id_for_source(source) is not None


def test_latest_row_wins_per_symbol() -> None:
    older = _row(provider_event_time="2026-07-30T02:00:00+00:00", bid=1.1, ask=1.2)
    newer = _row(provider_event_time="2026-07-30T03:30:00+00:00", bid=1.3, ask=1.4)
    quotes, _ = parse_decision_quotes([newer, older], ["GBPUSD"], now=NOW)
    assert quotes["GBPUSD"]["bid"] == 1.3


def test_stale_quote_is_rejected_with_warning() -> None:
    stale = _row(provider_event_time="2026-07-29T00:00:00+00:00")
    quotes, warnings = parse_decision_quotes([stale], ["GBPUSD"], now=NOW)
    assert quotes == {}
    assert any("古すぎます" in w for w in warnings)


def test_non_usable_quality_state_is_rejected() -> None:
    quotes, warnings = parse_decision_quotes([_row(quality_state="suspect")], ["GBPUSD"], now=NOW)
    assert quotes == {}
    assert any("usable" in w for w in warnings)


def test_crossed_or_missing_prices_are_rejected() -> None:
    for bad in ({"bid": 1.4, "ask": 1.3}, {"bid": None}, {"ask": 0}):
        quotes, warnings = parse_decision_quotes([_row(**bad)], ["GBPUSD"], now=NOW)
        assert quotes == {}, bad
        assert warnings


def test_other_providers_are_ignored() -> None:
    quotes, warnings = parse_decision_quotes([_row(provider="oanda")], ["GBPUSD"], now=NOW)
    assert quotes == {}
    assert any("見つかりません" in w for w in warnings)


def test_missing_symbol_reports_warning_without_inventing_quote() -> None:
    quotes, warnings = parse_decision_quotes([_row()], ["GBPUSD", "USDJPY"], now=NOW)
    assert set(quotes) == {"GBPUSD"}
    assert any("USDJPY" in w for w in warnings)


def test_received_time_before_observed_is_rejected() -> None:
    quotes, warnings = parse_decision_quotes(
        [_row(received_at="2026-07-30T01:00:00+00:00")], ["GBPUSD"], now=NOW
    )
    assert quotes == {}
    assert any("received_atが観測時刻より前" in warning for warning in warnings)


def test_naive_observed_or_received_time_is_rejected() -> None:
    for field in ("provider_event_time", "received_at"):
        quotes, warnings = parse_decision_quotes(
            [_row(**{field: "2026-07-30T03:00:00"})], ["GBPUSD"], now=NOW
        )
        assert quotes == {}, field
        assert any("timezone付きISO-8601" in warning for warning in warnings)


def test_future_observed_or_received_time_is_rejected_without_stale_fallback() -> None:
    valid = _row(provider_event_time="2026-07-30T03:30:00+00:00")
    for field in ("provider_event_time", "received_at"):
        quotes, warnings = parse_decision_quotes(
            [valid, _row(**{field: "2026-07-30T04:11:00+00:00"})],
            ["GBPUSD"],
            now=NOW,
        )
        assert quotes == {}, field
        assert any("未来の時刻" in warning for warning in warnings)


def test_naive_decision_time_is_rejected() -> None:
    with pytest.raises(ValueError, match="now must be timezone-aware"):
        parse_decision_quotes([_row()], ["GBPUSD"], now=datetime(2026, 7, 30, 4, 10))


def test_fetch_reads_tail_of_file(tmp_path) -> None:
    path = tmp_path / "quotes.jsonl"
    path.write_text("\n".join(_row() for _ in range(5)) + "\n", encoding="utf-8")
    quotes, warnings = fetch_decision_quotes(
        ["GBPUSD"], now=NOW, quote_log_path=path, max_age=DEFAULT_MAX_AGE
    )
    assert warnings == []
    assert quotes["GBPUSD"]["bid"] == 1.33485


def test_fetch_missing_file_fails_closed(tmp_path) -> None:
    quotes, warnings = fetch_decision_quotes(
        ["GBPUSD"], now=NOW, quote_log_path=tmp_path / "absent.jsonl"
    )
    assert quotes == {}
    assert warnings


def test_tail_window_covers_all_symbols_when_written_in_blocks(tmp_path) -> None:
    """collectorはsymbolごとに連続ブロックで書く。窓が狭いと最後のsymbolしか
    入らず、他ペアが恒久的にnet-R対象から漏れる(実機で実際に起きた)。"""

    path = tmp_path / "quotes.jsonl"
    blocks = []
    for symbol in ("USDJPY", "EURUSD", "GBPUSD"):
        for _ in range(40):
            blocks.append(_row(instrument=symbol))
    path.write_text("\n".join(blocks) + "\n", encoding="utf-8")
    quotes, warnings = fetch_decision_quotes(
        ["USDJPY", "EURUSD", "GBPUSD"], now=NOW, quote_log_path=path
    )
    assert set(quotes) == {"USDJPY", "EURUSD", "GBPUSD"}, warnings


def test_default_tail_window_is_large_enough_for_real_block_layout() -> None:
    """実機実測: 3通貨が窓に入るには約40MB必要。既定を縮めさせない。"""

    assert DEFAULT_TAIL_BYTES >= 40_000_000


def test_tail_truncation_discards_partial_first_line(tmp_path) -> None:
    """末尾走査で切れた行を捨て、壊れたJSONを読まないことを確認する。"""

    path = tmp_path / "quotes.jsonl"
    path.write_text("\n".join(_row() for _ in range(50)) + "\n", encoding="utf-8")
    quotes, _ = fetch_decision_quotes(["GBPUSD"], now=NOW, quote_log_path=path, tail_bytes=900)
    assert quotes["GBPUSD"]["bid"] == 1.33485

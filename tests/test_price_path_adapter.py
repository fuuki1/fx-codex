"""Dukascopy tick → 確定足アダプタのテスト。

守るべき性質は3つで、どれも壊れると採点が黙って誤る。
1. 形成中バーを書かない(未来情報の混入を防ぐ)
2. 採点が信頼するスコープを名乗り、high/low が実際に採用される
3. 既存の 15m/1h/4h/1d 行と自然キーで衝突しない
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import os

from fx_intel import trade_outcome
from fx_intel.price_history import append_snapshot_entries, snapshot_entries
from fx_intel.price_path_adapter import (
    ADAPTER_OHLC_SCOPE,
    ADAPTER_WRITER_ID,
    ADAPTER_TIMEFRAME,
    BAR_SECONDS,
    build_closed_bars,
    build_rows,
    iter_ticks,
    load_quotes,
    parse_tick,
)

BASE = datetime(2026, 7, 28, 2, 0, 0, tzinfo=UTC)


def _tick(
    offset_seconds: float,
    mid: float,
    *,
    symbol: str = "USDJPY",
    quality: str = "usable",
    bid: float | None = None,
    ask: float | None = None,
) -> dict:
    event = BASE + timedelta(seconds=offset_seconds)
    return {
        "instrument": symbol,
        "quality_state": quality,
        "provider_event_time": event.isoformat(),
        "received_at": (event + timedelta(hours=2)).isoformat(),
        "mid": mid,
        "bid": bid if bid is not None else mid - 0.001,
        "ask": ask if ask is not None else mid + 0.001,
    }


def _ticks(payloads: list[dict]):
    return list(iter_ticks(json.dumps(p, ensure_ascii=False) for p in payloads))


def test_forming_bar_is_never_emitted() -> None:
    """期間が終わっていないバーは出力しない(PIT保護の本体)。"""
    ticks = _ticks([_tick(0, 150.0), _tick(60, 150.5), _tick(120, 149.5)])

    # バー終端(02:05)より前を as_of にすると、まだ確定していない
    mid_bar = build_closed_bars(ticks, as_of=BASE + timedelta(seconds=120))
    assert mid_bar == []

    # 終端ちょうどで確定する
    closed = build_closed_bars(ticks, as_of=BASE + timedelta(seconds=BAR_SECONDS))
    assert len(closed) == 1
    assert closed[0].bar_end == BASE + timedelta(seconds=BAR_SECONDS)


def test_high_low_come_from_observed_ticks_only() -> None:
    ticks = _ticks([_tick(0, 150.0), _tick(60, 151.0), _tick(120, 149.0), _tick(180, 150.4)])
    bar = build_closed_bars(ticks, as_of=BASE + timedelta(hours=1))[0]

    assert bar.open == 150.0
    assert bar.high == 151.0
    assert bar.low == 149.0
    assert bar.close == 150.4
    assert bar.tick_count == 4


def test_unusable_ticks_are_dropped() -> None:
    """collector が usable と判定していない行は再解釈せず捨てる。"""
    assert parse_tick(_tick(0, 150.0, quality="quarantined")) is None
    assert parse_tick(_tick(0, 150.0, quality="")) is None

    ticks = _ticks([_tick(0, 150.0), _tick(60, 999.0, quality="rejected")])
    bar = build_closed_bars(ticks, as_of=BASE + timedelta(hours=1))[0]
    assert bar.high == 150.0, "捨てたはずの tick が high を汚染している"


def test_row_declares_a_scope_the_scorer_trusts() -> None:
    """採点が high/low を採用する条件を満たすこと。

    これが崩れると close_only_path に落ち、TP/SL 判定が沈黙して壊れる。
    """
    ticks = _ticks([_tick(0, 150.0), _tick(60, 151.0)])
    bar = build_closed_bars(ticks, as_of=BASE + timedelta(hours=1))[0]
    row = bar.to_row(run_id="r", writer_id="w")

    assert row["ohlc_scope"] == ADAPTER_OHLC_SCOPE
    assert ADAPTER_OHLC_SCOPE in trade_outcome.TRUSTED_POST_PREDICTION_OHLC_SCOPES

    point = trade_outcome._price_path_point(bar.bar_end, row)
    assert point is not None
    assert point.rejected_range is False
    assert point.high == 151.0
    assert point.low == 150.0


def test_event_time_and_available_time_are_separated() -> None:
    """成立時刻と観測可能時刻を混同しない。"""
    ticks = _ticks([_tick(0, 150.0), _tick(60, 150.2)])
    bar = build_closed_bars(ticks, as_of=BASE + timedelta(hours=1))[0]
    row = bar.to_row(run_id="r", writer_id="w")

    assert row["event_time"] == bar.bar_end.isoformat()
    # received_at は provider_event_time + 2h(lag)なので必ず後になる
    assert row["available_time"] > row["event_time"]


def test_does_not_collide_with_existing_timeframe_rows(tmp_path) -> None:
    """既存の 15m 行と同じ capture_slot でも writer が拒否しないこと。

    同じ timeframe 名を使うと自然キーが衝突し、writer が例外で落ちて
    実機の収集を止める。専用 timeframe 名がその防波堤になっている。
    """
    prices = tmp_path / "prices.jsonl"
    # 既存経路(5分スナップショット writer)が書いた 15m 行。
    # tick と同じ 5 分スロットに落ちるので、自然キーが重なる条件を作っている。
    existing = snapshot_entries(
        {"USDJPY": {"15m": {"close": 999.0, "high": 999.5, "low": 998.5}}},
        now=BASE,
    )
    append_snapshot_entries(prices, existing)

    ticks = _ticks([_tick(0, 150.0), _tick(60, 151.0)])
    bars = build_closed_bars(ticks, as_of=BASE + timedelta(hours=1))
    rows = build_rows(bars, run_id="r", writer_id="w")
    appended = append_snapshot_entries(prices, rows)

    assert appended == 1
    written = [json.loads(line) for line in prices.read_text().splitlines() if line.strip()]
    timeframes = {row["timeframe"] for row in written}
    assert timeframes == {"15m", ADAPTER_TIMEFRAME}
    # 既存行は書き換えられていない
    assert any(row["timeframe"] == "15m" and row["close"] == 999.0 for row in written)


def test_replay_is_idempotent(tmp_path) -> None:
    """同じ tick を再処理しても二重に増えない(再実行安全)。"""
    prices = tmp_path / "prices.jsonl"
    ticks = _ticks([_tick(0, 150.0), _tick(60, 151.0)])
    bars = build_closed_bars(ticks, as_of=BASE + timedelta(hours=1))

    first = append_snapshot_entries(prices, build_rows(bars, run_id="r", writer_id="w"))
    second = append_snapshot_entries(prices, build_rows(bars, run_id="r", writer_id="w"))

    assert (first, second) == (1, 0)


def test_bars_split_per_symbol_and_slot() -> None:
    ticks = _ticks(
        [
            _tick(0, 150.0),
            _tick(60, 150.5),
            _tick(BAR_SECONDS, 151.0),  # 次のスロット
            _tick(0, 1.10, symbol="EURUSD"),
        ]
    )
    bars = build_closed_bars(ticks, as_of=BASE + timedelta(hours=1))

    keys = {(bar.symbol, bar.bar_start) for bar in bars}
    assert len(keys) == 3
    assert ("EURUSD", BASE) in keys


def test_bid_ask_absence_is_flagged_not_faked() -> None:
    payload = _tick(0, 150.0)
    payload["bid"] = None
    payload["ask"] = None
    ticks = _ticks([payload])
    bar = build_closed_bars(ticks, as_of=BASE + timedelta(hours=1))[0]
    row = bar.to_row(run_id="r", writer_id="w")

    assert "bid" not in row and "ask" not in row
    assert "bid_ask_unavailable" in row["data_quality_flags"]


def test_load_quotes_missing_file_is_not_an_error(tmp_path) -> None:
    """collector 未稼働は異常ではない(空で返す)。"""
    assert load_quotes(tmp_path / "nope.jsonl") == []


def test_truncated_trailing_line_does_not_lose_earlier_ticks() -> None:
    """追記中のファイルを読んでも、完全な行までは活かす。"""
    good = json.dumps(_tick(0, 150.0), ensure_ascii=False)
    ticks = list(iter_ticks([good, '{"instrument": "USDJPY", "quality_st']))
    assert len(ticks) == 1


def test_rerun_from_a_different_process_does_not_conflict(tmp_path) -> None:
    """別 process が同じバーを再生成しても追記が失敗しないこと。

    実機の全量 run が exit 74 で全拒否された原因。`_same_snapshot` は
    writer_id の不一致を内容比較より先に「競合する writer」と判定するため、
    writer_id に PID を含めると再実行のたびに別 writer になり、既に書いた
    バーを再生成した時点で追記全体が fail-closed に当たる。
    """
    prices = tmp_path / "prices.jsonl"
    ticks = _ticks(
        [
            _tick(0, 150.0),
            _tick(60, 151.0),
            _tick(BAR_SECONDS, 152.0),
            _tick(BAR_SECONDS + 60, 152.5),
        ]
    )
    bars = build_closed_bars(ticks, as_of=BASE + timedelta(hours=1))
    assert len(bars) == 2

    # 1回目: 一部だけ書く(--limit 相当)
    first = append_snapshot_entries(prices, build_rows(bars[-1:], run_id="run-A"))
    assert first == 1

    # 2回目: 別 run_id で全量。既に書いたバーを含むが、内容は同一なので
    # 重複としてスキップされ、新しいバーだけが追記されるべき。
    second = append_snapshot_entries(prices, build_rows(bars, run_id="run-B"))
    assert second == 1, "既存バーの再生成が競合扱いされている"

    written = [json.loads(line) for line in prices.read_text().splitlines() if line.strip()]
    assert len(written) == 2


def test_writer_id_is_stable_across_runs() -> None:
    """writer_id に PID 等の実行ごとに変わる値を混ぜない。"""
    ticks = _ticks([_tick(0, 150.0)])
    bars = build_closed_bars(ticks, as_of=BASE + timedelta(hours=1))

    a = build_rows(bars, run_id="x")[0]
    b = build_rows(bars, run_id="y")[0]

    assert a["writer_id"] == b["writer_id"] == ADAPTER_WRITER_ID
    assert str(os.getpid()) not in str(a["writer_id"])


# --- collector 完了判定(2026-09-16 実機の未完成足の再発防止) -------------------

import importlib.util  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from fx_intel.price_history import PriceHistoryWriteError  # noqa: E402
from fx_intel.price_path_adapter import (  # noqa: E402
    WINDOW_LOOKBACK,
    PricePathAdapterError,
    adapter_window_start,
    iter_quote_file,
    load_collector_completion,
)

INCIDENT_HOUR = datetime(2026, 9, 15, 9, 0, 0, tzinfo=UTC)


def _dk(
    at: datetime,
    mid: float,
    *,
    symbol: str = "EURUSD",
    provider: str = "dukascopy",
    received: datetime | None = None,
) -> dict:
    return {
        "provider": provider,
        "instrument": symbol,
        "quality_state": "usable",
        "provider_event_time": at.isoformat(),
        "received_at": (received or at + timedelta(hours=2)).isoformat(),
        "mid": mid,
        "bid": mid - 0.00002,
        "ask": mid + 0.00002,
    }


def _dk_ticks(payloads: list[dict]):
    return list(iter_ticks(json.dumps(p) for p in payloads))


def _hour_batch(symbol: str = "EURUSD") -> list[dict]:
    # 09:30 足に 6 tick、09:35 足に 2 tick
    base = INCIDENT_HOUR + timedelta(minutes=30)
    mids = [1.1535, 1.1536, 1.1534, 1.15345, 1.1537, 1.15348]
    rows = [_dk(base + timedelta(seconds=40 * i), mid, symbol=symbol) for i, mid in enumerate(mids)]
    rows += [
        _dk(base + timedelta(minutes=5, seconds=10 * i), 1.154, symbol=symbol) for i in range(2)
    ]
    return rows


def test_gated_bars_wait_until_collector_finishes_the_hour() -> None:
    ticks = _dk_ticks(_hour_batch())
    as_of = INCIDENT_HOUR + timedelta(days=1)

    not_yet = build_closed_bars(
        ticks, as_of=as_of, complete_through={"EURUSD": INCIDENT_HOUR}  # 08 時台まで完了
    )
    done = build_closed_bars(
        ticks, as_of=as_of, complete_through={"EURUSD": INCIDENT_HOUR + timedelta(hours=1)}
    )

    assert not_yet == []
    assert [bar.tick_count for bar in done] == [6, 2]


def test_gated_mode_drops_symbols_without_collector_state() -> None:
    ticks = _dk_ticks(_hour_batch("GBPUSD"))
    bars = build_closed_bars(
        ticks,
        as_of=INCIDENT_HOUR + timedelta(days=1),
        complete_through={"EURUSD": INCIDENT_HOUR + timedelta(hours=1)},
    )
    assert bars == []


def test_gated_mode_uses_only_the_provider_the_state_certifies() -> None:
    payloads = [_dk(INCIDENT_HOUR + timedelta(minutes=1), 1.15, provider="oanda")]
    bars = build_closed_bars(
        _dk_ticks(payloads),
        as_of=INCIDENT_HOUR + timedelta(days=1),
        complete_through={"EURUSD": INCIDENT_HOUR + timedelta(hours=1)},
    )
    assert bars == []


def test_gated_mode_never_emits_a_bar_observed_after_as_of() -> None:
    at = INCIDENT_HOUR + timedelta(minutes=1)
    as_of = INCIDENT_HOUR + timedelta(hours=3)
    payloads = [_dk(at, 1.15, received=as_of + timedelta(seconds=1))]
    bars = build_closed_bars(
        _dk_ticks(payloads),
        as_of=as_of,
        complete_through={"EURUSD": INCIDENT_HOUR + timedelta(hours=1)},
    )
    assert bars == []


def test_load_collector_completion_returns_end_of_completed_hour(tmp_path) -> None:
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps({"completed_hours": {"eurusd": "2026-09-23T03:00:00+00:00"}}), encoding="utf-8"
    )
    assert load_collector_completion(state) == {"EURUSD": datetime(2026, 9, 23, 4, 0, tzinfo=UTC)}


@pytest.mark.parametrize(
    "content",
    [
        None,
        "not json",
        json.dumps({"x": 1}),
        json.dumps({"completed_hours": {"EURUSD": "2026-09-23T03:00:00"}}),
    ],
)
def test_load_collector_completion_fails_closed(tmp_path, content) -> None:
    state = tmp_path / "state.json"
    if content is not None:
        state.write_text(content, encoding="utf-8")
    with pytest.raises(PricePathAdapterError):
        load_collector_completion(state)


def test_iter_quote_file_never_reads_past_the_fixed_offset(tmp_path) -> None:
    quotes = tmp_path / "quotes.jsonl"
    first = json.dumps(_dk(INCIDENT_HOUR, 1.15)) + "\n"
    second = json.dumps(_dk(INCIDENT_HOUR + timedelta(seconds=1), 1.16)) + "\n"
    quotes.write_text(first + second, encoding="utf-8")

    whole_first = list(iter_quote_file(quotes, end_offset=len(first)))
    cut_second = list(iter_quote_file(quotes, end_offset=len(first) + len(second) // 2))

    assert [tick.mid for tick in whole_first] == [1.15]
    assert [tick.mid for tick in cut_second] == [1.15]


def test_iter_quote_file_skips_ticks_before_window(tmp_path) -> None:
    quotes = tmp_path / "quotes.jsonl"
    rows = [_dk(INCIDENT_HOUR - timedelta(days=3), 1.1), _dk(INCIDENT_HOUR, 1.2)]
    quotes.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    ticks = list(iter_quote_file(quotes, end_offset=quotes.stat().st_size, since=INCIDENT_HOUR))

    assert [tick.mid for tick in ticks] == [1.2]


def test_adapter_window_start_uses_oldest_latest_bar_per_symbol(tmp_path) -> None:
    prices = tmp_path / "prices.jsonl"
    late = build_rows(
        build_closed_bars(
            _dk_ticks(_hour_batch("EURUSD")), as_of=INCIDENT_HOUR + timedelta(days=1)
        ),
        run_id="a",
    )
    early = build_rows(
        build_closed_bars(
            _dk_ticks([_dk(INCIDENT_HOUR - timedelta(hours=5), 150.0, symbol="USDJPY")]),
            as_of=INCIDENT_HOUR + timedelta(days=1),
        ),
        run_id="b",
    )
    append_snapshot_entries(prices, late + early)

    assert adapter_window_start(prices, ["EURUSD", "USDJPY"]) == (
        INCIDENT_HOUR - timedelta(hours=5) - WINDOW_LOOKBACK
    )
    assert adapter_window_start(prices, ["EURUSD", "GBPUSD"]) is None
    assert adapter_window_start(tmp_path / "missing.jsonl", ["EURUSD"]) is None


def test_append_isolates_a_conflicting_key_only_when_asked(tmp_path) -> None:
    prices = tmp_path / "prices.jsonl"
    batch = _hour_batch()
    as_of = INCIDENT_HOUR + timedelta(days=1)
    partial = build_rows(build_closed_bars(_dk_ticks(batch[:4]), as_of=as_of), run_id="old")
    append_snapshot_entries(prices, partial)
    full = build_rows(build_closed_bars(_dk_ticks(batch), as_of=as_of), run_id="new")
    before = prices.read_bytes()

    with pytest.raises(PriceHistoryWriteError):
        append_snapshot_entries(prices, full)
    assert prices.read_bytes() == before

    conflicts: list[dict] = []
    appended = append_snapshot_entries(prices, full, conflicts=conflicts)

    assert appended == 1  # 09:35 足だけ
    assert [c["natural_key"][0] for c in conflicts] == [partial[0]["capture_slot"]]
    written = [json.loads(line) for line in prices.read_text().splitlines()]
    assert written[0] == partial[0]  # 既存行は書き換えない


def _load_runner():
    script = Path(__file__).resolve().parents[1] / "tools" / "price_path_adapter_run.py"
    spec = importlib.util.spec_from_file_location("price_path_adapter_run_under_test", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_state(path: Path, completed_hour: datetime) -> None:
    path.write_text(
        json.dumps({"completed_hours": {"EURUSD": completed_hour.isoformat()}}), encoding="utf-8"
    )


def test_runner_reproduces_and_contains_the_2026_09_16_partial_bar(tmp_path, capsys) -> None:
    runner = _load_runner()
    quotes = tmp_path / "quotes.jsonl"
    prices = tmp_path / "prices.jsonl"
    state = tmp_path / "state.json"
    ledger = tmp_path / "conflicts.jsonl"
    batch = _hour_batch()
    args = [
        "--quotes",
        str(quotes),
        "--prices",
        str(prices),
        "--collector-state",
        str(state),
        "--conflict-ledger",
        str(ledger),
        "--write",
    ]

    # collector が 09 時台を追記している途中: state はまだ 08 時台まで。
    quotes.write_text("".join(json.dumps(row) + "\n" for row in batch[:4]), encoding="utf-8")
    _write_state(state, INCIDENT_HOUR - timedelta(hours=1))
    assert runner.main(args) == 0
    assert not prices.exists() or prices.read_text() == ""

    # 旧実装が書いてしまった未完成足(先頭 4 tick)が既にある状態を再現する。
    as_of = INCIDENT_HOUR + timedelta(days=1)
    stale = build_rows(build_closed_bars(_dk_ticks(batch[:4]), as_of=as_of), run_id="legacy")
    append_snapshot_entries(prices, stale)

    # 時間帯の追記が終わり state が進む。
    quotes.write_text("".join(json.dumps(row) + "\n" for row in batch), encoding="utf-8")
    _write_state(state, INCIDENT_HOUR)
    capsys.readouterr()
    assert runner.main(args) == runner.EX_IOERR  # 新しい衝突は 1 回だけ知らせる
    report = json.loads(capsys.readouterr().out)
    assert report["appended"] == 1 and report["conflicts_new"] == 1
    rows = [json.loads(line) for line in prices.read_text().splitlines()]
    assert rows[0] == stale[0]
    assert rows[1]["tick_count"] == 2
    assert len(ledger.read_text().splitlines()) == 1

    # 次回: 同じ衝突は記録済みなので止まらない。
    assert runner.main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["conflicts_total"] == 1 and report["conflicts_new"] == 0
    assert report["appended"] == 0
    assert len(ledger.read_text().splitlines()) == 1


def test_runner_fails_closed_without_collector_state(tmp_path) -> None:
    runner = _load_runner()
    assert (
        runner.main(
            [
                "--quotes",
                str(tmp_path / "q.jsonl"),
                "--prices",
                str(tmp_path / "p.jsonl"),
                "--collector-state",
                str(tmp_path / "missing.json"),
                "--write",
            ]
        )
        == runner.EX_NOINPUT
    )


# --- quote index による読み取り開始位置(毎時 9GB 全読みの解消) --------------------

import hashlib  # noqa: E402
import shutil  # noqa: E402

from fx_intel import quote_tape_index  # noqa: E402
from fx_intel.price_path_adapter import quote_index_start_offset  # noqa: E402


def _write_quotes(path: Path, payloads: list[dict]) -> list[tuple[int, int, dict]]:
    spans: list[tuple[int, int, dict]] = []
    offset = 0
    with path.open("wb") as handle:
        for payload in payloads:
            raw = (json.dumps(payload) + "\n").encode()
            handle.write(raw)
            spans.append((offset, offset + len(raw), payload))
            offset += len(raw)
    return spans


def _build_index(
    database: Path,
    quotes: Path,
    spans: list[tuple[int, int, dict]],
    *,
    indexed_lines: int | None = None,
    rejected: int = 0,
) -> None:
    connection = quote_tape_index._connect_writer(database)
    indexed = spans if indexed_lines is None else spans[:indexed_lines]
    for start, end, payload in indexed:
        raw = quotes.read_bytes()[start:end]
        connection.execute(
            "INSERT INTO quote_offsets VALUES (?, ?, ?, ?, ?, ?)",
            (
                start,
                end,
                payload["instrument"],
                datetime.fromisoformat(payload["provider_event_time"]).isoformat(),
                datetime.fromisoformat(payload["received_at"]).isoformat(),
                hashlib.sha256(raw).hexdigest(),
            ),
        )
    stat = quotes.stat()
    last_start, last_end = (indexed[-1][0], indexed[-1][1]) if indexed else (0, 0)
    connection.execute(
        "INSERT INTO source_state VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            str(quotes.resolve()),
            stat.st_dev,
            stat.st_ino,
            last_end,
            stat.st_size,
            last_start,
            last_end,
            hashlib.sha256(quotes.read_bytes()[last_start:last_end]).hexdigest(),
            len(indexed),
            rejected,
            datetime.now(UTC).isoformat(),
        ),
    )
    connection.commit()
    connection.close()


def _multi_day_payloads() -> list[dict]:
    rows: list[dict] = []
    start = INCIDENT_HOUR - timedelta(days=3)
    for hour in range(0, 72 + 3):
        for symbol in ("EURUSD", "USDJPY"):
            base = 1.15 if symbol == "EURUSD" else 150.0
            for i in range(3):
                rows.append(
                    _dk(start + timedelta(hours=hour, minutes=20 * i), base + i, symbol=symbol)
                )
    return rows


def _tick_key(tick) -> tuple:
    return (tick.symbol, tick.event_time, tick.mid)


def test_index_seek_reads_exactly_the_ticks_a_full_scan_reads(tmp_path) -> None:
    quotes = tmp_path / "quotes.jsonl"
    database = tmp_path / "quotes.sqlite3"
    spans = _write_quotes(quotes, _multi_day_payloads())
    _build_index(database, quotes, spans)
    since = INCIDENT_HOUR - timedelta(hours=5)
    size = quotes.stat().st_size

    start, reason = quote_index_start_offset(
        quotes, database, symbols=["EURUSD", "USDJPY"], since=since, end_offset=size
    )
    seeked = list(iter_quote_file(quotes, end_offset=size, since=since, start_offset=start))
    full = list(iter_quote_file(quotes, end_offset=size, since=since))

    assert reason == "index_seek"
    assert size // 2 < start < size  # 窓より前の約 9 割を読まずに済む
    assert [_tick_key(t) for t in seeked] == [_tick_key(t) for t in full]


def test_index_seek_still_reads_the_unindexed_tail(tmp_path) -> None:
    quotes = tmp_path / "quotes.jsonl"
    database = tmp_path / "quotes.sqlite3"
    spans = _write_quotes(quotes, _multi_day_payloads())
    _build_index(database, quotes, spans, indexed_lines=len(spans) // 2)
    since = INCIDENT_HOUR  # 未 index の範囲にしか該当 tick がない
    size = quotes.stat().st_size

    start, reason = quote_index_start_offset(
        quotes, database, symbols=["EURUSD", "USDJPY"], since=since, end_offset=size
    )
    seeked = list(iter_quote_file(quotes, end_offset=size, since=since, start_offset=start))
    full = list(iter_quote_file(quotes, end_offset=size, since=since))

    assert reason == "index_seek"
    assert start == spans[len(spans) // 2 - 1][1]
    assert seeked and [_tick_key(t) for t in seeked] == [_tick_key(t) for t in full]


@pytest.mark.parametrize(
    "defect",
    ["missing", "replaced_file", "rewritten_last_line", "rejected_lines", "other_path"],
)
def test_index_seek_falls_back_to_full_scan_when_index_is_not_trustworthy(tmp_path, defect) -> None:
    quotes = tmp_path / "quotes.jsonl"
    database = tmp_path / "quotes.sqlite3"
    spans = _write_quotes(quotes, _multi_day_payloads())
    if defect != "missing":
        _build_index(database, quotes, spans, rejected=1 if defect == "rejected_lines" else 0)
    if defect == "replaced_file":
        copy = tmp_path / "copy.jsonl"
        shutil.copyfile(quotes, copy)
        copy.replace(quotes)
    elif defect == "rewritten_last_line":
        data = bytearray(quotes.read_bytes())
        data[spans[-1][0] + 5] ^= 1
        quotes.write_bytes(bytes(data))
    elif defect == "other_path":
        other = tmp_path / "other.jsonl"
        shutil.copyfile(quotes, other)
        quotes = other

    start, reason = quote_index_start_offset(
        quotes,
        database,
        symbols=["EURUSD"],
        since=INCIDENT_HOUR,
        end_offset=quotes.stat().st_size,
    )

    assert start == 0
    assert reason != "index_seek"


def test_iter_quote_file_rejects_a_start_offset_inside_a_line(tmp_path) -> None:
    quotes = tmp_path / "quotes.jsonl"
    _write_quotes(quotes, [_dk(INCIDENT_HOUR, 1.1), _dk(INCIDENT_HOUR, 1.2)])
    with pytest.raises(PricePathAdapterError, match="行頭"):
        list(iter_quote_file(quotes, end_offset=quotes.stat().st_size, start_offset=3))


def test_runner_with_index_writes_the_same_bars_as_a_full_scan(tmp_path, capsys) -> None:
    runner = _load_runner()
    payloads = _multi_day_payloads()
    written: dict[str, list[dict]] = {}
    for mode in ("full", "index"):
        root = tmp_path / mode
        root.mkdir()
        quotes = root / "quotes.jsonl"
        spans = _write_quotes(quotes, payloads)
        state = root / "state.json"
        state.write_text(
            json.dumps(
                {
                    "completed_hours": {
                        "EURUSD": (INCIDENT_HOUR + timedelta(hours=1)).isoformat(),
                        "USDJPY": (INCIDENT_HOUR + timedelta(hours=1)).isoformat(),
                    }
                }
            ),
            encoding="utf-8",
        )
        prices = root / "prices.jsonl"
        # 既存の足があると時間窓が効き、index で開始位置を詰められる。
        seed = build_rows(
            build_closed_bars(
                _dk_ticks(
                    [
                        _dk(INCIDENT_HOUR - timedelta(hours=10, minutes=-10), 1.1),
                        _dk(
                            INCIDENT_HOUR - timedelta(hours=10, minutes=-10), 150.0, symbol="USDJPY"
                        ),
                    ]
                ),
                as_of=INCIDENT_HOUR,
            ),
            run_id="seed",
        )
        append_snapshot_entries(prices, seed)
        index = root / "quotes.sqlite3"
        if mode == "index":
            _build_index(index, quotes, spans)
        args = [
            "--quotes",
            str(quotes),
            "--prices",
            str(prices),
            "--collector-state",
            str(state),
            "--conflict-ledger",
            str(root / "conflicts.jsonl"),
            "--quote-index",
            str(index),
            "--write",
        ]
        capsys.readouterr()
        assert runner.main(args) == 0
        report = json.loads(capsys.readouterr().out)
        assert (report["quote_index_seek"] == "index_seek") == (mode == "index")
        written[mode] = [
            {k: v for k, v in json.loads(line).items() if k not in ("run_id", "content_hash")}
            for line in prices.read_text().splitlines()
        ]

    assert len(written["index"]) > 2
    assert written["index"] == written["full"]

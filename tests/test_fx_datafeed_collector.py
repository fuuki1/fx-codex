"""資格情報不要の bid/ask 収集器(Dukascopy)のテスト。ネットワーク不要。

OANDA の read-only 資格情報が用意できるまでの代替経路。
OANDA collector と **同一の raw-first 経路**へ流すことを固定し、
将来 provider を差し替えても下流が変わらないことを保証する。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import lzma
from pathlib import Path
import struct

import pytest

from data_platform.collect import dukascopy
from tools import fx_datafeed_collector as collector

HOUR = datetime(2026, 7, 28, 1, 0, tzinfo=UTC)
_TICK = struct.Struct(">IIIff")


def _bi5(ticks: list[tuple[int, int, int, float, float]]) -> bytes:
    """Dukascopy .bi5(LZMA 圧縮された固定長 tick 列)を組み立てる。"""

    body = b"".join(_TICK.pack(*tick) for tick in ticks)
    return lzma.compress(body)


def _payload() -> bytes:
    # (ms, ask_points, bid_points, ask_volume, bid_volume)
    return _bi5(
        [
            (0, 163_749, 163_746, 1.5, 2.0),
            (1_000, 163_752, 163_748, 1.0, 1.2),
        ]
    )


def _fetcher(payload: bytes):
    def fetch(url: str) -> tuple[int, bytes]:
        return 200, payload

    return fetch


def test_collector_writes_real_bid_ask(tmp_path: Path, monkeypatch) -> None:
    """収集した quote が実 bid/ask とスプレッドを保持すること。"""

    monkeypatch.setattr(collector, "requests_fetcher", _fetcher(_payload()))
    monkeypatch.setattr(
        collector,
        "datetime",
        _FrozenDatetime(HOUR + timedelta(hours=2)),
    )

    code = collector.main(
        [
            "--output-root",
            str(tmp_path),
            "--instruments",
            "USDJPY",
        ]
    )
    assert code == collector.EX_OK

    rows = [
        json.loads(line)
        for line in (tmp_path / "log" / "quotes.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert rows, "quote が1件も書かれていない"
    first = rows[0]
    assert first["provider"] == "dukascopy"
    assert first["instrument"] == "USDJPY"
    assert first["bid"] == pytest.approx(163.746)
    assert first["ask"] == pytest.approx(163.749)
    assert first["ask"] > first["bid"], "スプレッドが正でない"
    # 発注可能な live quote ではないことを明示し続ける
    assert first["tradable"] is False
    assert first["account_environment"] == "datafeed"
    assert first["collection_mode"] == "historical_download"


def test_dry_run_requires_no_credentials(tmp_path: Path, capsys) -> None:
    """資格情報なしで計画を出して 0 で終わること(OANDA は 78 で落ちる)。"""

    code = collector.main(["--output-root", str(tmp_path), "--dry-run"])
    assert code == collector.EX_OK
    out = capsys.readouterr().out
    assert '"credentials_required": false' in out
    assert '"provider": "dukascopy"' in out


def test_second_instance_is_refused(tmp_path: Path, monkeypatch) -> None:
    """単一 writer。ロック保持中の二重起動は EX_TEMPFAIL。"""

    from tools.run_exclusive import ExclusiveLock

    held = ExclusiveLock("fx-datafeed-collector", locks_dir=tmp_path / "locks")
    assert held.acquire()
    try:
        monkeypatch.setattr(collector, "requests_fetcher", _fetcher(_payload()))
        code = collector.main(["--output-root", str(tmp_path), "--instruments", "USDJPY"])
        assert code == collector.EX_TEMPFAIL
    finally:
        held.release()


def test_fetch_failure_is_fail_closed(tmp_path: Path, monkeypatch) -> None:
    """取得失敗を握り潰さず EX_UNAVAILABLE を返すこと。"""

    def boom(url: str) -> tuple[int, bytes]:
        raise dukascopy.DukascopyFetchError("HTTP 503")

    monkeypatch.setattr(collector, "requests_fetcher", boom)
    monkeypatch.setattr(collector, "datetime", _FrozenDatetime(HOUR + timedelta(hours=2)))

    code = collector.main(["--output-root", str(tmp_path), "--instruments", "USDJPY"])
    assert code == collector.EX_UNAVAILABLE

    state = json.loads((tmp_path / "state" / "datafeed-collector.json").read_text())
    assert state["results"][0]["status"] == "fetch_failed"


def test_collector_cannot_place_orders() -> None:
    """発注経路を import していないこと(構造的な安全境界)。

    docstring の散文ではなく **実際の import 文** を AST で検査する。
    """

    import ast

    tree = ast.parse(Path(collector.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    for module in imported:
        assert not module.startswith("trader"), f"発注系を import している: {module}"
        assert "executor" not in module, f"executor を import している: {module}"
        assert "order" not in module, f"order 経路を import している: {module}"

    # 収集系は data_platform.collect / raw / tools のみに限定される
    external = {m for m in imported if m.startswith(("data_platform", "tools"))}
    assert external <= {
        "data_platform.collect.dukascopy",
        "data_platform.collect.raw_first",
        "data_platform.raw.immutable_store",
        "tools.run_exclusive",
    }, f"想定外の内部 import: {external}"


class _FrozenDatetime:
    """datetime.now(UTC) だけを固定する薄いスタブ。"""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self, tz=None):  # noqa: ANN001
        return self._now if tz is None else self._now.astimezone(tz)

    def __getattr__(self, name: str):
        return getattr(datetime, name)

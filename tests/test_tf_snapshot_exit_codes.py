"""fx_tf_snapshot.py の終了コードと technicals の一時障害分類のテスト。

核心の回帰: 旧実装は全取得失敗でも return 0 のため、launchd 上は成功に見え、
429 が続いても監視が気づけなかった。ここでは:
- 全時間足・全銘柄が一時障害 → 非zero(EXIT_TRANSIENT_FAILURE)
- 部分成功は取得点を保存するが非zero(coverage incomplete)
- technicals が ScannerError を「一時障害」として分類し空dataと区別する
をネットワーク無しで検証する。
"""

from __future__ import annotations

from unittest import mock

import fx_tf_snapshot
from fx_intel import technicals
from fx_intel.technicals import IntervalView, PairTechnicals
from fx_intel.tv_scanner import ScannerRateLimited


class _FakeAnalysis:
    """tradingview_ta.Analysis の代役(summary/indicators のみ使う)。"""

    def __init__(self, close: float) -> None:
        self.summary = {"RECOMMENDATION": "BUY", "BUY": 5, "SELL": 1, "NEUTRAL": 2}
        self.indicators = {"close": close, "open": close, "high": close + 0.1, "low": close - 0.1}


# ----------------------------------------------- technicals: 一時障害の分類


def test_fetch_records_transient_failure_on_scanner_error() -> None:
    def boom(*_args: object, **_kwargs: object) -> dict:
        raise ScannerRateLimited("429", retry_after=1.0)

    with mock.patch("fx_intel.technicals.get_multiple_analysis", side_effect=boom):
        tech_map, warnings = technicals.fetch_pair_technicals(["USDJPY", "EURUSD"])

    for symbol in ("USDJPY", "EURUSD"):
        # 全時間足が一時失敗として記録される
        assert tech_map[symbol].transient_failures == list(technicals.DEFAULT_INTERVALS)
        assert tech_map[symbol].views == {}
    assert any("一時障害" in w for w in warnings)


def test_empty_data_is_not_a_transient_failure() -> None:
    """取得は成功したが data が空(全銘柄 None)の場合は一時障害に数えない。"""

    def empty(*_args: object, symbols: list[str] | None = None, **_kwargs: object) -> dict:
        # get_multiple_analysis 互換: 全銘柄 None を返す
        return {sym: None for sym in (symbols or [])}

    with mock.patch("fx_intel.technicals.get_multiple_analysis", side_effect=empty):
        tech_map, _ = technicals.fetch_pair_technicals(["USDJPY"])

    assert tech_map["USDJPY"].transient_failures == []
    assert tech_map["USDJPY"].views == {}


def test_partial_interval_success_records_only_failed_intervals() -> None:
    """一部の足だけ一時障害なら、失敗した足だけが transient_failures に入る。"""
    exchange = technicals.DEFAULT_EXCHANGE

    def sometimes(*_args: object, interval: str = "", **_kwargs: object) -> dict:
        if interval == "1h":
            raise ScannerRateLimited("429", retry_after=1.0)
        return {f"{exchange}:USDJPY": _FakeAnalysis(150.0)}

    with mock.patch("fx_intel.technicals.get_multiple_analysis", side_effect=sometimes):
        tech_map, _ = technicals.fetch_pair_technicals(["USDJPY"])

    assert tech_map["USDJPY"].transient_failures == ["1h"]
    # 1h以外の足は取得できている
    assert set(tech_map["USDJPY"].views) == set(technicals.DEFAULT_INTERVALS) - {"1h"}


# ----------------------------------------------- snapshot main の終了コード


def test_had_transient_failure_helper() -> None:
    clean = PairTechnicals(symbol="USDJPY")
    assert fx_tf_snapshot.had_transient_failure({"USDJPY": clean}) is False

    failed = PairTechnicals(symbol="USDJPY", transient_failures=["1h"])
    assert fx_tf_snapshot.had_transient_failure({"USDJPY": failed}) is True


def test_main_returns_transient_exit_code_on_total_failure() -> None:
    def boom(*_args: object, **_kwargs: object) -> dict:
        raise ScannerRateLimited("429", retry_after=1.0)

    with mock.patch("fx_intel.technicals.get_multiple_analysis", side_effect=boom):
        rc = fx_tf_snapshot.main(["--symbols", "USDJPY", "--dry-run"])

    assert rc == fx_tf_snapshot.EXIT_TRANSIENT_FAILURE
    assert rc != 0


def test_main_returns_nonzero_on_partial_success(tmp_path, monkeypatch) -> None:
    """一部の足だけ取れた場合は証拠を保存するが成功扱いにしない。"""
    exchange = technicals.DEFAULT_EXCHANGE
    out = tmp_path / "prices.jsonl"
    monkeypatch.setattr(fx_tf_snapshot, "DEFAULT_TF_PRICES_PATH", out)

    def sometimes(*_args: object, interval: str = "", **_kwargs: object) -> dict:
        if interval in ("4h", "1d"):
            raise ScannerRateLimited("429", retry_after=1.0)
        return {f"{exchange}:USDJPY": _FakeAnalysis(150.0)}

    with mock.patch("fx_intel.technicals.get_multiple_analysis", side_effect=sometimes):
        rc = fx_tf_snapshot.main(["--symbols", "USDJPY"])

    assert rc == fx_tf_snapshot.EXIT_TRANSIENT_FAILURE
    lines = out.read_text(encoding="utf-8").splitlines()
    # 15m と 1h の2点は欠損を隠さず保存する
    assert len(lines) == 2


def test_main_returns_zero_when_all_intervals_succeed(tmp_path, monkeypatch) -> None:
    exchange = technicals.DEFAULT_EXCHANGE
    out = tmp_path / "prices.jsonl"
    monkeypatch.setattr(fx_tf_snapshot, "DEFAULT_TF_PRICES_PATH", out)

    def ok(*_args: object, **_kwargs: object) -> dict:
        return {f"{exchange}:USDJPY": _FakeAnalysis(150.0)}

    with mock.patch("fx_intel.technicals.get_multiple_analysis", side_effect=ok):
        rc = fx_tf_snapshot.main(["--symbols", "USDJPY"])

    assert rc == 0
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(technicals.DEFAULT_INTERVALS)


def test_snapshot_does_not_write_on_total_failure(tmp_path, monkeypatch) -> None:
    """全失敗時はファイルへ何も書かない(古い値を残さない・鮮度は成功まで critical)。"""
    out = tmp_path / "prices.jsonl"
    monkeypatch.setattr(fx_tf_snapshot, "DEFAULT_TF_PRICES_PATH", out)

    def boom(*_args: object, **_kwargs: object) -> dict:
        raise ScannerRateLimited("429", retry_after=1.0)

    with mock.patch("fx_intel.technicals.get_multiple_analysis", side_effect=boom):
        rc = fx_tf_snapshot.main(["--symbols", "USDJPY"])

    assert rc == fx_tf_snapshot.EXIT_TRANSIENT_FAILURE
    assert not out.exists()


# --------------------------------------- IntervalView 既定は transient_failures 無し


def test_pair_technicals_default_has_no_transient_failures() -> None:
    tech = PairTechnicals(symbol="USDJPY")
    tech.views["1h"] = IntervalView("1h", "BUY", 5, 1, 2, close=150.0)
    assert tech.transient_failures == []

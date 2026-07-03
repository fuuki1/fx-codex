"""ParamStore が params_gate を通過した値だけをホットリロードすることの検証。

来歴（provenance）の無い/過剰適合の疑いがあるパラメータがライブシグナルに
流れ込む事故を防ぐゲートが、読み込み側で実際に効いているかを確認する。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import params_gate
from strategy import ParamStore


def _valid_params(**overrides) -> dict:
    params = {
        "fast_window": 15,
        "slow_window": 40,
        "atr_window": 14,
        "atr_multiple": 2.0,
        "updated_at": "2026-07-02T00:00:00+00:00",
        "provenance": {
            "schema": params_gate.SCHEMA_VERSION,
            "generated_by": "auto_optimize.py",
            "data": {
                "path": "/data/real_prices.csv",
                "sha256": "ab" * 32,
                "rows": 5000,
                "start": "2025-01-01 00:00:00",
                "end": "2025-12-31 23:00:00",
            },
            "trade_count": 42,
            "warnings": [],
        },
    }
    params.update(overrides)
    return params


def _write(path: Path, params: dict) -> None:
    path.write_text(json.dumps(params, ensure_ascii=False), encoding="utf-8")


def test_missing_file_returns_none(tmp_path: Path) -> None:
    # 検証済みパラメータが一度も無い → None（呼び出し側は発注しない）
    store = ParamStore(str(tmp_path / "absent.json"))
    assert store.get() is None


def test_valid_params_are_loaded(tmp_path: Path) -> None:
    path = tmp_path / "strategy_params.json"
    _write(path, _valid_params())
    store = ParamStore(str(path))
    loaded = store.get()
    assert loaded["fast_window"] == 15
    assert loaded["slow_window"] == 40
    assert loaded["atr_multiple"] == 2.0


def test_legacy_params_without_provenance_are_rejected(tmp_path: Path) -> None:
    # リポジトリ現行の strategy_params.json と同じ形（provenance 無し）
    legacy = {
        "fast_window": 20,
        "slow_window": 100,
        "atr_window": 14,
        "atr_multiple": 2.5,
        "score": 140911.9994,
        "updated_at": "2026-06-30T07:46:47.297995+00:00",
    }
    path = tmp_path / "strategy_params.json"
    _write(path, legacy)
    store = ParamStore(str(path))
    # 不合格かつ合格実績なし → None（DEFAULT では発注しない）
    assert store.get() is None


def test_rejected_reload_keeps_last_good(tmp_path: Path) -> None:
    path = tmp_path / "strategy_params.json"
    _write(path, _valid_params(fast_window=12))
    store = ParamStore(str(path))
    assert store.get()["fast_window"] == 12

    # 汚染された更新（provenance 削除）が来ても直近合格値を維持する
    bad = _valid_params(fast_window=99)
    del bad["provenance"]
    _write(path, bad)
    # mtime を確実に進める
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 10))

    loaded = store.get()
    assert loaded["fast_window"] == 12  # 99 は採用されない


def test_out_of_bounds_params_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "strategy_params.json"
    _write(path, _valid_params(atr_multiple=50.0))
    store = ParamStore(str(path))
    # 境界外かつ合格実績なし → None
    assert store.get() is None


def test_unavailable_is_logged_once_per_mtime(tmp_path: Path, monkeypatch) -> None:
    import common

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(common, "log_event", lambda kind, payload: events.append((kind, payload)))

    path = tmp_path / "strategy_params.json"
    _write(path, {"fast_window": 20, "slow_window": 100})  # provenance 無し・合格実績なし
    store = ParamStore(str(path))

    assert store.get() is None
    assert store.get() is None
    assert store.get() is None
    # 合格実績が無いので params_unavailable が、同一 mtime につき一度だけ記録される
    unavailable = [e for e in events if e[0] == "params_unavailable"]
    assert len(unavailable) == 1
    assert unavailable[0][1]["errors"]


def test_rejected_after_valid_records_params_rejected(tmp_path: Path, monkeypatch) -> None:
    import common

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(common, "log_event", lambda kind, payload: events.append((kind, payload)))

    path = tmp_path / "strategy_params.json"
    _write(path, _valid_params(fast_window=12))
    store = ParamStore(str(path))
    assert store.get()["fast_window"] == 12  # 一度合格

    # 汚染更新 → 直近合格値を維持し、params_rejected（not unavailable）を記録
    bad = _valid_params(fast_window=99)
    del bad["provenance"]
    _write(path, bad)
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 10))

    assert store.get()["fast_window"] == 12
    assert any(e[0] == "params_rejected" for e in events)
    assert not any(e[0] == "params_unavailable" for e in events)


def test_rejected_reload_does_not_revalidate_every_call(tmp_path: Path, monkeypatch) -> None:
    # 指摘5: 拒否ファイルが居座っても、mtime 据え置きで毎ループ再検証しない
    import strategy as strategy_mod

    calls = {"n": 0}
    real = strategy_mod.load_validated_params

    def counting(path, **kw):
        calls["n"] += 1
        return real(path, **kw)

    monkeypatch.setattr(strategy_mod, "load_validated_params", counting)

    path = tmp_path / "strategy_params.json"
    _write(path, {"fast_window": 20, "slow_window": 100})  # 不合格
    store = ParamStore(str(path))

    for _ in range(5):
        assert store.get() is None
    # 同一 mtime のままなら検証は初回の一度きり
    assert calls["n"] == 1


def test_valid_then_deleted_keeps_last_good_and_records_missing(
    tmp_path: Path, monkeypatch
) -> None:
    import common

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(common, "log_event", lambda kind, payload: events.append((kind, payload)))

    path = tmp_path / "strategy_params.json"
    _write(path, _valid_params(fast_window=17))
    store = ParamStore(str(path))
    assert store.get()["fast_window"] == 17  # 一度合格

    # ファイル削除 → 直近合格値を維持しつつ、無音ではなく params_missing を記録
    path.unlink()
    assert store.get()["fast_window"] == 17

    missing = [e for e in events if e[0] == "params_missing"]
    assert len(missing) == 1
    assert missing[0][1]["errors"]
    # 一度も合格していないわけではないので unavailable は出ない
    assert not any(e[0] == "params_unavailable" for e in events)


def test_missing_after_valid_records_once(tmp_path: Path, monkeypatch) -> None:
    import common

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(common, "log_event", lambda kind, payload: events.append((kind, payload)))

    path = tmp_path / "strategy_params.json"
    _write(path, _valid_params(fast_window=17))
    store = ParamStore(str(path))
    store.get()

    path.unlink()
    # 削除が続く間、複数回 get() しても params_missing は一度だけ（アラート連投防止）
    for _ in range(5):
        assert store.get()["fast_window"] == 17
    assert len([e for e in events if e[0] == "params_missing"]) == 1


def test_deleted_then_restored_reloads(tmp_path: Path) -> None:
    # valid → 削除 → 別の valid で復活 したら新しい値を読み込める（回復パス）
    path = tmp_path / "strategy_params.json"
    _write(path, _valid_params(fast_window=13))
    store = ParamStore(str(path))
    assert store.get()["fast_window"] == 13

    path.unlink()
    assert store.get()["fast_window"] == 13  # 削除中は直近合格値を維持

    _write(path, _valid_params(fast_window=21))
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 10))
    assert store.get()["fast_window"] == 21  # 復活後は新しい合格値を反映

"""fx_intel.learning(判断ジャーナルからの自己学習)のテスト。ネットワーク不要。"""

from __future__ import annotations

from datetime import datetime, timedelta, UTC
from itertools import count

import pytest

from fx_intel import briefing, learning
from fx_intel.journal import append_plans, read_entries
from fx_intel.sentiment import CurrencySentiment
from fx_intel.technicals import PairTechnicals, build_interval_view

# 2026-06-30は火曜。24時間後の水曜まで週末を跨がない
NOW = datetime(2026, 6, 30, 10, 0, tzinfo=UTC)
DAY = timedelta(hours=24)

# call()ヘルパーの時刻を1時間ずつ進める(derive_profileの間引きで消えないように)
_CALL_SEQ = count()


def _label(spec: learning.FeatureSpec, value: float) -> str:
    """bucket_for が値をバケットに割り当てられることを確かめてラベルを返す。"""
    bucket = learning.bucket_for(spec, value)
    assert bucket is not None, f"{spec.key}={value} がどのバケットにも入らない"
    return bucket.label_ja


def entry(
    ts: datetime,
    symbol: str = "USDJPY",
    direction: str = "long",
    close: float = 100.0,
    atr: float = 1.0,
    conviction: int = 50,
    tech: float = 0.5,
    news: float = 0.2,
    features: dict | None = None,
) -> dict:
    """journal.append_plansが書くのと同じ形のジャーナル行。"""
    return {
        "ts": ts.isoformat(),
        "symbol": symbol,
        "direction": direction,
        "conviction": conviction,
        "composite": 0.4,
        "tech_score": tech,
        "news_score": news,
        "close": close,
        "atr": atr,
        "data_quality": 0.9,
        "features": features or {},
    }


def call(
    symbol: str = "USDJPY",
    direction: str = "long",
    outcome: str = "hit",
    conviction: int = 50,
    tech: float = 0.5,
    news: float = 0.2,
    features: dict | None = None,
    ts: datetime | None = None,
    data_quality: float | None = None,
) -> learning.EvaluatedCall:
    return learning.EvaluatedCall(
        symbol=symbol,
        direction=direction,
        conviction=conviction,
        tech_score=tech,
        news_score=news,
        outcome=outcome,
        ts=(ts or (NOW + timedelta(hours=next(_CALL_SEQ)))).isoformat(),
        features=features or {},
        data_quality=data_quality,
    )


# ---------------------------------------------------------- evaluate_history


def test_evaluate_history_scores_hits_misses_and_flat() -> None:
    entries = [
        entry(NOW, symbol="USDJPY", direction="long", close=100.0),
        entry(NOW, symbol="EURUSD", direction="short", close=100.0),
        entry(NOW, symbol="GBPJPY", direction="long", close=100.0),
        # 24時間後の価格ポイント(方向なしのneutral判断でも終値は学習に使える)
        entry(NOW + DAY, symbol="USDJPY", direction="neutral", close=101.0),
        entry(NOW + DAY, symbol="EURUSD", direction="neutral", close=101.0),
        entry(NOW + DAY, symbol="GBPJPY", direction="neutral", close=100.05),
        # 将来価格がまだ無い判断(未成熟)は採点されない
        entry(NOW + DAY - timedelta(hours=1), symbol="AUDUSD", direction="long", close=1.0),
    ]
    calls = learning.evaluate_history(entries)
    outcomes = {c.symbol: c.outcome for c in calls}
    assert outcomes == {"USDJPY": "hit", "EURUSD": "miss", "GBPJPY": "flat"}


def test_evaluate_history_picks_price_closest_to_horizon() -> None:
    entries = [
        entry(NOW, direction="long", close=100.0),
        # 22.5時間後(ずれ1.5h)は下落、24.2時間後(ずれ0.2h)は上昇
        entry(NOW + timedelta(hours=22.5), direction="neutral", close=99.0),
        entry(NOW + timedelta(hours=24.2), direction="neutral", close=101.0),
    ]
    calls = learning.evaluate_history(entries)
    assert len(calls) == 1
    assert calls[0].outcome == "hit"


def test_evaluate_history_counts_open_hours_across_weekend() -> None:
    """金曜の判断は週末クローズを除いた24時間後(=月曜)の価格で採点される。"""
    friday = datetime(2026, 6, 26, 10, 0, tzinfo=UTC)
    # 金10:00→金21:00で11時間 + 日22:00→月11:00で13時間 = オープン24時間
    monday = datetime(2026, 6, 29, 11, 0, tzinfo=UTC)
    assert (monday - friday) > timedelta(hours=70)  # 壁時計では窓外
    entries = [
        entry(friday, direction="long", close=100.0),
        entry(monday, direction="neutral", close=101.0),
    ]
    calls = learning.evaluate_history(entries)
    assert len(calls) == 1
    assert calls[0].outcome == "hit"


def test_evaluate_history_skips_malformed_entries() -> None:
    entries: list[dict] = [
        {"ts": "not-a-date", "symbol": "USDJPY", "direction": "long", "close": 100.0},
        {"ts": NOW.isoformat(), "symbol": "USDJPY", "direction": "long", "close": None},
        entry(NOW + DAY, direction="neutral", close=101.0),
    ]
    assert learning.evaluate_history(entries) == []


def test_evaluate_history_reads_real_journal_format(tmp_path) -> None:
    """journal.append_plansの実出力をそのまま学習の入力にできる。"""
    path = tmp_path / "journal.jsonl"
    plan = briefing.TradePlan(
        symbol="USDJPY",
        direction="long",
        conviction=60,
        composite=0.5,
        tech_score=0.8,
        news_score=0.2,
        close=150.0,
        atr=0.5,
    )
    later = briefing.TradePlan(
        symbol="USDJPY",
        direction="neutral",
        conviction=0,
        composite=0.0,
        tech_score=0.0,
        news_score=0.0,
        close=151.0,
        atr=0.5,
    )
    append_plans(path, [plan], now=NOW)
    append_plans(path, [later], now=NOW + DAY)
    calls = learning.evaluate_history(read_entries(path))
    assert len(calls) == 1
    assert calls[0].outcome == "hit"
    assert calls[0].conviction == 60


# ------------------------------------------------------------- calibration


def test_calibration_bins_group_by_conviction() -> None:
    calls = [
        call(conviction=10, outcome="hit"),
        call(conviction=30, outcome="miss"),
        call(conviction=40, outcome="hit"),
        call(conviction=60, outcome="hit"),
        call(conviction=80, outcome="miss"),
        call(conviction=90, outcome="flat"),  # flatは集計から除外
    ]
    bins = {(b.low, b.high): b for b in learning.calibration_bins(calls)}
    assert bins[(0, 25)].evaluated == 1 and bins[(0, 25)].hits == 1
    assert bins[(25, 50)].evaluated == 2 and bins[(25, 50)].hits == 1
    assert bins[(50, 75)].evaluated == 1 and bins[(50, 75)].hits == 1
    assert bins[(75, 101)].evaluated == 1 and bins[(75, 101)].hits == 0
    assert bins[(75, 101)].hit_rate == 0.0


# ---------------------------------------------------------- derive_profile


def test_derive_profile_keeps_default_weights_when_samples_scarce() -> None:
    calls = [call(outcome="hit", tech=0.5, news=-0.3) for _ in range(10)]
    profile = learning.derive_profile(calls, now=NOW)
    assert profile.tech_weight == briefing.TECH_WEIGHT
    assert profile.news_weight == briefing.NEWS_WEIGHT
    assert any("既定" in note for note in profile.notes_ja)


def test_derive_profile_shifts_weight_toward_accurate_signal() -> None:
    # テクニカルは常に正解方向、ニュースは常に逆方向 × 30件
    calls = [call(outcome="hit", tech=0.5, news=-0.3) for _ in range(30)]
    profile = learning.derive_profile(calls, now=NOW)
    assert profile.tech_hit_rate == 1.0
    assert profile.news_hit_rate == 0.0
    # raw=1.0へシュリンク(30/70)で寄せて 0.743 → 上限0.70にクランプ
    assert profile.tech_weight == learning.TECH_WEIGHT_MAX
    assert profile.news_weight == pytest.approx(1.0 - learning.TECH_WEIGHT_MAX)
    assert any("自動調整" in note for note in profile.notes_ja)


def test_derive_profile_keeps_defaults_when_both_signals_fail() -> None:
    # 両方とも判断方向どおり(=外れた方向)を指していた: どちらも的中率0%
    calls = [call(outcome="miss", tech=0.5, news=0.3) for _ in range(30)]
    profile = learning.derive_profile(calls, now=NOW)
    assert profile.tech_weight == briefing.TECH_WEIGHT
    assert profile.news_weight == briefing.NEWS_WEIGHT
    # サンプルは十分あるので「サンプル不足」ではなく「差がない」と説明する
    assert any("差がない" in note for note in profile.notes_ja)


def test_derive_profile_damps_poorly_performing_symbol() -> None:
    calls = [call(symbol="EURUSD", outcome="hit" if i < 3 else "miss") for i in range(10)] + [
        call(symbol="USDJPY", outcome="hit" if i < 6 else "miss") for i in range(10)
    ]
    profile = learning.derive_profile(calls, now=NOW)
    # EURUSD: 的中30% → 0.3/0.5=0.6に減衰。USDJPY: 60%なので減衰しない
    assert profile.conviction_factor("EURUSD") == 0.6
    assert profile.conviction_factor("USDJPY") == 1.0
    assert any("EURUSD" in note and "減衰" in note for note in profile.notes_ja)


def test_derive_profile_symbol_needs_min_samples() -> None:
    calls = [call(symbol="EURUSD", outcome="miss") for _ in range(7)]
    profile = learning.derive_profile(calls, now=NOW)
    assert profile.conviction_factor("EURUSD") == 1.0


def test_derive_profile_symbol_dead_zone_near_coin_flip() -> None:
    """的中率48%のような誤差範囲では減衰させない(トリガーは45%未満)。"""
    calls = [call(symbol="EURUSD", outcome="hit" if i < 12 else "miss") for i in range(25)]
    profile = learning.derive_profile(calls, now=NOW)
    assert profile.conviction_factor("EURUSD") == 1.0


def test_summary_ja_when_no_data() -> None:
    profile = learning.derive_profile([], now=NOW)
    assert "学習データ蓄積中" in profile.summary_ja()


# --------------------------------------------- 記録間隔非依存の間引き


def test_derive_profile_thins_high_frequency_duplicates() -> None:
    """5分間隔でほぼ同じ判断が30件並んでも、実効サンプルは1時間1件に間引く。

    Mac miniの5分間隔運用でサンプル数ガードが12倍緩くならないための検証。
    30件×5分=145分間 → 0分/60分/120分の3件だけが学習サンプルになる。
    """
    calls = [call(outcome="hit", ts=NOW + timedelta(minutes=5 * i)) for i in range(30)]
    profile = learning.derive_profile(calls, now=NOW)
    assert profile.evaluated == 3


def test_thin_calls_keeps_symbols_independent() -> None:
    """間引きはペアごとに独立(同時刻の別ペアは両方残る)。"""
    calls = [
        call(symbol="USDJPY", ts=NOW),
        call(symbol="EURUSD", ts=NOW),
        call(symbol="USDJPY", ts=NOW + timedelta(minutes=30)),  # 1時間未満 → 落ちる
        call(symbol="USDJPY", ts=NOW + timedelta(hours=1)),  # ちょうど1時間 → 残る
    ]
    thinned = learning.thin_calls(calls, 1.0)
    assert len(thinned) == 3


# --------------------------------------------- マルチホライズン観測


def test_horizon_report_scores_each_horizon() -> None:
    """4h/24h/72hのホライズン別に同じ判断が独立採点される。"""
    entries = [
        entry(NOW, direction="long", close=100.0),
        # 4h後は上昇(hit)、24h後も上昇(hit)、72h後は下落(miss)
        entry(NOW + timedelta(hours=4), direction="neutral", close=101.0),
        entry(NOW + DAY, direction="neutral", close=101.5),
        entry(NOW + timedelta(hours=72), direction="neutral", close=99.0),
    ]
    report = learning.horizon_report_ja(entries)
    assert "4h 100%(n=1)" in report
    assert "24h 100%(n=1)" in report
    assert "72h 0%(n=1)" in report
    assert "学習には24hのみ使用" in report


def test_horizon_report_empty_without_mature_calls() -> None:
    assert learning.horizon_report_ja([]) == ""
    assert learning.horizon_report_ja([entry(NOW, direction="long", close=100.0)]) == ""


# --------------------------------------------- 反省レポート(失敗理由の分類)


def test_reflection_report_flags_htf_against_pattern() -> None:
    """上位足逆行での判断が全体より目立って外れていれば反省レポートに載る。"""
    # 上位足(4h)売り寄りでのロング12件は2勝10敗、順行の20件は16勝4敗
    calls = [
        call(outcome="hit" if i < 2 else "miss", features={"rating_4h": -1.0}) for i in range(12)
    ] + [call(outcome="hit" if i < 16 else "miss", features={"rating_4h": 1.0}) for i in range(20)]
    notes = learning.reflection_report_ja(calls)
    assert notes and "反省レポート" in notes[0]
    assert any("上位足(4h)逆行" in n and "17%" in n and "12件" in n for n in notes)


def test_reflection_report_needs_min_samples() -> None:
    """該当10件未満の条件は報告しない(偶然のパターンを反省しない)。"""
    calls = [call(outcome="miss", features={"rating_4h": -1.0}) for _ in range(9)] + [
        call(outcome="hit") for _ in range(20)
    ]
    assert learning.reflection_report_ja(calls) == []


def test_reflection_report_silent_when_no_notable_pattern() -> None:
    """どの条件も全体並みの成績なら何も報告しない。"""
    calls = [
        call(outcome="hit" if i % 2 == 0 else "miss", features={"rating_4h": -1.0})
        for i in range(20)
    ]
    assert learning.reflection_report_ja(calls) == []


def test_reflection_report_classifies_multiple_patterns() -> None:
    """RSI極端圏追随・対立押し切り・レンジ判断も分類できる(悪い順に最大3件)。"""
    calls = (
        # RSI買われすぎ圏ロング: 1勝11敗
        [call(outcome="hit" if i < 1 else "miss", features={"rsi_1h": 70.0}) for i in range(12)]
        # テクニカル/ニュース対立: 2勝10敗
        + [call(outcome="hit" if i < 2 else "miss", tech=0.5, news=-0.4) for i in range(12)]
        # レンジ相場(ADX15): 3勝9敗
        + [call(outcome="hit" if i < 3 else "miss", features={"adx_1h": 15.0}) for i in range(12)]
        # 全体を引き上げる普通の判断
        + [call(outcome="hit") for _ in range(30)]
    )
    notes = learning.reflection_report_ja(calls)
    assert "反省レポート" in notes[0]
    body = "\n".join(notes)
    assert "RSI極端圏への追随" in body
    assert "対立を押し切った" in body
    assert "レンジ相場" in body
    assert len(notes) == 1 + learning.REFLECTION_MAX_ITEMS  # 見出し+最大3条件


def test_derive_profile_includes_reflection_notes() -> None:
    """derive_profileの学習メモにも反省レポートが合流する。"""
    calls = [
        call(outcome="hit" if i < 2 else "miss", features={"rating_1d": -1.0}) for i in range(12)
    ] + [call(outcome="hit", features={"rating_1d": 1.0}) for _ in range(20)]
    profile = learning.derive_profile(calls, now=NOW)
    assert any("反省レポート" in n for n in profile.notes_ja)
    assert any("上位足(1d)逆行" in n for n in profile.notes_ja)


# --------------------------------------------- 確信度Brier


def test_conviction_brier_rewards_informative_conviction() -> None:
    """高確信=的中/低確信=外れが揃っていればBrierが基準を下回る。"""
    calls = [call(conviction=80, outcome="hit") for _ in range(20)] + [
        call(conviction=20, outcome="miss") for _ in range(10)
    ]
    profile = learning.derive_profile(calls, now=NOW)
    assert profile.conviction_brier == pytest.approx(0.04)
    assert profile.conviction_brier is not None
    assert profile.conviction_brier_base is not None
    assert profile.conviction_brier < profile.conviction_brier_base
    assert any("Brier" in n and "情報を持っている" in n for n in profile.notes_ja)


def test_conviction_brier_flags_miscalibration() -> None:
    """確信度が的中と無関係ならBrierは基準を上回り、乖離として報告される。"""
    calls = [call(conviction=90, outcome="miss") for _ in range(15)] + [
        call(conviction=10, outcome="hit") for _ in range(15)
    ]
    profile = learning.derive_profile(calls, now=NOW)
    assert profile.conviction_brier is not None
    assert profile.conviction_brier_base is not None
    assert profile.conviction_brier > profile.conviction_brier_base
    assert any("乖離" in n for n in profile.notes_ja)


def test_conviction_brier_hidden_when_samples_scarce() -> None:
    calls = [call(conviction=80, outcome="hit") for _ in range(10)]
    profile = learning.derive_profile(calls, now=NOW)
    assert profile.conviction_brier is not None  # 値は常に計算する
    assert not any("Brier" in n for n in profile.notes_ja)  # 表示は20件から


# ------------------------------------------------- チャート状態別の学習


def test_bucket_for_maps_values_to_named_states() -> None:
    specs = {spec.key: spec for spec in learning.FEATURE_SPECS}
    assert _label(specs["rsi_1h"], 20.0) == "売られすぎ圏(35未満)"
    assert _label(specs["rsi_1h"], 50.0) == "中立圏(35-65)"
    assert _label(specs["rsi_1h"], 80.0) == "買われすぎ圏(65超)"
    # MA乖離は符号(向き)を無視して大きさで分類する
    assert _label(specs["ma_gap_atr"], -3.0) == "大(2以上)"
    assert _label(specs["tf_agreement"], 1.0) == "全時間足一致"
    assert learning.bucket_for(specs["tf_agreement"], -0.1) is None


def test_evaluate_history_carries_features() -> None:
    entries = [
        entry(NOW, direction="long", close=100.0, features={"rsi_1h": 72.0, "memo": "文字列"}),
        entry(NOW + DAY, direction="neutral", close=101.0),
    ]
    calls = learning.evaluate_history(entries)
    assert len(calls) == 1
    # 数値だけが特徴量として引き継がれる(文字列などは無視)
    assert calls[0].features == {"rsi_1h": 72.0}


def test_derive_profile_learns_condition_hit_rates() -> None:
    # RSI買われすぎ圏では20件中4件しか当たらず、中立圏では20件中16件当たる世界
    calls = [
        call(outcome="hit" if i < 4 else "miss", features={"rsi_1h": 80.0}) for i in range(20)
    ] + [call(outcome="hit" if i < 16 else "miss", features={"rsi_1h": 50.0}) for i in range(20)]
    profile = learning.derive_profile(calls, now=NOW)
    overheated = profile.condition_stats["rsi_1h"]["買われすぎ圏(65超)"]["long"]
    assert overheated == {"evaluated": 20, "hits": 4}
    # 的中率20% → 0.2/0.5=0.4だが下限0.7でクランプ
    assert profile.condition_factors["rsi_1h"]["買われすぎ圏(65超)"]["long"] == 0.7
    # 中立圏(80%)は減衰対象にならない
    assert "中立圏(35-65)" not in profile.condition_factors.get("rsi_1h", {})
    assert any("当たりやすいチャート状態" in n and "中立圏" in n for n in profile.notes_ja)
    assert any(
        "苦手なチャート状態" in n and "買われすぎ圏" in n and "×0.70" in n for n in profile.notes_ja
    )


def test_condition_needs_min_samples() -> None:
    calls = [call(outcome="miss", features={"rsi_1h": 80.0}) for _ in range(11)]
    profile = learning.derive_profile(calls, now=NOW)
    assert profile.condition_factors == {}


def test_condition_adjustment_picks_worst_matching_condition() -> None:
    # 買われすぎ圏(的中20%→×0.7)とニュース僅少(的中40%→×0.8)の両方が苦手
    calls = [
        call(outcome="hit" if i < 4 else "miss", features={"rsi_1h": 80.0}) for i in range(20)
    ] + [call(outcome="hit" if i < 8 else "miss", features={"news_count": 0.0}) for i in range(20)]
    profile = learning.derive_profile(calls, now=NOW)
    # 両方に該当 → 最悪の1条件(×0.7)だけを適用し、掛け合わせない
    factor, reason = profile.condition_adjustment({"rsi_1h": 70.0, "news_count": 1.0}, "long")
    assert factor == 0.7
    assert "買われすぎ圏" in reason
    # 片方だけ該当
    factor, reason = profile.condition_adjustment({"rsi_1h": 50.0, "news_count": 0.0}, "long")
    assert factor == 0.8
    assert "関連ニュース量" in reason
    # どの苦手状態にも該当しない
    assert profile.condition_adjustment({"rsi_1h": 50.0, "news_count": 3.0}, "long") == (1.0, "")


def test_condition_learning_separates_directions() -> None:
    """同じチャート状態でもロング/ショートの成績は別々に学習する。

    RSI買われすぎ圏: ロングは20件中4件(20%)しか当たらないが、
    ショートは20件中14件(70%)当たる世界。ロングだけ減衰する。
    """
    calls = [
        call(direction="long", outcome="hit" if i < 4 else "miss", features={"rsi_1h": 80.0})
        for i in range(20)
    ] + [
        call(direction="short", outcome="hit" if i < 14 else "miss", features={"rsi_1h": 80.0})
        for i in range(20)
    ]
    profile = learning.derive_profile(calls, now=NOW)
    bucket = profile.condition_stats["rsi_1h"]["買われすぎ圏(65超)"]
    assert bucket["long"] == {"evaluated": 20, "hits": 4}
    assert bucket["short"] == {"evaluated": 20, "hits": 14}
    # 減衰係数はロング側のセルにだけ付く
    assert profile.condition_factors["rsi_1h"]["買われすぎ圏(65超)"] == {"long": 0.7}
    factor, reason = profile.condition_adjustment({"rsi_1h": 78.0}, "long")
    assert factor == 0.7
    assert "ロング" in reason
    assert profile.condition_adjustment({"rsi_1h": 78.0}, "short") == (1.0, "")
    # 方向なしの照合は常に無調整
    assert profile.condition_adjustment({"rsi_1h": 78.0}, "neutral") == (1.0, "")
    # 学習メモにも方向付きで載る
    assert any("買われすぎ圏" in n and "ロング" in n and "×0.70" in n for n in profile.notes_ja)
    assert any("買われすぎ圏" in n and "ショート" in n for n in profile.notes_ja)


# ------------------------------------------------------------- persistence


def test_profile_save_and_load_roundtrip(tmp_path) -> None:
    calls = (
        [call(outcome="hit", tech=0.5, news=-0.3) for _ in range(30)]
        + [call(symbol="EURUSD", outcome="miss", conviction=80) for _ in range(10)]
        + [call(outcome="hit" if i < 4 else "miss", features={"rsi_1h": 80.0}) for i in range(20)]
    )
    profile = learning.derive_profile(calls, now=NOW)
    path = tmp_path / "learning.json"
    learning.save_profile(profile, path)
    loaded = learning.load_profile(path)
    assert loaded.tech_weight == profile.tech_weight
    assert loaded.news_weight == profile.news_weight
    assert loaded.symbol_factors == profile.symbol_factors
    assert loaded.evaluated == profile.evaluated
    assert loaded.bins == profile.bins
    assert loaded.conviction_brier == profile.conviction_brier
    assert loaded.conviction_brier_base == profile.conviction_brier_base
    assert loaded.condition_stats == profile.condition_stats
    assert loaded.condition_factors == profile.condition_factors
    assert loaded.expectancy == profile.expectancy
    assert loaded.notes_ja == profile.notes_ja
    # 復元したプロファイルでも状態別の減衰がそのまま機能する
    assert (
        loaded.condition_adjustment({"rsi_1h": 80.0}, "long")[0]
        == profile.condition_adjustment({"rsi_1h": 80.0}, "long")[0]
    )


def test_profile_save_and_load_preserves_expectancy_summary(tmp_path) -> None:
    expectancy = {
        "overall": {
            "evaluated": 25,
            "tradable": 25,
            "min_samples": 20,
            "sample_ok": True,
            "expectancy_r": -0.1,
        }
    }
    profile = learning.derive_profile([], now=NOW, expectancy_summary=expectancy)
    path = tmp_path / "learning_expectancy.json"

    learning.save_profile(profile, path)
    loaded = learning.load_profile(path)

    assert loaded.expectancy == expectancy
    assert any("期待R" in note for note in loaded.notes_ja)


def test_expectancy_adjustment_blocks_negative_expectancy() -> None:
    profile = learning.LearnedProfile(
        expectancy={
            "by_symbol": {
                "USDJPY": {
                    "tradable": 20,
                    "min_samples": 20,
                    "sample_ok": True,
                    "expectancy_r": -0.2,
                    "profit_factor_r": 0.8,
                }
            }
        }
    )

    factor, reason = profile.expectancy_adjustment("USDJPY", "long")

    assert factor == learning.EXPECTANCY_BLOCK_FACTOR
    assert "期待R" in reason and "非正" in reason
    assert profile.expectancy_adjustment("USDJPY", "neutral") == (1.0, "")


def test_expectancy_adjustment_marks_sample_guard() -> None:
    profile = learning.LearnedProfile(
        expectancy={
            "by_direction": {
                "long": {
                    "tradable": 6,
                    "min_samples": 20,
                    "sample_ok": False,
                    "expectancy_r": 0.3,
                }
            }
        }
    )

    factor, reason = profile.expectancy_adjustment("EURUSD", "long")

    assert factor == learning.EXPECTANCY_WEAK_FACTOR
    assert "期待値サンプル不足" in reason


def test_load_profile_missing_or_corrupt_returns_default(tmp_path) -> None:
    assert learning.load_profile(tmp_path / "nope.json").tech_weight == briefing.TECH_WEIGHT
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    assert learning.load_profile(corrupt).tech_weight == briefing.TECH_WEIGHT


def test_load_profile_rejects_out_of_range_weight(tmp_path) -> None:
    path = tmp_path / "learning.json"
    path.write_text('{"tech_weight": 0.9, "news_weight": 0.1}', encoding="utf-8")
    loaded = learning.load_profile(path)
    assert loaded.tech_weight == briefing.TECH_WEIGHT
    assert loaded.news_weight == briefing.NEWS_WEIGHT


# ------------------------------------------------- briefingへの学習の反映


def make_view(interval: str, rec: str):
    summary = {"RECOMMENDATION": rec, "BUY": 10, "SELL": 5, "NEUTRAL": 11}
    indicators = {
        "close": 150.0,
        "RSI": 55.0,
        "ATR": 0.5,
        "SMA20": 150.5,
        "SMA100": 149.0,
    }
    return build_interval_view(interval, summary, indicators, 20, 100)


def bullish_tech(symbol: str = "USDJPY") -> PairTechnicals:
    tech = PairTechnicals(symbol=symbol)
    tech.views = {
        "15m": make_view("15m", "BUY"),
        "1h": make_view("1h", "STRONG_BUY"),
        "4h": make_view("4h", "STRONG_BUY"),
        "1d": make_view("1d", "BUY"),
    }
    return tech


CURRENCIES = {
    "USD": CurrencySentiment("USD", score=0.5),
    "JPY": CurrencySentiment("JPY", score=-0.3),
}


def test_build_trade_plan_uses_learned_weights() -> None:
    plan = briefing.build_trade_plan(
        "USDJPY",
        bullish_tech(),
        CURRENCIES,
        [],
        [],
        now=NOW,
        tech_weight=0.70,
        news_weight=0.30,
    )
    assert plan.tech_weight == 0.70
    assert plan.news_weight == 0.30
    assert plan.composite == round(0.70 * plan.tech_score + 0.30 * plan.news_score, 3)


def test_build_trade_plan_conviction_factor_damps_and_warns() -> None:
    baseline = briefing.build_trade_plan("USDJPY", bullish_tech(), CURRENCIES, [], [], now=NOW)
    damped = briefing.build_trade_plan(
        "USDJPY", bullish_tech(), CURRENCIES, [], [], now=NOW, conviction_factor=0.6
    )
    assert damped.direction == baseline.direction  # 方向判断は変えない
    assert damped.conviction == round(baseline.conviction * 0.6)
    assert any("学習調整" in w for w in damped.warnings)
    assert not any("学習調整" in w for w in baseline.warnings)


def test_plan_embed_shows_actual_weights() -> None:
    plan = briefing.build_trade_plan(
        "USDJPY",
        bullish_tech(),
        CURRENCIES,
        [],
        [],
        now=NOW,
        tech_weight=0.70,
        news_weight=0.30,
    )
    embed = briefing._plan_embed(plan, 20, 100)
    judgement = next(f for f in embed["fields"] if f["name"] == "判断")
    assert "(70%)" in judgement["value"]
    assert "(30%)" in judgement["value"]


def test_agreement_ratio() -> None:
    tech = bullish_tech()
    assert tech.agreement_ratio() == 1.0  # 4時間足すべて買い方向
    tech.views["1d"] = make_view("1d", "SELL")
    assert tech.agreement_ratio() == 0.75  # 3/4が全体の向きと一致
    neutral = PairTechnicals(symbol="USDJPY")
    neutral.views = {"1h": make_view("1h", "NEUTRAL")}
    assert neutral.agreement_ratio() is None  # 全体が中立なら判定不能
    assert PairTechnicals(symbol="EURUSD").agreement_ratio() is None


def test_build_trade_plan_records_chart_features() -> None:
    plan = briefing.build_trade_plan("USDJPY", bullish_tech(), CURRENCIES, [], [], now=NOW)
    # make_view: close150 / RSI55 / ATR0.5 / SMA20=150.5 / SMA100=149
    assert plan.features["rsi_1h"] == 55.0
    assert plan.features["ma_gap_atr"] == pytest.approx(3.0)  # (150.5-149)/0.5
    assert plan.features["atr_pct"] == pytest.approx(0.3333, abs=1e-4)  # 0.5/150*100
    assert plan.features["tf_agreement"] == 1.0
    assert plan.features["news_count"] == 0.0
    # 上位足レーティング: 4h=STRONG_BUY(+1.0) / 1d=BUY(+0.5)
    assert plan.features["rating_4h"] == 1.0
    assert plan.features["rating_1d"] == 0.5
    assert "adx_1h" not in plan.features  # 取得できなかった指標は記録しない


def test_bucket_for_maps_htf_ratings() -> None:
    specs = {spec.key: spec for spec in learning.FEATURE_SPECS}
    assert _label(specs["rating_4h"], -0.5) == "売り寄り(-0.25未満)"
    assert _label(specs["rating_4h"], 0.0) == "中立(±0.25)"
    assert _label(specs["rating_1d"], 1.0) == "買い寄り(+0.25以上)"


def test_build_trade_plan_warns_when_atr_missing() -> None:
    """方向判断が出ているのにATRが無い場合は警告する(SL/TP・学習の欠陥データ)。"""
    tech = PairTechnicals(symbol="USDJPY")
    summary = {"RECOMMENDATION": "STRONG_BUY", "BUY": 20, "SELL": 2, "NEUTRAL": 4}
    indicators = {"close": 150.0, "RSI": 55.0, "SMA20": 150.5, "SMA100": 149.0}  # ATRなし
    tech.views = {
        interval: build_interval_view(interval, summary, indicators, 20, 100)
        for interval in ("15m", "1h", "4h", "1d")
    }
    plan = briefing.build_trade_plan("USDJPY", tech, CURRENCIES, [], [], now=NOW)
    assert plan.direction == "long"
    assert plan.atr is None and plan.stop is None
    assert any("ATR(1h)取得失敗" in w for w in plan.warnings)
    # ATRが取れていれば警告しない
    healthy = briefing.build_trade_plan("USDJPY", bullish_tech(), CURRENCIES, [], [], now=NOW)
    assert not any("ATR(1h)取得失敗" in w for w in healthy.warnings)


def test_journal_persists_features(tmp_path) -> None:
    path = tmp_path / "journal.jsonl"
    plan = briefing.build_trade_plan("USDJPY", bullish_tech(), CURRENCIES, [], [], now=NOW)
    append_plans(path, [plan], now=NOW)
    stored = list(read_entries(path))
    assert stored[0]["features"]["rsi_1h"] == 55.0
    assert stored[0]["features"]["tf_agreement"] == 1.0


def test_build_trade_plan_condition_adjuster_damps_and_explains() -> None:
    baseline = briefing.build_trade_plan("USDJPY", bullish_tech(), CURRENCIES, [], [], now=NOW)
    received: list[dict] = []

    def adjuster(features, direction):
        received.append({"features": dict(features), "direction": direction})
        return (
            0.7,
            "いまのチャート状態「RSI(1h): 買われすぎ圏(65超)」は過去の的中率20%(20件)と低いため確信度を×0.70に減衰",
        )

    damped = briefing.build_trade_plan(
        "USDJPY", bullish_tech(), CURRENCIES, [], [], now=NOW, condition_adjuster=adjuster
    )
    assert received and received[0]["features"]["rsi_1h"] == 55.0  # 判断時の特徴量が渡る
    assert received[0]["direction"] == "long"
    assert damped.direction == baseline.direction
    assert damped.conviction == round(baseline.conviction * 0.7)
    assert any("学習調整" in w and "買われすぎ圏" in w for w in damped.warnings)


def test_build_trade_plan_expectancy_adjuster_damps_and_explains() -> None:
    baseline = briefing.build_trade_plan("USDJPY", bullish_tech(), CURRENCIES, [], [], now=NOW)
    received: list[tuple[str, str]] = []

    def adjuster(symbol, direction):
        received.append((symbol, direction))
        return learning.EXPECTANCY_BLOCK_FACTOR, "通貨ペア USDJPYの期待Rは-0.20Rで非正"

    damped = briefing.build_trade_plan(
        "USDJPY", bullish_tech(), CURRENCIES, [], [], now=NOW, expectancy_adjuster=adjuster
    )

    assert received == [("USDJPY", "long")]
    assert damped.direction == baseline.direction
    assert damped.conviction == round(baseline.conviction * learning.EXPECTANCY_BLOCK_FACTOR)
    assert any("期待値ガード" in warning and "非正" in warning for warning in damped.warnings)


def test_condition_adjuster_skipped_for_non_directional_plans() -> None:
    """方向判断が出ないプラン(neutral等)では状態×方向の照合を行わない。"""
    conflicted = {
        "USD": CurrencySentiment("USD", score=-0.6),
        "JPY": CurrencySentiment("JPY", score=0.6),
    }
    weak_tech = PairTechnicals(symbol="USDJPY")
    weak_tech.views = {"1h": make_view("1h", "BUY")}
    received: list[str] = []

    def adjuster(features, direction):
        received.append(direction)
        return 0.7, "呼ばれてはいけない"

    plan = briefing.build_trade_plan(
        "USDJPY", weak_tech, conflicted, [], [], now=NOW, condition_adjuster=adjuster
    )
    assert plan.direction == "neutral"
    assert received == []
    assert not any("学習調整" in w for w in plan.warnings)


def test_build_discord_payload_includes_learning_note() -> None:
    from fx_intel.sentiment import MarketAnalysis

    analysis = MarketAnalysis(currencies=CURRENCIES, regime="risk_on", engine="lexicon")
    plan = briefing.build_trade_plan("USDJPY", bullish_tech(), CURRENCIES, [], [], now=NOW)
    payload = briefing.build_discord_payload(
        [plan],
        analysis,
        [],
        ["JPY", "USD"],
        20,
        100,
        learning_note="過去の方向判断30件を採点 — 的中率 63%",
        now=NOW,
    )
    macro = payload["embeds"][0]
    field = next(f for f in macro["fields"] if "学習メモ" in f["name"])
    assert "的中率 63%" in field["value"]

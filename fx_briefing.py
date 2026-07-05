"""ニュース×経済指標×テクニカルを統合したFXデスクブリーフィングをDiscordへ送る。

機関投資家のモーニングブリーフィングを模して、以下を1回の通知に統合する:

1. 経済指標カレンダー(ForexFactory公開フィード) — 今後48時間の重要イベント、
   イベント前後の警戒窓判定(research-maxプリセットと同じ 前120分/後180分)
2. ニュースヘッドライン(FXStreet / Google News RSS) — 通貨タグ付け
3. センチメント分析 — 語彙ベース(常時) + Claude API(ANTHROPIC_API_KEYがあれば)
4. TradingViewマルチタイムフレームテクニカル(15m/1h/4h/1d)
5. 複合スコア → ペアごとのトレードプラン(方向・確信度・ATRベースSL/TP)
   確信度はデータ品質(テクニカル取得率・関連ニュース量・カレンダー可用性)で減衰。
   FX市場の休場中(週末)はstale価格での判断を防ぐため方向判断を「休場」に固定
6. 判断ジャーナル(logs/briefing_journal.jsonl) — 記録から約24時間
   (市場オープン時間換算、週末除外)経過した方向判断を毎回検証して
   的中率をブリーフィングに表示。記録時ATRの10%未満の値動きは
   「小動き」として判定から除外(--no-journal で無効化)
7. 学習ループ(logs/briefing_learning.json) — ジャーナル履歴の全成熟判断を
   相互採点し、テクニカル/ニュース複合重みの再推定・確信度帯別
   キャリブレーション・不調ペアの確信度減衰を毎回導出して、
   今回の分析にそのまま反映する。さらに判断時のチャート状態
   (RSI・MA乖離・ボラティリティ・時間足一致度・ニュース量・ADX)を
   特徴量としてジャーナルに残し、「どんな状態のどちら向きが当たりやすい/
   外しやすいか」を状態バケット×ロング/ショート別に学習。同じ状態でも
   向きで成績は非対称になるため方向別に数え、いまの判断が過去に
   外しやすかった状態×方向に該当するときだけ確信度を自動減衰して
   理由を表示する。
   さらに学習サンプルは記録間隔非依存の間引き(同一ペア1時間1件)後に数え、
   確信度Brier(確率予測としての精度)・ホライズン別(4h/24h/72h)的中率・
   反省レポート(上位足逆行/RSI極端圏追随などの失敗理由テンプレート別成績)を
   学習メモとして表示する。
   分析を重ねるほど自分の当たり外れから学習して調整が効いてくる
   (--no-learning で無効化)

8. 複数AI委員会(fx_intel/committee.py) — テクニカル/ニュース/マクロ/MLの
   4委員が意見を出し、複合スコアを重み付き平均で合成。リスクオフィサー
   (build_trade_planの決定論ゲート)が常に拒否権を持つ。
9. マクロデータ層(fx_intel/macro.py) — COT・米金利・VIX・ドル指数を
   TTLキャッシュ+staleness品質ゲート付きで取得。リスクレジームを実データ判定。
10. ML確率モデル(fx_intel/gbm.py + ml.py) — 依存ゼロのGBDTでジャーナルから
    P(hit|状態,方向)を学習。自己相関間引き・時系列split+エンバーゴ・較正・
    スキルゲート付き。--train-ml で強制再学習。モデルが無い/7日以上古い場合は
    自動再学習(サンプル不足ならゲートが弾くだけで安全)。
11. 昇格ゲート(fx_intel/promotion.py) — 委員を実績で shadow→paper→live へ
    段階昇格。live昇格のみ --promote-live の人間承認が必須。

使い方:
    .venv/bin/python fx_briefing.py                  # Discordへ送信
    .venv/bin/python fx_briefing.py --dry-run        # 送信せず内容を表示
    .venv/bin/python fx_briefing.py --symbols USDJPY GBPJPY --no-llm
    .venv/bin/python fx_briefing.py --train-ml       # ML確率モデルを再学習して保存
    .venv/bin/python fx_briefing.py --promote-live ml # 条件を満たせばML委員をliveへ承認

副産物として以下を書き出す(いずれも fx_backtester の --events でそのまま使える形式):
- research_pack/upcoming_events.csv — 最新スナップショット(毎回上書き)
- research_pack/event_history.csv — 追記アーカイブ。実行のたびに未観測のイベント・
  改定分だけを recorded_at 付きで蓄積し、過去期間のイベント回避再生に使う
  (--no-event-archive で無効化)

Webhook URLは環境変数 DISCORD_WEBHOOK_URL か .env から読み込む。
Claude分析は ANTHROPIC_API_KEY が設定されている場合のみ有効。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, UTC
from pathlib import Path

import requests

import params_gate
from fx_intel import (
    briefing,
    calendar,
    committee,
    journal,
    learning,
    macro,
    ml,
    news,
    price_history,
    promotion,
    sentiment,
    technicals,
    tf_briefing,
    tf_learning,
    timeframe,
)

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_SYMBOLS = ["USDJPY", "EURUSD", "GBPUSD"]
DEFAULT_EVENTS_CSV = PROJECT_ROOT / "research_pack" / "upcoming_events.csv"
DEFAULT_EVENTS_ARCHIVE = PROJECT_ROOT / "research_pack" / "event_history.csv"
DEFAULT_JOURNAL_PATH = PROJECT_ROOT / "logs" / "briefing_journal.jsonl"
DEFAULT_LEARNING_PATH = PROJECT_ROOT / "logs" / "briefing_learning.json"
# 時間足別モード(--per-timeframe)専用の記録。融合1判断モードと混ざらないよう
# ジャーナルを分ける(採点ホライズンもスキーマも異なるため)
DEFAULT_TF_JOURNAL_PATH = PROJECT_ROOT / "logs" / "briefing_tf_journal.jsonl"
DEFAULT_TF_LEARNING_PATH = PROJECT_ROOT / "logs" / "briefing_tf_learning.json"
DEFAULT_TF_BASELINE_PATH = PROJECT_ROOT / "logs" / "briefing_tf_baseline.json"
# 時間足別採点用の価格専用系列(fx_tf_snapshot.py が5分ごとに追記)。
# 判断ジャーナルは毎時しか追記されず短い足の採点窓に入る点が得られないため、
# この密な価格系列を採点入力に結合して 15m/1h/4h/1d を採点可能にする。
# direction を持たない価格行なので採点対象は増やさず将来価格系列だけを密にする。
DEFAULT_TF_PRICES_PATH = PROJECT_ROOT / "logs" / "briefing_tf_prices.jsonl"
DEFAULT_MACRO_CACHE = PROJECT_ROOT / "logs" / "macro_cache.json"
DEFAULT_ML_MODEL_PATH = PROJECT_ROOT / "logs" / "ml_model.json"
DEFAULT_PROMOTION_STATE = PROJECT_ROOT / "logs" / "promotion_state.json"

# MLモデルの自動再学習: 学習済みモデルがこの日数より古いか、まだ一度も
# 学習に成功していない場合に再学習を試みる(train_artifactのサンプル不足
# ガードが早期returnするため、データが足りないうちは実質ノーコスト)
ML_RETRAIN_DAYS = 7.0


def ml_needs_retrain(
    artifact: ml.MLArtifact, now: datetime, max_age_days: float = ML_RETRAIN_DAYS
) -> bool:
    """保存済みMLモデルが再学習を要する状態か(モデル無し/日付不明/stale)。"""
    if artifact.model is None:
        return True
    try:
        trained = datetime.fromisoformat(artifact.trained_at)
    except (TypeError, ValueError):
        return True
    if trained.tzinfo is None:
        trained = trained.replace(tzinfo=UTC)
    return (now - trained) >= timedelta(days=max_age_days)


def load_webhook_url() -> str | None:
    import os

    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if url:
        return url.strip()
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("DISCORD_WEBHOOK_URL="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def load_strategy_params() -> tuple[int, int, float, str | None]:
    """strategy_params.json から (fast, slow, atr_multiple, warning) を読む。

    params_gate を通し、来歴の無い/過剰適合の疑いがあるパラメータは採用しない。
    ライブ戦略（trader/app/strategy.py）と同じゲートを共有し、検証されていない
    パラメータに基づくブリーフィングを出さないようにする。ゲートに落ちた場合は
    保守的な既定値で継続し、warning を返して通知本文にも明示する。
    """
    params_path = PROJECT_ROOT / "strategy_params.json"
    fast, slow, atr_multiple = 20, 100, briefing.DEFAULT_ATR_MULTIPLE
    if not params_path.exists():
        return fast, slow, atr_multiple, None

    params, errors = params_gate.load_validated_params(params_path)
    if errors or params is None:
        warning = (
            "strategy_params.json が検証ゲートに不合格のため既定値"
            f"(MA {fast}/{slow}, ATR×{atr_multiple})で継続: " + "; ".join(errors)
        )
        print(f"[warn] {warning}", file=sys.stderr)
        return fast, slow, atr_multiple, warning

    fast = int(params.get("fast_window", fast))
    slow = int(params.get("slow_window", slow))
    atr_multiple = float(params.get("atr_multiple", atr_multiple))
    return fast, slow, atr_multiple, None


def post_to_discord(webhook_url: str, payload: dict) -> None:
    response = requests.post(webhook_url, json=payload, timeout=15)
    if response.status_code >= 300:
        raise RuntimeError(f"Discord通知に失敗: HTTP {response.status_code} {response.text[:200]}")


def _run_per_timeframe(
    *,
    args,
    symbols,
    tech_map,
    analysis,
    events,
    events_48h,
    ordered_currencies,
    calendar_ok,
    atr_multiple,
    fetch_warnings,
    items,
    now,
) -> int:
    """時間足別モードの本体(main から分岐)。

    各時間足を独立に判断し、専用ジャーナルへ記録、時間足別の主ホライズンで
    自己採点・学習して次回の確信度に反映する。融合1判断モードとは
    ジャーナル・学習ファイルを分ける(スキーマも採点ホライズンも異なるため)。
    """
    journal_entries = list(journal.read_entries(DEFAULT_TF_JOURNAL_PATH))

    # 採点用の将来価格系列を組む。判断ジャーナル(源A)は毎時しか追記されず、
    # 短い足(15m:採点窓[9,21分])はそこに入る点が得られないため、
    # fx_tf_snapshot.py が5分ごとに記録する価格専用系列と、今回の現在価格を
    # 結合する。direction を持たない価格行は採点対象を増やさず将来価格系列だけを
    # 密にするので、15m/1h/4h/1d の全時間足が採点可能になる。
    price_rows = list(journal.read_entries(DEFAULT_TF_PRICES_PATH))
    current_snapshot = price_history.snapshot_entries(
        {
            symbol: {tf: tech_map[symbol].close(tf) for tf in timeframe.DEFAULT_TIMEFRAMES}
            for symbol in symbols
        },
        now=now,
    )
    scoring_entries = journal_entries + price_rows + current_snapshot

    # 学習: 時間足別ジャーナルを (symbol, timeframe) 別に採点しプロファイル導出
    tf_learn = tf_learning.TimeframeLearning()
    learning_note = ""
    if not args.no_learning:
        live_tf_learn = tf_learning.derive_timeframe_learning(scoring_entries, now=now)
        tf_learn = live_tf_learn
        baseline_path = Path(args.tf_learning_baseline) if args.tf_learning_baseline else None
        if baseline_path is not None and baseline_path.exists():
            baseline = tf_learning.load_timeframe_learning(baseline_path)
            if baseline.profiles or baseline.per_timeframe:
                tf_learn = tf_learning.merge_timeframe_learning(
                    live_tf_learn,
                    baseline,
                    min_live_evaluated=args.tf_learning_min_live_samples,
                )
            else:
                fetch_warnings.append(f"時間足別履歴ベースラインが空または破損: {baseline_path}")
        learning_note = tf_learn.summary_ja()
        if not args.dry_run:
            try:
                tf_learning.save_timeframe_learning(tf_learn, DEFAULT_TF_LEARNING_PATH)
            except OSError as error:
                fetch_warnings.append(f"時間足別学習プロファイル保存失敗: {error}")

    profile_lookup = tf_learn.profile_lookup if not args.no_learning else None

    # 各ペア・各時間足の独立判断
    plans_by_symbol: dict[str, list[timeframe.TimeframePlan]] = {}
    for symbol in symbols:
        base, quote = calendar.symbol_currencies(symbol)
        windows = calendar.risk_windows(events, {base, quote})
        plans_by_symbol[symbol] = timeframe.build_timeframe_plans(
            symbol,
            tech_map[symbol],
            analysis.currencies,
            windows,
            items,
            now=now,
            atr_multiple=atr_multiple,
            calendar_ok=calendar_ok,
            profile_lookup=profile_lookup,
        )

    # 補助ホライズン(観測専用)の的中率レポートを時間足別に用意。
    # 将来価格は採点と同じ結合系列(判断+価格スナップショット)から取る
    aux_reports_by_symbol: dict[str, dict[str, str]] = {}
    if not args.no_learning and journal_entries:
        for tf in timeframe.DEFAULT_TIMEFRAMES:
            line = tf_learning.auxiliary_horizon_report_ja(scoring_entries, tf)
            if line:
                aux_reports_by_symbol.setdefault("_shared", {})[tf] = line

    # ジャーナル: 今回の時間足別判断を専用ジャーナルへ追記
    if not args.no_journal and not args.dry_run:
        all_plans = [plan for plans in plans_by_symbol.values() for plan in plans]
        try:
            journal.append_timeframe_plans(DEFAULT_TF_JOURNAL_PATH, all_plans, now=now)
        except OSError as error:
            fetch_warnings.append(f"時間足別ジャーナル書き込み失敗: {error}")

    payload = tf_briefing.build_timeframe_discord_payload(
        plans_by_symbol,
        analysis,
        events_48h,
        ordered_currencies,
        fetch_warnings=fetch_warnings,
        learning_note=learning_note,
        aux_reports_by_symbol={s: aux_reports_by_symbol.get("_shared", {}) for s in symbols},
        now=now,
    )

    if args.dry_run:
        print(payload["content"])
        print(json.dumps(payload["embeds"], ensure_ascii=False, indent=2))
        return 0

    webhook_url = load_webhook_url()
    if not webhook_url:
        print(
            "DISCORD_WEBHOOK_URL が未設定です。環境変数か .env に設定してください。",
            file=sys.stderr,
        )
        return 1

    post_to_discord(webhook_url, payload)
    print(
        f"時間足別ブリーフィングを送信しました ({', '.join(symbols)} | "
        f"ニュース{len(items)}件 | イベント{len(events_48h)}件 | {analysis.engine})"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="ニュース×経済指標×テクニカル統合ブリーフィングをDiscordへ送る"
    )
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    parser.add_argument(
        "--hours-ahead",
        type=float,
        default=48.0,
        help="経済イベントを何時間先まで表示するか(既定48)",
    )
    parser.add_argument(
        "--hours-back",
        type=float,
        default=24.0,
        help="ニュースを何時間前まで集めるか(既定24)",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Claude API分析を使わず語彙ベースのみで実行",
    )
    parser.add_argument(
        "--no-export-events",
        action="store_true",
        help="research_pack/upcoming_events.csv の書き出しを行わない",
    )
    parser.add_argument(
        "--no-event-archive",
        action="store_true",
        help="research_pack/event_history.csv への追記アーカイブを行わない",
    )
    parser.add_argument(
        "--no-journal",
        action="store_true",
        help="判断ジャーナル(logs/briefing_journal.jsonl)の記録・検証を行わない",
    )
    parser.add_argument(
        "--no-learning",
        action="store_true",
        help="学習プロファイルによる重み・確信度の自動調整を行わない(既定重みで実行)",
    )
    parser.add_argument(
        "--no-macro",
        action="store_true",
        help="マクロデータ(COT・金利・VIX・ドル指数)の取得と委員を使わない",
    )
    parser.add_argument(
        "--no-ml",
        action="store_true",
        help="ML確率モデル委員を使わない(学習・予測をスキップ)",
    )
    parser.add_argument(
        "--train-ml",
        action="store_true",
        help="今回の実行でジャーナルからML確率モデルを再学習して保存する",
    )
    parser.add_argument(
        "--promote-live",
        nargs="*",
        default=None,
        metavar="MEMBER",
        help="指定した委員(macro/ml)を条件を満たせばliveへ昇格承認する(人間の明示承認)",
    )
    parser.add_argument(
        "--per-timeframe",
        action="store_true",
        help="時間足別モード: 15m/1h/4h/1d を独立に判断し、時間足ごとの主ホライズン"
        "(15m→15分後/1h→1h/4h→4h/1d→24h)で自己採点・学習する",
    )
    parser.add_argument(
        "--tf-learning-baseline",
        type=Path,
        default=DEFAULT_TF_BASELINE_PATH,
        help="時間足別モードで使う履歴学習ベースラインJSON(無ければ無視)",
    )
    parser.add_argument(
        "--tf-learning-min-live-samples",
        type=int,
        default=tf_learning.BASELINE_MIN_LIVE_EVALUATED,
        help="この採点件数に達した symbol×timeframe セルは履歴ベースラインよりライブ実績を優先",
    )
    parser.add_argument("--dry-run", action="store_true", help="Discordに送信せず内容を表示する")
    args = parser.parse_args(argv)
    if args.tf_learning_min_live_samples < 0:
        parser.error("--tf-learning-min-live-samples must be >= 0")

    symbols = [s.upper().replace("/", "") for s in args.symbols]
    fast_window, slow_window, atr_multiple, params_warning = load_strategy_params()
    now = datetime.now(UTC)

    currencies: set[str] = set()
    for symbol in symbols:
        base, quote = calendar.symbol_currencies(symbol)
        currencies.update((base, quote))
    ordered_currencies = sorted(currencies)

    fetch_warnings: list[str] = []
    if params_warning:
        fetch_warnings.append(params_warning)

    # 1. 経済指標カレンダー(レート制限対策にローカルキャッシュ併用)
    events, calendar_warnings = calendar.fetch_calendar(
        cache_path=PROJECT_ROOT / "logs" / "calendar_cache.json"
    )
    fetch_warnings.extend(calendar_warnings)
    # イベントが1件も取れていない=警戒窓判定が機能しない状態。判断側で安全側に倒す
    calendar_ok = bool(events)
    events_48h = calendar.upcoming_events(
        events, currencies, now, hours_ahead=args.hours_ahead, min_impact="high"
    )
    if not args.no_export_events and events:
        try:
            calendar.export_events_csv(events, DEFAULT_EVENTS_CSV)
        except OSError as error:
            fetch_warnings.append(f"イベントCSV書き出し失敗: {error}")
    if not args.no_event_archive and events:
        try:
            calendar.append_events_archive(events, DEFAULT_EVENTS_ARCHIVE, now=now)
        except OSError as error:
            fetch_warnings.append(f"イベント履歴アーカイブ追記失敗: {error}")

    # 2. ニュース収集
    items, news_warnings = news.fetch_news_for_symbols(symbols, hours_back=args.hours_back)
    fetch_warnings.extend(news_warnings)

    # 3. マクロデータ(COT・金利・VIX・ドル指数)。レジーム判定とマクロ委員に使う
    macro_snapshot = None
    if not args.no_macro:
        macro_snapshot = macro.fetch_macro_snapshot(DEFAULT_MACRO_CACHE, now=now)
        fetch_warnings.extend(macro_snapshot.warnings)

    # 4. センチメント分析(Claude API → 自前分析エンジン。レジームはマクロ実データ優先)
    analysis = sentiment.analyze_market(
        items, ordered_currencies, use_llm=not args.no_llm, macro=macro_snapshot, now=now
    )

    # 5. テクニカル取得
    tech_map, tech_warnings = technicals.fetch_pair_technicals(
        symbols, fast_window=fast_window, slow_window=slow_window
    )
    fetch_warnings.extend(tech_warnings)

    # 時間足別モード: ここで専用パスへ分岐して早期return(融合1判断の
    # 委員会・ML・昇格は使わず、時間足別の判断・採点・学習だけを回す)
    if args.per_timeframe:
        return _run_per_timeframe(
            args=args,
            symbols=symbols,
            tech_map=tech_map,
            analysis=analysis,
            events=events,
            events_48h=events_48h,
            ordered_currencies=ordered_currencies,
            calendar_ok=calendar_ok,
            atr_multiple=atr_multiple,
            fetch_warnings=fetch_warnings,
            items=items,
            now=now,
        )

    # 6. 学習ループ: ジャーナル履歴を相互採点し、重み・確信度の調整を導出
    profile = learning.LearnedProfile()
    learning_note = ""
    calls: list[learning.EvaluatedCall] = []
    journal_entries = list(journal.read_entries(DEFAULT_JOURNAL_PATH))
    if not args.no_learning:
        calls = learning.evaluate_history(journal_entries)
        profile = learning.derive_profile(calls, now=now)
        learning_note = profile.summary_ja()
        # ホライズン別(4h/24h/72h)の的中率観測。学習は24hのみを使う
        horizon_line = learning.horizon_report_ja(journal_entries)
        if horizon_line:
            learning_note = (learning_note + "\n" + horizon_line).strip()
        if not args.dry_run:
            try:
                learning.save_profile(profile, DEFAULT_LEARNING_PATH)
            except OSError as error:
                fetch_warnings.append(f"学習プロファイル保存失敗: {error}")

    # 7. ML確率モデル: --train-mlで強制再学習。それ以外も保存済みモデルが
    #    無い/staleなら自動再学習する(スキルゲートは train_artifact 内)
    ml_artifact = ml.MLArtifact()
    if not args.no_ml:
        if not args.train_ml:
            ml_artifact = ml.load_artifact(DEFAULT_ML_MODEL_PATH)
        if args.train_ml or ml_needs_retrain(ml_artifact, now):
            train_calls = calls or learning.evaluate_history(journal_entries)
            ml_artifact = ml.train_artifact(train_calls, now=now)
            # モデル本体ができたときだけ保存する(データ不足の空アーティファクトで
            # 毎回上書きしても意味がなく、--train-ml時は結果を必ず残す)
            if not args.dry_run and (args.train_ml or ml_artifact.model is not None):
                try:
                    ml.save_artifact(ml_artifact, DEFAULT_ML_MODEL_PATH)
                except OSError as error:
                    fetch_warnings.append(f"MLモデル保存失敗: {error}")

    # 8. 昇格ゲート: 委員(macro/ml)の実績をジャーナルから採点し段階を更新
    promotion_state = promotion.load_state(DEFAULT_PROMOTION_STATE)
    require_live_ack = args.promote_live if args.promote_live is not None else []
    promotion_state, _member_perf = promotion.evaluate_and_update(
        journal_entries, promotion_state, now=now, require_live_ack=require_live_ack
    )
    stages = promotion_state.as_stage_map()
    if args.no_macro:
        stages["macro"] = "shadow"
    if args.no_ml or not ml_artifact.usable:
        stages["ml"] = "shadow"
    promotion_note = promotion.summary_ja(promotion_state)
    if not args.dry_run:
        try:
            promotion.save_state(promotion_state, DEFAULT_PROMOTION_STATE)
        except OSError as error:
            fetch_warnings.append(f"昇格状態の保存失敗: {error}")

    # 9. ペアごとの委員会審議(tech/news/macro/ML、学習済み重み・段階ゲート反映)
    plans: list[briefing.TradePlan] = []
    for symbol in symbols:
        base, quote = calendar.symbol_currencies(symbol)
        windows = calendar.risk_windows(events, {base, quote})
        plans.append(
            committee.deliberate(
                symbol,
                tech_map[symbol],
                analysis.currencies,
                windows,
                items,
                now=now,
                atr_multiple=atr_multiple,
                calendar_ok=calendar_ok,
                tech_weight=profile.tech_weight,
                news_weight=profile.news_weight,
                conviction_factor=profile.conviction_factor(symbol),
                condition_adjuster=profile.condition_adjustment,
                macro_snapshot=macro_snapshot,
                ml_artifact=ml_artifact if not args.no_ml else None,
                stages=stages,
            )
        )

    # 10. 判断ジャーナル: 過去の判断を検証し、今回の判断を記録
    journal_note = ""
    if not args.no_journal:
        closes = {symbol: tech_map[symbol].close() for symbol in symbols}
        stats = journal.evaluate_directional_accuracy(DEFAULT_JOURNAL_PATH, closes, now=now)
        journal_note = journal.format_stats_ja(stats)
        if not args.dry_run:
            try:
                journal.append_plans(DEFAULT_JOURNAL_PATH, plans, now=now)
            except OSError as error:
                fetch_warnings.append(f"判断ジャーナル書き込み失敗: {error}")

    if ml_artifact.model is not None:
        learning_note = (learning_note + "\n" + ml_artifact.summary_ja()).strip()

    payload = briefing.build_discord_payload(
        plans,
        analysis,
        events_48h,
        ordered_currencies,
        fast_window,
        slow_window,
        fetch_warnings=fetch_warnings,
        journal_note=journal_note,
        learning_note=learning_note,
        promotion_note=promotion_note,
        now=now,
    )

    if args.dry_run:
        print(payload["content"])
        print(json.dumps(payload["embeds"], ensure_ascii=False, indent=2))
        return 0

    webhook_url = load_webhook_url()
    if not webhook_url:
        print(
            "DISCORD_WEBHOOK_URL が未設定です。環境変数か .env に設定してください。",
            file=sys.stderr,
        )
        return 1

    post_to_discord(webhook_url, payload)
    print(
        f"ブリーフィングを送信しました ({', '.join(symbols)} | "
        f"ニュース{len(items)}件 | イベント{len(events_48h)}件 | {analysis.engine})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

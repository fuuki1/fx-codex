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
9. マクロデータ層(fx_intel/macro.py / cot_pit.py) — 金利・VIX・ドル指数は
   current-only TTL、COTは明示された監査済みresearch artifactだけをas-of読込。
   legacy TTL COTへはfallbackしない。
10. ML確率モデル(fx_intel/gbm.py + ml.py) — 依存ゼロのGBDTでジャーナルから
    P(hit|状態,方向)を学習。自己相関間引き・時系列split+エンバーゴ・較正・
    スキルゲート付き。--train-ml で強制再学習。モデルが無い/7日以上古い場合は
    自動再学習(サンプル不足ならゲートが弾くだけで安全)。
11. 昇格ゲート(fx_intel/promotion.py) — 委員を実績で shadow→paper まで段階昇格。
    このCLIからのlive昇格は無効化されている。

使い方:
    .venv/bin/python fx_briefing.py                  # Discordへ送信
    .venv/bin/python fx_briefing.py --dry-run        # 送信せず内容を表示
    .venv/bin/python fx_briefing.py --symbols USDJPY GBPJPY --no-llm
    .venv/bin/python fx_briefing.py --train-ml       # ML確率モデルを再学習して保存
    .venv/bin/python fx_briefing.py --score-trade-outcomes # TP/SL込み期待値を監査

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
from collections.abc import Callable, Mapping
import json
import os
import sys
from datetime import datetime, timedelta, UTC
from pathlib import Path


from fx_intel import (
    briefing,
    calendar,
    committee,
    decision_feedback,
    decision_log,
    decision_pipeline,
    discord_delivery,
    freshness,
    direction_threshold,
    horizon_forecast,
    horizon_journal,
    horizon_learning,
    horizons,
    ibkr_prices,
    journal,
    learning,
    input_context,
    liquidity,
    macro,
    maximization,
    market_session,
    ml,
    news,
    oanda_prices,
    price_history,
    promotion,
    signal_board,
    sentiment,
    technicals,
    tf_briefing,
    tf_learning,
    tp_sl_learning,
    timeframe,
    trade_outcome,
)

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_SYMBOLS = ["USDJPY", "EURUSD"]
DEFAULT_EVENTS_CSV = PROJECT_ROOT / "research_pack" / "upcoming_events.csv"
DEFAULT_EVENTS_ARCHIVE = PROJECT_ROOT / "research_pack" / "event_history.csv"
DEFAULT_JOURNAL_PATH = PROJECT_ROOT / "logs" / "briefing_journal.jsonl"
DEFAULT_LEARNING_PATH = PROJECT_ROOT / "logs" / "briefing_learning.json"
# 時間足別モード(--per-timeframe)専用の記録。融合1判断モードと混ざらないよう
# ジャーナルを分ける(採点ホライズンもスキーマも異なるため)
DEFAULT_TF_JOURNAL_PATH = PROJECT_ROOT / "logs" / "briefing_tf_journal.jsonl"
DEFAULT_TF_LEARNING_PATH = PROJECT_ROOT / "logs" / "briefing_tf_learning.json"
DEFAULT_TP_SL_LEARNING_PATH = PROJECT_ROOT / "logs" / "briefing_tp_sl_learning.json"
DEFAULT_MAXIMIZATION_PATH = PROJECT_ROOT / "logs" / "briefing_maximization.json"
# 時間足別採点用の価格専用系列(fx_tf_snapshot.py が5分ごとに追記)。
# 判断ジャーナルだけでは短い足の採点窓に十分な価格点が得られないため、
# この密な価格系列を採点入力に結合して 15m/1h/4h/1d を採点可能にする。
# direction を持たない価格行なので採点対象は増やさず将来価格系列だけを密にする。
DEFAULT_TF_PRICES_PATH = PROJECT_ROOT / "logs" / "briefing_tf_prices.jsonl"
DEFAULT_CALENDAR_CACHE = PROJECT_ROOT / "logs" / "calendar_cache.json"
DEFAULT_HORIZON_JOURNAL_PATH = PROJECT_ROOT / "logs" / "briefing_horizon_forecasts.jsonl"
DEFAULT_HORIZON_LEARNING_PATH = PROJECT_ROOT / "logs" / "briefing_horizon_learning.json"
DEFAULT_MACRO_CACHE = PROJECT_ROOT / "logs" / "macro_cache.json"
DEFAULT_ML_MODEL_PATH = PROJECT_ROOT / "logs" / "ml_model.json"
DEFAULT_PROMOTION_STATE = PROJECT_ROOT / "logs" / "promotion_state.json"
DEFAULT_TRADE_IMPROVEMENT_REGISTRY = PROJECT_ROOT / "logs" / "trade_improvement_candidates.json"
DEFAULT_TRADE_MONITOR_PATH = PROJECT_ROOT / "logs" / "trade_outcome_monitor.json"
DEFAULT_DECISION_LOG_PATH = PROJECT_ROOT / "logs" / "briefing_decisions.jsonl"
DEFAULT_DECISION_LATEST_PATH = PROJECT_ROOT / "logs" / "briefing_decisions_latest.json"
DEFAULT_DECISION_OUTCOMES_PATH = PROJECT_ROOT / "logs" / "briefing_decision_outcomes.json"
DEFAULT_DECISION_FEEDBACK_PATH = PROJECT_ROOT / "logs" / "briefing_decision_feedback.json"
JOURNAL_WRITE_FAILURE_EXIT_CODE = 4
NOTIFICATION_FAILURE_EXIT_CODE = 5
DEFAULT_FRESHNESS_REPORT_PATH = PROJECT_ROOT / "logs" / "freshness_report.json"
DEFAULT_DIRECTION_THRESHOLD_POLICY_PATH = PROJECT_ROOT / "logs" / "direction_threshold_policy.json"
DEFAULT_INPUT_POLICY_PATH = PROJECT_ROOT / "ops" / "input_policy.json"

# MLモデルの自動再学習: 学習済みモデルがこの日数より古いか、まだ一度も
# 学習に成功していない場合に再学習を試みる(train_artifactのサンプル不足
# ガードが早期returnするため、データが足りないうちは実質ノーコスト)
ML_RETRAIN_DAYS = 7.0


def _attach_cot_pit_dataset(
    snapshot: macro.MacroSnapshot,
    dataset_path: Path | None,
    *,
    prediction_time: datetime,
) -> None:
    """Attach only audited, release-attested COT rows available at prediction time."""

    if prediction_time.tzinfo is None:
        raise ValueError("COT prediction_time must be timezone-aware")
    snapshot.cot = {}
    if dataset_path is None:
        snapshot.cot_evidence = {
            "status": "disabled",
            "prediction_time": prediction_time.astimezone(UTC).isoformat(),
            "usable": False,
        }
        snapshot.warnings.append(
            "COT PIT dataset未指定: legacy current-snapshot COTは判断入力から除外"
        )
        return
    # Keep the normal notification runtime independent from fx_backtester.  The
    # shared PIT artifact dependency is loaded only when this research-only input
    # is explicitly selected.
    from fx_intel import cot_pit

    try:
        result = cot_pit.load_cot_as_of(dataset_path, prediction_time)
    except cot_pit.COTPITError as error:
        snapshot.cot_evidence = {
            "status": "invalid",
            "prediction_time": prediction_time.astimezone(UTC).isoformat(),
            "errors": [str(error)],
            "usable": False,
        }
        snapshot.warnings.append(f"COT PIT dataset監査失敗のためCOTを除外: {error}")
        return
    snapshot.cot_evidence = result.to_dict()
    if not result.usable:
        detail = "; ".join((*result.errors, *result.warnings[:1]))
        snapshot.warnings.append(
            f"COT PIT status={result.status} のためCOTを除外" + (f": {detail}" if detail else "")
        )
        return
    snapshot.cot = dict(result.reports)


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


def _realized_expectancy_r(summary: dict | None, symbol: str, direction: str) -> float | None:
    """Return promotion-grade net OOS expectancy evidence for one cell.

    The current descriptive trade-outcome summary lacks independent-test, CI,
    label-version, and full net-cost provenance, so its naked mean is not accepted.
    A future producer must explicitly satisfy this typed evidence contract; absence
    of any field returns None and the downstream checklist remains fail-closed.
    """
    if not summary or direction not in ("long", "short"):
        return None
    cell = (summary.get("by_symbol_direction") or {}).get(f"{symbol}:{direction}")
    if not isinstance(cell, dict):
        return None
    if not (
        cell.get("evidence_schema") == 2
        and cell.get("sample_ok") is True
        and cell.get("net_of_costs") is True
        and cell.get("independent_test") is True
        and isinstance(cell.get("label_version"), str)
        and bool(cell.get("label_version"))
    ):
        return None
    ci_lower = cell.get("expectancy_r_ci_lower")
    if not isinstance(ci_lower, (int, float)) or isinstance(ci_lower, bool) or ci_lower <= 0:
        return None
    value = cell.get("expectancy_r")
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


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


def load_env_value(name: str, default: str = "") -> str:
    """環境変数を優先し、未設定ならproject .envから非秘密設定を読む。"""

    value = os.environ.get(name)
    if value is not None:
        return value.strip()
    env_path = PROJECT_ROOT / ".env"
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return default
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, raw = stripped.split("=", 1)
        if key.strip() == name:
            return raw.strip().strip("\"'")
    return default


def build_decision_input_contexts(
    symbols,
    tech_map,
    *,
    macro_snapshot,
    learning_dimensions,
    price_rows,
    now,
    fetch_warnings,
):
    """Build one immutable context per symbol and share it across all decision paths."""

    policy = liquidity.load_policy(DEFAULT_INPUT_POLICY_PATH)
    broker_quotes: dict[str, dict[str, object]] = {}
    provider = load_env_value("FX_DECISION_QUOTE_PROVIDER", "scanner").lower()
    if provider == "oanda":
        try:
            config = oanda_prices.OandaPriceConfig.from_env(project_root=PROJECT_ROOT)
            broker_quotes, quote_warnings = oanda_prices.fetch_decision_quotes(
                symbols, config, captured_at=now
            )
            fetch_warnings.extend(quote_warnings)
        except ValueError as error:
            fetch_warnings.append(f"判断時quote設定不備: {error}")
    elif provider == "ibkr":
        try:
            config = ibkr_prices.IbkrPriceConfig.from_env(project_root=PROJECT_ROOT)
            broker_quotes, quote_warnings = ibkr_prices.fetch_decision_quotes(
                symbols, config, captured_at=now
            )
            fetch_warnings.extend(quote_warnings)
        except ValueError as error:
            fetch_warnings.append(f"判断時quote設定不備: {error}")
    elif provider != "scanner":
        fetch_warnings.append(
            f"FX_DECISION_QUOTE_PROVIDER={provider!r} は未対応のためscanner proxyを使用"
        )

    contexts: dict[str, dict[str, object]] = {}
    session_bucket = str(learning_dimensions.get("session_bucket", "unknown"))
    for symbol in symbols:
        quote = liquidity.quote_from_mapping(broker_quotes.get(symbol))
        if quote is None:
            pair_tech = tech_map.get(symbol)
            view = pair_tech.views.get("1h") if pair_tech is not None else None
            quote = liquidity.scanner_quote(
                symbol,
                bid=view.bid if view is not None else None,
                ask=view.ask if view is not None else None,
                observed_at=now,
            )
        liquidity_snapshot = liquidity.build_liquidity_snapshot(
            symbol,
            decision_time=now,
            quote=quote,
            price_rows=price_rows,
            session_bucket=session_bucket,
            policy=policy,
        )
        macro_features = input_context.build_macro_feature_snapshot(
            macro_snapshot,
            symbol,
            decision_time=now,
        )
        context = input_context.build_decision_input_context(
            symbol,
            decision_time=now,
            macro=macro_features,
            liquidity=liquidity_snapshot,
            learning_dimensions=learning_dimensions,
            run_id=f"{now.astimezone(UTC):%Y%m%dT%H%M%SZ}:briefing",
        )
        contexts[symbol] = context.to_dict()
    return contexts


def load_strategy_params() -> tuple[int, int, float, str | None]:
    """テクニカル分析用の MA 窓と ATR 倍率を返す。

    このシステムは自動売買を行わず分析→Discord通知に専念するため、
    発注戦略の最適化パラメータ（strategy_params.json）は持たない。
    テクニカル委員が使う MA クロスの窓と SL/TP 距離の ATR 倍率は、
    保守的な固定値（MA 20/100・ATR×2.5）で運用する。
    戻り値は互換のため (fast, slow, atr_multiple, warning) の4要素を保つ。
    """
    return 20, 100, briefing.DEFAULT_ATR_MULTIPLE, None


def post_to_discord(webhook_url: str, payload: dict) -> None:
    discord_delivery.send_webhook(webhook_url, payload)


def score_trade_outcomes_cli(
    journal_path: Path,
    *,
    json_report_path: Path | None = None,
    improvement_registry_path: Path | None = None,
    monitor_json_path: Path | None = None,
) -> int:
    """判断ジャーナルをMFE/MAE/TP/SLで採点し、期待値監査レポートを出す。"""
    entries = [
        entry
        for entry in journal.read_entries(journal_path)
        if journal.is_pit_eligible_entry(entry)
    ]
    outcomes = trade_outcome.evaluate_trade_outcomes(entries)
    summary = trade_outcome.summarize_expectancy(outcomes)
    findings = trade_outcome.expectancy_findings(summary)
    candidates = trade_outcome.improvement_candidates(summary)
    registry = None
    paused_policies: list[dict] = []

    if improvement_registry_path is not None:
        previous = load_pit_improvement_registry(improvement_registry_path)
        registry = trade_outcome.update_improvement_registry(
            previous,
            candidates,
            managed_action_types=trade_outcome.EXPECTANCY_CANDIDATE_ACTION_TYPES,
            data_contract=journal.FUSION_PIT_DATA_CONTRACT,
        )
        registry, paused_policies = trade_outcome.auto_pause_underperforming_approved_policies(
            registry,
            summary,
        )
        trade_outcome.save_improvement_registry(registry, improvement_registry_path)

    print(trade_outcome.format_expectancy_report_ja(summary))
    print(trade_outcome.format_improvement_candidates_ja(candidates))
    if registry is not None:
        print(trade_outcome.format_improvement_registry_ja(registry))
        for paused in paused_policies:
            print(
                f"承認済みTP/SLを自動停止しました: {paused['candidate_id']} — {paused['reason_ja']}"
            )
        print(f"改善候補レジストリを保存しました: {improvement_registry_path}")

    if json_report_path is not None:
        json_report_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": 1,
            "summary": summary,
            "findings": findings,
            "improvement_candidates": [candidate.to_dict() for candidate in candidates],
            "improvement_registry": registry,
            "auto_paused_policies": paused_policies,
            "outcomes": [outcome.to_dict() for outcome in outcomes],
        }
        json_report_path.write_text(
            json.dumps(
                trade_outcome.json_safe(payload),
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            ),
            encoding="utf-8",
        )
        print(f"トレード期待値監査JSONを保存しました: {json_report_path}")
    if monitor_json_path is not None:
        health_report = trade_outcome.check_expectancy_health(summary)
        snapshot = trade_outcome.build_monitoring_snapshot(
            summary,
            registry=registry,
            health_report=health_report,
        )
        monitor_json_path.parent.mkdir(parents=True, exist_ok=True)
        monitor_json_path.write_text(
            json.dumps(
                trade_outcome.json_safe(snapshot),
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            ),
            encoding="utf-8",
        )
        print(f"トレード期待値監視JSONを保存しました: {monitor_json_path}")
    return 0


def check_trade_outcome_health_cli(
    journal_path: Path,
    *,
    require_sample_ok: bool = False,
) -> int:
    """期待値監査のCI/cron向けヘルスチェック。"""
    entries = [
        entry
        for entry in journal.read_entries(journal_path)
        if journal.is_pit_eligible_entry(entry)
    ]
    outcomes = trade_outcome.evaluate_trade_outcomes(entries)
    summary = trade_outcome.summarize_expectancy(outcomes)
    report = trade_outcome.check_expectancy_health(
        summary,
        require_sample_ok=require_sample_ok,
    )
    print(trade_outcome.format_expectancy_health_ja(report))
    return report.exit_code


def approve_trade_candidate_cli(
    registry_path: Path,
    candidate_id: str,
    *,
    decision: str,
    actor: str = "manual",
    note: str = "",
) -> int:
    registry = load_pit_improvement_registry(registry_path)
    updated, result = trade_outcome.set_improvement_candidate_approval(
        registry,
        candidate_id,
        decision,
        actor=actor,
        note=note,
    )
    print(result["message_ja"])
    if result["status"] not in {"approved", "rejected", "resumed"}:
        return 1
    trade_outcome.save_improvement_registry(updated, registry_path)
    print(trade_outcome.format_improvement_registry_ja(updated))
    print(f"改善候補レジストリを保存しました: {registry_path}")
    return 0


def retest_trade_variants_cli(
    journal_path: Path,
    *,
    json_report_path: Path | None = None,
    improvement_registry_path: Path | None = None,
    target1_r_candidates: list[float] | None = None,
    target2_r_candidates: list[float] | None = None,
) -> int:
    """TP1/TP2候補を過去ジャーナルでpaper再採点する。"""
    entries = [
        entry
        for entry in journal.read_entries(journal_path)
        if journal.is_pit_eligible_entry(entry)
    ]
    report = trade_outcome.retest_tp_sl_variants(
        entries,
        target1_r_candidates=target1_r_candidates or trade_outcome.DEFAULT_TP1_R_CANDIDATES,
        target2_r_candidates=target2_r_candidates or trade_outcome.DEFAULT_TP2_R_CANDIDATES,
    )
    candidates = trade_outcome.variant_improvement_candidates(report)
    registry = None
    baseline = report.get("baseline")
    overall = baseline.get("overall") if isinstance(baseline, dict) else None
    evaluated = int(overall.get("evaluated", 0)) if isinstance(overall, dict) else 0
    if improvement_registry_path is not None and evaluated > 0:
        previous = load_pit_improvement_registry(improvement_registry_path)
        registry = trade_outcome.update_improvement_registry(
            previous,
            candidates,
            managed_action_types=trade_outcome.VARIANT_CANDIDATE_ACTION_TYPES,
            data_contract=journal.FUSION_PIT_DATA_CONTRACT,
        )
        trade_outcome.save_improvement_registry(registry, improvement_registry_path)

    print(trade_outcome.format_variant_retest_report_ja(report))
    print(trade_outcome.format_improvement_candidates_ja(candidates))
    if registry is not None:
        print(trade_outcome.format_improvement_registry_ja(registry))
        print(f"改善候補レジストリを保存しました: {improvement_registry_path}")
    if json_report_path is not None:
        json_report_path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(report)
        payload["improvement_registry"] = registry
        json_report_path.write_text(
            json.dumps(
                trade_outcome.json_safe(payload),
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            ),
            encoding="utf-8",
        )
        print(f"TP/SL候補paper再採点JSONを保存しました: {json_report_path}")
    return 0


def make_trade_expectancy_adjuster(
    summary: dict,
) -> Callable[[str, str, int], tuple[float, str, bool]]:
    def adjust(symbol: str, direction: str, conviction: int) -> tuple[float, str, bool]:
        adjustment = trade_outcome.decision_adjustment(summary, symbol, direction, conviction)
        return adjustment.factor, adjustment.reason_ja, adjustment.block

    return adjust


def compose_trade_expectancy_adjusters(
    first: Callable[[str, str, int], tuple[float, str, bool]] | None,
    second: Callable[[str, str, int], tuple[float, str, bool]] | None,
) -> Callable[[str, str, int], tuple[float, str, bool]] | None:
    """Compose two non-mutating trade expectancy adjusters."""

    if first is None:
        return second
    if second is None:
        return first

    def adjust(symbol: str, direction: str, conviction: int) -> tuple[float, str, bool]:
        factor1, reason1, block1 = first(symbol, direction, conviction)
        adjusted_conviction = round(conviction * max(0.0, min(1.10, factor1)))
        factor2, reason2, block2 = second(symbol, direction, adjusted_conviction)
        reasons = [reason for reason in (reason1, reason2) if reason]
        return factor1 * factor2, " / ".join(reasons), block1 or block2

    return adjust


def make_approved_tp_sl_adjuster(
    registry: Mapping[str, object],
) -> briefing.TargetRAdjuster | None:
    if registry.get("data_contract") != journal.FUSION_PIT_DATA_CONTRACT:
        return None
    if not trade_outcome.approved_target_policies(registry):
        return None

    def adjust(
        symbol: str,
        direction: str,
        conviction: int,
    ) -> briefing.TargetRAdjustment | None:
        policy = trade_outcome.select_approved_target_policy(
            registry,
            symbol,
            direction,
            conviction,
        )
        if policy is None:
            return None
        reason = (
            f"{policy.reason_ja} "
            f"(TP1={policy.target1_r:g}R / TP2={policy.target2_r:g}R, "
            f"id={policy.candidate_id})"
        )
        return policy.target1_r, policy.target2_r, reason, policy.to_dict()

    return adjust


def load_pit_improvement_registry(path: str | Path) -> dict:
    """Load only a registry whose candidates were derived from PIT-eligible fusion rows."""
    registry = trade_outcome.load_improvement_registry(path)
    if registry.get("data_contract") != journal.FUSION_PIT_DATA_CONTRACT:
        return {}
    return registry


def make_timeframe_trade_expectancy_lookup(
    scoring_entries: list[Mapping[str, object]],
) -> tuple[timeframe.ExpectancyLookup | None, str, dict[str, dict]]:
    summaries: dict[str, dict] = {}
    for tf in timeframe.DEFAULT_TIMEFRAMES:
        tf_entries = [entry for entry in scoring_entries if str(entry.get("timeframe", "")) == tf]
        if not tf_entries:
            continue
        horizon_hours = timeframe.PRIMARY_HORIZON_HOURS.get(tf, 24.0)
        outcomes = trade_outcome.evaluate_trade_outcomes(
            tf_entries,
            horizon_hours=horizon_hours,
            tolerance_hours=timeframe.tolerance_for(horizon_hours),
        )
        summary = trade_outcome.summarize_expectancy(outcomes)
        overall = summary.get("overall")
        if isinstance(overall, Mapping) and int(overall.get("evaluated", 0) or 0) > 0:
            summaries[tf] = summary

    if not summaries:
        return None, "", {}

    adjusters = {tf: make_trade_expectancy_adjuster(summary) for tf, summary in summaries.items()}

    def lookup(_symbol: str, tf: str) -> timeframe.ExpectancyAdjuster | None:
        return adjusters.get(tf)

    return lookup, format_timeframe_expectancy_report_ja(summaries), summaries


def compose_timeframe_expectancy_lookups(
    first: timeframe.ExpectancyLookup | None,
    second: timeframe.ExpectancyLookup | None,
) -> timeframe.ExpectancyLookup | None:
    """Compose two non-mutating timeframe expectancy lookups.

    The TP/SL accuracy MVP uses the same hook as the existing expectancy guard,
    but it never blocks by itself.  Composition lets both adjustments apply
    without forcing either subsystem to know about the other.
    """

    if first is None:
        return second
    if second is None:
        return first

    def lookup(symbol: str, tf: str) -> timeframe.ExpectancyAdjuster | None:
        first_adjuster = first(symbol, tf)
        second_adjuster = second(symbol, tf)
        if first_adjuster is None:
            return second_adjuster
        if second_adjuster is None:
            return first_adjuster

        def adjust(symbol_arg: str, direction: str, conviction: int) -> tuple[float, str, bool]:
            factor1, reason1, block1 = first_adjuster(symbol_arg, direction, conviction)
            adjusted_conviction = round(conviction * max(0.0, min(1.10, factor1)))
            factor2, reason2, block2 = second_adjuster(symbol_arg, direction, adjusted_conviction)
            reasons = [reason for reason in (reason1, reason2) if reason]
            return factor1 * factor2, " / ".join(reasons), block1 or block2

        return adjust

    return lookup


def format_timeframe_expectancy_report_ja(summaries: Mapping[str, Mapping[str, object]]) -> str:
    if not summaries:
        return ""
    lines = ["時間足別期待値監視(MFE/MAE/TP/SL):"]
    for tf in timeframe.DEFAULT_TIMEFRAMES:
        summary = summaries.get(tf)
        if not isinstance(summary, Mapping):
            continue
        overall = summary.get("overall")
        if not isinstance(overall, Mapping):
            continue
        evaluated = int(overall.get("evaluated", 0) or 0)
        if evaluated <= 0:
            continue
        tradable = int(overall.get("tradable", 0) or 0)
        min_samples = int(overall.get("min_samples", 0) or 0)
        sample_status = "OK" if bool(overall.get("sample_ok")) else f"不足 {tradable}/{min_samples}"
        lines.append(
            f"・{tf}: 期待R {_fmt_expectancy_value(overall.get('expectancy_r'), 'R')}"
            f" / PF {_fmt_expectancy_value(overall.get('profit_factor_r'), '')}"
            f" / SL {_fmt_expectancy_pct(overall.get('sl_rate'))}"
            f" (n={tradable}/{evaluated}, sample={sample_status})"
        )
    return "\n".join(lines) if len(lines) > 1 else ""


def _fmt_expectancy_value(value: object, suffix: str) -> str:
    if not isinstance(value, int | float):
        return "n/a"
    if value == float("inf"):
        return "∞"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.2f}{suffix}"


def _fmt_expectancy_pct(value: object) -> str:
    if not isinstance(value, int | float):
        return "n/a"
    return f"{value * 100:.0f}%"


def append_note(base: str, addition: str) -> str:
    if not addition:
        return base
    return (base + "\n" + addition).strip()


def _run_horizon_track(
    *,
    args,
    symbols,
    tech_map,
    analysis,
    events,
    calendar_ok,
    fetch_warnings,
    items,
    now,
    price_rows,
    input_contexts,
) -> int:
    """Generate, score, and persist the isolated five-minute shadow track."""
    entries = list(horizon_journal.read_horizon_entries(DEFAULT_HORIZON_JOURNAL_PATH))
    learning_state = None
    if not args.no_learning:
        score_result = horizon_learning.score_horizon_history(entries, price_rows, now=now)
        learning_state = horizon_learning.derive_horizon_learning(score_result, now=now)
        if not args.dry_run:
            try:
                horizon_learning.save_horizon_learning(
                    learning_state, DEFAULT_HORIZON_LEARNING_PATH
                )
            except OSError as error:
                fetch_warnings.append(f"ホライズン学習状態の保存失敗: {error}")

    profile_lookup = horizon_learning.make_profile_lookup(learning_state)
    band_provider = horizon_learning.make_band_provider(learning_state)
    calibration_provider = horizon_learning.make_calibration_provider(learning_state)
    feature_time = datetime.now(UTC)
    forecasts: list[horizon_forecast.HorizonForecast] = []
    for symbol in symbols:
        tech = tech_map.get(symbol)
        if tech is None:
            fetch_warnings.append(f"ホライズン予測のテクニカル欠損: {symbol}")
            continue
        base, quote = calendar.symbol_currencies(symbol)
        forecasts.extend(
            horizon_forecast.build_horizon_forecasts(
                symbol,
                tech,
                analysis.currencies,
                calendar.risk_windows(events, {base, quote}),
                items,
                input_contexts.get(symbol),
                now=feature_time,
                calendar_ok=calendar_ok,
                profile_lookup=profile_lookup if not args.no_learning else None,
                band_provider=band_provider if not args.no_learning else None,
                calibration_provider=calibration_provider if not args.no_learning else None,
            )
        )

    if args.dry_run:
        summary = {
            "contract": horizon_journal.HORIZON_PIT_CONTRACT,
            "symbols": list(symbols),
            "rows": len(forecasts),
            "horizons": [spec.label for spec in horizons.HORIZON_SPECS],
            "warnings": fetch_warnings,
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    try:
        written = horizon_journal.append_horizon_forecasts(
            DEFAULT_HORIZON_JOURNAL_PATH,
            forecasts,
            prediction_time=datetime.now(UTC),
            source_cutoff=now,
            max_feature_available_time=feature_time,
        )
    except (OSError, horizon_journal.HorizonPointInTimeError) as error:
        print(f"ホライズン予測(shadow)の記録失敗: {error}", file=sys.stderr)
        return 1
    print(
        f"ホライズン予測をshadow記録しました "
        f"({', '.join(symbols)} | {written}行 | contract={horizon_journal.HORIZON_PIT_CONTRACT})"
    )
    return 0


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
    news_warnings,
    macro_snapshot,
    atr_multiple,
    fetch_warnings,
    items,
    now,
    operational_data_ok,
    operational_data_reason,
    active_direction_threshold,
    learning_dimensions,
    price_rows,
    input_contexts,
) -> int:
    """時間足別モードの本体(main から分岐)。

    各時間足を独立に判断し、専用ジャーナルへ記録、時間足別の主ホライズンで
    自己採点・学習して次回の確信度に反映する。融合1判断モードとは
    ジャーナル・学習ファイルを分ける(スキーマも採点ホライズンも異なるため)。
    """
    journal_entries = list(journal.read_entries(DEFAULT_TF_JOURNAL_PATH))

    # 採点用の将来価格系列を組む。判断ジャーナル(源A)に加え、通知停止中も
    # fx_tf_snapshot.py で継続できる価格専用系列と、今回の現在価格を
    # 結合する。direction を持たない価格行は採点対象を増やさず将来価格系列だけを
    # 密にするので、15m/1h/4h/1d の全時間足が採点可能になる。
    current_snapshot = price_history.snapshot_entries(
        {
            symbol: {tf: tech_map[symbol].price_snapshot(tf) for tf in timeframe.DEFAULT_TIMEFRAMES}
            for symbol in symbols
        },
        # The main analysis timestamp is captured before network I/O. Price
        # availability must instead reflect acquisition completion.
        now=datetime.now(UTC),
    )
    # 判断時点のTradingView現在値は方向採点には使うが、完了済み約定経路ではない。
    # trade_outcome側がTP/SL・MFE/MAE経路から除外できるよう用途を明記する。
    for row in current_snapshot:
        row["price_usage"] = "direction_only"
    # 5分ボード自身が方向を持たない価格系列も保存するため、別の価格取得ループを
    # 併走せずに短期足の採点と鮮度監視を維持できる。
    if args.signal_board and not args.no_price_write and not args.no_journal and not args.dry_run:
        try:
            price_history.append_snapshot_entries(DEFAULT_TF_PRICES_PATH, current_snapshot)
        except (OSError, price_history.PriceHistoryWriteError) as error:
            fetch_warnings.append(f"時間足別価格スナップショット書き込み失敗: {error}")
    scoring_entries = journal_entries + price_rows + current_snapshot

    # 学習: 時間足別ジャーナルを (symbol, timeframe) 別に採点しプロファイル導出
    tf_learn = tf_learning.TimeframeLearning()
    learning_note = ""
    if not args.no_learning:
        tf_learn = tf_learning.derive_timeframe_learning(scoring_entries, now=now)
        learning_note = tf_learn.summary_ja()
        if not args.dry_run:
            try:
                tf_learning.save_timeframe_learning(tf_learn, DEFAULT_TF_LEARNING_PATH)
            except OSError as error:
                fetch_warnings.append(f"時間足別学習プロファイル保存失敗: {error}")

    profile_lookup = tf_learn.profile_lookup if not args.no_learning else None
    decision_feedback_profile = decision_feedback.DecisionFeedbackProfile()
    decision_feedback_lookup = None
    if not args.no_learning and not args.no_trade_expectancy:
        decision_feedback_profile = decision_feedback.derive_decision_feedback(
            decision_feedback.load_decision_outcome_report(DEFAULT_DECISION_OUTCOMES_PATH),
            now=now,
        )
        learning_note = append_note(learning_note, decision_feedback_profile.summary_ja())
        if not args.no_trade_expectancy_guard:
            decision_feedback_lookup = decision_feedback_profile.expectancy_lookup
        if not args.dry_run:
            try:
                decision_feedback.save_decision_feedback(
                    decision_feedback_profile,
                    DEFAULT_DECISION_FEEDBACK_PATH,
                )
            except OSError as error:
                fetch_warnings.append(f"失敗理由フィードバック保存失敗: {error}")

    tp_sl_lookup = None
    tp_sl_learn = None
    if not args.no_learning and not args.no_trade_expectancy:
        tp_sl_learn = tp_sl_learning.derive_timeframe_tp_sl_learning(scoring_entries, now=now)
        learning_note = append_note(learning_note, tp_sl_learn.summary_ja())
        tp_sl_lookup = tp_sl_learn.expectancy_lookup
        if not args.dry_run:
            try:
                tp_sl_learning.save_timeframe_tp_sl_learning(
                    tp_sl_learn, DEFAULT_TP_SL_LEARNING_PATH
                )
            except OSError as error:
                fetch_warnings.append(f"TP/SL学習プロファイル保存失敗: {error}")

    target_r_adjuster = None
    if not args.no_trade_expectancy:
        target_r_adjuster = make_approved_tp_sl_adjuster(
            trade_outcome.load_improvement_registry(args.trade_improvement_registry)
        )
    expectancy_lookup = None
    _expectancy_summaries: dict[str, dict] = {}
    if not args.no_trade_expectancy:
        expectancy_lookup, expectancy_note, _expectancy_summaries = (
            make_timeframe_trade_expectancy_lookup(scoring_entries)
        )
        learning_note = append_note(learning_note, expectancy_note)
        if args.no_trade_expectancy_guard:
            expectancy_lookup = None
    maximization_lookup = None
    max_profile = None
    if not args.no_learning and not args.no_trade_expectancy:
        max_profile = maximization.derive_timeframe_maximization(scoring_entries, now=now)
        learning_note = append_note(learning_note, max_profile.summary_ja())
        if not args.no_trade_expectancy_guard:
            maximization_lookup = max_profile.expectancy_lookup
        if not args.dry_run:
            try:
                maximization.save_timeframe_maximization(max_profile, DEFAULT_MAXIMIZATION_PATH)
            except OSError as error:
                fetch_warnings.append(f"最大化プロファイル保存失敗: {error}")
    if maximization_lookup is not None:
        expectancy_lookup = maximization_lookup
    expectancy_lookup = compose_timeframe_expectancy_lookups(
        decision_feedback_lookup,
        expectancy_lookup,
    )
    expectancy_lookup = compose_timeframe_expectancy_lookups(tp_sl_lookup, expectancy_lookup)

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
            operational_data_ok=operational_data_ok,
            operational_data_reason=operational_data_reason,
            profile_lookup=profile_lookup,
            expectancy_lookup=expectancy_lookup,
            target_r_adjuster=target_r_adjuster,
            direction_threshold=active_direction_threshold,
            learning_dimensions=learning_dimensions,
            input_context=input_contexts.get(symbol),
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
            print(f"時間足別ジャーナル書き込み失敗: {error}", file=sys.stderr)
            return JOURNAL_WRITE_FAILURE_EXIT_CODE
        try:
            prior_decision_events = list(
                decision_log.read_decision_events(DEFAULT_DECISION_LOG_PATH)
            )
            decision_events = decision_log.build_timeframe_decision_events(
                plans_by_symbol,
                now=now,
                analysis=analysis,
                tech_map=tech_map,
                news_items=items,
                events_48h=events_48h,
                fetch_warnings=fetch_warnings,
                calendar_ok=calendar_ok,
                macro_snapshot=macro_snapshot,
                timeframe_learning=tf_learn if not args.no_learning else None,
                tp_sl_learning=tp_sl_learn,
                maximization_profile=max_profile,
                decision_feedback_profile=decision_feedback_profile,
                expectancy_summaries=_expectancy_summaries,
            )
            decision_outcome_report = decision_log.score_decision_events(
                [*prior_decision_events, *decision_events],
                price_entries=[*price_rows, *current_snapshot],
                now=now,
            )
            decision_log.append_decision_events(DEFAULT_DECISION_LOG_PATH, decision_events)
            decision_log.save_latest_snapshot(
                DEFAULT_DECISION_LATEST_PATH,
                decision_events,
                now=now,
            )
            decision_log.save_outcome_report(
                decision_outcome_report,
                DEFAULT_DECISION_OUTCOMES_PATH,
            )
            decision_feedback.save_decision_feedback(
                decision_feedback.derive_decision_feedback(decision_outcome_report, now=now),
                DEFAULT_DECISION_FEEDBACK_PATH,
            )
        except OSError as error:
            fetch_warnings.append(f"完全判断ログ書き込み失敗: {error}")

    if args.signal_board:
        data_quality = signal_board.assess_data_quality(
            plans_by_symbol,
            news_warnings=news_warnings,
            calendar_ok=calendar_ok,
            macro_snapshot=macro_snapshot,
            now=now,
        )
        payload = signal_board.build_signal_board_payload(
            plans_by_symbol,
            analysis,
            tech_map,
            data_quality,
            now=now,
        )
    else:
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
        if payload.get("embeds"):
            print(json.dumps(payload["embeds"], ensure_ascii=False, indent=2))
        return 0

    if args.no_discord:
        print(
            f"時間足別ブリーフィングを記録しました(Discord送信なし) "
            f"({', '.join(symbols)} | ニュース{len(items)}件 | "
            f"イベント{len(events_48h)}件 | {analysis.engine})"
        )
        return 0

    webhook_url = load_webhook_url()
    if not webhook_url:
        print(
            "DISCORD_WEBHOOK_URL が未設定です。環境変数か .env に設定してください。",
            file=sys.stderr,
        )
        return NOTIFICATION_FAILURE_EXIT_CODE

    try:
        post_to_discord(webhook_url, payload)
    except discord_delivery.DiscordDeliveryError as error:
        print(str(error), file=sys.stderr)
        return NOTIFICATION_FAILURE_EXIT_CODE
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
        "--horizon-only",
        action="store_true",
        help="設計Aの9ホライズンshadow生成・採点だけを実行する(Discord通知なし)",
    )
    parser.add_argument(
        "--horizon-symbols",
        nargs="+",
        default=list(horizons.DEFAULT_HORIZON_SYMBOLS),
        metavar="SYMBOL",
        help="ホライズンtrack対象ペア(既定: USDJPY EURUSD GBPUSD)",
    )
    parser.add_argument(
        "--no-horizon-forecasts",
        action="store_true",
        help="設計Aのshadow journal追記を停止するロールバックフラグ",
    )
    parser.add_argument(
        "--no-learning",
        action="store_true",
        help="学習プロファイルによる重み・確信度の自動調整を行わない(既定重みで実行)",
    )
    parser.add_argument(
        "--no-trade-expectancy",
        action="store_true",
        help="TP/SL込み期待値監査・改善候補レジストリ・期待値ガードを使わない",
    )
    parser.add_argument(
        "--no-trade-expectancy-guard",
        action="store_true",
        help="期待値監査は表示・記録するが、今回の判断への減衰/見送り反映は行わない",
    )
    parser.add_argument(
        "--no-macro",
        action="store_true",
        help="マクロデータ(COT・金利・VIX・ドル指数)の取得と委員を使わない",
    )
    parser.add_argument(
        "--cot-pit-dataset",
        type=Path,
        default=None,
        help="監査済みCFTC COT PIT artifactディレクトリ。未指定時はlegacy COTを使用しない",
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
        help="廃止済み。研究/分析CLIからのlive昇格は禁止",
    )
    parser.add_argument(
        "--require-freshness",
        action="store_true",
        help="新規方向判断に最新の正常なfreshness reportを必須化する",
    )
    parser.add_argument(
        "--freshness-report",
        type=Path,
        default=DEFAULT_FRESHNESS_REPORT_PATH,
        help="運用データ鮮度レポートJSON",
    )
    parser.add_argument(
        "--freshness-max-age-seconds",
        type=float,
        default=600.0,
        help="freshness report自体の最大許容経過秒数",
    )
    parser.add_argument(
        "--no-price-write",
        action="store_true",
        help="signal-boardから価格系列へ追記しない(snapshot writer併走時に使用)",
    )
    parser.add_argument(
        "--per-timeframe",
        action="store_true",
        help="時間足別モード: 15m/1h/4h/1d を独立に判断し、時間足ごとの主ホライズン"
        "(15m→15分後/1h→1h/4h→4h/1d→24h)で自己採点・学習する",
    )
    parser.add_argument(
        "--signal-board",
        action="store_true",
        help="Discord通知を上位3候補・システム状態・データ品質をまとめた単一ボードにする"
        "（時間足別モードを自動的に有効化）",
    )
    parser.add_argument(
        "--score-trade-outcomes",
        action="store_true",
        help="判断ジャーナルをMFE/MAE/TP/SLで採点し、期待値監査レポートを表示する",
    )
    parser.add_argument(
        "--retest-trade-variants",
        action="store_true",
        help="TP1/TP2のR倍率候補を過去ジャーナルでpaper再採点する",
    )
    parser.add_argument(
        "--check-trade-outcome-health",
        action="store_true",
        help="期待値・サンプル数・経路品質のヘルスチェックを実行し、失敗時に終了コード1を返す",
    )
    parser.add_argument(
        "--trade-outcome-journal",
        type=Path,
        default=DEFAULT_JOURNAL_PATH,
        help="期待値監査に使う判断ジャーナル(JSONL)",
    )
    parser.add_argument(
        "--trade-outcome-json",
        type=Path,
        default=None,
        help="期待値監査の詳細JSONを書き出すパス",
    )
    parser.add_argument(
        "--trade-variant-json",
        type=Path,
        default=None,
        help="TP/SL候補paper再採点の詳細JSONを書き出すパス",
    )
    parser.add_argument(
        "--trade-monitor-json",
        type=Path,
        default=None,
        help="cron/dashboard向けの期待値監視JSONを書き出すパス",
    )
    parser.add_argument(
        "--tp1-r-candidates",
        type=float,
        nargs="+",
        default=None,
        help="paper再採点するTP1のR倍率候補(例: 0.75 1.0 1.25)",
    )
    parser.add_argument(
        "--tp2-r-candidates",
        type=float,
        nargs="+",
        default=None,
        help="paper再採点するTP2のR倍率候補(例: 1.5 2.0 2.5)",
    )
    parser.add_argument(
        "--trade-improvement-registry",
        type=Path,
        default=DEFAULT_TRADE_IMPROVEMENT_REGISTRY,
        help="改善候補レジストリJSONの保存先",
    )
    parser.add_argument(
        "--update-trade-improvement-registry",
        action="store_true",
        help="期待値監査やTP/SL候補再採点で検出した改善候補をレジストリへ反映する",
    )
    parser.add_argument(
        "--approve-trade-candidate",
        metavar="CANDIDATE_ID",
        default=None,
        help="paper_readyの改善候補を人間承認し、stage=approvedとして記録する",
    )
    parser.add_argument(
        "--reject-trade-candidate",
        metavar="CANDIDATE_ID",
        default=None,
        help="activeな改善候補を却下し、stage=rejectedとして記録する",
    )
    parser.add_argument(
        "--resume-trade-candidate",
        metavar="CANDIDATE_ID",
        default=None,
        help="auto_pausedの改善候補を人間判断で再開し、stage=approvedへ戻す",
    )
    parser.add_argument(
        "--trade-approval-actor",
        default="manual",
        help="改善候補の承認/却下者名",
    )
    parser.add_argument(
        "--trade-approval-note",
        default="",
        help="改善候補の承認/却下メモ",
    )
    parser.add_argument(
        "--trade-outcome-health-require-sample",
        action="store_true",
        help="ヘルスチェックで最低サンプル数未満を失敗扱いにする",
    )
    parser.add_argument("--dry-run", action="store_true", help="Discordに送信せず内容を表示する")
    parser.add_argument(
        "--no-discord",
        action="store_true",
        help="Discordには送信せず、判断ジャーナル・学習ファイルなどのローカル保存だけ行う",
    )
    args = parser.parse_args(argv)
    if args.promote_live is not None:
        parser.error("--promote-live is disabled; this build is research/shadow only")
    if args.signal_board:
        args.per_timeframe = True

    lifecycle_actions = [
        bool(args.approve_trade_candidate),
        bool(args.reject_trade_candidate),
        bool(args.resume_trade_candidate),
    ]
    if sum(lifecycle_actions) > 1:
        print(
            "--approve-trade-candidate / --reject-trade-candidate / "
            "--resume-trade-candidate は同時に使えません。"
        )
        return 2
    if args.approve_trade_candidate:
        return approve_trade_candidate_cli(
            args.trade_improvement_registry,
            args.approve_trade_candidate,
            decision="approved",
            actor=args.trade_approval_actor,
            note=args.trade_approval_note,
        )
    if args.reject_trade_candidate:
        return approve_trade_candidate_cli(
            args.trade_improvement_registry,
            args.reject_trade_candidate,
            decision="rejected",
            actor=args.trade_approval_actor,
            note=args.trade_approval_note,
        )
    if args.resume_trade_candidate:
        return approve_trade_candidate_cli(
            args.trade_improvement_registry,
            args.resume_trade_candidate,
            decision="resumed",
            actor=args.trade_approval_actor,
            note=args.trade_approval_note,
        )

    if args.score_trade_outcomes:
        return score_trade_outcomes_cli(
            args.trade_outcome_journal,
            json_report_path=args.trade_outcome_json,
            improvement_registry_path=(
                args.trade_improvement_registry if args.update_trade_improvement_registry else None
            ),
            monitor_json_path=args.trade_monitor_json,
        )
    if args.retest_trade_variants:
        return retest_trade_variants_cli(
            args.trade_outcome_journal,
            json_report_path=args.trade_variant_json,
            improvement_registry_path=(
                args.trade_improvement_registry if args.update_trade_improvement_registry else None
            ),
            target1_r_candidates=args.tp1_r_candidates,
            target2_r_candidates=args.tp2_r_candidates,
        )
    if args.check_trade_outcome_health:
        return check_trade_outcome_health_cli(
            args.trade_outcome_journal,
            require_sample_ok=args.trade_outcome_health_require_sample,
        )

    symbols = [s.upper().replace("/", "") for s in args.symbols]
    horizon_symbols = list(dict.fromkeys(s.upper().replace("/", "") for s in args.horizon_symbols))
    if args.horizon_only and args.no_horizon_forecasts:
        print("--horizon-only と --no-horizon-forecasts は同時に指定できません")
        return 2
    data_symbols = horizon_symbols if args.horizon_only else symbols
    fast_window, slow_window, atr_multiple, params_warning = load_strategy_params()
    now = datetime.now(UTC)
    trade_improvement_registry = (
        load_pit_improvement_registry(args.trade_improvement_registry)
        if not args.no_trade_expectancy
        else {}
    )
    target_r_adjuster = (
        make_approved_tp_sl_adjuster(trade_improvement_registry)
        if not args.no_trade_expectancy
        else None
    )

    currencies: set[str] = set()
    for symbol in data_symbols:
        base, quote = calendar.symbol_currencies(symbol)
        currencies.update((base, quote))
    ordered_currencies = sorted(currencies)

    fetch_warnings: list[str] = []
    if params_warning:
        fetch_warnings.append(params_warning)
    freshness_gate = freshness.FreshnessGate(
        allow_new_risk=True,
        status="not_required",
        reason="freshness gate not requested",
    )
    if args.require_freshness:
        freshness_gate = freshness.evaluate_freshness_report(
            args.freshness_report,
            now=now,
            max_report_age_seconds=args.freshness_max_age_seconds,
        )
        if not freshness_gate.allow_new_risk:
            fetch_warnings.append(f"⛔ 運用データ鮮度ゲート: {freshness_gate.reason}")
    threshold_policy = (
        None
        if args.horizon_only
        else direction_threshold.load_policy(DEFAULT_DIRECTION_THRESHOLD_POLICY_PATH)
    )
    threshold_report = (
        {}
        if args.horizon_only
        else decision_feedback.load_decision_outcome_report(DEFAULT_DECISION_OUTCOMES_PATH)
    )
    raw_threshold_outcomes = threshold_report.get("outcomes")
    threshold_outcomes = (
        [row for row in raw_threshold_outcomes if isinstance(row, dict)]
        if isinstance(raw_threshold_outcomes, list)
        else []
    )
    if threshold_policy is not None:
        updated_threshold_policy = direction_threshold.auto_pause_policy(
            threshold_policy,
            threshold_outcomes,
        )
        if updated_threshold_policy != threshold_policy:
            threshold_policy = updated_threshold_policy
            fetch_warnings.append(
                "売買閾値ポリシーを自動停止: " f"{threshold_policy.auto_pause_reason or '純R劣化'}"
            )
            if not args.dry_run:
                direction_threshold.save_policy(
                    threshold_policy,
                    DEFAULT_DIRECTION_THRESHOLD_POLICY_PATH,
                )
    active_direction_threshold = direction_threshold.effective_threshold(threshold_policy, now=now)

    # 1. 経済指標カレンダー(レート制限対策にローカルキャッシュ併用)
    events, calendar_warnings = calendar.fetch_calendar(cache_path=DEFAULT_CALENDAR_CACHE)
    fetch_warnings.extend(calendar_warnings)
    # イベントが1件も取れていない=警戒窓判定が機能しない状態。判断側で安全側に倒す
    calendar_ok = bool(events)
    events_48h = calendar.upcoming_events(
        events, currencies, now, hours_ahead=args.hours_ahead, min_impact="high"
    )
    if not args.horizon_only and not args.no_export_events and events:
        try:
            calendar.export_events_csv(events, DEFAULT_EVENTS_CSV)
        except OSError as error:
            fetch_warnings.append(f"イベントCSV書き出し失敗: {error}")
    if not args.horizon_only and not args.no_event_archive and events:
        try:
            calendar.append_events_archive(events, DEFAULT_EVENTS_ARCHIVE, now=now)
        except OSError as error:
            fetch_warnings.append(f"イベント履歴アーカイブ追記失敗: {error}")

    # 2. ニュース収集
    items, news_warnings = news.fetch_news_for_symbols(data_symbols, hours_back=args.hours_back)
    fetch_warnings.extend(news_warnings)

    # 3. マクロデータ。FRED系は現行snapshot、COTは明示された監査済みPIT artifact
    # だけをprediction timeでas-of読込みする。legacy current snapshotは使わない。
    macro_snapshot = None
    if not args.no_macro:
        macro_snapshot = macro.fetch_macro_snapshot(
            DEFAULT_MACRO_CACHE,
            now=now,
            include_cot=False,
        )
        _attach_cot_pit_dataset(
            macro_snapshot,
            args.cot_pit_dataset,
            prediction_time=now,
        )
        fetch_warnings.extend(macro_snapshot.warnings)

    # 4. センチメント分析(Claude API → 自前分析エンジン。レジームはマクロ実データ優先)
    analysis = sentiment.analyze_market(
        items, ordered_currencies, use_llm=not args.no_llm, macro=macro_snapshot, now=now
    )
    learning_dimensions = market_session.build_learning_dimensions(
        now,
        regime=analysis.regime,
        analysis_engine=analysis.engine,
        macro_available=(macro_snapshot is not None and macro_snapshot.coverage() > 0),
    ).to_dict()

    # 5. テクニカル取得
    tech_map, tech_warnings = technicals.fetch_pair_technicals(
        data_symbols, fast_window=fast_window, slow_window=slow_window
    )
    fetch_warnings.extend(tech_warnings)

    # C input context: completed M5 history is used only as an as-of spread
    # baseline.  Each symbol gets one context shared by fusion and every
    # timeframe; the context is record/shadow-only at this stage.
    input_price_rows = list(journal.read_entries(DEFAULT_TF_PRICES_PATH))
    input_contexts = build_decision_input_contexts(
        data_symbols,
        tech_map,
        macro_snapshot=macro_snapshot,
        learning_dimensions=learning_dimensions,
        price_rows=input_price_rows,
        now=now,
        fetch_warnings=fetch_warnings,
    )

    if args.horizon_only:
        return _run_horizon_track(
            args=args,
            symbols=horizon_symbols,
            tech_map=tech_map,
            analysis=analysis,
            events=events,
            calendar_ok=calendar_ok,
            fetch_warnings=fetch_warnings,
            items=items,
            now=now,
            price_rows=input_price_rows,
            input_contexts=input_contexts,
        )

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
            news_warnings=news_warnings,
            macro_snapshot=macro_snapshot,
            atr_multiple=atr_multiple,
            fetch_warnings=fetch_warnings,
            items=items,
            now=now,
            operational_data_ok=freshness_gate.allow_new_risk,
            operational_data_reason=freshness_gate.reason,
            active_direction_threshold=active_direction_threshold,
            learning_dimensions=learning_dimensions,
            price_rows=input_price_rows,
            input_contexts=input_contexts,
        )

    # 6. 学習ループ: ジャーナル履歴を相互採点し、重み・確信度の調整を導出
    profile = learning.LearnedProfile()
    learning_note = ""
    calls: list[learning.EvaluatedCall] = []
    journal_entries = list(journal.read_entries(DEFAULT_JOURNAL_PATH))
    pit_journal_entries = [
        entry for entry in journal_entries if journal.is_pit_eligible_entry(entry)
    ]
    if not args.no_learning:
        calls = learning.evaluate_history(journal_entries, require_pit=True)
        profile = learning.derive_profile(calls, now=now)
        learning_note = profile.summary_ja()
        # ホライズン別(4h/24h/72h)の的中率観測。学習は24hのみを使う
        horizon_line = learning.horizon_report_ja(journal_entries, require_pit=True)
        if horizon_line:
            learning_note = (learning_note + "\n" + horizon_line).strip()
        if not args.dry_run:
            try:
                learning.save_profile(profile, DEFAULT_LEARNING_PATH)
            except OSError as error:
                fetch_warnings.append(f"学習プロファイル保存失敗: {error}")

    expectancy_adjuster = None
    decision_feedback_adjuster = None
    decision_feedback_profile = decision_feedback.DecisionFeedbackProfile()
    decision_outcome_history = decision_feedback.load_decision_outcome_report(
        DEFAULT_DECISION_OUTCOMES_PATH
    )
    trade_expectancy_summary: dict[str, object] = {}
    if not args.no_learning and not args.no_trade_expectancy:
        decision_feedback_profile = decision_feedback.derive_decision_feedback(
            decision_outcome_history,
            now=now,
        )
        learning_note = append_note(learning_note, decision_feedback_profile.summary_ja())
        if not args.no_trade_expectancy_guard:
            decision_feedback_adjuster = decision_feedback_profile.fusion_adjuster()
        if not args.dry_run:
            try:
                decision_feedback.save_decision_feedback(
                    decision_feedback_profile,
                    DEFAULT_DECISION_FEEDBACK_PATH,
                )
            except OSError as error:
                fetch_warnings.append(f"失敗理由フィードバック保存失敗: {error}")

    if not args.no_trade_expectancy:
        trade_outcomes = trade_outcome.evaluate_trade_outcomes(pit_journal_entries)
        trade_expectancy_summary = trade_outcome.summarize_expectancy(trade_outcomes)
        trade_expectancy_note = trade_outcome.format_expectancy_report_ja(
            trade_expectancy_summary, limit=3
        )
        if trade_expectancy_note != "トレード期待値監視: 対象なし":
            learning_note = append_note(learning_note, trade_expectancy_note)
        overall_stats = trade_expectancy_summary.get("overall")
        evaluated_outcomes = (
            int(overall_stats.get("evaluated", 0)) if isinstance(overall_stats, dict) else 0
        )
        if not args.dry_run and evaluated_outcomes > 0:
            try:
                candidates = trade_outcome.improvement_candidates(trade_expectancy_summary)
                registry = trade_outcome.update_improvement_registry(
                    trade_improvement_registry,
                    candidates,
                    now=now,
                    managed_action_types=trade_outcome.EXPECTANCY_CANDIDATE_ACTION_TYPES,
                    data_contract=journal.FUSION_PIT_DATA_CONTRACT,
                )
                registry, paused_policies = (
                    trade_outcome.auto_pause_underperforming_approved_policies(
                        registry,
                        trade_expectancy_summary,
                        now=now,
                    )
                )
                # 自動停止をこの実行のプランへ即時反映する。実行冒頭の
                # レジストリから作った注入器のままだと、いま停止した
                # ポリシーのTP/SLが今回のプランに最後まで適用されてしまう
                target_r_adjuster = make_approved_tp_sl_adjuster(registry)
                for paused in paused_policies:
                    fetch_warnings.append(
                        "承認済みTP/SLを自動停止: "
                        f"{paused['candidate_id']} — {paused['reason_ja']}"
                    )
                trade_outcome.save_improvement_registry(registry, args.trade_improvement_registry)
            except OSError as error:
                fetch_warnings.append(f"期待値改善候補レジストリ保存失敗: {error}")
        if not args.no_trade_expectancy_guard:
            expectancy_adjuster = make_trade_expectancy_adjuster(trade_expectancy_summary)
    expectancy_adjuster = compose_trade_expectancy_adjusters(
        decision_feedback_adjuster,
        expectancy_adjuster,
    )

    # 7. ML確率モデル: --train-mlで強制再学習。それ以外も保存済みモデルが
    #    無い/staleなら自動再学習する(スキルゲートは train_artifact 内)
    ml_artifact = ml.MLArtifact()
    if not args.no_ml:
        if not args.train_ml:
            ml_artifact = ml.load_artifact(DEFAULT_ML_MODEL_PATH)
        if args.train_ml or ml_needs_retrain(ml_artifact, now):
            train_calls = calls or learning.evaluate_history(journal_entries, require_pit=True)
            # 収益ラベル(trade_outcomeのrealized_net_r)のML接続はMLモデル拡張PRで
            # train_artifact(return_outcomes=...) に配線される
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
    # Live acknowledgement is deliberately unreachable from this research CLI.
    require_live_ack: list[str] = []
    raw_shadow_outcomes = decision_outcome_history.get("shadow_outcomes", [])
    shadow_outcomes = (
        [row for row in raw_shadow_outcomes if isinstance(row, Mapping)]
        if isinstance(raw_shadow_outcomes, list)
        else []
    )
    promotion_state, _member_perf = promotion.evaluate_and_update(
        pit_journal_entries,
        promotion_state,
        now=now,
        require_live_ack=require_live_ack,
        shadow_outcomes=shadow_outcomes,
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

    # 外部取得・特徴量変換・学習済み状態の準備がすべて終わった時刻を記録する。
    # prediction time は全プラン構築後に別途採るため、PIT契約は
    # source cutoff <= max feature available time <= prediction time になる。
    feature_available_time = datetime.now(UTC)
    if args.require_freshness:
        refreshed_gate = freshness.evaluate_freshness_report(
            args.freshness_report,
            now=feature_available_time,
            max_report_age_seconds=args.freshness_max_age_seconds,
        )
        if not refreshed_gate.allow_new_risk and (
            freshness_gate.allow_new_risk or refreshed_gate.reason != freshness_gate.reason
        ):
            fetch_warnings.append(f"⛔ 運用データ鮮度ゲート: {refreshed_gate.reason}")
        freshness_gate = refreshed_gate
    events_48h = calendar.upcoming_events(
        events,
        currencies,
        feature_available_time,
        hours_ahead=args.hours_ahead,
        min_impact="high",
    )

    # 9. ペアごとの委員会審議(tech/news/macro/ML、学習済み重み・段階ゲート反映)
    plans: list[briefing.TradePlan] = []
    for symbol in symbols:
        base, quote = calendar.symbol_currencies(symbol)
        windows = calendar.risk_windows(events, {base, quote})
        plan = committee.deliberate(
            symbol,
            tech_map[symbol],
            analysis.currencies,
            windows,
            items,
            now=feature_available_time,
            atr_multiple=atr_multiple,
            calendar_ok=calendar_ok,
            operational_data_ok=freshness_gate.allow_new_risk,
            operational_data_reason=freshness_gate.reason,
            tech_weight=profile.tech_weight,
            news_weight=profile.news_weight,
            conviction_factor=profile.conviction_factor(symbol),
            condition_adjuster=profile.condition_adjustment,
            expectancy_adjuster=expectancy_adjuster,
            target_r_adjuster=target_r_adjuster,
            macro_snapshot=macro_snapshot,
            ml_artifact=ml_artifact if not args.no_ml else None,
            stages=stages,
            direction_threshold=active_direction_threshold,
            learning_dimensions=learning_dimensions,
            input_context=input_contexts.get(symbol),
        )
        # 発注前9段チェックリスト: 完成した判断を順序付きゲートに写像し、
        # スプレッド/執行コスト控除/ポジションサイズを付ける(表示・記録用)。
        realized_r = _realized_expectancy_r(
            trade_expectancy_summary if not args.no_trade_expectancy else None,
            symbol,
            plan.direction,
        )
        checklist = decision_pipeline.build_checklist(
            plan,
            tech_map[symbol],
            now=feature_available_time,
            realized_expectancy_r=realized_r,
            operational_data_ok=freshness_gate.allow_new_risk,
            operational_data_reason=freshness_gate.reason,
        )
        plan.checklist = checklist.to_dict()
        plans.append(plan)

    prediction_time = datetime.now(UTC)

    # 10. 判断ジャーナル: 過去の判断を検証し、今回の判断を記録
    journal_note = ""
    if not args.no_journal:
        closes = {symbol: tech_map[symbol].close() for symbol in symbols}
        stats = journal.evaluate_directional_accuracy(
            DEFAULT_JOURNAL_PATH, closes, now=prediction_time
        )
        journal_note = journal.format_stats_ja(stats)
        if not args.dry_run:
            try:
                journal.append_plans(
                    DEFAULT_JOURNAL_PATH,
                    plans,
                    now=prediction_time,
                    source_cutoff=now,
                    max_feature_available_time=feature_available_time,
                )
            except (OSError, journal.PointInTimeError) as error:
                print(f"判断ジャーナル書き込み失敗: {error}", file=sys.stderr)
                return JOURNAL_WRITE_FAILURE_EXIT_CODE
            try:
                prior_decision_events = list(
                    decision_log.read_decision_events(DEFAULT_DECISION_LOG_PATH)
                )
                decision_events = decision_log.build_fusion_decision_events(
                    plans,
                    now=prediction_time,
                    analysis=analysis,
                    tech_map=tech_map,
                    news_items=items,
                    events_48h=events_48h,
                    fetch_warnings=fetch_warnings,
                    calendar_ok=calendar_ok,
                    macro_snapshot=macro_snapshot,
                    learning_profile=profile if not args.no_learning else None,
                    trade_expectancy_summary=(
                        trade_expectancy_summary if not args.no_trade_expectancy else None
                    ),
                    decision_feedback_profile=decision_feedback_profile,
                    ml_artifact=ml_artifact if not args.no_ml else None,
                    promotion_state=promotion_state,
                )
                decision_outcome_report = decision_log.score_decision_events(
                    [*prior_decision_events, *decision_events],
                    now=prediction_time,
                )
                decision_log.append_decision_events(DEFAULT_DECISION_LOG_PATH, decision_events)
                decision_log.save_latest_snapshot(
                    DEFAULT_DECISION_LATEST_PATH,
                    decision_events,
                    now=prediction_time,
                )
                decision_log.save_outcome_report(
                    decision_outcome_report,
                    DEFAULT_DECISION_OUTCOMES_PATH,
                )
                decision_feedback.save_decision_feedback(
                    decision_feedback.derive_decision_feedback(
                        decision_outcome_report, now=prediction_time
                    ),
                    DEFAULT_DECISION_FEEDBACK_PATH,
                )
            except OSError as error:
                fetch_warnings.append(f"完全判断ログ書き込み失敗: {error}")

    if ml_artifact.model is not None:
        learning_note = append_note(learning_note, ml_artifact.summary_ja())

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
        now=prediction_time,
    )

    if args.dry_run:
        print(payload["content"])
        print(json.dumps(payload["embeds"], ensure_ascii=False, indent=2))
        return 0

    if args.no_discord:
        print(
            f"ブリーフィングを記録しました(Discord送信なし) "
            f"({', '.join(symbols)} | ニュース{len(items)}件 | "
            f"イベント{len(events_48h)}件 | {analysis.engine})"
        )
        return 0

    webhook_url = load_webhook_url()
    if not webhook_url:
        print(
            "DISCORD_WEBHOOK_URL が未設定です。環境変数か .env に設定してください。",
            file=sys.stderr,
        )
        return NOTIFICATION_FAILURE_EXIT_CODE

    try:
        post_to_discord(webhook_url, payload)
    except discord_delivery.DiscordDeliveryError as error:
        print(str(error), file=sys.stderr)
        return NOTIFICATION_FAILURE_EXIT_CODE
    print(
        f"ブリーフィングを送信しました ({', '.join(symbols)} | "
        f"ニュース{len(items)}件 | イベント{len(events_48h)}件 | {analysis.engine})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

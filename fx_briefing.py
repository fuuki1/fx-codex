"""ニュース×経済指標×テクニカルを統合したFXデスクブリーフィングをDiscordへ送る。

機関投資家のモーニングブリーフィングを模して、以下を1回の通知に統合する:

1. 経済指標カレンダー(ForexFactory公開フィード) — 今後48時間の重要イベント、
   イベント前後の警戒窓判定(research-maxプリセットと同じ 前120分/後180分)
2. ニュースヘッドライン(FXStreet / Google News RSS) — 通貨タグ付け
3. センチメント分析 — 語彙ベース(常時) + Claude API(ANTHROPIC_API_KEYがあれば)
4. TradingViewマルチタイムフレームテクニカル(15m/1h/4h/1d)
5. 複合スコア → ペアごとのトレードプラン(方向・確信度・ATRベースSL/TP)

使い方:
    .venv/bin/python fx_briefing.py                  # Discordへ送信
    .venv/bin/python fx_briefing.py --dry-run        # 送信せず内容を表示
    .venv/bin/python fx_briefing.py --symbols USDJPY GBPJPY --no-llm

副産物として research_pack/upcoming_events.csv を書き出す
(fx_backtester の --events でそのまま使える形式)。

Webhook URLは環境変数 DISCORD_WEBHOOK_URL か .env から読み込む。
Claude分析は ANTHROPIC_API_KEY が設定されている場合のみ有効。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

from fx_intel import briefing, calendar, news, sentiment, technicals

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_SYMBOLS = ["USDJPY", "EURUSD"]
DEFAULT_EVENTS_CSV = PROJECT_ROOT / "research_pack" / "upcoming_events.csv"


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


def load_strategy_params() -> tuple[int, int, float]:
    """strategy_params.json から (fast, slow, atr_multiple) を読む。"""
    params_path = PROJECT_ROOT / "strategy_params.json"
    fast, slow, atr_multiple = 20, 100, briefing.DEFAULT_ATR_MULTIPLE
    if params_path.exists():
        try:
            params = json.loads(params_path.read_text(encoding="utf-8"))
            fast = int(params.get("fast_window", fast))
            slow = int(params.get("slow_window", slow))
            atr_multiple = float(params.get("atr_multiple", atr_multiple))
        except (ValueError, json.JSONDecodeError):
            pass
    return fast, slow, atr_multiple


def post_to_discord(webhook_url: str, payload: dict) -> None:
    response = requests.post(webhook_url, json=payload, timeout=15)
    if response.status_code >= 300:
        raise RuntimeError(
            f"Discord通知に失敗: HTTP {response.status_code} {response.text[:200]}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="ニュース×経済指標×テクニカル統合ブリーフィングをDiscordへ送る"
    )
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    parser.add_argument(
        "--hours-ahead", type=float, default=48.0,
        help="経済イベントを何時間先まで表示するか(既定48)",
    )
    parser.add_argument(
        "--hours-back", type=float, default=24.0,
        help="ニュースを何時間前まで集めるか(既定24)",
    )
    parser.add_argument(
        "--no-llm", action="store_true",
        help="Claude API分析を使わず語彙ベースのみで実行",
    )
    parser.add_argument(
        "--no-export-events", action="store_true",
        help="research_pack/upcoming_events.csv の書き出しを行わない",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Discordに送信せず内容を表示する"
    )
    args = parser.parse_args(argv)

    symbols = [s.upper().replace("/", "") for s in args.symbols]
    fast_window, slow_window, atr_multiple = load_strategy_params()
    now = datetime.now(timezone.utc)

    currencies: set[str] = set()
    for symbol in symbols:
        base, quote = calendar.symbol_currencies(symbol)
        currencies.update((base, quote))
    ordered_currencies = sorted(currencies)

    fetch_warnings: list[str] = []

    # 1. 経済指標カレンダー(レート制限対策にローカルキャッシュ併用)
    events, calendar_warnings = calendar.fetch_calendar(
        cache_path=PROJECT_ROOT / "logs" / "calendar_cache.json"
    )
    fetch_warnings.extend(calendar_warnings)
    events_48h = calendar.upcoming_events(
        events, currencies, now, hours_ahead=args.hours_ahead, min_impact="high"
    )
    if not args.no_export_events and events:
        try:
            calendar.export_events_csv(events, DEFAULT_EVENTS_CSV)
        except OSError as error:
            fetch_warnings.append(f"イベントCSV書き出し失敗: {error}")

    # 2. ニュース収集
    items, news_warnings = news.fetch_news_for_symbols(
        symbols, hours_back=args.hours_back
    )
    fetch_warnings.extend(news_warnings)

    # 3. センチメント分析(Claude API → 語彙ベースの順に試行)
    analysis = sentiment.analyze_market(
        items, ordered_currencies, use_llm=not args.no_llm
    )

    # 4. テクニカル取得
    tech_map, tech_warnings = technicals.fetch_pair_technicals(
        symbols, fast_window=fast_window, slow_window=slow_window
    )
    fetch_warnings.extend(tech_warnings)

    # 5. ペアごとのトレードプラン
    plans: list[briefing.TradePlan] = []
    for symbol in symbols:
        base, quote = calendar.symbol_currencies(symbol)
        windows = calendar.risk_windows(events, {base, quote})
        plans.append(
            briefing.build_trade_plan(
                symbol,
                tech_map[symbol],
                analysis.currencies,
                windows,
                items,
                now=now,
                atr_multiple=atr_multiple,
            )
        )

    payload = briefing.build_discord_payload(
        plans,
        analysis,
        events_48h,
        ordered_currencies,
        fast_window,
        slow_window,
        fetch_warnings=fetch_warnings,
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

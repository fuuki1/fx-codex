"""TradingViewのテクニカル分析を取得してDiscordに通知するツール。

TradingViewのスキャナーAPI(tradingview-ta経由)から各通貨ペアの
テクニカル指標と総合レーティングを取得し、fx-codexのMAクロス戦略
(SMA fast/slow)の目線と突き合わせてDiscord Webhookに送信する。

使い方:
    .venv/bin/python tv_discord_notify.py                # 通知を送信
    .venv/bin/python tv_discord_notify.py --dry-run      # 送信せず内容を表示
    .venv/bin/python tv_discord_notify.py --symbols USDJPY GBPJPY

Webhook URLは環境変数 DISCORD_WEBHOOK_URL か、プロジェクト直下の
.env ファイル (DISCORD_WEBHOOK_URL=...) から読み込む。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, UTC
from pathlib import Path

import requests
from tradingview_ta import get_multiple_analysis

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_SYMBOLS = ["USDJPY", "EURUSD"]
DEFAULT_INTERVALS = ["15m", "1h", "4h", "1d"]
EXCHANGE = "OANDA"
SCREENER = "forex"

COLOR_BUY = 0x2ECC71
COLOR_SELL = 0xE74C3C
COLOR_NEUTRAL = 0x95A5A6

RECOMMENDATION_JA = {
    "STRONG_BUY": "強い買い",
    "BUY": "買い",
    "NEUTRAL": "中立",
    "SELL": "売り",
    "STRONG_SELL": "強い売り",
}


def load_webhook_url() -> str | None:
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


def load_strategy_windows() -> tuple[int, int]:
    params_path = PROJECT_ROOT / "strategy_params.json"
    fast, slow = 20, 100
    if params_path.exists():
        try:
            params = json.loads(params_path.read_text(encoding="utf-8"))
            fast = int(params.get("fast_window", fast))
            slow = int(params.get("slow_window", slow))
        except (ValueError, json.JSONDecodeError):
            pass
    return fast, slow


def ma_cross_state(indicators: dict, fast_window: int, slow_window: int) -> tuple[str | None, str]:
    """TradingViewが返すSMA値から自作戦略と同じMAクロスの目線を判定する。"""
    fast = indicators.get(f"SMA{fast_window}")
    slow = indicators.get(f"SMA{slow_window}")
    if fast is None or slow is None:
        return None, f"MA({fast_window}/{slow_window}): データなし"
    if fast > slow:
        return "long", f"MA({fast_window}/{slow_window}): ゴールデン(ロング目線)"
    if fast < slow:
        return "short", f"MA({fast_window}/{slow_window}): デッド(ショート目線)"
    return None, f"MA({fast_window}/{slow_window}): 拮抗"


def agreement_line(symbol: str, recommendation: str, ma_side: str | None) -> str:
    rec_ja = RECOMMENDATION_JA.get(recommendation, recommendation)
    if ma_side is None or recommendation == "NEUTRAL":
        return f"➖ {symbol} 1h: TradingView総合は「{rec_ja}」、判断は保留"
    tv_side = (
        "long"
        if recommendation in ("BUY", "STRONG_BUY")
        else "short" if recommendation in ("SELL", "STRONG_SELL") else None
    )
    if tv_side == ma_side:
        side_ja = "ロング" if ma_side == "long" else "ショート"
        return f"✅ {symbol} 1h: 自作MAクロス({side_ja})とTradingView({rec_ja})が一致"
    return f"⚠️ {symbol} 1h: 自作MAクロスとTradingView({rec_ja})の見解が不一致"


def fetch_analysis(symbols: list[str], intervals: list[str]) -> dict[str, dict[str, object]]:
    """interval → {"EXCHANGE:SYMBOL": Analysis|None} の辞書を返す。"""
    qualified = [f"{EXCHANGE}:{s}" for s in symbols]
    results: dict[str, dict[str, object]] = {}
    for interval in intervals:
        results[interval] = get_multiple_analysis(
            screener=SCREENER, interval=interval, symbols=qualified
        )
    return results


def build_embeds(
    symbols: list[str],
    intervals: list[str],
    analysis: dict[str, dict[str, object]],
    fast_window: int,
    slow_window: int,
) -> tuple[list[dict], list[str]]:
    embeds: list[dict] = []
    headlines: list[str] = []
    now_iso = datetime.now(UTC).isoformat()

    for symbol in symbols:
        key = f"{EXCHANGE}:{symbol}"
        fields = []
        color = COLOR_NEUTRAL
        for interval in intervals:
            result = analysis.get(interval, {}).get(key)
            if result is None:
                fields.append({"name": interval, "value": "取得失敗", "inline": True})
                continue
            summary = result.summary
            ind = result.indicators
            rec = summary.get("RECOMMENDATION", "NEUTRAL")
            rec_ja = RECOMMENDATION_JA.get(rec, rec)
            ma_side, ma_text = ma_cross_state(ind, fast_window, slow_window)

            close = ind.get("close")
            rsi = ind.get("RSI")
            macd = ind.get("MACD.macd")
            macd_signal = ind.get("MACD.signal")
            lines = [
                f"総合: **{rec_ja}** (買{summary.get('BUY', 0)}/中立{summary.get('NEUTRAL', 0)}/売{summary.get('SELL', 0)})",
            ]
            if close is not None:
                lines.append(f"終値: {close:.5g}")
            if rsi is not None:
                lines.append(f"RSI(14): {rsi:.1f}")
            if macd is not None and macd_signal is not None:
                macd_state = "上抜け" if macd > macd_signal else "下抜け"
                lines.append(f"MACD: {macd:+.5f} ({macd_state})")
            lines.append(ma_text)
            fields.append({"name": interval, "value": "\n".join(lines), "inline": True})

            if interval == "1h":
                if rec in ("BUY", "STRONG_BUY"):
                    color = COLOR_BUY
                elif rec in ("SELL", "STRONG_SELL"):
                    color = COLOR_SELL
                headlines.append(agreement_line(symbol, rec, ma_side))

        embeds.append(
            {
                "title": f"{symbol} — TradingView テクニカル分析",
                "color": color,
                "fields": fields,
                "footer": {
                    "text": f"fx-codex tv_discord_notify | MA({fast_window}/{slow_window}) | {EXCHANGE}"
                },
                "timestamp": now_iso,
            }
        )
    return embeds, headlines


def post_to_discord(webhook_url: str, content: str, embeds: list[dict]) -> None:
    payload = {
        "username": "fx-codex チャート分析",
        "content": content,
        "embeds": embeds,
    }
    response = requests.post(webhook_url, json=payload, timeout=15)
    if response.status_code >= 300:
        raise RuntimeError(f"Discord通知に失敗: HTTP {response.status_code} {response.text[:200]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TradingViewテクニカル分析をDiscordに通知する")
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    parser.add_argument("--intervals", nargs="+", default=DEFAULT_INTERVALS)
    parser.add_argument("--dry-run", action="store_true", help="Discordに送信せず内容を表示する")
    args = parser.parse_args(argv)

    symbols = [s.upper().replace("/", "") for s in args.symbols]
    fast_window, slow_window = load_strategy_windows()

    try:
        analysis = fetch_analysis(symbols, args.intervals)
    except Exception as error:  # noqa: BLE001 - 外部API起因の失敗を集約
        print(f"TradingView分析の取得に失敗: {error}", file=sys.stderr)
        return 1

    embeds, headlines = build_embeds(symbols, args.intervals, analysis, fast_window, slow_window)
    content = "\n".join(headlines) if headlines else "TradingViewテクニカル分析"

    if args.dry_run:
        print(content)
        print(json.dumps(embeds, ensure_ascii=False, indent=2))
        return 0

    webhook_url = load_webhook_url()
    if not webhook_url:
        print(
            "DISCORD_WEBHOOK_URL が未設定です。環境変数か .env に設定してください。",
            file=sys.stderr,
        )
        return 1

    post_to_discord(webhook_url, content, embeds)
    print(f"Discordに通知を送信しました ({', '.join(symbols)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

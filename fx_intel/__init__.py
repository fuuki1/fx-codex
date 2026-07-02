"""fx_intel — ニュース・経済指標・テクニカルを統合するFX分析パッケージ。

機関投資家のデスクが毎朝行う分析プロセスを自動化する:

1. calendar    — 経済指標カレンダー取得とイベントリスク窓の判定
2. news        — ニュースヘッドラインの収集と通貨タグ付け
3. sentiment   — 語彙ベース + Claude API(任意)による通貨センチメント
4. technicals  — TradingViewマルチタイムフレームのテクニカル集約
5. briefing    — 上記を融合したトレードプラン生成とDiscord配信
6. journal     — 判断の記録と方向的中率の自己検証

tv_discord_notify.py と同じく fx_backtester 非依存で単体動作する
(必要な外部パッケージは requests と tradingview_ta のみ)。
"""

__all__ = ["calendar", "news", "sentiment", "technicals", "briefing", "journal"]

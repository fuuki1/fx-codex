"""fx_intel — ニュース・経済指標・テクニカル・マクロを統合するFX分析パッケージ。

機関投資家のデスクの投資委員会を模した役割分担で分析を自動化する:

1.  calendar    — 経済指標カレンダー取得とイベントリスク窓の判定
2.  news        — ニュースヘッドラインの収集と通貨タグ付け
3.  analyst     — 自前分析エンジン(否定・強調・鮮度減衰・テーマ抽出を備えた
                   決定論的センチメント。Claude API非依存の既定経路)
4.  sentiment   — センチメント統合(Claude API 任意 → analyst の序列)
5.  technicals  — TradingViewマルチタイムフレームのテクニカル集約
6.  macro       — OHLC・米金利・DXY・VIX・CFTC COTの取得とリスクレジーム判定
6b. dukascopy   — 実ティック(.bi5)の取得・バー集約・CSV出力・将来価格供給
                   (fetch_dukascopy.py の実装本体。源B FuturePriceProvider を提供)
7.  gbm         — 依存ゼロの勾配ブースティング決定木(確率モデルの基盤)
8.  ml          — ジャーナルからGBDT確率モデルを学習する時系列パイプライン
9.  committee   — tech/news/macro/MLの委員会オーケストレータ
10. promotion   — 委員の shadow→paper→live 昇格ゲート
11. briefing    — 上記を融合したトレードプラン生成とDiscord配信
12. journal     — 判断の記録と方向的中率の自己検証
13. learning    — 履歴の相互採点による重み・確信度の自己調整
13b. drift      — ADWIN適応窓によるコンセプトドリフト検出(再学習トリガー)

時間足別モード(fx_briefing --per-timeframe)の追加レイヤ:

14. timeframe    — 15m/1h/4h/1d を独立に判断(各足に主ホライズンを紐付け)
15. price_history — 採点用の将来価格調達(ジャーナル後続行 + 外部OHLC注入口)
16. tf_learning  — (symbol×timeframe) セル別の主ホライズン採点・学習
17. tf_briefing  — 時間足別のDiscordペイロード生成
18. market_structure / notice_history / trade_notice / notice_renderer /
    notice_journal / discord_delivery
                 — 構造化された長文売買通知とDiscord分割送信
19. notice_quality — 詳細通知ジャーナルを未来OHLCで品質評価
20. notice_feedback — 詳細通知品質を条件別に集計し次回通知へ注意反映
21. notice_smoke — 詳細通知の生成→監査→採点→フィードバックE2Eスモーク
22. notice_health — 詳細通知の運用前ヘルスチェック
23. trade_outcome — 判断ジャーナルをMFE/MAE/TP/SL期待値で監査

tv_discord_notify.py と同じく fx_backtester 非依存で単体動作する
(必要な外部パッケージは requests と tradingview_ta のみ。gbm/ml/promotion は
標準ライブラリのみで動作し、ネイティブ拡張の重い依存を持ち込まない)。
"""

__all__ = [
    "analyst",
    "briefing",
    "calendar",
    "committee",
    "drift",
    "discord_delivery",
    "dukascopy",
    "gbm",
    "journal",
    "learning",
    "macro",
    "market_structure",
    "ml",
    "news",
    "notice_feedback",
    "notice_health",
    "notice_history",
    "notice_journal",
    "notice_policy",
    "notice_quality",
    "notice_renderer",
    "notice_smoke",
    "price_history",
    "promotion",
    "sentiment",
    "technicals",
    "tf_briefing",
    "tf_learning",
    "timeframe",
    "trade_notice",
    "trade_outcome",
]

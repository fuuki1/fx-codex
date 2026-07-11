"""fx_intel — ニュース・経済指標・テクニカル・マクロを統合するFX分析パッケージ。

機関投資家のデスクの投資委員会を模した役割分担で分析を自動化する:

1.  calendar    — 経済指標カレンダー取得とイベントリスク窓の判定
2.  news        — ニュースヘッドラインの収集と通貨タグ付け
3.  analyst     — 自前分析エンジン(否定・強調・鮮度減衰・テーマ抽出を備えた
                   決定論的センチメント。Claude API非依存の既定経路)
4.  sentiment   — センチメント統合(Claude API 任意 → analyst の序列)
5.  technicals  — TradingViewマルチタイムフレームのテクニカル集約
6.  macro       — OHLC・米金利・DXY・VIX・CFTC COTの取得とリスクレジーム判定
7.  gbm         — 依存ゼロの勾配ブースティング決定木(確率モデルの基盤)
8.  ml          — ジャーナルからGBDT確率モデルを学習する時系列パイプライン
9.  committee   — tech/news/macro/MLの委員会オーケストレータ
10. promotion   — 委員のlegacy実績診断（research buildはshadow固定）
11. briefing    — 上記を融合したトレードプラン生成とDiscord配信
12. journal     — 判断の記録と方向的中率の自己検証
13. learning    — 履歴の相互採点による重み・確信度の自己調整
14. decision_log — 判断・入力・学習適用状況の完全監査ログ
15. decision_feedback — 失敗理由を次回判断の見送り/確信度補正へ反映

時間足別モード(fx_briefing --per-timeframe)の追加レイヤ:

16. timeframe    — 15m/1h/4h/1d を独立に判断(各足に主ホライズンを紐付け)
17. price_history — 採点用の将来価格調達(ジャーナル後続行 + 外部OHLC注入口)
18. tf_learning  — (symbol×timeframe) セル別の主ホライズン採点・学習
19. tf_briefing  — 時間足別のDiscordペイロード生成
20. trade_outcome — MFE/MAE/TP/SL採点・期待値監査・改善候補レジストリ
21. tp_sl_learning — TP/SL先着正答率によるMVP確信度補正
22. maximization — 期待R/PF/Brier/経路品質による最大化プロファイル

tv_discord_notify.py と同じく fx_backtester 非依存で単体動作する
(必要な外部パッケージは requests と tradingview_ta のみ。gbm/ml/promotion は
標準ライブラリのみで動作し、ネイティブ拡張の重い依存を持ち込まない)。
"""

__all__ = [
    "analyst",
    "briefing",
    "calendar",
    "committee",
    "decision_feedback",
    "decision_log",
    "gbm",
    "journal",
    "learning",
    "macro",
    "maximization",
    "ml",
    "news",
    "price_history",
    "promotion",
    "sentiment",
    "technicals",
    "tf_briefing",
    "tf_learning",
    "timeframe",
    "trade_outcome",
    "tp_sl_learning",
]

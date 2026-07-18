"""fx_intel — ニュース・経済指標・テクニカル・マクロを統合するFX分析パッケージ。

機関投資家のデスクの投資委員会を模した役割分担で分析を自動化する:

1.  calendar    — 経済指標カレンダー取得とイベントリスク窓の判定
2.  news        — ニュースヘッドラインの収集と通貨タグ付け
3.  analyst     — 自前分析エンジン(否定・強調・鮮度減衰・テーマ抽出を備えた
                   決定論的センチメント。Claude API非依存の既定経路)
4.  sentiment   — センチメント統合(Claude API 任意 → analyst の序列)
5.  technicals  — TradingViewマルチタイムフレームのテクニカル集約
    tv_scanner  — 管理された TradingView スキャナーHTTPクライアント
                  (ブラウザ互換UA・ステータス検証・429バックオフ・typed failure)
6.  macro       — OHLC・米金利・DXY・VIX・CFTC COTの取得とリスクレジーム判定
7.  cot_pit     — CFTC COTのraw保存・release/revision-aware PIT境界(研究専用)
8.  gbm         — 依存ゼロの勾配ブースティング決定木(確率モデルの基盤)
9.  ml          — ジャーナルからGBDT確率モデルを学習する時系列パイプライン
10. committee   — tech/news/macro/MLの委員会オーケストレータ
11. promotion   — 委員のlegacy実績診断（research buildはshadow固定）
12. briefing    — 上記を融合したトレードプラン生成とDiscord配信
13. journal     — 判断の記録と方向的中率の自己検証
14. learning    — 履歴の相互採点による重み・確信度の自己調整
15. decision_log — 判断・入力・学習適用状況の完全監査ログ
16. decision_feedback — 失敗理由を次回判断の見送り/確信度補正へ反映

時間足別モード(fx_briefing --per-timeframe)の追加レイヤ:

17. timeframe    — 15m/1h/4h/1d を独立に判断(各足に主ホライズンを紐付け)
18. price_history — 採点用の将来価格調達(ジャーナル後続行 + 外部OHLC注入口)
19. tf_learning  — (symbol×timeframe) セル別の主ホライズン採点・学習
20. tf_briefing  — 時間足別のDiscordペイロード生成
21. trade_outcome — MFE/MAE/TP/SL採点・期待値監査・改善候補レジストリ
22. tp_sl_learning — TP/SL先着正答率によるMVP確信度補正
23. maximization — 期待R/PF/Brier/経路品質による最大化プロファイル
24. oanda_prices — 完了済みM5 bid/ask OHLCの取得と採点用行への変換
25. ibkr_prices — IBKR paperの判断quote・完了済みBID/ASK足の取得
26. direction_threshold — 純Rで検証する承認制の見送り閾値ポリシー
27. input_context — 判断時点で固定するマクロ・流動性の共通入力契約
28. liquidity — broker spread・セッションによる流動性proxy

通常の通知runtimeは fx_backtester 非依存で単体動作する。
例外は明示的に選択する研究用 cot_pit 境界で、共通PIT artifactを再利用するため
fx_backtester.point_in_time / pit_dataset に依存する。gbm/ml/promotion は標準ライブラリ
のみで動作し、ネイティブ拡張の重い依存を持ち込まない。
"""

__all__ = [
    "analyst",
    "briefing",
    "calendar",
    "committee",
    "cot_pit",
    "decision_feedback",
    "decision_log",
    "direction_threshold",
    "gbm",
    "historical_chart",
    "ibkr_prices",
    "journal",
    "learning",
    "input_context",
    "liquidity",
    "market_session",
    "shadow_learning",
    "macro",
    "maximization",
    "ml",
    "news",
    "oanda_prices",
    "price_history",
    "promotion",
    "sentiment",
    "technicals",
    "tf_briefing",
    "tf_learning",
    "timeframe",
    "trade_outcome",
    "tp_sl_learning",
    "tv_scanner",
]

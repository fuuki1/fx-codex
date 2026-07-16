# Runbook: 実データ取込

**対象:** 実データを PIT契約に沿って取り込む手順。**このrunbookのCOT節は本セッションで実際に実行・検証済み。** broker/macro/news は設計。
**原則:** timestamp を推測しない / fetch失敗を0や前回値で埋めない / OHLC から架空quoteを復元しない / revision を遡及上書きしない。

## 1. COT（CFTC）— 実行検証済み ✅

### 前提
- 認証不要（`publicreporting.cftc.gov` 公開 Socrata dataset `6dca-aqww`）。
- ネットワーク到達性: `publicreporting.cftc.gov` と `www.cftc.gov`。

### 手順（`tools/cot_pit_pipeline.py`）
```bash
# 1. capture: 完全pagination取得（count整合ガード、全ページSHA256、fail-closed）
python3 -m tools.cot_pit_pipeline capture --capture-root <DIR> --writer-id "<host:pid>"

# 2. attest: 公開release evidence を local sidecar へ束縛（report_date と released_at を正直に）
#    released_at は CFTC 発表実時刻（火曜データ→翌金曜 ~15:30 ET / 20:30 UTC）
python3 -m tools.cot_pit_pipeline attest --output <ATT.json> --evidence <EVIDENCE.html> \
  --report-date YYYY-MM-DD --basis actual_release_notice \
  --released-at <ISO8601+00:00> --evidence-uri https://www.cftc.gov/...

# 3. materialize: PIT dataset 生成（audit 内蔵、fail-closed）
python3 -m tools.cot_pit_pipeline materialize --root <DS_ROOT> --capture <CAP.json> \
  --release <ATT.json> <EVIDENCE.html>

# 4. audit: raw から決定論的再構成
python3 -m tools.cot_pit_pipeline audit <DATASET_DIR>

# 5. as-of: 予測時点の typed COT state（将来情報を返さない）
python3 -m tools.cot_pit_pipeline as-of <DATASET_DIR> \
  --prediction-time <ISO8601+00:00> --required-currencies JPY EUR GBP
```

### 検証ポイント（実測値あり）
- capture: `expected_row_count` が count-before==count-after（例: 13,727）。errors=[] で `admitted`。
- audit: `passed: true, errors: []`, obs 13,727。
- **as-of PIT ゲート**: prediction time が capture 完了時刻より**前**なら `unavailable`、**後**なら `usable`。取得前データを判断に使わせない。
- 全 artifact に `research_only: true`, `promotion_eligible: false`。

### 注意 / 限界
- 一発の capture は ~82MB。**raw と records.jsonl は git にコミットしない**（content-addressed、`reproduce.sh` で再生成）。
- release attestation は **local 束縛**（外部署名・独立タイムスタンプではない）。
- revision 検出は CFTC の stable row id 依存。
- 完全再現: `reports/evidence/cot-cftc-real-pit-20260713/reproduce.sh`。

## 2. broker bid/ask quote — 設計（未接続、認証情報が必要）

### 実装すべきこと
- 取引予定broker（IBKR等）の quote API に対する `QuoteSource` 実装（`data_platform/adapters/broker.py` の Protocol を満たす）。
- **実注文は絶対に行わない**（quote購読のみ）。
- 保存フィールド（`MarketQuote`）: instrument/bid/ask/(mid,spread導出)/bid_size/ask_size/source_timestamp/received_timestamp/first_seen_at/ingested_at/available_at/sequence_id/source_id/writer_id/raw_sha256/schema_version/tradable。
- 不明値は捏造しない（`None`）。single-writer: `writer_id` を全 quote にスタンプ。

### 品質
- immutable raw store（content-addressed）へ生bytes保存 → bid/ask bar materialize → `QualityState` 判定。
- primary/secondary の2系統で cross-source divergence を測定（平均化せず品質を落とす）。

### 運用配備
- Mac mini `~/srv/fx-codex/` に single-writer 常駐（launchd）。**開発機は TCC 制限で `~/Desktop` 配下を常駐から読めない**ため本番は Mac mini。
- 30取引日連続稼働で初めて shadow→paper のデータ前提が満たされる。

## 3. macro / calendar / news — 設計（契約のみ）

- `data_platform/contracts/{macro_release,economic_event,news_event}.py` の契約に沿う。
- `scheduled_time` を公開時刻として扱わない。actual は first_seen 前に使用不可。revision は別record保存（初回値を上書きしない）。fetch失敗を0/前回値へ置換しない（`unavailable`）。
- バックフィル記事を過去時点で既知だったように扱わない。

## 4. チェックリスト（実データを正式研究へ入れる前）
- [ ] `QualityState == usable`（degraded は明示waiver＋昇格不可条件付きのみ）
- [ ] raw hash 照合 OK / duplicate なし / out-of-order なし / clock skew 許容内
- [ ] available_at が「実際に保持した時刻」（source_timestamp や scheduled_time ではない）
- [ ] revision は別record / fetch失敗は 0 埋めしていない
- [ ] `research_only` フラグ維持（実broker接続でも実注文なし）

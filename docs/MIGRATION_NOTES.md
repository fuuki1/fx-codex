# Migration notes — institutional research pipeline (2026-07-12)

対象ブランチ: `codex/institutional-research-pipeline`(base: PR #31先端 `0fce3fa`)。
**すべて追加であり、既存public APIの変更・削除はない。**

## 新規モジュール

| モジュール | 役割 |
|---|---|
| `fx_backtester/failures.py` | typed fail-closed失敗分類(`FailureReason` / `TypedFailure`)。enum値は証跡スキーマの一部であり改名は破壊的変更 |
| `fx_backtester/experiment_manifest.py` | 実験の事前登録manifest(schema v1、厳格JSON) |
| `fx_backtester/experiment_pipeline.py` | authoritative pipeline本体+CLI(`run` / `evaluate-lockbox`) |
| `fx_backtester/trial_ledger.py` | append-only・改竄検出付きtrial ledger |
| `fx_backtester/lockbox.py` | lockbox registry(single-use access・アクセス台帳) |
| `fx_backtester/promotion_policy.py` | 昇格しきい値の設定ファイルloader |
| `fx_backtester/shadow_execution.py` | order intent / simulated execution / TCA / 無効化済みgateway |
| `fx_intel/source_contracts.py` | broker quote・macro calendarスキーマ、source実装状態registry、SLO計測 |

## CLI

```bash
python -m fx_backtester.experiment_pipeline run \
  --experiment-manifest experiments/<id>.json \
  [--output-root runs/experiments] [--trial-ledger PATH] [--lockbox-registry DIR]

python -m fx_backtester.experiment_pipeline evaluate-lockbox \
  --evidence-dir runs/experiments/<id> --purpose "..." --actor "..." \
  [--lockbox-registry DIR]
```

既定の永続位置: `runs/trial_ledger.jsonl`・`runs/lockbox_registry/`(git管理外)。

## 設計上の決定(仕様との差分)

1. **manifestはYAMLでなくJSON**。`requirements.lock` にYAMLパーサが無く、
   hash-pinned lock方針(PR #30)と新規依存が衝突するため。canonical JSONは
   既存のhash基盤とも一致する。
2. **stage名のマッピング**: 指示の `validated_research` は既存
   `governance.ModelStage` の `validated` に、`live_candidate` は
   `limited_live`(registry実装が遷移を拒否する評価専用状態)に対応する。
   既存registryのstage名は変更しない(破壊的変更の回避)。`live` は実装上無効。
3. **v1は1実験=1通貨ペア**(`chronological_model_partitions` が重複時刻を
   拒否するため)。multi-pair集約は将来契約として明示。
4. **lockbox outcomesは保存しない**。評価時にmanifest+rawから決定論的に
   再計算し、lineage hash一致を強制する(保存型よりcustody面が単純)。

## 運用者向け注意

- 同一 `experiment_id` はlockbox開封後frozen。**いかなる変更も新IDが必要**。
- trial ledger/lockbox registryは削除・編集しない。破損は「性能主張不能」
  として扱われる(復旧ではなく再実験)。
- 昇格しきい値を変える場合はpolicy JSONを新規作成しrationaleを書く。
  コード内既定値の書き換えはしない。

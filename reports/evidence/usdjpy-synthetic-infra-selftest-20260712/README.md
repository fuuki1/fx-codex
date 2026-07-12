# Evidence bundle — usdjpy-synthetic-infra-selftest-20260712

**これはインフラ自己試験の証跡であり、性能・収益性の証拠ではない。**
入力は `examples/sample_prices.csv`(合成、seed 42、SHA-256
`b93513ba…8427` — 2026-07-11 benchmark報告の記録値と一致)。

## 実行

- コード: commit `94466e6427b31fd7e70d1ffc4e2a6bd47c1ffb33`、clean worktree。
- 依存: `requirements.lock` SHA-256をmanifestに固定し実測一致。
- コマンド:
  `python -m fx_backtester.experiment_pipeline run --experiment-manifest experiments/usdjpy-synthetic-infra-selftest-20260712.json`
  → `evaluate-lockbox`(single-use、目的・実施者を記録)。

## 結果(要点)

- **再現性**: 独立2回実行で `deterministic_result_sha256` が完全一致
  (`replay_determinism.txt`)。
- **選択**: baseline群7種+complex 3種の10 trialすべて記録
  (`trial_ledger_snapshot.jsonl`)。complexはbaselineを上回れず、
  優越ルールにより baseline `prev-sign` が選択された。
- **test区画(記述的)**: `evaluation.json` 参照。sample guard通過につき
  `evaluation_available_descriptive`(合成データにつき性能主張には使用不能)。
- **lockbox区画(1回のみ開封)**: net E[R] = **−0.962R**、勝率5.7%
  (`lockbox_result.json`)。**alphaは存在しない**という否定を明確に出力。
- **昇格判定**: run時・post-lockbox時とも **denied**
  (`promotion_decision.json` / `promotion_decision_post_lockbox.json`)。
  post-lockboxでは `untouched_lockbox` ゲートのみ通過に転じ、
  `non_synthetic_data` ほか10ゲートが不合格のまま。

## 品質ゲート(このcommitで取得)

- `test_results.txt`: 637 passed, 1 skipped
- `ruff_results.txt` / `black_results.txt` / `mypy_results.txt`: すべてpass

## 既知のノイズ

実行時にLAPACKの `DLASCL` 診断行がstdoutへ出る(PBOのdegradation回帰で
退化行列を渡した際のFortran診断)。計算はfail-closed検査を通過しており、
結果への影響はない。後続課題としてP2記録(red-team RT-16と同系の拡張)。

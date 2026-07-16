# experiments/ — experiment manifests

`python -m fx_backtester.experiment_pipeline run --experiment-manifest experiments/<id>.json`
の入力となる事前登録マニフェストを置く場所です。

## 形式がJSONである理由

`requirements.lock` にYAMLパーサが含まれておらず、新規依存の追加は
hash-pinned lock (PR #30) の方針と衝突します。またマニフェストは
canonical JSONとしてSHA-256計算の対象になるため、JSONが最も自然です。

## 運用ルール

1. マニフェストは実行**前**に完成させる(pre-registration)。test/lockbox結果を
   見た後の変更は新しい `experiment_id` を必要とします。
2. `git.commit` は実行するworktreeのHEADと完全一致が必要。`dirty_worktree_allowed`
   はfalseのまま使う(正式claimはclean worktreeのみ)。
3. `data.sources[].raw_sha256` は `shasum -a 256 <file>` の実測値。
4. `data.synthetic` を偽って `false` にしても、他のゲート(lockbox・実データ系譜)で
   昇格は拒否されます。合成データは必ず `true` にする。
5. `TEMPLATE.experiment.json` はプレースホルダのままでは検証エラーで実行できません
   (安全側)。全フィールドを実値で埋めてから使ってください。

evidence bundleは `runs/experiments/<experiment_id>/`(git管理外)に生成されます。
正式報告に使う場合は `reports/evidence/<experiment_id>/` へbundle一式をコピーし、
`artifact_hashes.json` のhashで同一性を示してください。

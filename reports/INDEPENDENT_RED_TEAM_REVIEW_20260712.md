# Independent red-team review — 2026-07-12

対象: `codex/institutional-research-pipeline` ブランチで追加された
authoritative pipeline / trial ledger / lockbox / 評価ゲート / shadow・TCA。
視点: 攻撃者およびモデルリスク検証者。実装レビューとは別に、
「どうすれば偽の性能主張を通せるか」を能動的に試行した。

判定基準: P0 = 偽の昇格・実注文・データ破壊が可能 / P1 = 統制の実質的迂回が可能 /
P2 = 迂回に既知の限界の悪用が必要、または開示済みの制約。

## 発見と処置

### RT-1 (P1 → 修正済み): evidence改竄+hash再生成でpost-lockbox判定を汚染できた

- **攻撃**: bundleの `promotion_decision.json` の `evidence.net_expectancy_r` 等を
  改竄し、`artifact_hashes.json` を自分で再計算して整合させる。
  当初実装の `evaluate-lockbox` はartifact hash検査(自己参照)しか行わず、
  改竄済みevidenceをそのまま `PromotionEvidence` に復元してpost-lockbox判定へ
  使ってしまう。
- **修正**: claim消費**前**に `_verify_prior_evidence` が deterministic replay
  (dataset_hash / git_commit / synthetic_data / net_expectancy_r / pair_count)
  と記録値の一致を強制。局所的に自己整合なbundleでも、raw+manifest+code から
  再導出できない数値は `lineage_broken` で拒否される。
- **検証**: `tests/test_lockbox.py::test_rehashed_evidence_edit_is_still_detected`
  (改竄+hash再生成→拒否、かつsingle-use accessは未消費のまま)。
- **残余**: 当初はDSR/PBO/bootstrap CI等の統計値が再検証対象外だったが、
  後続修正(RT-16)で `expectancy_ci_lower_r` / `dsr_probability` /
  `pbo_probability` / `cost_stress_2x_expectancy_r` もreplayから再計算して
  照合するよう拡張済み。

### RT-2 (P1 → 修正済み): registry削除で同一bundleのlockboxを再評価できた

- **攻撃**: `runs/lockbox_registry/` をファイルシステム上で削除すると
  single-use claimマーカーが消え、同じbundleに対して `evaluate-lockbox` を
  再実行できた。
- **修正**: bundle自身に残る `lockbox_result.json` の存在をclaim前に検査し、
  存在すれば `lockbox_violation`。registryとbundleの二点を同時に消さない限り
  再評価できない。
- **検証**: `tests/test_lockbox.py::test_registry_wipe_cannot_reopen_an_evaluated_bundle`。
- **残余**: registry・bundle・ledgerを**すべて**削除して最初から再実行する攻撃は
  local custodyでは防げない(RT-8参照)。

## 検査済み・欠陥なし(テストで固定)

| # | 攻撃仮説 | 防衛 | テスト |
|---|---|---|---|
| RT-3 | 未来情報の混入(feature) | truncated-history再構成検査、label-aware 5分割、as-of cutoff、PBOの欠測拒否 | `test_experiment_pipeline.py`(leakage/split)、既存`test_time_series_validation.py` |
| RT-4 | trial数の過少申告 | manifest全candidateのledger存在チェック(欠落=`incomplete`で性能主張不能)、DSR/PBOへ全試行供給 | `test_lockbox.py::test_ledger_records_all_candidates_and_failures` |
| RT-5 | 失敗試行の削除・編集・末尾切詰め | hash連鎖+headサイドカー。削除=`lineage_broken`、編集=`hash_mismatch` | `test_trial_ledger.py`(4系統) |
| RT-6 | コストを0にして昇格 | `spread_pips>0`必須、cost_model_version必須、1.0/1.25/1.5/2.0x stress必須、cost欠如=`cost_model_unavailable`系拒否 | `test_experiment_pipeline.py::test_required_stress_multipliers` ほか |
| RT-7 | live有効化の誤発火 | policy設定の`allow_live`は`promotion_rejected`、governance registryはlive遷移拒否、`ExecutionEvent`はsimulated以外のvenueを表現不能、`DisabledOrderGateway`は常に拒否、発注コード自体が不存在 | `test_evaluation_gates.py::test_live_enablement_rejected`、`test_shadow_execution.py`(venue/gateway) |
| RT-9 | lockbox後の同一実験の再実行・変更 | registryのfrozen状態+内容不一致拒否 | `test_lockbox.py`(rerun/changed manifest) |
| RT-10 | 二重writer | ledgerはflock+append、registryはcreate-exclusive、price snapshotはOS lock(#26) | `test_trial_ledger.py`、既存ops試験 |
| RT-11 | baseline無しで複雑モデルだけ通す | baseline必須+「baselineを厳密に上回る場合のみ選択可」 | `test_evaluation_gates.py`(dominance 3系統) |
| RT-12 | 薄い・重複・偏在サンプルでの性能宣言 | 有効(非重複)取引数・レジーム/月集中度guard→`evaluation_unavailable` | `test_evaluation_gates.py`(guard 4系統) |

## 未解決(P2、受容理由付き)

| # | 内容 | 受容理由・影響・後続課題 |
|---|---|---|
| RT-8 | **local custodyの根本限界**: registry+bundle+ledgerを全削除すれば実験をゼロから再実行できる。外部タイムスタンプ・独立管理者が無い | 既存監査のExit criteria #4(独立custody)として明示済み。影響: 悪意ある単独運用者を検証者は最終的に検出できない。後続: 外部attestation(署名・第三者保管)の導入。**現状はいかなる昇格も人間レビュー必須のため、実害は昇格審査で捕捉可能** |
| RT-13 | **実験レベルの多重性**: 多数のexperiment_idを試し最良のみ報告できる。DSR/PBOは実験内試行のみ補正 | 共有ledgerが全実験の全試行を保持するためレビュアは検出可能(隠すにはledger削除が必要=RT-8に帰着)。後続: research-program横断の多重性補正 |
| RT-14 | `data.synthetic`は自己申告であり、偽装(false宣言)は可能 | 偽装しても現時点ではregime/pair/lockbox/実データ系譜の他ゲートで昇格不能。後続: source license/attestation契約(source_contractsのUNIMPLEMENTED群)の実装が本質的対策 |
| RT-15 | feature registryのコードがleakage検査のsample位置を特殊処理する攻撃 | registryは in-repo のレビュー対象コードで、manifest からは登録名しか選べない。コードレビュー境界として受容 |

### RT-16 (P2 → 修正済み): 統計値のpost-lockbox再検証

当初「統計値(DSR/PBO/CI/2×cost)はpost-lockbox再検証されない」として
P2受容していたが、後続修正で `_verify_prior_evidence` を拡張し、
`_headline_statistics`(runと共有)とcost stressのreplayから
`expectancy_ci_lower_r` / `dsr_probability` / `pbo_probability` /
`cost_stress_2x_expectancy_r` を再計算して記録値と厳密照合するようにした。
検証: `tests/test_lockbox.py::test_rehashed_statistics_edit_is_still_detected`。
残る非照合フィールドは運用状態系(incidents/shadow_days等、replayから
導出不能でNoneのままゲート不合格になるもの)のみ。

## 結論

- **P0: 0件。P1: 2件発見、いずれも本ブランチ内で修正しテストで固定。
  P2のうち1件(RT-16)も追加修正済み。**
- 残るP2は4件で、すべて (a) 既存監査で開示済みのlocal custody限界に帰着するか、
  (b) 他ゲートにより現時点で昇格へ到達不能。
- 本レビューは「悪意ある単独運用者に対する完全防御」を主張しない。主張するのは
  「**事故・自己欺瞞・単純な改竄では偽の性能主張が通らず、痕跡なしに統制を
  迂回するには複数の保管場所を同時に破壊する必要がある**」ことまでである。

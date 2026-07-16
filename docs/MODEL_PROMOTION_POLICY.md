# モデル昇格ポリシー

**ステータス:** 状態機械・ゲート・policy設定ローダは実装済み（テスト緑）。`live`/`limited_live` はコードレベルで hard disable。実データ shadow 実績なし。
**一次コード:** `fx_backtester/{governance,promotion_policy}.py`。**コードが正。**

## 1. 段階（`governance.py` STAGES）

```
research → validated → shadow → paper → limited_live → live
```

**`limited_live` / `live` は無効のまま。** `governance.py:365-366` が `target_stage in {"limited_live","live"}` を無条件で拒否（`this registry build cannot enable live trading`）。`promotion_policy.py` も `allow_live`/`allow_limited_live=true` の policy を拒否。

## 2. 昇格は「遅く」、降格は「速く」

- 昇格: 一段階ずつ（`promotion must move exactly one stage`）＋人間承認者＋書面理由が必須（`:369`）。
- 降格: 的中率劣化・drift・データ品質悪化・artifact破損・schema不一致で**即時**（`promotion.py` の状態機械、および shadow へ自動降格）。

## 3. policy は設定ファイル（`promotion_policy.py`）

`load_promotion_policy(path)` が JSON からロード。**全閾値が必須**（欠けると `TypedFailure`）、かつ**実質的な written rationale が必須**（なぜその閾値かの説明なしには受理しない）。主な閾値:

```
min_samples / min_regimes / min_pairs
min_shadow_days_for_paper = 30 / min_paper_days_for_limited_live = 60
min_net_expectancy_r / min_expectancy_ci_lower_r
min_dsr_probability (0,1] / min_brier_improvement / min_cost_stress_2x_expectancy_r
```

## 4. research → shadow の条件

`governance.py` の require（全て満たさないと不合格、`test_governance.py`）:
- 正式 pipeline 完走 / **synthetic ではない**（`synthetic_data is False` 厳密）/ test 未汚染
- baseline 優越 / no-trade 優越 / 最低サンプル
- 正の net expectancy / CI下限 > 0 / cost stress 通過 / calibration 良好
- lineage（commit/hash 整合）/ lockbox 未開封または規則遵守

## 5. shadow → paper の条件（実運用 shadow データで評価）

- **30取引日以上**（`min_shadow_days_for_paper`）/ stale 判定ゼロまたは許容内 / duplicate writer なし
- intent と simulated fill の照合 / TCA / 実spread分布 / drift / 複数レジーム / 複数月
- **実データでの正の期待値** / 人間レビュー

→ **現状これらの実データ証拠は皆無**（P0-2, P0-3）。shadow へ進める根拠すら未整備。

## 6. 劣化時の自動降格

- paper→shadow 自動降格（的中率劣化）/ drift時 abstain / データ品質悪化で停止 / artifact破損で停止 / schema不一致で停止。

## 7. 完了条件に対する現状

| 条件 | 状態 |
|---|---|
| 状態機械 / 一段階ずつ / 人間承認必須 | ✅ 構造 |
| synthetic 昇格不能 / live hard disable | ✅ 構造（レッドチームで実証） |
| policy 設定ファイル化＋rationale必須 | ✅ 構造 |
| 30取引日 shadow ゲート | ✅ 構造（**実データ実績なし**） |
| 実データでの正の期待値 | ❌ **未** |

→ 昇格構造は完成。実データ shadow 実績が無いため、いかなるモデルも shadow より先へ進められない（これは**正しい fail-closed 状態**）。

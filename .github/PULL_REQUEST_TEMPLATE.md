## 変更の目的

<!-- 解決する障害または変更する1つの契約を記載 -->

## 正本と依存関係

- Base branch: `main`
- Parent PR / stacked dependency: なし
- 実機固有変更を含む: いいえ
- 未追跡ファイルへの依存: なし

<!-- main以外がbase、またはstacked PRの場合は理由・親PR・統合順を記載 -->

## 変更範囲

- [ ] 1 PR = 1契約または1障害になっている
- [ ] データ収集
- [ ] PIT / provenance
- [ ] storage / migration
- [ ] scoring / labels
- [ ] model / decision logic
- [ ] notification
- [ ] dashboard
- [ ] operations / launchd
- [ ] documentation / CI

## 安全境界

- [ ] broker発注・注文・position変更経路を追加していない
- [ ] analysis-only境界を維持している
- [ ] fail-closed条件を弱めていない
- [ ] writer ownershipとatomicityを説明した
- [ ] PIT / future leakageへの影響を説明した

## 再現性

- [ ] 新規clean cloneで検証した
- [ ] `git status --porcelain`が空だった
- [ ] 未追跡Python、plist、script、fixtureに依存していない
- [ ] interpreter / dependency lockを記録した

## 検証

- [ ] import collection
- [ ] ruff
- [ ] black
- [ ] mypy
- [ ] pytest（対象または全件）
- [ ] safety tests
- [ ] PIT / parity tests
- [ ] 負例またはfailure injection

実行結果:

```text
ここにコマンドと結果を記載
```

未実施項目と理由:

<!-- 未実施がある場合、PRはDraftのままにする -->

## 実機への影響

- [ ] 実機変更なし
- [ ] 配備対象SHAを固定した
- [ ] plist/service manifestを更新した
- [ ] backupを定義した
- [ ] health/parity確認を定義した
- [ ] rollbackを定義・検証した

## 文書

- [ ] READMEへの影響なし、または更新済み
- [ ] SYSTEM_OVERVIEWへの影響なし、または更新済み
- [ ] runbookへの影響なし、または更新済み
- [ ] provenance/source ledgerへの影響なし、または更新済み

## Root cause / evidence

<!-- 修正PRでは、再現手順、一次証拠、修正前に失敗するテストを記載 -->

## Rollback

<!-- revert対象commit、runtime data復元、service切替手順を記載 -->

## Merge条件

- [ ] レビュー指摘が解決済み
- [ ] clean cloneゲート成功
- [ ] 依存PRがmerge済み、または依存なし
- [ ] Draft解除の根拠が揃っている

# Runbook: research-v2 統合 & Mac mini 移行

**対象:** PR #26→#35 スタック（`integration/research-v2`）を main へ統合し、Mac mini へ配備する手順。
**最重要の注意:** この統合は **`trader/` を丸ごと削除する**（-7202行）。自動マージ禁止・レビューなし一括統合禁止・main直push禁止（タスク§14）。

## 0. 前提の確認（着手前）

```bash
gh pr list --state open          # 重複PR・並行セッションの確認
git fetch --all --prune
```

現状（2026-07-13）:
- main `3595582`: `trader/` **現存**、`data_platform/` 不在。
- `integration/research-v2` = #35先端 `eb4263c` + 本セッション（mypy修正・COT evidence・CI強化・docs）。
- pytest 708 / ruff / black / mypy(104, tools含む) 緑。

## 1. 統合順序（stacked、逆順不可）

```
#26 → #29 → #31 → #32 → (#33 / #34) → #35 → [#30 を再検証]
```

- **段階レビュー必須**。31k行を一度に見ない。層ごとに diff レビュー:
  - #26: trader/削除 + decision_pipeline + labeling/stats/calibration/overfitting
  - #29: pit_dataset / research_experiment
  - #31: cot_pit（実CFTC）/ CLI / briefing統合
  - #32: authoritative pipeline中核
  - #33: no-trade候補 / #34: data_platform契約 / #35: bid/ask bar + broker adapter
- 実務上は `integration/research-v2`（全層統合済み・CI緑）を1本のPRとして**層ごとにレビューコメントを付けて**進めるのが現実的。

### #30 の扱い
`codex/hash-pinned-requirements-lock` は main ベース・trader-build ジョブを含む可能性。#26 の trader/削除・CI 変更と競合するため、**スタック統合後に rebase して再検証**。

## 2. `trader/` 削除の安全措置（P0-1）

**統合前に必ず:**
```bash
# 1. rescue ブランチで trader/ を保全（将来また発注を組む時のため）
git branch rescue/trader-snapshot-$(date +%Y%m%d) main
# 2. ユーザーの明示承認を取得（trader/ が main から消えることの確認）
```
- IBKR未開設で executor は稼働不能なため**機能影響は無い**が、大きな削除なので黙って実行しない。
- 削除理由: 分析→Discord通知への方針転換（[pivot_analysis_only]）。

## 3. マージ前チェック（全て緑を確認）

```bash
python3 -m pytest -q                                    # 708 passed
python3 -m ruff check .
python3 -m black --check .
python3 -m mypy fx_backtester fx_intel data_platform tools *.py   # 104 files clean（tools含む）
```
CI（`.github/workflows/ci.yml`）: test(3.11/3.12) / ruff / black / mypy(tools含む) / 衝突マーカー / safety不変条件。

## 4. Mac mini 配備

**ファイル単位 rsync 禁止**（依存漏れで本番が落ちた事故歴）。全体を揃える:
```bash
# Mac mini 上で
cd ~/trader   # または research-v2 の配置先
git fetch origin
git checkout origin/main -- .        # 全体を揃える（部分同期しない）
```
- 収集の single-writer 常駐（launchd `com.fx-codex.snapshot`/`briefing`/`health`）を配備。手順・閾値・rollback は [docs/OPERATIONS_RUNBOOK.md](../OPERATIONS_RUNBOOK.md)。
- **開発機で常駐させない**（TCC制限で `~/Desktop` 配下を読めない）。本番データは Mac mini `~/srv/fx-codex/logs/` が正。
- COT PIT 実取込は [REAL_DATA_INGESTION.md](REAL_DATA_INGESTION.md) の手順。

## 5. 配備後の検証

- 価格スナップショットが5分ごとに更新されているか（止まると学習層がデータ飢餓 → 昇格ゲート0件で足止め）。
- 鮮度監視アラートが正常（重複抑制含む）。
- **30取引日連続稼働**の計測開始（shadow→paper の前提）。

## 6. ロールバック手順

| 事象 | ロールバック |
|---|---|
| 統合後 main が壊れた | `git revert -m 1 <merge_commit>` で統合を巻き戻し。`rescue/trader-snapshot-*` から trader/ を復元可 |
| Mac mini 配備で本番停止 | `git checkout <前のmain SHA> -- .` で全体を戻し `docker compose build`（部分同期しない） |
| CI が想定外に赤 | tools/ mypy・衝突マーカー・safety不変条件のどれで落ちたか確認。ツール版数は固定済（ruff 0.15.20 / black 26.5.1 / mypy 2.1.0） |
| 収集常駐が多重起動 | launchd の排他ロックで防止済み。手動 `fx_*_loop.sh` は常用しない |

## 7. 統合後に残る作業（正式到達に必須）

- 実broker bid/ask 接続（P1-1、認証情報必要）
- 外部lockbox custody 実装（P1-2、[LOCKBOX_CUSTODY.md](LOCKBOX_CUSTODY.md)）
- GBDT を pipeline 候補family へ登録（P1-3）
- 実価格 triple-barrier の OOS 評価bundle生成（P0-2）
- 30取引日連続稼働の達成（P0-3）

これらが揃うまで **判定2**（構造完成・実証不足）。

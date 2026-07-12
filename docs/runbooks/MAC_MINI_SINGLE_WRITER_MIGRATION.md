# Mac mini single-writer 移行 runbook

**このrunbookは手順書であり、実行記録ではない。** 本移行は人間の明示承認と
立ち会いがあるまで実施しない。各ステップの出力は
`reports/evidence/mac_mini_migration_<YYYYMMDD>/` へ保存し、証跡bundleとする。
どのステップでも想定と異なる出力が出たら**停止して記録**し、推測で継続しない。

前提:
- 対象: Mac mini上の checkout(2026-07-10監査時点で `/Users/fuuki/srv/fx-codex`、
  別Docker checkoutが `/Users/fuuki/fx-codex/trader` に存在)。**実行前に必ず再確認**。
- 監査(2026-07-10)での既知状態: mainから約18 commit遅れ・dirty・cron が
  `ModuleNotFoundError` で失敗・複数collectorの競合・journal重複汚染。
  これらは観測であり現状保証ではない。
- 本移行は分析→Discord通知系のみを対象とする。発注系は存在せず、作らない。
- `EXPECTED_SHA` は PR #26/#29/#31 統合後の `origin/main` HEAD とする
  (統合前の移行は不可 — `docs/audits/PR26_REVIEW_LEDGER.md` の統合手順を先に完了)。

## 手順

### 1. 事前状態取得

```bash
mkdir -p ~/migration_evidence/$(date +%Y%m%d) && cd ~/migration_evidence/$(date +%Y%m%d)
date -u +"%Y-%m-%dT%H:%M:%SZ" | tee 00_started_at.txt
sw_vers | tee 01_os.txt
git -C ~/srv/fx-codex rev-parse HEAD | tee 02_head.txt
git -C ~/srv/fx-codex status --porcelain | tee 03_status.txt   # dirtyなら停止し内容を保全
git -C ~/srv/fx-codex log --oneline -10 | tee 04_log.txt
```

### 2. process一覧

```bash
ps aux | grep -iE "fx_|python|docker" | grep -v grep | tee 05_processes.txt
```

### 3. launchd一覧

```bash
launchctl list | grep -i fx | tee 06_launchd.txt
ls -la ~/Library/LaunchAgents/ | tee 07_launchagents.txt
for p in ~/Library/LaunchAgents/com.fx-codex.*.plist; do echo "== $p"; plutil -p "$p"; done | tee 08_plists.txt
```

### 4. cron一覧

```bash
crontab -l | tee 09_crontab.txt   # 監査時に失敗cronが観測されている。内容を必ず記録
```

### 5. writer特定

journal/priceスナップショットへ書いている全プロセスを特定する:

```bash
lsof +D ~/srv/fx-codex/runs 2>/dev/null | tee 10_writers.txt
scripts/status_fx_services.sh | tee 11_status.txt   # writer候補・legacyラベル検出を含む
```

複数writerが見えたら、それが本runbookの除去対象。全て記録してから先へ。

### 6. raw/log/databaseバックアップ

```bash
tar -czf backup_runs_$(date +%Y%m%d%H%M).tar.gz -C ~/srv/fx-codex runs logs
shasum -a 256 backup_runs_*.tar.gz | tee 12_backup_sha.txt
```

バックアップのhashを記録するまで、いかなる停止・削除も行わない。

### 7. dry-run

```bash
git -C ~/srv/fx-codex fetch origin
git -C ~/srv/fx-codex diff --stat HEAD origin/main | tee 13_pending_diff.txt
# EXPECTED_SHA一致確認(不一致なら停止)
[ "$(git -C ~/srv/fx-codex rev-parse origin/main)" = "$EXPECTED_SHA" ] && echo OK | tee 14_sha_check.txt
# venvはRunbook §2に従い新規作成し、hash付きlockで検証(旧venv再利用禁止)
python3 -m venv ~/srv/fx-codex/.venv-new
~/srv/fx-codex/.venv-new/bin/pip install --require-hashes -r requirements.lock 2>&1 | tail -5 | tee 15_pip.txt
~/srv/fx-codex/.venv-new/bin/pip check | tee 16_pip_check.txt
```

### 8. writer停止

```bash
# cronのfx関連行を削除(crontab -l のバックアップは手順4で取得済み)
crontab -l | grep -v fx | crontab -
# launchd bootout(uninstallスクリプトはbootout失敗時に非零で止まる)
scripts/uninstall_launchd.sh | tee 17_uninstall.txt
```

### 9. single-writer導入

```bash
git -C ~/srv/fx-codex checkout "$EXPECTED_SHA"
scripts/install_launchd.sh | tee 18_install.txt
# installは手動/直接/cron writerを検出すると拒否する。拒否されたら手順5へ戻る
```

### 10. 二重起動検査

```bash
sleep 360   # 1周期待つ
lsof +D ~/srv/fx-codex/runs 2>/dev/null | tee 19_writers_after.txt
# price snapshotのwriter IDが単一であること、OS lockの競合拒否ログが無いことを確認
grep -i "conflicting-writer\|lock" ~/srv/fx-codex/logs/*.log | tail -20 | tee 20_lock_log.txt
```

### 11. freshness検査

```bash
~/srv/fx-codex/.venv-new/bin/python tools/data_freshness_monitor.py --report | tee 21_freshness.txt
# overall=ok になるまで昇格系の判断は全てveto状態が正しい
```

### 12. Discord通知検査

```bash
~/srv/fx-codex/.venv-new/bin/python fx_briefing.py --dry-run | tee 22_briefing_dry.txt
# 実送信はdry-run確認後に1回のみ。失敗時のリトライ挙動をログで確認
```

### 13. restart試験

```bash
scripts/restart_fx_services.sh | tee 23_restart.txt   # 全ラベルpreflight後にkickstart
launchctl list | grep fx | tee 24_after_restart.txt
```

### 14. rollback

rollback条件: freshnessが2周期連続でcritical、二重writer検出、通知不達。

```bash
scripts/uninstall_launchd.sh
git -C ~/srv/fx-codex checkout <PREVIOUS_SHA>   # 手順1の02_head.txt の値
# 旧plistを再インストールする場合も必ず install_launchd.sh 経由(手動launchctl禁止)
scripts/install_launchd.sh
# rollback後も手順10の二重起動検査を必ず再実行(rollbackが二重writerを生む事故の防止)
```

### 15. 証跡bundle保存

```bash
cd ~/migration_evidence/$(date +%Y%m%d)
shasum -a 256 * | tee MANIFEST.sha256
# リポジトリの reports/evidence/mac_mini_migration_<YYYYMMDD>/ へコピーしPRで提出
```

## 障害注入手順(移行完了後、人間承認の下で実施)

各注入の前後で手順10(二重起動)・11(freshness)を実行し、
**fail-closed(veto/停止)になること**を合格条件とする。復旧はrestart試験(13)。

| 注入 | 手順 | 期待挙動 |
|---|---|---|
| process kill | `launchctl list \| grep fx` でPID特定→`kill -9 <PID>` | launchdが再起動。price snapshotは自然キーで重複を拒否 |
| network outage | Wi-Fi/Ethernetを物理的に切断し1周期待つ | 取得失敗がtyped failureで記録され、freshnessがcriticalへ。偽データが書かれない |
| API unavailable | `/etc/hosts` で対象APIホストを `127.0.0.1` に向ける(終了後必ず戻す) | 同上。silent fallbackが無いこと |
| stale data | collectorを1つ停止し2周期待つ | freshness veto発動、briefingが見送りになる |
| clock skew | `sudo sntp -sS time.apple.com` の前に `sudo date -u <2h前>`(検証後即復元) | aware UTC検査・future-dated検査がcritical |
| disk full | `dd if=/dev/zero of=~/srv/fx-codex/runs/fill.tmp bs=1m count=<残容量>`(終了後削除) | 書き込み失敗が例外で停止し、部分書き込みが残らない |
| corrupted raw file | 最新スナップショット1件を `echo broken >>` で破壊(バックアップ後) | hash/schema検査が拒否し、当該窓が使用不能とマークされる |
| duplicate writer | 手動で `fx_tf_snapshot.py` を並行起動 | OS lockが競合を拒否し、ログに記録される |
| reboot | `sudo reboot` | launchdジョブが自動復帰し、freshnessが1周期内にokへ戻る |

注入で期待挙動にならなかった項目は P0 とし、修正まで移行を完了扱いにしない。

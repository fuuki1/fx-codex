# OPERATIONS RUNBOOK — 学習データ収集の常駐運用

学習データ収集（価格スナップショット・ブリーフィング判断・鮮度監視）を
launchdのワンショットサービスとして運用するための手順書。
2026-07-10のMac mini実機監査に基づくが、実行環境の一次情報が常に優先する。

> **設計と観測を分離する。** §0は目標とする正規構成であり、導入済みという意味ではない。
> §2の観測値は監査時点のスナップショットであり、移行当日に必ず再取得する。この文書の
> コマンドを開発機で読んだだけでは、Mac miniの状態は変わらない。

> **【2026-07-10 追記】** 自動売買(発注)は取りやめ、`trader/` 発注スタックは削除済み
> （→ [SYSTEM_OVERVIEW](../SYSTEM_OVERVIEW.md)）。本Runbookが扱う**分析・学習データ収集の
> 常駐サービス（snapshot / briefing / health）は現行システムでも有効**。§2冒頭の`trader/`や
> `params_gate`は削除前のインシデント記録である。一方、§2-3/§8はrepository外の旧process、
> container、別checkoutが実機に残っていないことを確認するためのfail-closed手順である。

## 0. 正規運用の設計

| Label | 周期 | 実体 | 役割 |
|---|---|---|---|
| `com.fx-codex.snapshot` | 5分毎(StartInterval 300) | `fx_tf_snapshot.py` | TradingViewを取得する唯一の定期producer。全テクニカルcacheと時間足別採点用価格系列を更新 |
| `com.fx-codex.briefing` | 5分境界(StartCalendarInterval) | `scripts/fx_briefing_once.sh` | 共有テクニカルcacheをread-onlyで使う時間足別統合通知と、最大1時間ごとの融合判断・学習更新 |
| `com.fx-codex.health` | 5分毎 | `tools/data_freshness_monitor.py` | データ鮮度監視。WARNING/CRITICAL/RECOVERYをDiscordへ |
| `com.fx-codex.horizon` | 5分毎(StartInterval 300) | `scripts/fx_horizon_once.sh` | 共有テクニカルcacheをread-onlyで使う3ペア×9本のshadow予測、満期採点、セル学習 |
| `com.fx-codex.monitors` | 15分毎 | `scripts/fx_monitors_once.sh` | 完全判断ログの全履歴採点・期待値フィードバック更新。5分writerから分離した重い派生処理 |
| `com.fx-codex.quote-index` | 1分毎(StartInterval 60) | `tools/quote_tape_index.py` | 原本気配JSONLを変更せず、追記された完全行だけをSQLite byte-offset索引へ反映 |

### 0-1. writer所有権

| 状態ファイル | 正規writer | 禁止する競合 |
|---|---|---|
| `logs/tradingview_technicals.json` | `com.fx-codex.snapshot`のみ | briefing/horizonからの直接TradingView取得、別checkoutのsnapshot |
| `logs/briefing_tf_prices.jsonl` | `com.fx-codex.snapshot`のみ | signal boardの価格書込み、raw snapshot loop、別checkoutのcron |
| `logs/briefing_journal.jsonl` | `com.fx-codex.briefing`のみ | manual briefing、signal board、旧cron/旧plist |
| `logs/briefing_tf_journal.jsonl` | `com.fx-codex.briefing`のみ | manual per-timeframe、signal board、旧cron/旧plist |
| `logs/briefing_decisions.jsonl` / `briefing_decisions_latest.json` | `com.fx-codex.briefing`のみ | monitor、manual briefing、別checkout |
| `logs/briefing_decision_outcomes.json` / `briefing_decision_feedback.json` / `decision_expectancy_monitor.json` | `com.fx-codex.monitors`のみ | briefing hot path、manual monitor、別checkout |
| その他の学習・昇格状態 | `com.fx-codex.briefing`のみ | 同じ状態を更新する任意の別プロセス |

全サービスは`tools/run_exclusive.py`の排他ロック（`flock`）経由で起動する。ただしロックは
同一checkout・同一ロック名の呼出しにしか効かない。rawな手動コマンド、別名ロック、別checkout、
旧loopとの競合を構造的には防げないため、移行時のプロセス/cron/launchd監査が必須である。
`fx_intel/journal.py`と`fx_intel/decision_log.py`の直接appendにも、ライブラリ全体を横断する
単一writer保証はない。正規writerの所有権は当面の運用統制であり、トランザクションDBまたは
共通ファイルロックへ移行するまで**未解決の残存リスク**とする。

`briefing_decisions.jsonl`の全履歴採点は、ファイル肥大時に5分周期を超えるため
`com.fx-codex.briefing`内では実行しない。briefingは監査ログ追記とlatest更新までで終了し、
TP/SL/MFE/MAE採点・outcome・feedback更新は`com.fx-codex.monitors`だけが担当する。

TradingView取得は`com.fx-codex.snapshot`の`--refresh-technical-cache`だけが行う。
cacheはhash、aware `available_time`、`ingested_time`、取得対象、MA窓、全interval viewを持ち、
tmp→fsync→renameで原子的に更新する。`available_time`は上流market event timeではなく、
全TradingView取得が完了してローカル利用可能になった時刻である。briefing/horizonは
`--technical-cache-only`を必須とし、cache欠損・hash破損・6分超過・要求対象不足時に
直接scannerへfallbackしない。古いcache観測を現在時刻で再スタンプすることも禁止する。
これにより正規runtimeのscanner POSTは3サービス合計ではなく、snapshotの1取得周期
（4時間足につき各1 POST）へ集約される。
healthはcacheファイルのmtimeではなく、hash検証済み`available_time`を鮮度判定に使う。
コピー・手動touchで古い観測を新鮮に見せず、hash不一致や未来時刻もCRITICALにする。

`fx_briefing_once.sh`は同じ`fx-briefing`ロック内で、時間足別統合を5分ごとに実行した後、
`logs/briefing_journal.jsonl`の最終aware UTC時刻を読み取り、55分以上経過した場合だけ融合判断を実行する。
時間足別Discord通知はUSDJPY/EURUSD、Discordなしの融合判断はsnapshotが完全性を監視している
USDJPY/EURUSD/GBPUSDを対象にする。GBDTはモデル無し・stale時に融合実行内で自動再学習し、
PIT適格な融合行だけを使う。件数・クラス数・時系列分割を通過してモデル本体ができた時点で
artifactを保存し、検証スコア不合格なら`usable=false`のまま判断参加を禁止する。
融合・時間足別の両行には`source_cutoff <= max_feature_available_time <= prediction_time`に加え、
`pit_contract`、`decision_id`、`input_context_id`、`source_record_ids`、明示producer/versionを記録する。
完全な契約を持たない旧形式行は可視化には残すが、方向・収益学習、閾値停止、昇格から除外する。
GBDT artifactはschema 5と`training_contract=decision-journal-pit-v2`を必須とし、旧artifactは
未学習扱いで自動再学習する。期待値monitor/改善registryも同じdata contractを必須とし、
旧registryのTP/SL候補は現行判断へ適用しない。
融合判断には`--no-discord --no-price-write --require-freshness`を必須とし、Discordの重複通知と
snapshot以外からの価格追記を防ぐ。判定不能・未来時刻・末尾破損はfail-closedで次工程を開始しない。
時間足別処理が失敗した場合も融合writerを開始しない。ただし、主要ジャーナル保存後の
Discord通知失敗だけは専用終了コード5で識別し、launchdへ失敗を報告しつつ融合取得を継続する。
主要ジャーナルの書込み失敗は終了コード4で即時停止し、後続の完全判断ログへ部分更新を広げない。
時間足別の出力は`logs/fx_integrated_briefing.log`、融合判断とschedule判定は
`logs/fx_fusion_capture.log`で確認する。

`--signal-board --dry-run`は開発・一時確認専用である。旧常駐loopは削除済みだが、
別checkout・cron・既存processに残っていれば競合writerとして扱う。状態更新を伴う手動実行は
briefing writerと共存できない。`--dry-run`もsource cache/event exportを更新し得るため、
zero-write確認は正規runtimeと分離したcopyで行う。

- LaunchAgent(gui/$UID)として動く。**Mac miniは自動ログイン運用が前提**
  (再起動→自動ログイン→エージェント自動起動)。
- ワンショット型のため「異常終了からの復旧」は次周期の再実行で担保される
  (常駐プロセスの再起動ループより単純で、部分実行の中間状態が残らない)。
- 秘密情報(Discord webhook)はplistに書かず、実行時に `.env` から読む。
  鮮度監視は `DISCORD_OPS_WEBHOOK_URL`(運用専用)があれば優先し、
  無ければ既存の `DISCORD_WEBHOOK_URL` を使う。
- 価格取得用の `.env` には `OANDA_API_TOKEN` と
  `OANDA_ENVIRONMENT=practice`（または`live`）が必要。未設定時は品質を偽装する
  close-onlyフォールバックを行わず、snapshotを失敗させる。

### 0-2. 通知マトリクス

| 事象 | 発信元 | 宛先/頻度 | 障害時の扱い |
|---|---|---|---|
| 定期分析（時間足別統合） | `com.fx-codex.briefing` | 分析Webhook、5分境界 | 送信失敗はbriefingログと鮮度遅延で検知 |
| 鮮度WARNING/CRITICAL/RECOVERY | `com.fx-codex.health` | `DISCORD_OPS_WEBHOOK_URL`優先、状態遷移時+cooldown | briefing/signal boardに依存させない |
| launchdジョブ失敗/非ロード | launchd stderr + `status_fx_services.sh` | 運用者確認、CRITICAL時はops通知 | `logs/launchd/*.err.log`を一次証跡にする |
| 手動signal board | 開発者の手動loop | 開発/一時確認先のみ | 正規インシデント通知に使わない |
| legacy executionの安全異常（残存時のみ） | legacy stack/手動監査 | 分析Webhookとは別の運用経路 | paper/live assertion失敗なら移行中止 |

`DISCORD_OPS_WEBHOOK_URL`が未設定で分析Webhookへのfallbackも失敗する場合、通知到達は保証されない。
そのため`status_fx_services.sh`、launchd stderr、`freshness_report.json`の確認を移行完了条件に含める。

### 0-3. COT PITは手動research境界（未配備）

`fx_briefing.py --cot-pit-dataset <artifact>`は既存artifactを監査してas-of読込するだけで、CFTC取得、release evidence作成、materialize、更新は行わない。省略時はlegacy TTL COTへfallbackせず、COTを判断入力から除外する。invalid/unavailable/incomplete/staleもCOTだけを除外してtyped warningを残し、現状ではbriefing全体を停止しない。

`scripts/fx_briefing_once.sh`とlaunchd plistはこのoptionを渡していないため、§0の正規構成ではCOTは意図的に無効である。COT用の承認済み定期取得service、single-writer規則、release-evidence取得手順、retention/backup、freshness monitor、Mac mini配備、実prospective corpusは存在しない。明示的な人手レビューなしにplist/cron/既存Mac mini serviceへ接続してはならない。

手動research用CLIは次の5操作を分離する。`attest`は実行時UTCをevidence取得時刻として記録し、遡及指定を許さない。`materialize`は現在のGit HEAD/dirty状態を自動記録する。入力探索や「latest」選択はせず、すべてのpathを明示する。

```bash
# 1. configured contract codesのcount-bounded raw capture（network read + local create）
.venv/bin/python tools/cot_pit_pipeline.py capture \
  --capture-root "$HOME/fx-codex-research/cot/captures"

# 2. 運用者が別途保存・確認したCFTC release/schedule bytesをlocal sidecarへ結合
#    released-atは公式情報をtimezone付きISO-8601で転記する。
.venv/bin/python tools/cot_pit_pipeline.py attest \
  --output "$HOME/fx-codex-research/cot/release-2026-07-07.json" \
  --evidence "$HOME/fx-codex-research/cot/release-2026-07-07.html" \
  --report-date 2026-07-07 \
  --basis scheduled \
  --released-at 2026-07-10T15:30:00-04:00 \
  --evidence-uri 'https://www.cftc.gov/MarketReports/CommitmentsofTraders/ReleaseSchedule/index.htm'

# 3. capture/sidecar/evidenceを明示してresearch-only artifactをcreate-only materialize
.venv/bin/python tools/cot_pit_pipeline.py materialize \
  --root "$HOME/fx-codex-research/cot/artifacts" \
  --capture '<capture-bundle.json>' \
  --release '<release-sidecar.json>' '<exact-evidence-file>'

# 4. source-specific raw replay audit（read-only）
.venv/bin/python tools/cot_pit_pipeline.py audit '<dataset-directory>'

# 5. 指定時刻のtyped state確認（read-only。ok以外はexit 1）
.venv/bin/python tools/cot_pit_pipeline.py as-of '<dataset-directory>' \
  --prediction-time 2026-07-11T00:00:00Z \
  --required-currencies JPY USD
```

このCLIの成功は、CFTC-host URI構文、local bytes/hash/time結合、取得bundleと正規化recordの再構成を検査したという意味に限る。evidence内容・実公表時刻・外部署名/trusted timestamp・ライセンスを認証せず、start/end count一致も同件数の途中改定を排除できない。artifactは常に`research_only`かつ`promotion_eligible=false`であり、FREDやfeature graph全体のPIT、予測性能、情報優位性を証明しない。

## 1. インストール / 確認 / 再起動 / 撤去

以下は§2の証跡取得、SHA検証、競合writer停止、安全assertionを完了した後にだけ使う。

```bash
ROOT=/Users/fuuki/srv/fx-codex
cd "$ROOT"
./scripts/install_launchd.sh --dry-run   # 生成されるplistの確認(変更なし)
./scripts/install_launchd.sh             # インストール+旧サービス置換
./scripts/status_fx_services.sh          # 状態・鮮度・ログを1画面で確認
./scripts/restart_fx_services.sh         # 全サービス再起動(kickstart -k)
./scripts/uninstall_launchd.sh           # rollback(データには触れない)
```

インストーラは旧`com.fx-codex.briefing.hourly`を自動でbootout・退避し、手動loop、
direct writer、writerを含むcronを検知すると**変更前に拒否**する（自動killはしない）。
別checkoutや検出パターン外のプロセスまで保証しないため、事前監査は省略できない。

## 2. 2026-07-10監査で観測した実機状態

Mac mini (`trader-mini`) の実測。監査期間中、同じログ群へ次の経路が書込みまたは
起動を試みた履歴を確認した。

1. 手動起動 `fx_briefing_loop.sh`+`fx_tf_snapshot_loop.sh` ×**3組**
   (日曜15時/木曜0時/金曜10:56開始。全てcwd=~/srv/fx-codex、ロック無し)
2. launchd `com.fx-codex.briefing.hourly`(毎時:10、per-timeframeのみ)
3. cron `*/5 * * * 1-5` が `~/trader/fx-codex` の**別チェックアウト**で
   `fx_briefing.py` を5分毎起動 → `params_gate` 欠落で**クラッシュループ中**
   (ModuleNotFoundError。~/trader/logs/fx_briefing.log 参照)
4. cron `5 * * * 1-5` の`tv_discord_notify.py`（ジャーナルwriterではないが通知経路の重複要因）

結果: 融合ジャーナルに毎時2〜3回の重複判断（スナップショットログには同一秒の
3プロセス書込を確認)。**重複はlearning.pyのサンプル数を水増しし的中率推定を歪める。**

監査終了時点の別スナップショットでは、手動loopは停止済みだった一方、旧
`com.fx-codex.briefing.hourly`と壊れたcronが残り、新しいsnapshot/briefing/healthは未導入、
価格スナップショットはCRITICAL相当（45分超の遅延）だった。したがって「過去に3組いた」ことと
「移行直前にも3組いる」ことを混同せず、当日の一次情報を取り直す。

リポジトリ`/Users/fuuki/srv/fx-codex`は観測時点で`HEAD=025db10`、
`origin/main`から**18コミット遅延**し、tracked変更と`.env.save`を含むuntrackedファイルがあった。
18は固定値ではない。移行当日の`git fetch`後にSHA、ahead/behind、dirty状態を再確認する。

### 2-1. 移行前証跡（最初に行う）

最初のフェーズはサービスやworking treeを変更しない。監査ディレクトリは公開リポジトリの外に置き、
権限を`0700`にする。remote URLや`.env`本文はtoken/webhookを含み得るため保存しない。

```bash
set -eu
set -o pipefail
umask 077
ROOT=/Users/fuuki/srv/fx-codex
RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)
AUDIT_ROOT="$HOME/fx-codex-audit/$RUN_ID"
RESCUE_ROOT="$HOME/fx-codex-rescue/$RUN_ID"
mkdir -p "$AUDIT_ROOT" "$RESCUE_ROOT"
chmod 700 "$AUDIT_ROOT" "$RESCUE_ROOT"
export ROOT RUN_ID AUDIT_ROOT RESCUE_ROOT
cd "$ROOT"

date -u +%FT%TZ > "$AUDIT_ROOT/observed_at_utc.txt"
hostname > "$AUDIT_ROOT/hostname.txt"
sw_vers > "$AUDIT_ROOT/sw_vers.txt"
git status --short --branch > "$AUDIT_ROOT/git-status-before-fetch.txt"
git rev-parse HEAD > "$AUDIT_ROOT/head-before-fetch.txt"
git branch --show-current > "$AUDIT_ROOT/branch-before-fetch.txt"
git remote > "$AUDIT_ROOT/remote-names.txt"       # URLは保存しない
shasum -a 256 pyproject.toml > "$AUDIT_ROOT/dependency-definition-sha256.txt"
test -x .venv/bin/python
.venv/bin/python -VV > "$AUDIT_ROOT/python-version.txt" 2>&1
.venv/bin/python -m pip list --format=json > "$AUDIT_ROOT/pip-package-versions.json" 2>&1
.venv/bin/python -m pip check > "$AUDIT_ROOT/pip-check.txt" 2>&1
if ! crontab -l > "$AUDIT_ROOT/crontab.before" \
  2> "$AUDIT_ROOT/crontab-read-error.txt"; then
  : > "$AUDIT_ROOT/crontab.before"
fi
launchctl list | rg 'fx-codex' > "$AUDIT_ROOT/launchctl-list.txt" || true
for label in com.fx-codex.snapshot com.fx-codex.briefing \
  com.fx-codex.health com.fx-codex.briefing.hourly; do
  launchctl print "gui/$(id -u)/$label" \
    2>/dev/null | rg 'state =|path =|program =|pid =|runs =|last exit code =' \
    > "$AUDIT_ROOT/launchctl-$label.safe.txt" || true
done
pgrep -fl 'fx_briefing|fx_tf_snapshot|tv_discord_notify|trader' \
  > "$AUDIT_ROOT/processes.txt" || true
lsof "$ROOT/logs/briefing_tf_prices.jsonl" \
  "$ROOT/logs/briefing_journal.jsonl" "$ROOT/logs/briefing_tf_journal.jsonl" \
  > "$AUDIT_ROOT/writers-lsof.txt" 2>&1 || true

for file in logs/briefing_tf_prices.jsonl logs/briefing_journal.jsonl \
  logs/briefing_tf_journal.jsonl logs/freshness_report.json; do
  if [ -f "$file" ]; then
    shasum -a 256 "$file"
    wc -l "$file"
    stat -f '%N %z bytes mode=%Sp mtime=%Sm' "$file"
  fi
done > "$AUDIT_ROOT/log-manifest-before.txt"

if [ -x .venv/bin/python ] && [ -f tools/journal_gap_audit.py ]; then
  .venv/bin/python tools/journal_gap_audit.py logs/briefing_journal.jsonl \
    --output "$AUDIT_ROOT/journal-gap-fusion.json" || true
  .venv/bin/python tools/journal_gap_audit.py logs/briefing_tf_journal.jsonl \
    --output "$AUDIT_ROOT/journal-gap-tf.json" || true
fi

find "$HOME/Library/LaunchAgents" -maxdepth 1 -type f \
  -name 'com.fx-codex*.plist*' -exec shasum -a 256 {} \; \
  > "$AUDIT_ROOT/launchagents-sha256.txt"
```

Python/pipの証跡取得に失敗した場合も続行しない。URL/tokenを含み得る`pip freeze`や
launchctl/plist全文は保存せず、package名/version、safe field、plist hashだけを残す。依存が再現できない環境をそのまま
「承認済みruntime」としてコピーすることはできないため、レビュー済みのlock/constraintsを
別途用意してclean venvを構築するまで移行を保留する。

以降のコードブロックは、この`set -eu`を有効にした**同じshell session**で順番に実行する。
sessionを失った場合は変数を推測して再開せず、新しい`RUN_ID`で§2-1から証跡を取り直す。

次にremote-tracking refだけを更新し、観測時の「18コミット遅延」を再測定する。`EXPECTED_SHA`は
GitHub上でレビュー・承認した**完全な40桁SHA**を別経路で入力する。`origin/main`がそのSHAと
一致しない場合、または現在のmainからfast-forwardでない場合は中止する。

```bash
cd "$ROOT"
git fetch --prune origin
git rev-parse refs/remotes/origin/main > "$AUDIT_ROOT/origin-main-after-fetch.txt"
git rev-list --count HEAD..refs/remotes/origin/main \
  > "$AUDIT_ROOT/behind-after-fetch.txt"
git rev-list --count refs/remotes/origin/main..HEAD \
  > "$AUDIT_ROOT/ahead-after-fetch.txt"

EXPECTED_SHA='REPLACE_WITH_REVIEWED_40_HEX_SHA'
test "${#EXPECTED_SHA}" -eq 40
test -z "$(printf '%s' "$EXPECTED_SHA" | tr -d '0123456789abcdef')"
test "$(git rev-parse refs/remotes/origin/main)" = "$EXPECTED_SHA"
git cat-file -e "$EXPECTED_SHA^{commit}"
git merge-base --is-ancestor "$(git rev-parse main)" "$EXPECTED_SHA"
```

### 2-2. dirty checkoutのローカル救出（push禁止）

`git add -A`や未監査のrescue branch pushは行わない。実機には`.env.save`、バックアップ、
取得データが存在したため、公開remoteへ秘密や大容量データを送る危険がある。tracked差分は
binary patch、既存commit履歴はbundle、untrackedは人間が承認したallowlistだけで保全する。
救出物はローカル`0700`領域に保持し、この移行中はどのbranchもpushしない。

```bash
cd "$ROOT"
git status --porcelain=v1 > "$RESCUE_ROOT/status.txt"
git diff --binary HEAD -- . > "$RESCUE_ROOT/tracked-working-tree.patch"
git diff --name-status HEAD -- . > "$RESCUE_ROOT/tracked-candidates.txt"
git ls-files --others --exclude-standard > "$RESCUE_ROOT/untracked-candidates.txt"
git bundle create "$RESCUE_ROOT/repository.bundle" --all
git bundle verify "$RESCUE_ROOT/repository.bundle" \
  > "$RESCUE_ROOT/bundle-verify.txt" 2>&1

# 人間がuntracked-candidates.txtを1行ずつ確認し、必要な相対pathだけをここへ記入する。
# .env*, credential, *.pem/*.key, DB dump, raw market data, backupはallowlistへ入れない。
ALLOWLIST="$RESCUE_ROOT/untracked.allowlist"
touch "$ALLOWLIST"
test ! -s "$ALLOWLIST" || ! rg -n \
  '(^/|(^|/)\.\.(/|$)|(^|/)(\.env($|\.)|id_(rsa|ed25519)|.*\.(pem|key|p12|pfx)|.*\.(dump|sqlite3?))$)' \
  "$ALLOWLIST"
mkdir -p "$RESCUE_ROOT/approved-untracked"
rsync -aR --files-from="$ALLOWLIST" ./ "$RESCUE_ROOT/approved-untracked/"
```

allowlistを書いた担当者とは別の担当者が、patchとapproved-untrackedをsecret scanする。
次のscanは**値を出力せず、疑わしいファイル名だけ**を出す補助ゲートである。1件でも出たら
内容を安全な端末で確認し、secretを除去して再実行する。組織標準のgitleaks等がある場合は併用する。

```bash
SECRET_PATTERN="(?i)(api[_-]?key|client[_-]?secret|access[_-]?token|password|webhook(_url)?)[[:space:]]*[:=][[:space:]]*['\"]?[A-Za-z0-9_./+=-]{20,}|BEGIN [A-Z ]*PRIVATE KEY|discord(app)?\.com/api/webhooks/"
rg -l --hidden --no-ignore-vcs "$SECRET_PATTERN" \
  "$AUDIT_ROOT" \
  "$RESCUE_ROOT/tracked-working-tree.patch" \
  "$RESCUE_ROOT/approved-untracked" \
  > "$RESCUE_ROOT/secret-scan-suspects.txt" || true
test ! -s "$RESCUE_ROOT/secret-scan-suspects.txt"
shasum -a 256 "$RESCUE_ROOT/tracked-working-tree.patch" \
  "$RESCUE_ROOT/repository.bundle" > "$RESCUE_ROOT/rescue-sha256.txt"
```

scanがfalse positiveを出す場合も、単に`test`を外さない。該当ファイルをallowlistから外すか、
独立レビューの承認記録とredactedなscan結果を`AUDIT_ROOT`へ残してから進む。

### 2-3. 旧execution不在assertion

現行repositoryに発注機能はない。旧`trader`プロセス、container、別checkout、cron、plist、
`--promote-live`指定を1つでも検出した場合は移行を中止し、プロセス一覧と所有者・cwd・親PIDを
証拠として保存する。この分析リポジトリから旧executionの設定変更、再起動、停止、注文操作を
行わず、別インシデントとして人間へエスカレーションする。「安全そうな設定値」が見えることを
旧executionの存在許可に読み替えない。

### 2-4. 承認SHAをclean checkoutへfast-forward

dirty checkoutはin-placeで更新しない。承認SHAからcleanなcandidateを別ディレクトリに作り、
remote SHAをもう一度照合して`--ff-only`を通す。既存checkout全体は後でそのまま退避する。

```bash
ORIGIN_URL=$(git -C "$ROOT" remote get-url origin)   # 表示・監査ファイルへの保存は禁止
CANDIDATE="$HOME/srv/fx-codex.candidate-$RUN_ID"
test ! -e "$CANDIDATE"
git clone --no-checkout "$ORIGIN_URL" "$CANDIDATE"
git -C "$CANDIDATE" fetch --prune origin
test "$(git -C "$CANDIDATE" rev-parse refs/remotes/origin/main)" = "$EXPECTED_SHA"
git -C "$CANDIDATE" switch main
git -C "$CANDIDATE" merge --ff-only "$EXPECTED_SHA"
test "$(git -C "$CANDIDATE" rev-parse HEAD)" = "$EXPECTED_SHA"
test -z "$(git -C "$CANDIDATE" status --porcelain)"
```

### 2-5. writer停止、checkout切替、導入

`pkill`でパターン一致したプロセスを一括停止しない。§2-1で記録したPID、cwd、親プロセスを
人間が照合し、対象PIDへTERMを送り、終了を確認する。cronもbackupから提案版を作ってdiffを
レビューしてから適用する。

```bash
# 1) writer/旧通知cronを提案版から除き、diffを人間承認後にだけ反映する
awk '!/fx_briefing\.py|fx_tf_snapshot\.py|fx_.*_loop\.sh|tv_discord_notify\.py/' \
  "$AUDIT_ROOT/crontab.before" > "$AUDIT_ROOT/crontab.proposed"
diff -u "$AUDIT_ROOT/crontab.before" "$AUDIT_ROOT/crontab.proposed" || true
crontab "$AUDIT_ROOT/crontab.proposed"   # 上のdiffを承認してから実行

# 2) 新旧すべてのscheduleを停止。plist backupは§2-1で取得済み
for label in com.fx-codex.snapshot com.fx-codex.briefing \
  com.fx-codex.health com.fx-codex.briefing.hourly; do
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
done

# 3) 競合プロセスを再取得し、承認したPIDだけを停止する
pgrep -fl 'fx_briefing|fx_tf_snapshot|tv_discord_notify' \
  | tee "$AUDIT_ROOT/processes-before-stop.txt" || true
APPROVED_PIDS=()  # 例: (123 456)。上の一覧からcwd/親を確認したPIDだけを設定する
if [ "${#APPROVED_PIDS[@]}" -gt 0 ]; then
  kill -TERM "${APPROVED_PIDS[@]}"
else
  test ! -s "$AUDIT_ROOT/processes-before-stop.txt"
fi
pgrep -fl 'fx_briefing|fx_tf_snapshot|tv_discord_notify' \
  > "$AUDIT_ROOT/processes-after-stop.txt" || true
test ! -s "$AUDIT_ROOT/processes-after-stop.txt"

# 4) dirty checkoutを丸ごと退避し、clean candidateを正規pathへ置く
cd "$HOME"
mv "$ROOT" "$RESCUE_ROOT/runtime-checkout"
mv "$CANDIDATE" "$ROOT"
OLD_ROOT="$RESCUE_ROOT/runtime-checkout"

# 5) 明示したruntime状態だけを戻す。.env.saveやbackup/dataは戻さない
test -f "$OLD_ROOT/.env"
install -m 600 "$OLD_ROOT/.env" "$ROOT/.env"
mkdir -p "$ROOT/logs"
rsync -a "$OLD_ROOT/logs/" "$ROOT/logs/"

# 6) 承認commitのhash固定lockからclean runtimeを構築。旧venvはコピーしない
cd "$ROOT"
test "$(git rev-parse HEAD)" = "$EXPECTED_SHA"
git diff --exit-code
git diff --cached --exit-code
test -f requirements.lock
shasum -a 256 requirements.lock > "$AUDIT_ROOT/requirements-lock-sha256.txt"
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.lock
.venv/bin/python -m pip install --no-deps --no-build-isolation .
.venv/bin/python -m pip check
.venv/bin/python -m pip list --format=json > "$AUDIT_ROOT/candidate-package-versions.json"
test -x .venv/bin/python
.venv/bin/python -m compileall -q fx_intel fx_briefing.py tools
./scripts/install_launchd.sh --dry-run > "$AUDIT_ROOT/install-dry-run.txt"
set +e
.venv/bin/python fx_briefing.py --promote-live macro \
  > "$AUDIT_ROOT/promote-live.stdout" 2> "$AUDIT_ROOT/promote-live.stderr"
PROMOTE_LIVE_RC=$?
set -e
test "$PROMOTE_LIVE_RC" -ne 0

# 7) 正規3サービスを導入
./scripts/install_launchd.sh
./scripts/status_fx_services.sh | tee "$AUDIT_ROOT/status-after-install.txt"
```

現行repositoryの`requirements.lock`を承認commitと同じSHAから取得し、
`pip install --require-hashes -r requirements.lock`で検証する。`pyproject.toml`の範囲指定だけで
当日最新版を解決したり、旧venvを承認commitへコピーしたり、`--require-hashes`を外して
先へ進めてはならない。

導入後、少なくとも2回の5分周期をまたいで再確認する。`lsof`/`pgrep`で
価格writerがsnapshot 1経路、判断writerがbriefing 1経路だけであること、5分間隔の価格追記、
5分間隔の時間足別ジャーナル追記、freshness report更新、ops通知の到達を確認する。確認結果とログの
SHA/行数を`AUDIT_ROOT`へ追記し、移行担当者と独立確認者を記録して完了とする。

## 3. 鮮度監視の閾値(ops/freshness_targets_timeframe.json)

閾値はコードでなく設定ファイルで管理する。既定値の根拠:

| 対象 | 期待周期 | WARNING | CRITICAL | 根拠 |
|---|---|---|---|---|
| `tradingview_technicals.json` | 5分 | 7分 | 15分 | read-only consumerの許容は6分。hash検証済み`available_time`で停止・改変・未来時刻を検知 |
| `briefing_tf_prices.jsonl` | 5分 | 15分(3周期) | 45分(9周期) | 15m採点窓(9〜21分)を守るには45分停止が実害ライン |
| `briefing_tf_journal.jsonl` | 5分 | 15分(3周期) | 45分(9周期)、または直近30分の周期充足率80%未満 | 最新行だけ新しい断続運転も検知し、現行`--per-timeframe`定期経路の判断鮮度と連続性を監視 |

融合判断は自身の実行前に`--require-freshness`を通すため、同じhard gateへ
`briefing_journal.jsonl`を追加すると自己依存になる。独立した非循環monitorを導入するまでは、
融合判断の鮮度は学習ダッシュボードと`logs/fx_fusion_capture.log`で確認する。

週末クローズ中もwriter自体は動き続ける設計(判断はstandbyでも書込みは継続)のため、
休場例外は設けていない。週末に誤検知が出る場合はこの前提が壊れた証拠なので、
閾値を緩める前にwriterの挙動を確認すること。

## 4. Discord通知仕様

- **WARNING**(黄): 更新遅延(warn閾値超過)。同一状態の再通知はcooldown(既定6時間)後のみ
- **CRITICAL**(赤): ファイル欠落 / critical閾値超過 / JSONL末尾破損 / 判断周期充足率低下。悪化遷移は即通知
- **RECOVERY**(緑): 通知済みの異常が正常へ戻ったとき1回だけ。停止時間を含む
- 全通知に: ホスト名 / 対象 / 発生時刻 / 最終更新 / 経過 / 最終正常 / 理由 / 連続検知回数 / 手動対応
- 重複抑止: 状態遷移時のみ送信 + cooldown + `logs/freshness_state.json` に永続化
  (監視プロセスが再起動しても再送しない)
- **Discord送信失敗は監視を止めない**(失敗はレポートに `sent: false` で記録)

## 5. 欠損期間の扱い(捏造禁止)

- 停止期間のデータを現在値から補間・捏造しない
- `tools/journal_gap_audit.py` で欠損期間(開始・終了の絶対時刻)・重複行数・
  時刻逆転を監査レポートとして残す(読み取り専用)
- 既知の欠損(2026-07-10時点):
  - 開発機 `~/Desktop/fx-codex/logs/`: 2026-07-08T15:21Z以降停止(開発機は収集責務なし。
    本番データはMac mini `~/srv/fx-codex/logs/` が正)
  - Mac mini: ジャーナルは2026-07-04頃から継続。ただし多重起動期間(上記)の
    重複汚染があるため、learning評価時は監査レポートを参照
- **バックフィル方針**: 外部OHLC(§6)から価格経路の後埋めは可能だが、行に
  `source` / `source_timestamp` / `ingested_at` / `is_backfill: true` / `backfill_run_id` /
  quality flagsを必須とし、リアルタイム収集行と区別する。
  当時のニュース・スプレッド・取得条件は再現できないため、バックフィル期間を
  **完全なpoint-in-time判断データとして扱わない**(価格採点の補助のみ)

## 6. OHLCデータソース設計

現状のcommunity TradingView scannerは現在値/形成中barのproxyであり、信頼できる過去の
bid/ask経路ではない。改善は`fx_intel/price_history.py`の注入口へproviderを差し込むが、
**この監査では採用providerを決定していない**。機能の存在と、ライセンス・timestamp・revision・
first-ingestion・SLAを含むPIT契約は別問題である。

| 候補 | 期待する用途 | 採用前に解決する事項 | 現在の位置付け |
|---|---|---|---|
| Dukascopy/JForex | 仮想口座の遅延bid/ask replay | exact `.bi5` transportのversioned契約、利用/再配布権、欠損/訂正/SLAは未受入 | research-only遅延再生。real-time/paper fillではない |
| OANDA v20 | broker candle/quoteとpaper照合の候補 | 口座/権限、取得範囲、bid/ask保持、timestamp/SLA/ライセンス検証 | 候補のみ |
| IBKR | paper order/fill/reconciliationと補助market data | 口座/購読、pacing、履歴制限、API version、paper/live差の検証 | 候補のみ |
| community TradingView scanner（現行） | current分析表示 | 公式のscanner履歴契約、source timestamp、immutable raw/revisionがない | research-only proxy |
| yfinance | 開発時の日次比較候補 | FX実行quoteではなく、非公式/制限/訂正契約が不十分 | 主要ソース不採用 |

Source/contractの正本は[Source ledger](research/SOURCE_LEDGER.md)とし、vendor選定時に再確認する。

プロバイダ抽象(実装時のインターフェース):
`fetch_latest(symbols)` / `fetch_range(symbol, start, end, granularity)` /
`fetch_ohlc(...)` / `fetch_bid_ask(...)` / `health_check()` /
`provider_metadata()`(名称・粒度・遅延・ライセンス) / 各行にquality flags。
全行に `source_timestamp` と `ingested_at` を保持し、point-in-time監査を可能にする。

## 7. rollback

rollbackの発動条件は、writer重複、paper/live assertion失敗、承認SHA不一致、継続する
CRITICAL鮮度、ジャーナル破損、または通知経路の不達である。最初に全writerを止め、
失敗したreleaseと切替後ログを保全する。**収集停止は重複汚染より安全**であり、旧raw loopを
2本起動する手順へは戻さない。

```bash
cd /Users/fuuki/srv/fx-codex
./scripts/uninstall_launchd.sh
pgrep -fl 'fx_briefing|fx_tf_snapshot|tv_discord_notify' || true
# 表示されたPIDがあればcwd/親を確認し、対象PIDだけTERMで停止してから続行する。

ROLLBACK_ID=$(date -u +%Y%m%dT%H%M%SZ)
ROLLBACK_AUDIT="$HOME/fx-codex-audit/${ROLLBACK_ID}-rollback"
mkdir -p "$ROLLBACK_AUDIT"
chmod 700 "$ROLLBACK_AUDIT"
for file in logs/briefing_tf_prices.jsonl logs/briefing_journal.jsonl \
  logs/briefing_tf_journal.jsonl; do
  [ ! -f "$file" ] || { shasum -a 256 "$file"; wc -l "$file"; }
done > "$ROLLBACK_AUDIT/log-manifest.txt"
rsync -a logs/ "$ROLLBACK_AUDIT/logs-at-stop/"
```

次に、移行証跡に記録した**直前の承認済みSHA**からcleanなrollback candidateを作る。
失敗したrelease上で`reset --hard`せず、同じclean-checkout/swap方式を使う。rollback SHAの
由来とremote照合、runtimeデータのコピー、swap前後のhashを記録する。直前checkoutがdirtyで
多重writerを含んでいた場合、それを「known-good」とはみなさず、そのまま再起動しない。

rollback releaseに§0の3サービスと`run_exclusive.py`が揃い、dry-runと安全assertionを
通過した場合だけ`install_launchd.sh`で再導入する。揃わなければサービス停止状態を維持して
修正版を用意する。退避済みの旧plistも、writer所有権、root path、引数、秘密情報不在を
再レビューしない限りbootstrapしない。

緊急に1回だけ収集する必要がある場合は、launchd/cron/loopがゼロであることを確認し、
次のような**排他付きワンショットを1つずつ**実行する。while loop化、`nohup`常駐化、
`fx_tf_snapshot_loop.sh`と`fx_briefing_loop.sh`の二重起動は禁止する。

```bash
.venv/bin/python tools/run_exclusive.py --name fx-snapshot --locks-dir logs/locks \
  -- .venv/bin/python fx_tf_snapshot.py
.venv/bin/python tools/run_exclusive.py --name fx-briefing --locks-dir logs/locks \
  -- /bin/zsh scripts/fx_briefing_once.sh
```

rollback後も§2-3のlegacy safety assertion、writer数、鮮度、ジャーナルhash/行数、通知到達を
再確認する。旧cronは自動復元せず、必要な非writer行だけを`crontab.before`から明示的に戻す。

## 8. analysis-only安全境界

**現行repositoryにbroker発注経路は存在せず、復元しない。** 許可される稼働は
research、offline validation、shadow判断、通知までで、`--promote-live`は無効、実注文は出さない。
削除前の`trader/`、別checkout、container、LaunchAgentが実機に残っている可能性はrepositoryの
宣言だけでは否定できないため、移行/rollbackの前後に§2-3を実測する。

legacy executionを検出した場合は移行を停止し、証拠を保存して人間へエスカレーションする。
この分析系移行に、旧executionへ接続・操作・設定変更する権限はない。

## 9. 仮想取引→決済→学習

日次サイクルは、判断を`decision_intakes`へ先に固定し、既に収集済みの
`collect/log/quotes.jsonl`だけで遅延履歴再生する。

```bash
.venv/bin/python tools/virtual_portfolio.py cycle \
  --quotes collect/log/quotes.jsonl --close-session
.venv/bin/python tools/virtual_portfolio.py summary
```

正常時でも当日判断は`waiting_for_historical_bid_ask`になり得る。Dukascopy downloadが
判断より遅れて届くためで、次回サイクルが判断後5分以内の最初のtickをentryとして使う。
判断後のsource watermarkが5分を超えてもtickが無ければ見送り、timeframeの経路が成熟すれば
stop/targetの最初の実行可能bid/askで満期前でも決済する。未決済中は最新の利用可能な
実行価格で`simulated_position_marks`へ評価損益を追記する。`--close-session`の要求は
`session_close_requests`へ永続化され、遅延気配が未着でも後続5分サイクルが決済を継続する。
完了判定は同じ`request_id`を持つ`session_close_completions`だけを使う。
`session_events.closed`は互換投影であり、要求より前の同日eventでは完了しない。
要求後は決済完了まで、cutoff以降の新規仮想建てを停止する。完了はその時刻までの
建玉0件と台帳照合を封印するcheckpointであり、完了時刻より後の判断は直ちに新規評価へ
戻す。17:30からJST日付変更までを一律停止しない。完了時刻以前の遅延判断は、
後から到着しても決済済み状態へ遡及して建てない。完了後の遅延quoteで建てた場合は
`simulated_trade_open_observations.open_known_at_ns`をreplay as-ofで固定し、それより前の
snapshot・risk gate・session lifecycleへ建玉を遡及表示しない。
遅延決済も`simulated_trade_close_observations.close_known_at_ns`を観測時刻で固定し、
`close_known_at >= open_known_at`を必須とする。それ以前のsnapshot・cash・capacity・
V2 valuationへ決済を遡及表示しない。V2のevent head、accounting count、同期valuationも
snapshotのeffective/knowledge cutoffを超える行を参照しない。

時間軸容量は`virtual-horizon-allocation-v1`で固定する。15m/1hだけがas-of方針の
同時保有上限（v1は2件、v2は10件）を使用し、4h/1dはquote待ちも建玉も作らない
観測専用である。v2は日・週・月損失、hard drawdown、同時保有枠だけをv1の5倍にし、
1取引0.5%、PIT、bid/ask、コスト、鮮度、データ品質、安全学習の停止条件は変えない。
方針変更はschema v7の`portfolio_policy_updates`へeffective/known時刻と前方針hash付きで
追記し、遅延判断には判断時点で既知だった方針を使う。
ダッシュボードの主表示は20日ローリング純R、全コスト後期待値、95%区間、drawdown、
成熟行数とする。1日7万円はJST日次の必須運用タスクとして確定純損益、残額、進捗、
完了状態を表示するが、最適化、学習ラベル、達成保証、リスク上限の上書きには使わない。

確認項目:

- `broker_connected=false`、`execution_mode=offline_simulation`
- pending判断の初回観測時刻が判断から300秒以内
- quoteの`event <= available <= ingested <= replay as_of`
- long=`ask entry / bid exit`、short=`bid entry / ask exit`
- gross - spread - slippage - commission - financing - conversion = net
- 成熟した終了だけがlearning rowになり、日次KPIはfeature/labelに無い
- 4h/1dが`observation_only`で、15m/1h以外が保有枠を使用していない
- close requestとcompletionの`request_id`が一致し、完了時の建玉が0件
- 遅延replay建玉の`open_known_at`以前のsnapshotに建玉・容量消費が現れない
- 遅延決済の`close_known_at`以前のsnapshotに決済・現金・容量解放が現れない
- 同一区分3連敗の自動還流は安全停止だけで、riskやalphaを増やさない

週次成果物:

```bash
.venv/bin/python tools/virtual_portfolio_learning.py
```

`challenger_gate.json`、`outcome_memory.json`、`validation_evidence.json`、
`promotion_audit.json`、`multi_axis_model.json`、全ファイルのSHA-256を列挙する
`completion.json`が同一staging directoryで生成され、directory renameにより一括公開される。
同じrun IDは上書きしない。`validation_evidence.json`はrolling development test窓の
block bootstrapと1.0/1.5/2.0/3.0倍コストstressを診断として計算するが、これは固定
final testではなく昇格証拠へ転記しない。trial matrixや候補確率が無い
PBO/DSR/CPCVは`unavailable`として昇格を拒否する。
`promotion_audit.json`はauthoritative governance gateへ欠損を`None`のまま渡し、
作業ツリー、PIT、全trial、DSR/PBO、較正、lockbox、coverage、incidentの全失敗理由を保存する。
`multi_axis_model.json`はnet-R分布、regime、流動性proxy、初回公表macro surprise、
cross-asset、不確実性、執行コスト補助教師、判断時点portfolio riskを統合する。
公開/ブローカー気配はdealer order flowと表示しない。実現コストを予測featureへ逆流させず、
macro surpriseの改定値、未来availability、portfolio snapshot hash不整合を拒否する。
確率校正には分離したcalibration窓を使い、net-Rのp10-p90区間は同じ窓の
split-conformal補正を記録する。有限標本coverageの前提であるexchangeabilityは
時系列・通貨ペア間依存では保証されないため、依存構造を考慮した検証を残存ゲートとする。
第5分割は境界が動くrolling development holdoutであり、lockboxとは呼ばない。
固定final testと一回限りの固定lockboxは対象行・境界・dataset hashを事前コミットするまで
`unavailable_no_fixed_test_commitment` /
`unavailable_no_fixed_lockbox_commitment`とする。
`tools/virtual_portfolio_readiness.py`は最新の非hidden run directoryを一つだけ選び、
`completion.json`のcontract・run ID・5ファイルの完全な名前集合・SHA-256を検証してから
同一runの成果物を読む。completion欠落、hash不一致、部分staging、異なるrunの混在は
P5不合格にする。旧`sealed_not_evaluated`を固定lockbox合格として扱わない。
`trained_shadow_candidate`でも`model_usable_for_decisions=false`であり、
PBO/DSR/CPCV/lockbox/独立レビューが揃うまで通知判断へ接続しない。
150件未満、purge/embargo後の窓不足、cost/PIT不整合はfail closedであり、
自動的なparameter昇格や注文経路は存在しない。

5分consumer、平日の日次照合、金曜の検証証拠生成は次のlaunchdテンプレートを使う。

- `ops/launchd/com.fx-codex.quote-index.plist.tmpl`
- `ops/launchd/com.fx-codex.virtual-portfolio.plist.tmpl`
- `ops/launchd/com.fx-codex.virtual-portfolio-close.plist.tmpl`
- `ops/launchd/com.fx-codex.virtual-portfolio-learning.plist.tmpl`
- `ops/launchd/com.fx-codex.virtual-portfolio-read.plist.tmpl`
- `ops/launchd/com.fx-codex.dashboard-state.plist.tmpl`

writerは3サービス間で同じ`fx-virtual-portfolio`排他ロックを使用する。既存のcron、
heartbeat、手動常駐処理が同じ台帳writerを起動していないことを確認してから導入する。
quote-indexは`collect/log/quotes.jsonl`をquery-onlyで読み、`collect/index/quotes.sqlite3`
だけを単一writerで更新する。原本のdevice/inode、前回完全行hash、byte offsetを照合し、
置換・切詰め・prefix改変・15分超の索引staleを検出した場合は仮想取引をfail closedにする。
初回だけ原本全体を索引化し、以後は前回offset以降の完全行だけを処理する。
各cycleは`runs/virtual_portfolio/<JST日付>/<run-id>/cycle.json`へ台帳照合付きの
create-only監査成果物を保存する。readサービスは127.0.0.1:8771だけへbindし、
SQLite `query_only`で台帳を読み、書込み・残高変更・昇格操作を提供しない。
日次決済完了後は`runs/virtual_portfolio_daily/<JST日付>/daily_report.json`をcreate-onlyで
生成し、損益恒等式を再照合してからDiscordへ送る。配信成功は別のcreate-only receiptへ
記録し、遅延決済・一時的な送信失敗は次の5分サイクルで安全に再試行する。
日次reportの取引・損益窓はrequest-scoped completion時刻までを封印し、同じJST日付の
完了後再開取引を混ぜない。再試行では既存reportの自己hashを再計算し、request/completion、
取引、損益帰属、残高・照合を含むcanonical部分が一致するときだけ既存artifactを返す。
完了後に遅延分類されたobserver件数だけの変化では既存reportを書き換えず、canonical部分の
変化または自己hash不一致はfail-closedとする。
dashboard-stateは重い既存ログ集計を15分ごとに1回だけ実行し、
`logs/dashboard_state_cache.json`をatomic replaceする。8788の`/api/state`はこの固定
キャッシュだけを読み、HTTP request内で巨大JSONLを再走査しない。

工程完成度はコードの存在だけで判断せず、次のread-only監査で確認する。

```bash
.venv/bin/python tools/virtual_portfolio_readiness.py \
  --check-launchd \
  --dashboard-url http://127.0.0.1:8788 \
  --output logs/virtual_portfolio_readiness.json
```

出力は`engineering_score_pct`と`empirical_evidence_score_pct`を分ける。終了取引0件、
日報0件、検証済みChallenger無しを、実装済みという理由で95%へ補完してはならない。

V2 event chainのcheckpointは、SQLiteのtransactionally consistentなread-only backupを
作って全V2恒等式を再監査し、event count/head、DB snapshot SHA-256、schemaを
create-only JSONへ固定する。

```bash
.venv/bin/python tools/virtual_portfolio_checkpoint.py create \
  --database logs/fx_virtual_portfolio.sqlite3 \
  --output "$HOME/fx-codex-checkpoints/<run-id>/checkpoint.json"

.venv/bin/python tools/virtual_portfolio_checkpoint.py verify \
  --database logs/fx_virtual_portfolio.sqlite3 \
  --checkpoint "$HOME/fx-codex-checkpoints/<run-id>/checkpoint.json" \
  --expected-checkpoint-file-sha256 "<create出力を別経路で保全したraw file SHA-256>"
```

同じホスト内のcheckpointはwhole-database replacementに対する独立custodyではない。
別ホストへcheckpoint bytesをコピーし、その別ホスト上で最新の整合DB copyに対して
`prefix_preserved=true`、`out_of_band_digest_match=true`、
`different_host_copy_observed=true`を確認する。期待digestをcheckpointと同じ経路だけから
取得してはならない。ホスト名の相違は暗号署名や組織的な独立権限を意味しないため、
`independent_custody_verified`は常にfalseであり、結果は
「別ホストcopyと別経路digestを観測」を超えて主張しない。checkpoint生成・検証は台帳を
変更せず、broker、注文、口座操作を持たない。

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
| `com.fx-codex.snapshot` | 5分毎(StartInterval 300) | `fx_tf_snapshot.py` | 時間足別採点用の価格系列を供給する唯一の定期writer |
| `com.fx-codex.briefing` | 5分境界(StartCalendarInterval) | `scripts/fx_briefing_once.sh`(時間足別統合) | 判断生成・ジャーナル追記・学習更新・Discord通知 |
| `com.fx-codex.health` | 5分毎 | `tools/data_freshness_monitor.py` | データ鮮度監視。WARNING/CRITICAL/RECOVERYをDiscordへ |

### 0-1. writer所有権

| 状態ファイル | 正規writer | 禁止する競合 |
|---|---|---|
| `logs/briefing_tf_prices.jsonl` | `com.fx-codex.snapshot`のみ | signal boardの価格書込み、raw snapshot loop、別checkoutのcron |
| `logs/briefing_journal.jsonl` | `com.fx-codex.briefing`のみ | manual briefing、signal board、旧cron/旧plist |
| `logs/briefing_tf_journal.jsonl` | `com.fx-codex.briefing`のみ | manual per-timeframe、signal board、旧cron/旧plist |
| 学習・昇格・decision系の状態 | `com.fx-codex.briefing`のみ | 同じ状態を更新する任意の別プロセス |

全サービスは`tools/run_exclusive.py`の排他ロック（`flock`）経由で起動する。ただしロックは
同一checkout・同一ロック名の呼出しにしか効かない。rawな手動コマンド、別名ロック、別checkout、
旧loopとの競合を構造的には防げないため、移行時のプロセス/cron/launchd監査が必須である。
`fx_intel/journal.py`と`fx_intel/decision_log.py`の直接appendにも、ライブラリ全体を横断する
単一writer保証はない。正規writerの所有権は当面の運用統制であり、トランザクションDBまたは
共通ファイルロックへ移行するまで**未解決の残存リスク**とする。

`--signal-board`と`fx_briefing_loop.sh`は開発・一時確認専用である。Mac miniの正規サービス、
cron、旧plistのいずれかが動いている間は起動しない。`--no-price-write`でも判断ジャーナルは
更新するので、briefing writerと共存できない。`--dry-run`もsource cache/event exportを更新し得るため、
zero-write確認は正規runtimeと分離したcopyで行う。

- LaunchAgent(gui/$UID)として動く。**Mac miniは自動ログイン運用が前提**
  (再起動→自動ログイン→エージェント自動起動)。
- ワンショット型のため「異常終了からの復旧」は次周期の再実行で担保される
  (常駐プロセスの再起動ループより単純で、部分実行の中間状態が残らない)。
- 秘密情報(Discord webhook)はplistに書かず、実行時に `.env` から読む。
  鮮度監視は `DISCORD_OPS_WEBHOOK_URL`(運用専用)があれば優先し、
  無ければ既存の `DISCORD_WEBHOOK_URL` を使う。

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
| `briefing_tf_prices.jsonl` | 5分 | 15分(3周期) | 45分(9周期) | 15m採点窓(9〜21分)を守るには45分停止が実害ライン |
| `briefing_tf_journal.jsonl` | 5分 | 15分(3周期) | 45分(9周期) | 現行`--per-timeframe`定期経路の判断鮮度を直接監視 |

週末クローズ中もwriter自体は動き続ける設計(判断はstandbyでも書込みは継続)のため、
休場例外は設けていない。週末に誤検知が出る場合はこの前提が壊れた証拠なので、
閾値を緩める前にwriterの挙動を確認すること。

## 4. Discord通知仕様

- **WARNING**(黄): 更新遅延(warn閾値超過)。同一状態の再通知はcooldown(既定6時間)後のみ
- **CRITICAL**(赤): ファイル欠落 / critical閾値超過 / JSONL末尾破損。悪化遷移は即通知
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

## 6. OHLCデータソース設計(close-only経路の改善、次段階)

現状のcommunity TradingView scannerは現在値/形成中barのproxyであり、信頼できる過去の
bid/ask経路ではない。改善は`fx_intel/price_history.py`の注入口へproviderを差し込むが、
**この監査では採用providerを決定していない**。機能の存在と、ライセンス・timestamp・revision・
first-ingestion・SLAを含むPIT契約は別問題である。

| 候補 | 期待する用途 | 採用前に解決する事項 | 現在の位置付け |
|---|---|---|---|
| Dukascopy/JForex | tick/quote履歴の候補 | 安定したversioned API、利用/再配布権、source timestamp、欠損/訂正契約を未受入 | 候補のみ |
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

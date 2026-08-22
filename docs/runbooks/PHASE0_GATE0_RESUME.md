# Phase 0 / Gate 0 再開手順書

**作成日:** 2026-08-04
**対象ホスト:** `trader-mini`
**到達点:** Gate 0 の正式通過と closeout manifest の差し替えまで
**含まないもの:** activation(本番の symlink 切替・サービス再起動・データコピー)

---

## 実行記録 — 2026-08-04T00:09〜00:13Z:**GATE 0 PASS**

本手順書は実行済み。**Gate 0 は正式に通過**し、証跡は
`fx-codex-phase0-gate0-pass-evidence/20260804T001250Z-phase0-gate0-pass/`
(6ファイル + 検証済み `SHA256SUMS`)に保全されている。
旧 closeout manifest は §7.3 のとおり**無改変**で、`supersedes` から参照のみ。

| Step | 判定 | 実測 |
|---|---|---|
| 1 接続 | OK | `trader-mini` / up 34日 |
| 2 listener (A1) | PASS | PID 11764 のみ・`100.118.242.40:8788`・wildcard なし・承認リリース配下 |
| 3 系譜 (A2) | PASS | 9プロセス全て mapped。tty 親・出所不明の `ppid=1` なし |
| 4 インベントリ (A3/A5) | PASS | 16ラベル / 17plist / writer **1** / cron 0 / 空き124GB |
| 5 候補 (A4) | PASS | tree `b5faa03d…e83d9` 一致・245/245・mismatch 0 |

**§1.2(PID 63257 は子プロセス)は実データで検証済み。**
`ppid=63256`・起動秒一致・plist の `--` 以降と argv 完全一致を確認し、
さらに今回 PID が回転した状態(`79718→79719` ほか)でも同じ構造を再確認した。
判定則は証跡の `unmapped_process_rule` に明記済み。

### この実行で判明した手順書の誤り(本文に反映済み)

1. **§6 の「ファイル数 245」は誤り** → 正しくは `find` = **246** / manifest = **245**。
   `RELEASE_MANIFEST.json` の自己除外による正常な差。数で A4 を判定してはならない。
2. **§0.3 A1/A2 が PID 実数値に依存していた** → PID は毎周期変わるため、
   系譜と bind 範囲で判定する形に修正。
3. **tree 検証に `-B` の警告が無かった** → import が凍結ツリーに `__pycache__` を
   書き込む。実際に発生させ、承認のうえ削除し開示記録として証跡化した。

### 未解決(Gate 0 の合否対象外)

- `com.fx-codex.operational-sync` の `status: 1` は継続。Phase 0 完了前に要調査(§9.3)。
- ランタイムは `partial`(依存未インストール)。仕様どおりで、
  `activation_eligible: false` / `promotable: false` のままが正しい。

> **activation は未実施かつ未承認。** 次に着手してよいのは §8 の「準備(非破壊)」群のみ。

---

## 0. この手順書の位置づけ

### 0.1 なぜ必要か

`fx-codex-phase0-closeout-evidence/20260803T075502Z-phase0-closeout/manifest.json` は
`PHASE 0 BLOCKED` を記録したまま残っている。しかしその停止理由は**記録の11分後に
別セッションで解消済み**であり、現在の manifest は事実と乖離している。

この乖離を放置すると、次の判断者が「Phase 0 はブロック中」という誤った前提で動く。
本手順書は、解消済みであることを証跡で確定し、Gate 0 を正式に再判定して
closeout を差し替えるまでを扱う。

### 0.2 実行者と権限

- 実行者は人間(`fuuki`)。ssh 経由で `trader-mini` 上で実行する。
- **本手順書のコマンドは観測のみを意図している。** プロセスへのシグナル送信、
  launchctl 操作、symlink 変更、runtime data の書き換えは一切含まない。
  意図した書き込みは Step 6 の証跡ファイル生成のみで、これは Mac 側の
  `~/Desktop/` 配下にのみ行う。
- **⚠️ ただし §6.1 の tree 検証は、`-B` を付け忘れると凍結リリース内に
  `__pycache__` を書き込む。** 「読み取り専用のつもり」が実際には書き込みになる
  唯一の箇所であり、2026-08-04 の実行で実際に発生した。手順どおり `-B` を使うこと。
  万一書き込んでしまった場合は、無言で消さず、削除したうえで
  manifest の `mutations` と `disclosed_mutation` に開示記録として残す。
- CLAUDE.md の「Mac mini の process、launchd、cron、Docker、runtime data は
  人間の明示承認なしに変更しない」に従い、変更操作は本手順書の範囲外とする。

### 0.3 中止条件(いずれかに該当したら即座に停止し、報告する)

| # | 条件 | 意味 |
|---|---|---|
| A1 | Step 2 で 8788 に **launchd 管理外**の listener がいる、または wildcard bind (`*:8788` / `0.0.0.0:8788`) が現れる | 非管理ダッシュボードが再発している |
| A2 | Step 3 で、**launchd ラベルのどの PID からも系譜を辿れない** fx-codex 系プロセスが出る(ssh 監査セッションを除く) | 出所不明の書き手が増えている |
| A3 | Step 4 で `unknown_launchd_labels` が 0 でない | plist が想定外に増減している |
| A4 | Step 5 で `normalized_tree_sha256` 不一致、または manifest 収録ファイルに `missing` / `mismatch` がある | 凍結ビルドが汚染されている |
| A5 | 任意のステップで `writer_lsof` に複数 PID が現れる | single-writer 契約の破れ |

> **⚠️ A1 / A2 を PID の数値で判定しないこと。** launchd は周期実行のたびに
> 新しい PID を割り当てるため、`11764` `63256` `63257` といった具体値は
> **次回の実行では必ず変わる**。判定基準は常に
> 「**launchd ラベルから系譜を辿れるか**」と「**bind 範囲**」であって PID の値ではない。
> 2026-08-04 の実行では常駐4件を除く全 PID が回転していたが、これは正常である。

中止した場合、**回復操作を自分で行わないこと。** 観測結果をそのまま記録して止める。
Gate 0 は「異常を直してから通す」ゲートではなく「異常がないことを確認する」ゲートである。

---

## 1. 事実の確定(前提の共有)

### 1.1 解消済みブロッカーの経緯

| 時刻 (UTC) | 出来事 | 証跡 |
|---|---|---|
| 2026-08-03 08:04 | `PHASE 0 BLOCKED` 記録。PID 55195 が unmapped | `fx-codex-phase0-closeout-evidence/.../manifest.json` |
| 08:14 | PID 55195 を指紋照合で捕捉(11項目全一致) | `unmanaged-dashboard-process.json` |
| 08:15 | SIGTERM で正常停止。30秒監視で再起動なし | `unmanaged-dashboard-stop-results.json`, `unmanaged-dashboard-restart-watch.json` |
| 08:29 | Gate 0 再実行 | `gate0-rerun-raw.json` |

停止対象だった PID 55195 の素性:

```text
実行体   /opt/homebrew/Cellar/python@3.14/3.14.6/.../Python   (Python 3.14)
cwd      /Users/fuuki/srv/fx-codex
ブランチ deploy/dashboard-wilson-20260729 (dirty)
listen   *:8788 (wildcard)
親       PID 63592 = -zsh (ttys007) ← 手動起動
承認     approved_release: false / approved_runtime: false
```

停止操作の副作用は記録上ゼロ:
`operator_db_jsonl_mutation: 0`、`other_process_signals: 0`、
`writable_runtime_data_handles_zero: true`(停止前チェックで書き込みハンドルなしを確認済)。

### 1.2 PID 63257 は unmapped ではない

Gate 0 再実行の生データで、launchd ラベルに PID が直接一致しないプロセスが 7 件ある。
うち 6 件は `ssh trader-mini`(監査セッション自身)。残る 1 件が PID 63257 で、
これは以下の証拠から `com.fx-codex.quote-index` の子プロセスと確定する。

| 照合項目 | PID 63256 (launchd 直下) | PID 63257 | 判定 |
|---|---|---|---|
| ppid | 1 (launchd) | **63256** | 親子関係 |
| 起動時刻 | Mon Aug 3 17:29:58 | Mon Aug 3 17:29:58 | 秒まで一致 |
| コマンド | `run_exclusive.py --name fx-quote-index -- <child>` | `<child>` と完全一致 | ラッパーと被起動体 |
| cwd | `/Users/fuuki/srv/fx-codex` | 同一 | 一致 |

plist `com.fx-codex.quote-index` の `program_arguments` の `--` 以降が、
PID 63257 のコマンドと文字列一致する。`run_exclusive.py` はロックを取得してから
子を exec するラッパーであり、子が別 PID になるのは設計どおりの挙動である。

`writer_lsof` で `collect/index/quotes.sqlite3` を保持しているのも 63257 のみで、
`multiple_writer: 0` と整合する。

**結論:** PID 63257 は Gate 0 の `unmapped_processes` に数えるべきではない。
Gate 0 のプロセス照合ロジックは、launchd ラベルの PID と**その子孫**を
mapped と扱う必要がある。この点は Step 4 で明示的に検証する。

### 1.3 現在の期待状態

```text
loaded_labels                    16
unknown_launchd_labels            0
partial_runtime_references.total  0
multiple_writer                   0
8788 の listener                  PID 11764 のみ (com.fx-codex.dashboard)
候補リリース normalized_tree      b5faa03d9ee802e1340ec46b5af39e66d631ff76036023e9b4f6ed0ab50e83d9
候補リリース mismatch_count       0
```

---

## 2. Step 1: 接続と基点の確認

```bash
ssh trader-mini 'hostname && date -u +%Y-%m-%dT%H:%M:%SZ && uptime'
```

**期待:** `hostname` が `trader-mini`。

以降の全ステップの出力は、後で証跡化するため手元に保存すること。
各コマンドの実行時刻(UTC)も併せて控える。

---

## 3. Step 2: 8788 listener の確認(中止条件 A1)

```bash
ssh trader-mini 'lsof -nP -iTCP -sTCP:LISTEN'
```

**期待される 8788 の行(これ1行だけ):**

```text
Python  11764 fuuki    3u  IPv4 ...  TCP 100.118.242.40:8788 (LISTEN)
```

**判定基準:**

- 8788 で listen しているのが **PID 11764 ただ1つ** → 続行
- listen アドレスが `100.118.242.40:8788`(Tailscale 限定)である → 続行
- `*:8788` や `0.0.0.0:8788` が現れた → **中止(A1)**。非管理ダッシュボードの再発
- 8788 に何も listen していない → 中止。`com.fx-codex.dashboard` が落ちている

PID が 11764 から変わっている場合は、launchd による正常な再起動の可能性がある。
その場合は Step 4 で `com.fx-codex.dashboard` の PID と一致するかを確認すれば足りる
(PID の値そのものではなく、launchd 管理下であることが要件)。

---

## 4. Step 3: プロセス系譜の確認(中止条件 A2)

launchd ラベルの PID と、その子孫を含めて突き合わせる。

```bash
ssh trader-mini '
launchctl list | grep fx-codex
echo "---PROCESSES---"
ps -Ao pid,ppid,lstart,command | grep -E "fx-codex|quote_tape|run_exclusive" | grep -v grep
'
```

**判定基準:**

1. `ps` に出る各 fx-codex 系プロセスについて、`pid` または `ppid` を辿って
   `launchctl list` の PID に到達できること。
2. どの系譜にも属さないプロセスがあれば、その `ppid` を確認する。
   - `ppid` が `1`(launchd 直下)で launchd ラベルに無い → **中止(A2)**
   - `ppid` が `-zsh` や ssh セッション → **手動起動。中止(A2)**
3. `ssh trader-mini` 自体は監査セッションなので除外してよい。

**PID 63257 型のケースの扱い:**
`run_exclusive.py` 配下の子プロセスは、親が launchd ラベルに一致していれば mapped とする。
これは §1.2 で確定した扱いであり、Gate 0 の判定式に反映する必要がある。

---

## 5. Step 4: Gate 0 インベントリの再取得(中止条件 A3, A5)

Gate 0 のプリフライトを再実行する。前回と同じスクリプトがある場合はそれを使う。
無い場合は以下の読み取り専用コマンドで同等の情報を取る。

```bash
ssh trader-mini '
echo "===LOADED LABELS==="
launchctl list | grep fx-codex | sort
echo "===PLIST FILES==="
ls -1 ~/Library/LaunchAgents/com.fx-codex.*.plist | sort
echo "===WRITER LSOF==="
lsof -nP /Users/fuuki/srv/fx-codex/collect/index/quotes.sqlite3 2>/dev/null
lsof -nP /Users/fuuki/srv/fx-codex/logs/fx_virtual_portfolio.sqlite3 2>/dev/null
echo "===CRON==="
crontab -l 2>&1 | grep -i fx || echo "(no fx cron entries)"
echo "===DISK==="
df -h /Users/fuuki
'
```

**判定基準:**

| 項目 | 期待値 | 不一致時 |
|---|---|---|
| `launchctl list` の fx-codex ラベル数 | 16 | 増減あれば内訳を記録して**中止(A3)** |
| plist ファイル数 | 17 | ラベル 16 との差 1 は既知(未 load の plist)。それ以外は中止 |
| `quotes.sqlite3` の writer | 1 PID のみ | 複数なら**中止(A5)** |
| `fx_virtual_portfolio.sqlite3` の writer | 0 または 1 PID | 複数なら**中止(A5)** |
| fx 系 cron エントリ | 0 件 | 存在すれば中止(launchd 外の起動経路) |
| 空き容量 | 100GB 以上 | 下回れば記録(即中止ではない) |

ラベル数が 16 でない場合、前回との差分を出すこと:

```text
前回(2026-08-03 08:29)の16ラベル:
  briefing, dashboard, dashboard-state, datafeed-collector, health, horizon,
  monitors, operational-read, operational-sync, price-path-adapter, quote-index,
  snapshot, virtual-portfolio, virtual-portfolio-close, virtual-portfolio-learning,
  virtual-portfolio-read
```

なお `com.fx-codex.operational-sync` は前回 `status: 1`(異常終了)を記録している。
これは Gate 0 の合否要件ではないが、記録に残すこと。

---

## 6. Step 5: 候補リリースの完全性確認(中止条件 A4)

凍結ビルドが Gate 0 記録時点から変わっていないことを確認する。

```bash
ssh trader-mini '
CAND=/Users/fuuki/releases/fx-codex/releases/fx-codex-phase0-r3-provider-6f2465db7c3c-290e64f3137a
RT=/Users/fuuki/runtimes/fx-codex/fx-codex-python-f29f58a36128
echo "===CANDIDATE==="
ls -ld "$CAND"
find "$CAND" -type f | wc -l
echo "===RUNTIME==="
ls -ld "$RT"
"$RT/bin/python" -c "import sys,platform; print(sys.version.split()[0], platform.machine())"
echo "===RUNTIME PYTHON SHA==="
shasum -a 256 "$RT/bin/python"
'
```

**期待値:**

| 項目 | 期待値 |
|---|---|
| `find -type f` の出力 | **246** |
| manifest の `file_count` | **245** |
| ランタイム Python バージョン | `3.12.13 arm64` |
| ランタイム Python SHA256 | `1ce7aea77992ab97bb6225339a1d72a79925d5641e461cdfc2584916119a7edb` |

> **⚠️ 246 と 245 の差は正常であり、汚染ではない。**
> `RELEASE_MANIFEST.json` は自分自身のハッシュを記録できないため、manifest の
> `identity_exclusions` に自分を挙げている。したがって
> **manifest が網羅するファイル = 245 / 自分自身を含むディスク上の総数 = 246**。
> 2026-08-04 の実行では、ここで `find` が 246 を返したことを A4 と誤認しかけた。
> **`find` の数だけで A4 を判定してはならない。**

**A4 の正しい判定は数ではなくハッシュで行う**(§6.1 参照)。
`normalized_tree_sha256` 不一致、または manifest 収録ファイルの
`missing` / `mismatch` が 1 件でもあれば → **中止(A4)**。
ランタイム Python の SHA が不一致 → **中止(A4)**。

**注意:** ランタイムは `partial_runtime` 状態(`runtime_state: "partial"`,
`reason: "dependency_install_incomplete"`, インストール済みは pip のみ)である。
これは Gate 0 の合否要件ではない。依存インストールは activation 前工程であり、
本手順書の範囲外。`activation_eligible: false` / `promotable: false` のままで正しい。

### 6.1 tree hash による A4 判定(これが最も強い証拠)

正規の実装は候補ディレクトリ内の `tools/build_release.py` にある
(`normalized_tree_sha256()`)。

> **⚠️ このファイルは候補リリース配下にしか存在しない。**
> 稼働ツリー `~/srv/fx-codex/tools/` にも、開発ツリー(`codex/*` ブランチ)にも
> **無い**。2026-08-04 に3箇所を実測して確認済み。
> `tools/build_release.py` という相対パスで探さず、必ず
> `$CAND/tools/build_release.py` として参照すること。

**ハッシュ式を自分で書き起こしてはならない** —
`{path, type, mode, sha256}` の canonical JSON を取る式であり、
`path` と `sha256` だけを連結する素朴な実装では**一致しない**
(2026-08-04 の実行で実際に不一致を出し、汚染と誤認しかけた)。

> **🚨 `-B` を必ず付ける。**
> 凍結リリース内の `.py` を import すると、CPython が
> `tools/__pycache__/*.pyc` を**凍結ツリー内に書き込む**。これは Gate 0 記録時点に
> 存在しなかったファイルであり、以後の完全性チェックが 247 を報告するようになる。
> 2026-08-04 の実行でこれを実際に発生させ、削除して証跡に記録した。
> `python -B`(または `PYTHONDONTWRITEBYTECODE=1`)で回避できる。

```bash
ssh trader-mini '
CAND=/Users/fuuki/releases/fx-codex/releases/fx-codex-phase0-r3-provider-6f2465db7c3c-290e64f3137a
RT=/Users/fuuki/runtimes/fx-codex/fx-codex-python-f29f58a36128
"$RT/bin/python" -B - <<PY
import json, pathlib, importlib.util
spec = importlib.util.spec_from_file_location("br", "$CAND/tools/build_release.py")
br = importlib.util.module_from_spec(spec); spec.loader.exec_module(br)
root = pathlib.Path("$CAND")
m = json.load(open(root/"RELEASE_MANIFEST.json"))
entries = br._manifest_entries(root, exclude={"RELEASE_MANIFEST.json"})
man  = {e["path"]: e for e in m["files"]}
disk = {e["path"]: e for e in entries}
tree = br.normalized_tree_sha256(entries)
print("entry_count  :", len(entries), "(manifest:", m["file_count"], ")")
print("extra        :", sorted(set(disk)-set(man)))
print("missing      :", sorted(set(man)-set(disk)))
print("field_diffs  :", len([p for p in set(disk)&set(man) if disk[p]!=man[p]]))
print("tree         :", tree)
print("TREE_MATCH   :", tree == m["normalized_tree_sha256"])
PY
'
```

**合格条件(全て満たすこと):**

```text
entry_count   245 (manifest: 245)
extra         []
missing       []
field_diffs   0
TREE_MATCH    True
tree          b5faa03d9ee802e1340ec46b5af39e66d631ff76036023e9b4f6ed0ab50e83d9
```

`extra` に `__pycache__` 配下が出た場合、それは**監査者自身が作った可能性が高い**。
mtime を確認し、自分の作業由来なら削除したうえで証跡に開示記録として残す
(無言で消さない)。`missing` や `field_diffs` が 0 でなければ本物の汚染 → **中止(A4)**。

---

## 7. Step 6: Gate 0 判定と closeout の差し替え

Step 2〜5 が全て期待どおりなら、Gate 0 は通過。以下を Mac 側で作成する。

### 7.1 新しい証跡ディレクトリ

```bash
mkdir -p ~/Desktop/fx-codex/fx-codex-phase0-gate0-pass-evidence/$(date -u +%Y%m%dT%H%M%SZ)-phase0-gate0-pass
```

Step 1〜5 の生出力をこの配下に保存する。ファイル名は既存の慣行に合わせる
(`gate0-rerun-raw.json`, `gate0-listeners-raw.json` など)。

### 7.2 manifest の必須フィールド

新しい manifest には、少なくとも以下を含める。既存 closeout を**上書きせず、
新規 evidence として追加**し、旧 manifest には後続関係を書く(不変記録の原則)。

```json
{
  "schema": "fx-codex-phase0-gate0-pass-manifest-v1",
  "stage": "R3-P0-GATE0-PASS",
  "verdict": "GATE 0 PASS",
  "supersedes": {
    "evidence_id": "20260803T075502Z-phase0-closeout",
    "prior_verdict": "PHASE 0 BLOCKED",
    "resolution": "unmapped PID 55195 stopped gracefully at 2026-08-03T08:15Z",
    "resolution_evidence": "20260803T081030Z-phase0-unmanaged-dashboard-closeout"
  },
  "gate0": {
    "unmapped_processes": 0,
    "unmapped_process_rule": "launchd label PIDs and their descendants are mapped; ssh audit sessions excluded",
    "unknown_launchd_labels": 0,
    "multiple_writer": 0,
    "partial_runtime_reference_total": 0,
    "candidate_manifest_mismatch": 0,
    "pass": true
  },
  "mutations": {
    "source": 0, "tests": 0, "candidate_release": 0, "runtime": 0,
    "plist": 0, "launchctl": 0,
    "process_signal": 0, "symlink": 0, "db_or_jsonl": 0,
    "provider_or_broker_communication": 0, "push_or_pr": 0
  },
  "activation": {
    "authorized": false,
    "note": "Gate 0 通過のみ。activation は別途の明示承認を要する"
  }
}
```

`unmapped_process_rule` を明記するのが重要である。前回 PID 63257 が
unmapped に数えられかけたのは、判定式が「launchd の PID と直接一致」だけを
見ていたためで、ルールを証跡に書き残さないと同じ誤判定を繰り返す。

**`mutations` は「0 と書くための欄」ではなく実測を書く欄である。**
観測のみのつもりでも書き込みは起こりうる(§0.2 の `__pycache__`)。
1件でも発生したら、該当キーを実数にしたうえで `disclosed_mutation` を併記する:

```json
  "disclosed_mutation": {
    "path": "tools/__pycache__/build_release.cpython-312.pyc",
    "cause": "監査者が build_release.py を import した際の副作用(-B 付け忘れ)",
    "authorized_by": "operator (fuuki), 削除と開示に明示承認",
    "remediation": "削除済み。空の __pycache__ ディレクトリも削除",
    "manifest_covered_files_affected": 0,
    "tree_sha256_before_removal": "<64桁の実測値をそのまま記録する。省略形不可>",
    "tree_sha256_after_removal":  "<64桁の実測値をそのまま記録する。省略形不可>",
    "net_effect": "none; 候補は Gate 0 時点へ bit-for-bit 復元"
  }
```

2026-08-04 の実行では実際にこれが必要になり、`candidate_release: 1` を記録した。
**mutations を全て 0 にしたい誘惑で事実を曲げないこと。**

### 7.3 旧 manifest の扱い

`20260803T075502Z-phase0-closeout/manifest.json` は**改変しない**。
不変記録として残し、新 evidence の `supersedes` から参照する。
これは計画書 §3.1 の「削除や上書きではなく、メタデータで状態を表現する」
という原則と同じ扱いである。

---

## 7.4 プロセス照合の保証範囲(`entrypoint_provenance`)

`tools/phase0_inventory.py` の `resolve_process_mapping()` は、Gate 0 の
プロセス判定を2軸で行う。手作業判定に戻さないこと。

| 軸 | 答えること | 答えないこと |
|---|---|---|
| 系譜(lineage) | 誰が起動したか | 何のコードを実行しているか |
| 来歴(entrypoint provenance) | entrypoint script と interpreter の同一性 | **import される全モジュール** |

**⚠️ `entrypoint_provenance` は entrypoint までしか保証しない。**
`sys.path`、インストール済み distribution、`.pth`、`PYTHONPATH`、editable install
経由で差し替えられたコードは `ps` に現れず、この検証を通過する。
合格を「実行中プログラム全体が承認済み」と読んではならない。
import closure の検証は**別の未実装コントロール**である。

判定値の意味:

| `pass` | 意味 |
|---|---|
| `true` | 系譜・来歴の両方が承認値と一致 |
| `false` | いずれかが不一致、または解析不能(`indeterminate`) |
| `null` | **観測モード**。承認値未指定のため未検証 |

`null` は合格ではない。承認値を渡さない実行は候補来歴の収集専用である。

### 観測プロセスの扱い

collector 自身の PID 系譜だけを `observer_processes` として分類する。
**実行ファイル名では分類しない。** `ssh` を一律除外すると、未知の ssh セッション、
listener、writer まで免責してしまうためである。
2026-08-04 の実機観測で現れた6件の `ssh` は collector の系譜外(outbound client)
なので `unmapped` のまま残る。

### 承認値の定義(未実施・人間の作業)

承認値はコードにも PR にもハードコードしない。次を人間が確認したうえで、
**サービスごとに**定義する。

1. release manifest との照合
2. entrypoint の `file_sha256`
3. tree identity
4. dirty 状態がないこと

実機で観測された3ルート(`srv/releases/...`、`srv/fx-codex`、
`srv/fx-codex-operational-...`)を、観測されたという理由で承認済みとしてはならない。

---

## 8. Gate 0 通過後に残るもの

Gate 0 は「変更前状態が再現可能であること」の確認までで、Phase 0 の完了ではない。
`not_run_due_gate0` として残る 40 項目は、大きく次の 3 群に分かれる。

| 群 | 内容 | 性質 |
|---|---|---|
| 準備(非破壊) | wheelhouse 構築、wheel build A/B 比較、import smoke、pip check、runtime validator | 本番に触れない。Gate 0 通過後すぐ着手可 |
| activation | サービス停止、quiescence、データコピー、plist 設置、symlink 切替、サービス起動 | **本番稼働中システムの実切替。別途の明示承認が必要** |
| 事後 | observation cycle、post-activation safety、rollback rehearsal | activation 後にのみ実行可能 |

準備群のうち、ランタイムの依存インストール
(`partial_runtime` の `dependency_install_incomplete` を解消する工程)は
`~/runtimes/` 配下への書き込みを伴うが、本番の稼働経路には触れない。
それでも本手順書の範囲外とし、Gate 0 通過を確認してから別途判断する。

---

## 9. 想定される差異と対処

### 9.1 PID が全て変わっている

launchd は定期実行のたびにプロセスを起こす。`com.fx-codex.briefing` などは
5分周期なので、PID は当然変わる。**PID の値ではなく、系譜と listen 範囲を見る。**

常駐しているのは 3 つ:`dashboard`、`operational-read`、`virtual-portfolio-read`。
`quote-index` は周期実行だが実行時間が長く、常駐しているように見えることが多い。
これ以外(`briefing`、`monitors`、`snapshot` 等)は起動時のみ現れる。

2026-08-04 の実測では、常駐3件の PID は前回から不変
(`11764` / `90218` / `11721` = 8/1〜7/30 起動)である一方、
`quote-index` は `63256/63257` → `79718/79719` に回転し、
さらに `monitors` (78711/78712) と `briefing` (79695/79696) が実行中だった。
**いずれも `run_exclusive.py` の親子ペアで、正常である。**

### 9.2 quote-index が実行中でない

`quote-index` は周期実行なので、観測タイミングによっては 63256/63257 が
存在しないことがある。その場合 `writer_lsof` は空になる。これは正常。
**存在しないことを異常と扱わない。**

### 9.3 operational-sync の status: 1

前回 `status: 1` を記録している。Gate 0 の合否には含めないが、
Phase 0 完了前に原因を特定すべき項目として記録に残す。
本手順書では調査しない(範囲外)。

### 9.4 8788 の listener が 11764 以外の launchd 管理 PID

launchd による正常再起動。Step 4 で `com.fx-codex.dashboard` の PID と
一致することを確認できれば続行してよい。**ただし実行体のパスを必ず確認する** —
承認リリース `fx-codex-ai-learning-net-r-v3-a11801970c8c` 配下であること。
`/Users/fuuki/srv/fx-codex`(dirty チェックアウト)配下なら中止。

---

## 10. 再発防止として記録すべきこと

今回のブロッカーは、Phase 0 開始前に手動起動されたダッシュボードが原因だった。
同じことを防ぐため、以下を Phase 0 の成果物に含めることを推奨する
(本手順書では実装しない)。

1. **手動起動の検出**: `ppid` が tty 付きシェルの fx-codex 系プロセスを
   定期的に検出し警告する。Gate 0 まで気づかない状態を避ける。
2. **8788 の bind 範囲**: 管理版は `100.118.242.40:8788`(Tailscale 限定)で、
   手動版は `0.0.0.0:8788`(全インターフェース)だった。
   wildcard bind は手動起動の強い指標であり、監視項目にできる。
3. **Gate 0 の判定式**: §7.2 の `unmapped_process_rule` を判定コードに
   反映し、子孫プロセスを mapped と扱う。

---

## 付録: 参照する証跡ファイル

| ファイル | 場所 |
|---|---|
| **★ Gate 0 PASS 証跡(2026-08-04・最新)** | **`fx-codex-phase0-gate0-pass-evidence/20260804T001250Z-phase0-gate0-pass/manifest.json`** |
| 同 生出力 | 同上 `gate0-step1-host.txt` / `gate0-listeners-raw.txt` / `gate0-process-lineage-raw.txt` / `gate0-inventory-raw.txt` |
| 同 候補検証(remediation 後) | 同上 `candidate-gate0-verification.json` |
| 同 チェックサム | 同上 `SHA256SUMS`(`shasum -a 256 -c` で検証可) |
| 旧 closeout manifest(**無改変・superseded**) | `fx-codex-phase0-closeout-evidence/20260803T075502Z-phase0-closeout/manifest.json` |
| ダッシュボード停止証跡 | `fx-codex-phase0-unmanaged-dashboard-closeout-evidence/20260803T081030Z-phase0-unmanaged-dashboard-closeout/` |
| Gate 0 再実行の生データ | 同上 `gate0-rerun-raw.json` |
| リリース固定の元 manifest | `fx-codex-r3-provider-test-closure-evidence/20260803T070638Z-.../manifest.json` |
| tree hash 正規実装 | 候補リリース内 `tools/build_release.py` の `normalized_tree_sha256()` |

全て `/Users/takahashifuuki/Desktop/fx-codex/` 配下。

**読む順序:** 現状を知りたいだけなら
`20260804T001250Z-phase0-gate0-pass/manifest.json` の1件で足りる。
旧 closeout は `PHASE 0 BLOCKED` のまま残っているが、これは不変記録として
意図的に保持されているものであり、**現在の状態ではない**(§7.3)。

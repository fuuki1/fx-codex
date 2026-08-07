# S1: canonical frame アンカリングの shadow 実測

**作成日:** 2026-08-07
**対象:** 15m / 1h の decision 2,405件（実機 decision_records より抽出）
**コード:** `[DEV]` HEAD `2c7bd38` + `fx_intel/decision_price_anchor.py`
**設計:** `docs/design/CANONICAL_PRICE_FRAME_DATA_TRUTH_20260807.md` §4.2 / §4.3
**⚠️ PRODは未変更。** 実機のコード・DB・設定を一切変更していない（§6）。

---

## 結論

**1. アンカー成功率 96.7%（2,325 / 2,405）。**
設計時に想定した棄却率（15m 10.9% / 1h 10.6%）より**大幅に良い**。

**2. 棄却は 80件（3.3%）のみ。**

| 理由 | 件数 |
|---|---:|
| `microstructure_floor` | 73 |
| `no_canonical_tick_after_decision` | 7 |

**3. 分母崩壊は完全に解消した。**
canonical 上の rd/spread は **最小 2.09 / 中央値 13.38** で、
**floor(2.0) を下回る行は0件**（棄却済みのため定義上そうなる）。

**4. ⚠️ 私の §4.3 の見積もりは誤っていた。**
「15m/1h の約10%が floor で落ちる」と予測したが、実測は **3.0%**。
理由は §3 に示す。

---

## 1. 実装

`fx_intel/decision_price_anchor.py`（新規、純関数・I/Oなし）

```
入力: direction, stop_atr_multiple, target_rr, reference_atr,
      decision_time, 最初の canonical tick (event_time >= decision_time)
出力: entry / stop / target / risk_distance / spread / lag / 棄却理由
```

**座標系を越えて保存されるのは絶対価格ではなく plan の「形」**である。
`stop_atr_multiple` と `target_rr`（S0 で記録済み）を canonical tick へ
当て直すことで、下流と同じ座標系の levels を再構成する。

**PIT 保証:** `tick_event_time < decision_time` は棄却する。
呼び出し側の順序付けを信頼せず、関数内で再検査する。

**棄却は例外を投げない。** 棄却件数そのものが S1 の測定対象であるため、
すべて verdict として返す。

---

## 2. アンカー結果（実機 decision 2,405件）

| status | 件数 | 割合 |
|---|---:|---:|
| **anchored** | **2,325** | **96.7%** |
| rejected | 80 | 3.3% |

**timeframe 別:**

| timeframe | anchored | 全体 | 成功率 |
|---|---:|---:|---:|
| 15m | 1,106 | 1,145 | **96.6%** |
| 1h | 1,219 | 1,253 | **97.3%** |

---

## 3. なぜ §4.3 の予測（約10%）が外れたか

`PRICE_SOURCE_DIVERGENCE_20260807.md` §4 で測った「10.9% / 10.6%」は
**参照座標系の stop が t0 の tick を跨いでいた割合**である。

アンカリングは**その stop を捨てて canonical 上で引き直す**ため、
「跨いでいた」こと自体が問題にならない。残る棄却は
**ATR × multiple が spread の2倍に満たない場合のみ**で、それは 3.0% だった。

→ **アンカリングは、参照座標系の stop 位置ずれを構造的に解消する。**
これは設計の狙いどおりだが、規模は予測より良い。

---

## 4. アンカー lag

| 統計 | decision → 最初の canonical tick |
|---|---:|
| min | 0.00 s |
| p50 | **1.13 s** |
| p90 | 9.48 s |
| p99 | 337.87 s |
| max | 5,682.87 s |

**⚠️ これは tick の event time までの距離であり、取込ラグではない。**

- tick は decision の **1.13秒後（中央値）には市場に存在**している
- しかし Dukascopy の1時間アーカイブ経由でしか読めないため、
  **実際に読めるのは約73分後**（`LABEL_FRESHNESS_CAUSAL_DELAY_20260807.md` §2）

したがって S1 が導入する遅延は「アンカーに使う tick を待つ時間」ではなく、
**「その tick が取込まれるまでの時間」**である。

p99 の 337秒と max の 5,683秒は、市場が薄い時間帯（週末境界・21時台）で
次の tick までが空いたケースと考えられる（**推論**。個別検証はしていない）。

---

## 5. 分母は修復されたか

canonical 上の `risk_distance / spread`:

| 統計 | 値 |
|---|---:|
| min | **2.09** |
| p10 | 5.66 |
| p50 | **13.38** |
| p90 | 42.15 |
| floor(2.0)未満 | **0件** |

参照座標系では 15m/1h の median rd/spread は 0.410 / 0.385 だった
（`PRICE_SOURCE_DIVERGENCE` §4）。**canonical では 13.38 になる。**

### アンカーが参照 close からどれだけ動いたか

| 統計 | \|乖離\| |
|---|---:|
| p50 | 1.70 pips |
| p90 | 7.40 pips |
| p99 | 37.20 pips |
| max | 138.30 pips |

**参照 risk 距離に対する比:**

| 統計 | 値 |
|---|---:|
| p50 | 0.345 |
| p75 | 0.695 |
| p90 | **1.141** |
| ≥ 1.0 | **305件（13.1%）** |

→ **13.1% の decision は、乖離が参照 stop 距離を丸ごと超えていた。**
これらは参照座標系では評価不能だったが、canonical 上では
**最小 rd/spread 2.09 の健全な geometry になる。**

---

## 6. 実機を変更していないことの確認

⚠️ **作業中に一度、実機の `fx_intel/` へモジュールを配置してしまった。**
即座に削除し、`git status` で実機ツリーがクリーンであることを確認した。

その後の shadow 実行は、モジュールを `/tmp/s1pkg/` に置き
`importlib` で読み込む方式に変更し、**実機のリポジトリに一切触れていない**。

実行後は `/tmp/s1pkg` `/tmp/s1_shadow.py` `/tmp/decision_price_anchor.py` を削除済み。

| 確認項目 | 結果 |
|---|---|
| 実機 `fx_intel/decision_price_anchor.py` | **存在しない** |
| 実機 `git status` の該当行 | **0件** |
| 実機DB | 読み取り専用（`.backup` のみ） |
| 実機 launchd / plist | 未変更 |

---

## 7. テスト

| 項目 | 結果 |
|---|---|
| `tests/test_decision_price_anchor.py` | **8 passed** |
| 全tracked | **1,300 passed**, 1 failed |
| 唯一の failure | `test_learning.py::test_plan_embed_...` — baseline でも同一に失敗（確認済） |
| ruff / black / mypy | 全て green |

テストで固定した契約:

- 実機の divergence 事例（decision `5a731b85`）が修復されること
- short は bid でアンカーし stop/target が反転すること
- floor 未満は棄却されること
- `tick_event_time < decision_time` は棄却されること（PIT）
- 不正入力で**例外を投げない**こと
- 同一入力に対し決定論的であること

---

## 8. 言えないこと

1. **成績が改善するとは言えない。** 本測定は geometry の健全性のみを見ており、
   アンカー後の net-R は測っていない
2. **13.1% の「修復された」decision が良い取引になるとは言えない。**
   `PATH_DEPENDENT_SIGNAL_AUDIT_20260807.md` は方向自体に情報が無いことを示している
3. **サイドカー表への永続化は未実装。** 本測定は shadow 実行のみで、
   `decision_price_anchors` テーブルは作っていない
4. **建玉判定の遅延は未測定。** §4 のとおり tick は1.13秒後に存在するが、
   読めるのは73分後である。実運用で建玉をアンカー後まで待つ場合の
   影響は本測定の対象外

---

## 9. 次工程

| 段階 | 内容 | 状態 |
|---|---|---|
| S1残 | `decision_price_anchors` サイドカー表への永続化 | **未実装** |
| S2 | `divergence.py` の配線（乖離の継続監視） | 未着手 |
| S3 | scoring/learning がアンカー済み価格を読む | **要・設計承認** |
| S4 | decision の `stop`/`target1` を `reference_*` へ改名 | **要・人間承認**（通知文面が変わる） |

**⚠️ S3 以降は `FX_CODEX_TARGET_ARCHITECTURE_INTEGRATION_PLAN.md` §8 により
独立した設計承認を要する。** 本文書は測定であり、変更提案ではない。

---

## 付録: 再現手順

```bash
# モジュールを実機リポジトリの外へ置く（実機ツリーを汚さない）
ssh fuuki@trader-mini "mkdir -p /tmp/s1pkg/fx_intel"
scp fx_intel/decision_price_anchor.py fuuki@trader-mini:/tmp/s1pkg/fx_intel/

# shadow 実行（読み取り専用）
ssh fuuki@trader-mini "cd /Users/fuuki/srv/fx-codex && PYTHONPATH=. \
  .venv/bin/python /tmp/s1_shadow.py /tmp/audit_cohort.csv \
  collect/log/quotes.jsonl collect/index/quotes.sqlite3 /tmp/s1_result.json"
```

**文書終端**

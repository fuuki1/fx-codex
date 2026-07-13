# Runbook: Lockbox custody

**対象:** 最終検証用 lockbox の保管方式。**現状は durable local custody（実装済）。外部 custody は未実装（本runbookは interface/手順の設計）。**
**重要:** 「ローカル保管で完全防御できる」とは主張してはならない（タスク§4）。

## 1. なぜローカルでは不十分か

現在の `fx_backtester/lockbox.py` は single-use / 開封後frozen / 改竄検出（bundle再hash・registry全消しでも再開封不可）を実装し、テスト緑。しかし:

- 研究実行プロセスと **同一ホスト・同一OSユーザー・同一権限**で動く。
- したがって「研究プロセス自身が lockbox 記録を削除・改変できない」保証は**原理的に不完全**（root/同ユーザーはファイルを消せる）。
- materialize 時の warning も明示: 「release sidecars are locally bound, not externally signed or independently timed」。

## 2. 外部 custody の要件（タスク§4）

研究実行プロセスが**削除できない**保管先で、以下を保存:
- manifest hash / dataset hash / git commit / dependency lock hash / access actor / purpose / timestamp
- single-use / 開封後 frozen / artifact 改竄検出 / raw からの deterministic replay / evidence 数値の再計算照合

## 3. 実装案（優先順）

### 案A: GitHub Actions artifact（推奨・低コスト）
- 研究 CI ジョブが lockbox 登録・評価を実行し、**登録レコードを artifact としてアップロード**。
- 研究者のローカル権限では GitHub の artifact を改変できない（別 trust boundary）。
- access ledger を PR/commit に紐付け、追記のみ。
- 制約: artifact 保持期間（既定90日）。長期は Release asset か外部ストレージへ退避。

### 案B: 別アカウントの S3 互換ストレージ（write-only）
- 研究実行ロールに **PutObject のみ**（Delete/Overwrite 不可）の IAM ポリシー。Object Lock（WORM）併用。
- versioning + Object Lock で改竄・削除を防止。
- access actor / purpose をオブジェクトメタデータへ。

### 案C: 書き込み専用外部ストレージ（append-only log）
- append-only な外部ログ（例: 監査ログサービス）へ登録レコードを送出。

## 4. インターフェース設計（`lockbox.py` 拡張点）

```
class ExternalCustody(Protocol):
    def register(self, record: LockboxRegistration) -> CustodyReceipt: ...   # 冪等・単回
    def read(self, experiment_id: str) -> LockboxRegistration | None: ...     # 読み取り専用
    def verify(self, receipt: CustodyReceipt) -> bool: ...                    # 改竄検出
    # delete は存在しない（write-only）
```

- ローカル lockbox は `ExternalCustody` の **secondary mirror** として動作し、外部 receipt との hash 一致を昇格ゲートで要求する。
- 外部 receipt が無い / 一致しない → 昇格 fail-closed。

## 5. 移行手順（実装時）

1. `ExternalCustody` の案A実装（GitHub Actions artifact）を追加。
2. `experiment_pipeline.py` の昇格前検査に「外部 receipt 存在＋hash一致」を追加（無ければ `unavailable`）。
3. `test_lockbox.py` に外部custody契約テストを追加（register冪等 / read-only / verify改竄検出 / receipt欠如で昇格拒否）。
4. runbook にアクセス手順・rollback を追記。

## 6. 現状の正直な位置づけ

- **実装済み**: durable local custody（single-use / frozen / tamper検出）。
- **未実装**: 外部 custody（案A–C いずれも未配線）。
- したがって現時点の lockbox は「同一ホスト内の事故・うっかり再探索」は防ぐが、「意図的な同一ユーザーによる削除」は防げない。**正式到達には案Aの実装が必要。**

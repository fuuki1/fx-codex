"""建てる根拠の立証ゲート(§14 ABSTAIN)。

このゲートは「いつABSTAINするか」を学習しない。**ABSTAINが既定行動**であり、
建てる側に立証責任を課す。立証が無い・不足・欠測のいずれでもABSTAINへ
fail closedする。

背景(2026-09-04、反実仮想5,667件の実測):

- heuristic net_r は mean -0.0614 (z=-16.14)。週次で正の週 0/5、
  symbol×timeframe×direction の 8セルすべて負。
- TP/SL/horizon はどの水準でも負。entry filter も168単一条件すべて負で、
  正に見えた候補はout-of-sampleで全崩壊した。
- ABSTAIN は net 0.0000 であり、現時点で実装可能な全代替案に優越する
  (5,667件で累積 +348.1 R)。

したがって既定は「建てない」。この判定を覆すには、下の EntryEvidence が
すべての要件を満たす必要がある:

1. out-of-sample の実効サンプル数が十分であること
2. **net** 期待値が正であること(grossは不可 — grossは既にentry=ask/exit=bidで
   スプレッドを渡済みであり、コストを引く前の値で建てる根拠にはできない)
3. in-sample と out-of-sample の**両方**で正であること
   (単一レジームのエピソードを弾く。W34集中型の候補はここで落ちる)
4. 多重検定補正後も有意であること(探索した候補数を申告させ、Bonferroni補正する)
5. その有意性が**時間クラスタリング由来でない**こと

要件5が要る理由(2026-09-04の実測):
通常のz検定は観測が独立であることを前提とする。しかしFXの判断行は
特定の日・週に密集するため、この前提は成立しない。実測では
USDJPY 1h short の gross が naive z=+3.26 と有意に見えたが、
日ブロック平均を1標本として数え直すと z=-1.63、週ブロックでは z=+0.03 で
**符号すら定まらなかった**。15日中で正だったのは4日のみで、
行平均が正だったのは正の日に行数が集中していたためである
(08-18にn=174、09-02にn=130)。日ブロックのbootstrapでは
片側p=0.258、95%CIは0をまたぐ。
同様にentry filter探索で見つかった候補は441行中82%が単一週(W34)に集中していた。

したがって本ゲートは、行数ベースのzに加えて
**独立ブロック数(market_days)とブロック単位のz**を要求する。
`effective_samples.summarize_effective_samples` が返す
`market_days` / `cluster_count` がこの用途の既存指標である。

いずれも `None` は不合格として扱う。証拠の欠如を健全と読み替えない。

このモジュールはネットワークアクセスを持たない純粋ロジックで、
テストから直接検証できる。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

GATE_CONTRACT = "entry-evidence-gate-v1"

# 既定行動。立証が成立しない限りこれを返す。
DEFAULT_ACTION = "abstain"

# 立証要件の閾値
MIN_OOS_SAMPLES = 200  # out-of-sampleの実効サンプル数下限
MIN_NET_EXPECTANCY_R = 0.0  # net期待値はこれを**超える**必要がある
GATE_ALPHA = 0.05  # 多重検定補正前の有意水準(両側)

# 時間クラスタリング対策。行数ではなく独立ブロック数で数える。
MIN_MARKET_DAYS = 20  # out-of-sampleが跨ぐべきFX市場日数の下限
MAX_BLOCK_CONCENTRATION = 0.35  # 単一ブロックが占めてよい行数比率の上限


@dataclass(frozen=True)
class EntryEvidence:
    """1つの「建てる」提案に対する立証。

    すべて net(コスト控除後)で表現する。gross は受け付けない。
    """

    label: str
    in_sample_net_r: float | None
    out_of_sample_net_r: float | None
    out_of_sample_samples: int
    out_of_sample_z: float | None
    # 日ブロック平均を1標本として計算したz。行単位のzと違い、
    # 同一日に密集した観測が有意性を水増しするのを防ぐ。
    out_of_sample_block_z: float | None = None
    # out-of-sampleが跨ぐFX市場日数(独立ブロック数)。
    out_of_sample_market_days: int = 0
    # 最大のブロックが占める行数の比率。W34型の集中を弾く。
    max_block_share: float | None = None
    # この提案に至るまでに探索した候補の総数。1なら補正なし。
    # 探索して選んだ候補を1と申告してはならない(Bonferroni補正が無効化される)。
    candidates_searched: int = 1

    def required_abs_z(self) -> float:
        """Bonferroni補正後に要求される |z|。"""
        k = max(1, self.candidates_searched)
        return abs(_normal_inv_cdf(GATE_ALPHA / 2 / k))


@dataclass(frozen=True)
class GateDecision:
    """ゲートの判定。action は必ず "abstain" か "enter"。"""

    action: str
    reasons: list[str]
    evidence_label: str
    required_abs_z: float | None = None

    @property
    def abstained(self) -> bool:
        return self.action == DEFAULT_ACTION

    def to_dict(self) -> dict[str, object]:
        return {
            "contract": GATE_CONTRACT,
            "action": self.action,
            "reasons": list(self.reasons),
            "evidence_label": self.evidence_label,
            "required_abs_z": self.required_abs_z,
        }


def evaluate_entry_evidence(evidence: EntryEvidence | None) -> GateDecision:
    """立証を評価する。要件を1つでも満たさなければABSTAINへfail closedする。"""

    if evidence is None:
        return GateDecision(
            action=DEFAULT_ACTION,
            reasons=["立証が提出されていない"],
            evidence_label="<none>",
        )

    reasons: list[str] = []
    required = evidence.required_abs_z()

    if evidence.out_of_sample_samples < MIN_OOS_SAMPLES:
        reasons.append(
            f"out-of-sampleサンプル不足({evidence.out_of_sample_samples}/{MIN_OOS_SAMPLES}件)"
        )

    oos = evidence.out_of_sample_net_r
    if oos is None or oos <= MIN_NET_EXPECTANCY_R:
        shown = f"{oos:+.4f}" if oos is not None else "—"
        reasons.append(f"out-of-sample純R期待値が正でない({shown})")

    ins = evidence.in_sample_net_r
    if ins is None or ins <= MIN_NET_EXPECTANCY_R:
        shown = f"{ins:+.4f}" if ins is not None else "—"
        reasons.append(f"in-sample純R期待値が正でない({shown})")

    z = evidence.out_of_sample_z
    if z is None or abs(z) < required or z <= 0:
        shown = f"{z:+.2f}" if z is not None else "—"
        reasons.append(
            f"多重検定補正後の有意性不足(z={shown}, 要求|z|>={required:.2f}, "
            f"探索候補{max(1, evidence.candidates_searched)}件)"
        )

    block_z = evidence.out_of_sample_block_z
    if block_z is None or abs(block_z) < required or block_z <= 0:
        shown = f"{block_z:+.2f}" if block_z is not None else "—"
        reasons.append(f"ブロック単位の有意性不足(block_z={shown}, 要求|z|>={required:.2f})")

    if evidence.out_of_sample_market_days < MIN_MARKET_DAYS:
        reasons.append(
            f"独立ブロック不足(市場日{evidence.out_of_sample_market_days}" f"/{MIN_MARKET_DAYS}日)"
        )

    share = evidence.max_block_share
    if share is None or share > MAX_BLOCK_CONCENTRATION:
        shown = f"{share:.1%}" if share is not None else "—"
        reasons.append(f"単一ブロックへの集中({shown}>{MAX_BLOCK_CONCENTRATION:.0%})")

    if reasons:
        return GateDecision(
            action=DEFAULT_ACTION,
            reasons=reasons,
            evidence_label=evidence.label,
            required_abs_z=required,
        )
    return GateDecision(
        action="enter",
        reasons=[],
        evidence_label=evidence.label,
        required_abs_z=required,
    )


def _normal_inv_cdf(p: float) -> float:
    """標準正規の逆累積分布(Acklam近似)。scipy非依存。

    promotion.py / overfitting.py と同じくscipyを持ち込まない方針に従う。
    """

    if not 0.0 < p < 1.0:
        raise ValueError(f"p must be in (0, 1): {p}")
    a = (
        -3.969683028665376e01,
        2.209460984245205e02,
        -2.759285104469687e02,
        1.383577518672690e02,
        -3.066479806614716e01,
        2.506628277459239e00,
    )
    b = (
        -5.447609879822406e01,
        1.615858368580409e02,
        -1.556989798598866e02,
        6.680131188771972e01,
        -1.328068155288572e01,
    )
    c = (
        -7.784894002430293e-03,
        -3.223964580411365e-01,
        -2.400758277161838e00,
        -2.549732539343734e00,
        4.374664141464968e00,
        2.938163982698783e00,
    )
    d = (
        7.784695709041462e-03,
        3.224671290700398e-01,
        2.445134137142996e00,
        3.754408661907416e00,
    )
    plow = 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    if p > 1 - plow:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    q = p - 0.5
    r = q * q
    return (
        (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5])
        * q
        / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
    )


__all__ = [
    "DEFAULT_ACTION",
    "GATE_CONTRACT",
    "EntryEvidence",
    "GateDecision",
    "evaluate_entry_evidence",
]

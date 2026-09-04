"""反実仮想ラベル用の会計コストpolicy(③)。

**目的は教師ラベルの会計を正しくすることに限定する。負エッジの解消ではない。**
2026-09-04のblock-aware検定で、コスト修正に期待されていた唯一の使い道
(USDJPY 1h short の黒字化)は棄却された — 当該セルのgross自体が
時間クラスタ由来で有意でなかった(day-z -1.63、bootstrap片側p=0.258)。
したがって本modelの成功条件は「純Rが改善すること」ではなく
「各コスト項が観測可能性に照らして正しく計上されること」である。

既存の ``VirtualReplayCostPolicy`` は**変更しない**。
その ``sha256`` は ``asdict(self)`` を封じており、フィールドを1つ足すだけで
既存DB全行の ``policy_sha256`` が無効になる(実測で baseline manifest の
``1e094a66...`` と一致することを確認済み)。よって別classとして追加する。

コストを観測可能性で3層に分ける:

L1 観測済 — スプレッド
    ``gross_r`` は entry=ask / exit=bid で計算されており
    (``counterfactual_labels.py`` の entry_executable / exit_executable)、
    スプレッドは**既に1往復控除されている**。したがって本policyは
    スプレッドを**課金しない**。旧policyの slippage 200bps は
    実測スプレッド中央値 0.0169 R をほぼ二重に引いていた。

L2 導出可能 — financing
    実際のFXは日跨ぎ(NY 17:00 ロールオーバー)時にのみ発生する。
    旧policyは ``max(1, ...)`` で1日未満でも必ず1日分を課金しており、
    15m で 96.0倍、1h で 24.0倍の過大だった。本policyは
    **跨いだロールオーバー回数**だけ課金する。母集団は15m/1hなので
    大半が0回となる。

L3 未観測 — 真の執行コスト
    成行の市場インパクト、約定拒否・再クオート、部分約定、
    キューイング遅延。shadow運用では約定自体が存在しないため
    **構造的に未観測**であり、このデータからは測れない。
    ``execution_r_bps`` として独立させ、既定は 0 とする。

    ⚠️ 既定0は「執行コストが無い」という主張では**ない**。
    「未観測であり、この教師ラベルでは値を置く根拠がない」という意味である。
    measured層(実約定)が用意されるまで、この0を
    「無コストで約定できる」と読んではならない。net_rを実運用の
    期待値として解釈する際は、L3が欠けていることを必ず併記すること。

このモジュールはネットワークアクセスを持たない純粋ロジックで、
テストから直接検証できる。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from .effective_samples import FX_SESSION_ROLLOVER, FX_SESSION_TIMEZONE
from .virtual_portfolio_replay import ReplayDataError, _sha256_json

ACCOUNTING_COST_CONTRACT = "counterfactual-accounting-cost-policy-v1"


@dataclass(frozen=True)
class CounterfactualAccountingCostPolicy:
    """観測可能性で分解した反実仮想ラベル用コストpolicy。

    旧 ``VirtualReplayCostPolicy`` との差:
      - slippage_r_bps を持たない(L1はgrossで控除済み)
      - financing は「開始日数」ではなく「跨いだロールオーバー回数」で課金
      - execution_r_bps(L3)を独立させ、既定0で未観測であることを明示
    """

    cost_model_id: str = "counterfactual-accounting-cost-policy"
    version: str = "v1"
    commission_r_bps: int = 100
    financing_r_bps_per_rollover: int = 100
    conversion_r_bps: int = 50
    # L3: 未観測。既定0は「無コスト」ではなく「値を置く根拠がない」の意。
    execution_r_bps: int = 0

    def validate(self) -> None:
        if not self.cost_model_id or not self.version:
            raise ReplayDataError("cost policy identity is required")
        for name, value in asdict(self).items():
            if name in {"cost_model_id", "version"}:
                continue
            if not isinstance(value, int) or value < 0:
                raise ReplayDataError(f"{name} must be a non-negative integer")

    @property
    def sha256(self) -> str:
        return _sha256_json(asdict(self))


def rollovers_crossed(entry_time: datetime, exit_time: datetime) -> int:
    """保有区間が跨いだNY 17:00ロールオーバーの回数。

    ``effective_samples._fx_market_day`` と同じ境界定義を使う。
    exit が entry より前なら 0 を返す(負の課金を作らない)。
    """

    if entry_time.tzinfo is None or entry_time.utcoffset() is None:
        raise ReplayDataError("entry_time must be timezone aware")
    if exit_time.tzinfo is None or exit_time.utcoffset() is None:
        raise ReplayDataError("exit_time must be timezone aware")
    if exit_time <= entry_time:
        return 0
    return _market_day_index(exit_time) - _market_day_index(entry_time)


def accounting_cost_r(
    *,
    policy: CounterfactualAccountingCostPolicy,
    symbol: str,
    entry_time: datetime,
    exit_time: datetime,
) -> float:
    """観測可能性別に積み上げた、R単位のコスト。

    L1(スプレッド)は ``gross_r`` で控除済みのため**ここには含まれない**。
    """

    rollovers = rollovers_crossed(entry_time, exit_time)
    bps = (
        policy.commission_r_bps
        + policy.financing_r_bps_per_rollover * rollovers
        + policy.execution_r_bps
        + (0 if symbol[3:].upper() == "JPY" else policy.conversion_r_bps)
    )
    return bps / 10_000.0


def cost_breakdown(
    *,
    policy: CounterfactualAccountingCostPolicy,
    symbol: str,
    entry_time: datetime,
    exit_time: datetime,
) -> dict[str, object]:
    """観測可能性層を明示した内訳。監査用に層のラベルを保持する。"""

    rollovers = rollovers_crossed(entry_time, exit_time)
    conversion = 0 if symbol[3:].upper() == "JPY" else policy.conversion_r_bps
    return {
        "contract": ACCOUNTING_COST_CONTRACT,
        "policy_sha256": policy.sha256,
        "observed_l1_spread_r": {
            "charged_here": False,
            "basis": "gross_r already crosses the spread (entry=ask / exit=bid)",
        },
        "derivable_l2_r": {
            "financing_r": policy.financing_r_bps_per_rollover * rollovers / 10_000.0,
            "rollovers_crossed": rollovers,
            "commission_r": policy.commission_r_bps / 10_000.0,
            "conversion_r": conversion / 10_000.0,
        },
        "unobserved_l3_r": {
            "execution_r": policy.execution_r_bps / 10_000.0,
            "basis": (
                "market impact, rejects, partial fills and queueing are not "
                "observable in shadow operation; 0 means unmeasured, not costless"
            ),
        },
        "total_r": accounting_cost_r(
            policy=policy, symbol=symbol, entry_time=entry_time, exit_time=exit_time
        ),
    }


def _market_day_index(value: datetime) -> int:
    """FX市場日を通日番号にする(ロールオーバー回数の差分計算用)。"""

    local = value.astimezone(FX_SESSION_TIMEZONE)
    day = local.date()
    if local.timetz().replace(tzinfo=None) >= FX_SESSION_ROLLOVER:
        day = day + timedelta(days=1)
    return day.toordinal()


__all__ = [
    "ACCOUNTING_COST_CONTRACT",
    "CounterfactualAccountingCostPolicy",
    "accounting_cost_r",
    "cost_breakdown",
    "rollovers_crossed",
]

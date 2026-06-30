"""トレード・ジャーナル / 成績分析（期待値・R 倍数・連敗）。

リサーチの結論は「勝率ではなく期待値（損小利大 × サイズ管理 × 撤退速度）」。本モジュールは
確定済みトレード（fills の realized_pnl）から、勝率に依存しない指標を計算する:

  - expectancy        : 1 トレードあたり平均実現損益（期待値）
  - expectancy_r      : 1 トレードあたり平均 R 倍数（リスク正規化した期待値）
  - profit_factor     : 総利益 ÷ 総損失
  - payoff_ratio      : 平均利益 ÷ 平均損失（損小利大の度合い）
  - max_loss_streak   : 最大連敗（連敗スロットルの実効を点検）
  - current_loss_streak: 直近からの連敗（risk_engine と同義）

集計（compute_journal）は純粋関数で、DB 無しに単体テストできる。CLI は fills を読んで表示し、
monitor の日次サマリにも要約を載せる。
"""
from __future__ import annotations

from typing import Any

from risk_engine import loss_streak


def _max_loss_streak(pnls_oldest_first: list[float]) -> int:
    """時系列（古い順）から最大連敗を数える（引き分け 0 はスキップ）。"""
    worst = 0
    cur = 0
    for p in pnls_oldest_first:
        if p < 0:
            cur += 1
            worst = max(worst, cur)
        elif p > 0:
            cur = 0
    return worst


def compute_journal(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """確定トレードの一覧から成績を集計する。

    trades: [{"realized_pnl": float, "realized_r": float|None}, ...] を「新しい順」で渡す。
    realized_pnl == 0 の行（未確定）は集計から除外する。
    """
    closed = [t for t in trades if float(t.get("realized_pnl") or 0.0) != 0.0]
    pnls_new_first = [float(t["realized_pnl"]) for t in closed]
    n = len(pnls_new_first)
    if n == 0:
        return {
            "num_trades": 0, "win_rate": 0.0, "expectancy": 0.0, "expectancy_r": 0.0,
            "profit_factor": 0.0, "payoff_ratio": 0.0, "gross_profit": 0.0, "gross_loss": 0.0,
            "avg_win": 0.0, "avg_loss": 0.0, "max_loss_streak": 0, "current_loss_streak": 0,
            "total_pnl": 0.0,
        }

    wins = [p for p in pnls_new_first if p > 0]
    losses = [p for p in pnls_new_first if p < 0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    avg_win = gross_profit / len(wins) if wins else 0.0
    avg_loss = gross_loss / len(losses) if losses else 0.0

    rs = [float(t["realized_r"]) for t in closed if t.get("realized_r") is not None]

    return {
        "num_trades": n,
        "win_rate": round(len(wins) / n, 4),
        "expectancy": round(sum(pnls_new_first) / n, 4),
        "expectancy_r": round(sum(rs) / len(rs), 4) if rs else 0.0,
        "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 0 else (
            999.0 if gross_profit > 0 else 0.0
        ),
        "payoff_ratio": round(avg_win / avg_loss, 4) if avg_loss > 0 else 0.0,
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "max_loss_streak": _max_loss_streak(list(reversed(pnls_new_first))),
        "current_loss_streak": loss_streak(pnls_new_first),
        "total_pnl": round(sum(pnls_new_first), 2),
        "r_samples": len(rs),
    }


# ============================================================================
# DB 連携（CLI / monitor 用）
# ============================================================================
def fetch_trades(days: int) -> list[dict[str, Any]]:
    """直近 days 日の確定トレードを新しい順で返す。"""
    import common

    rows = common.db_query(
        "SELECT realized_pnl, realized_r FROM fills "
        "WHERE realized_pnl <> 0 AND ts >= now() - make_interval(days => %s) "
        "ORDER BY ts DESC",
        (days,),
    )
    return [{"realized_pnl": r[0], "realized_r": r[1]} for r in rows]


def journal_summary(days: int = 30) -> dict[str, Any]:
    """monitor の日次サマリ用。DB に届かなければ空集計（best-effort）。"""
    try:
        return compute_journal(fetch_trades(days))
    except Exception:
        return compute_journal([])


def format_summary(j: dict[str, Any], days: int) -> str:
    if j["num_trades"] == 0:
        return f"直近{days}日: 確定トレードなし"
    return (
        f"直近{days}日 成績: {j['num_trades']}件 / 勝率{j['win_rate'] * 100:.1f}% / "
        f"期待値{j['expectancy']:.0f}（{j['expectancy_r']:+.2f}R）/ PF{j['profit_factor']:.2f} / "
        f"損益比{j['payoff_ratio']:.2f} / 最大連敗{j['max_loss_streak']} / "
        f"現連敗{j['current_loss_streak']} / 合計{j['total_pnl']:.0f}"
    )


def main() -> None:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="トレード成績（期待値・R 倍数・連敗）")
    parser.add_argument("--days", type=int, default=30, help="集計対象の日数（既定 30）")
    parser.add_argument("--json", action="store_true", help="JSON で出力")
    args = parser.parse_args()

    j = compute_journal(fetch_trades(args.days))
    if args.json:
        print(json.dumps(j, ensure_ascii=False, indent=2))
    else:
        print(format_summary(j, args.days))


if __name__ == "__main__":
    main()

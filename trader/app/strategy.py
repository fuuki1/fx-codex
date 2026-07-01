"""④ 自作戦略ロジック（MA クロス + ATR ストップ）。

- strategy_interval_sec ごとに decide() を実行。
- シグナルがあれば webhook と同じ `signals` ストリームへ publish
  （= リスク管理・執行を共有）。
- strategy_params.json を mtime 監視してホットリロード（再起動不要）。
  auto_optimize.py が書き出す最良パラメータを自動で取り込む。

安全のため STRATEGY_ENABLED=0（既定）では一切シグナルを出さない。

ライブ発注をバックテストのポジション遷移に一致させる:
  バックテスト（fx_backtester.engine）は「目標ポジション（-1/0/+1）」で駆動し、
  反転は「クローズ＋新規」、フラット化は「クローズ」で表現される。ライブも同じく
  **現在の建玉（ブローカー実ポジション）から目標へ遷移する差分注文** を出す:
    - フラット→ロング       : entry BUY  unit
    - ロング→フラット(目標0): exit  SELL |pos|
    - ロング→ショート(反転) : exit  SELL |pos| ＋ entry SELL unit（= 実質 2 単位）
    - ストップで建玉が消えた後も、目標が続くなら次サイクルで自動再エントリー
  これにより「反転で 1 単位しか出ず実際はフラットにしかならない」「目標0でも撤退
  注文が出ない」といったバックテストとの乖離を解消する。エントリー注文には
  ``stop_distance`` を載せ、executor が保護ストップ（IBKR）を出す（= バックテストの
  ATR ストップと対応）。撤退注文は ``intent=exit`` を載せ、executor が保護ストップを
  取り消してからフラット化する。

価格データは IB の historical bars を使う（取得失敗時／建玉不明時はアイドル）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import common
import pandas as pd
from config import settings
from domain import compute_idem
from logging_setup import log_extra, setup_logging

log = setup_logging("strategy", settings.log_level)

DEFAULT_PARAMS = {"fast_window": 20, "slow_window": 60, "atr_window": 14, "atr_multiple": 2.0}
STATE_KEY = "strategy:state"  # hash: symbol -> -1/0/1（観測用。判断は実ポジション基準）

# 履歴バーの粒度。5 秒バーで MA クロスを回す（既定パラメータの想定タイムフレーム）。
HIST_BAR_SIZE = "5 secs"
HIST_BAR_SECONDS = 5
# reqHistoricalData の "S" 期間の上限（5 秒バーの安全側）。これを超える窓は要求しない。
HIST_MAX_DURATION_SEC = 7200


# ============================================================================
# 純粋ロジック（テスト可能）
# ============================================================================
def compute_atr(df: pd.DataFrame, window: int) -> float:
    """ATR を返す。high/low があれば true range、無ければ close 差分の絶対値。"""
    if {"high", "low", "close"}.issubset(df.columns):
        prev_close = df["close"].shift(1)
        tr = pd.concat(
            [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
            axis=1,
        ).max(axis=1)
    else:
        tr = df["close"].diff().abs()
    atr = tr.rolling(window).mean().iloc[-1]
    return float(atr) if pd.notna(atr) else 0.0


def ma_cross_signal(df: pd.DataFrame, params: dict[str, Any]) -> dict[str, Any] | None:
    """最新バーでの目標ポジションとストップ距離を返す。

    返り値: {"target": -1|0|1, "stop_distance": float} もしくは None（データ不足）。
    """
    fast = int(params.get("fast_window", DEFAULT_PARAMS["fast_window"]))
    slow = int(params.get("slow_window", DEFAULT_PARAMS["slow_window"]))
    atr_w = int(params.get("atr_window", DEFAULT_PARAMS["atr_window"]))
    atr_m = float(params.get("atr_multiple", DEFAULT_PARAMS["atr_multiple"]))
    if fast >= slow or len(df) < slow + 1:
        return None

    fast_ma = df["close"].rolling(fast).mean().iloc[-1]
    slow_ma = df["close"].rolling(slow).mean().iloc[-1]
    if pd.isna(fast_ma) or pd.isna(slow_ma):
        return None

    target = 1 if fast_ma > slow_ma else -1 if fast_ma < slow_ma else 0
    atr = compute_atr(df, atr_w)
    return {"target": target, "stop_distance": atr * atr_m}


def required_bars(params: dict[str, Any]) -> int:
    """このパラメータで判断が確定するのに必要な最小バー数。

    slow SMA（+1 で確定）と ATR がともに NaN でなくなるまでのバー数に余裕を足す。
    ``ma_cross_signal`` は ``len(df) < slow + 1`` で None を返すため、少なくとも
    ``slow + 1`` は必須。実データは歯抜けしうるので ATR 窓ぶんと余白を上乗せする。
    """
    slow = int(params.get("slow_window", DEFAULT_PARAMS["slow_window"]))
    atr_w = int(params.get("atr_window", DEFAULT_PARAMS["atr_window"]))
    return max(slow + atr_w + 5, 60)


def hist_duration_str(bars: int, bar_seconds: int = HIST_BAR_SECONDS) -> str:
    """必要バー数から reqHistoricalData の durationStr（"<sec> S"）を作る。

    旧実装は ``durationStr="200 S"`` 固定で 5 秒バー＝40 本しか取れず、既定
    ``slow_window=60``（61 本必要）では ``ma_cross_signal`` が恒常的に None を返し、
    **既定設定では一切シグナルが出せなかった**。ここで必要バー数から期間を逆算する。
    実データの歯抜け（配信断・薄商い）に備えて 3 倍の窓を要求し、5 秒バーの安全上限で
    クランプする。
    """
    secs = min(max(bars * bar_seconds * 3, 300), HIST_MAX_DURATION_SEC)
    return f"{secs} S"


# ============================================================================
# パラメータのホットリロード
# ============================================================================
class ParamStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self._mtime: float = 0.0
        self.params: dict[str, Any] = dict(DEFAULT_PARAMS)

    def get(self) -> dict[str, Any]:
        try:
            mtime = self.path.stat().st_mtime
            if mtime != self._mtime:
                self.params = {**DEFAULT_PARAMS, **json.loads(self.path.read_text())}
                self._mtime = mtime
                log.info("params reloaded", **log_extra(params=self.params))
        except FileNotFoundError:
            pass
        except Exception:
            log.exception("failed to load params; keeping previous")
        return self.params


# ============================================================================
# 価格取得（IB historical bars, 取得失敗時は None）
# ============================================================================
def fetch_prices(ib: Any, symbol: str, asset: str, params: dict[str, Any]) -> pd.DataFrame | None:
    """判断に必要なだけの historical bars を取得する（不足すると恒常 None になる）。

    取得期間は ``required_bars(params)`` から逆算する（既定設定でも slow+1 本を満たす）。
    """
    try:
        if asset.lower() in ("fx", "forex"):
            from ib_async import Forex

            contract = Forex(symbol)
        else:
            from ib_async import Stock

            contract = Stock(symbol, "SMART", "USD")
        data = ib.reqHistoricalData(
            contract,
            endDateTime="",
            durationStr=hist_duration_str(required_bars(params)),
            barSizeSetting=HIST_BAR_SIZE,
            whatToShow="MIDPOINT",
            useRTH=False,
        )
        if not data:
            return None
        return pd.DataFrame(
            [{"high": b.high, "low": b.low, "close": b.close} for b in data]
        )
    except Exception:
        log.exception("fetch_prices failed")
        return None


# ============================================================================
# ポジション遷移（バックテストと同じ「目標ポジション基準」）
# ============================================================================
# ブローカーのポジション数量はごく小さな端数を持ちうるので、フラット判定に用いる閾値。
_FLAT_EPS = 1e-9


def _pos_sign(qty: float) -> int:
    if qty > _FLAT_EPS:
        return 1
    if qty < -_FLAT_EPS:
        return -1
    return 0


def plan_transition(
    current_pos: float, target: int, unit_qty: float, stop_distance: float
) -> list[dict[str, Any]]:
    """現在の建玉から目標ポジションへ遷移する注文（0〜2 件）を組み立てる純粋関数。

    バックテスト（engine.run）のポジション遷移をライブで再現する:
      - 目標と同方向で保有中          → []（積み増さない = バックテスト同様）
      - フラット→ロング/ショート      → entry 1 件（unit_qty・stop 付き）
      - 保有→フラット(目標0)          → exit 1 件（現在建玉ぶんをクローズ・stop なし）
      - ロング⇄ショート(反転)         → exit（現在建玉）＋ entry（unit_qty）の 2 件
    entry には ``stop_distance`` を載せ、executor が保護ストップを出す。exit は
    ``intent=exit`` を載せ、executor が既存の保護ストップを取り消してフラット化する。
    """
    cur_sign = _pos_sign(current_pos)
    # 既に目標方向で保有している（フラット目標でもない）なら何もしない。
    if target == cur_sign and target != 0:
        return []

    orders: list[dict[str, Any]] = []
    # 1) 目標と食い違う既存建玉はクローズ（反転・フラット化の両方）。
    if cur_sign != 0 and target != cur_sign:
        orders.append(
            {"side": "SELL" if cur_sign > 0 else "BUY", "qty": abs(current_pos), "intent": "exit"}
        )
    # 2) 目標が非フラットなら新規建て（1 単位）。
    if target != 0 and target != cur_sign:
        orders.append(
            {
                "side": "BUY" if target > 0 else "SELL",
                "qty": unit_qty,
                "intent": "entry",
                "stop_distance": stop_distance,
            }
        )
    return orders


def net_from_positions(positions: list[Any], symbol: str) -> float:
    """ブローカーの建玉一覧から対象シンボルの純ポジション（BUY=+/SELL=-）を合算する。

    FX は Forex('USDJPY') が symbol='USD'/currency='JPY'/localSymbol='USD.JPY' に化けるため、
    ``symbol+currency`` と ``localSymbol`` を正規化して一致を見る。株は contract.symbol で一致。
    """
    want = symbol.upper().replace(".", "").replace("/", "")
    net = 0.0
    for p in positions:
        c = getattr(p, "contract", None)
        if c is None:
            continue
        sym = (getattr(c, "symbol", "") or "").upper()
        cur = (getattr(c, "currency", "") or "").upper()
        local = (getattr(c, "localSymbol", "") or "").upper().replace(".", "").replace("/", "")
        if want in {sym, sym + cur, local}:
            net += float(getattr(p, "position", 0) or 0)
    return net


def net_position(ib: Any, symbol: str) -> float | None:
    """対象シンボルの現在純ポジション。読めない場合は None（=判断を見送る）。"""
    try:
        return net_from_positions(ib.positions(), symbol)
    except Exception:
        log.exception("failed to read broker positions", **log_extra(symbol=symbol))
        return None


def emit_transition(
    symbol: str, asset: str, target: int, stop_distance: float, current_pos: float
) -> None:
    """実ポジションから目標へ遷移する差分注文を signals ストリームへ publish する。

    バックテストと同じく、目標に対して過不足がある時だけ注文を出す（積み増さない）。
    ストップで建玉が消えた後に目標が続いていれば、自動的に再エントリー注文になる。
    """
    orders = plan_transition(current_pos, target, settings.strategy_qty, stop_distance)
    if not orders:
        common.r().hset(STATE_KEY, symbol, target)  # 既に整合。観測用の状態だけ更新。
        return

    now_ms = int(time.time() * 1000)
    for o in orders:
        raw: dict[str, Any] = {
            "symbol": symbol,
            "asset": asset,
            "side": o["side"],
            "qty": o["qty"],
            "type": "MARKET",
            "ts": time.time(),
            "intent": o["intent"],
        }
        if o.get("stop_distance"):
            raw["stop_distance"] = o["stop_distance"]
        sig = {
            "source": "strategy",
            **raw,
            "idem": compute_idem({**raw, "id": f"strat-{symbol}-{o['intent']}-{now_ms}"}),
        }
        common.log_event("signal_received", sig)
        common.publish(common.STREAM_SIGNALS, sig)
        log.info(
            "strategy order",
            **log_extra(symbol=symbol, side=o["side"], qty=o["qty"], intent=o["intent"]),
        )
    common.r().hset(STATE_KEY, symbol, target)


def main() -> None:
    stop = common.install_signal_handlers()
    params = ParamStore(settings.strategy_params_file)

    if not settings.strategy_enabled:
        log.info("strategy disabled (STRATEGY_ENABLED=0) -> heartbeat only")
        while not stop.is_set():
            common.heartbeat("strategy")
            stop.wait(settings.strategy_interval_sec)
        return

    ib = None
    try:
        from ib_async import IB

        ib = IB()
        ib.connect(settings.ib_host, settings.ib_port, clientId=settings.ib_client_id + 70, timeout=15)
        log.info("strategy connected to IB")
    except Exception:
        log.exception("strategy could not connect to IB -> idle loop")

    while not stop.is_set():
        common.heartbeat("strategy")
        try:
            if ib is not None and ib.isConnected():
                df = fetch_prices(ib, settings.strategy_symbol, settings.strategy_asset, params.get())
                if df is not None:
                    sig = ma_cross_signal(df, params.get())
                    if sig is not None:
                        # 実ポジションを基準に遷移を決める（バックテストと同じ挙動）。
                        # 建玉が読めない時は誤発注を避けて見送る（fail-safe）。
                        pos = net_position(ib, settings.strategy_symbol)
                        if pos is not None:
                            emit_transition(
                                settings.strategy_symbol,
                                settings.strategy_asset,
                                sig["target"],
                                sig["stop_distance"],
                                pos,
                            )
                ib.sleep(0.2)
        except Exception:
            log.exception("strategy loop error")
        stop.wait(settings.strategy_interval_sec)

    if ib is not None and ib.isConnected():
        ib.disconnect()
    log.info("strategy stopped")


if __name__ == "__main__":
    main()

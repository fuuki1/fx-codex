"""④ 自作戦略ロジック（MA クロス + ATR ストップ）。

- strategy_interval_sec ごとに decide() を実行。
- シグナルがあれば webhook と同じ `signals` ストリームへ publish
  （= リスク管理・執行を共有）。
- strategy_params.json を mtime 監視してホットリロード（再起動不要）。
  auto_optimize.py が書き出す最良パラメータを自動で取り込む。

安全のため STRATEGY_ENABLED=0（既定）では一切シグナルを出さない。
シグナルはポジション状態が変化した時だけ出す（毎ループの連投を防ぐ）。
価格データは IB の historical bars を使う（取得失敗時はアイドル）。
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
STATE_KEY = "strategy:state"  # hash: symbol -> -1/0/1


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
def fetch_prices(ib: Any, symbol: str, asset: str, bars: int = 200) -> pd.DataFrame | None:
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
            durationStr=f"{max(bars, 60)} S",
            barSizeSetting="5 secs",
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
# シグナル発行（状態変化時のみ）
# ============================================================================
def emit_if_changed(symbol: str, asset: str, target: int, stop_distance: float) -> None:
    prev = common.r().hget(STATE_KEY, symbol)
    prev_state = int(prev) if prev is not None else 0
    if target == prev_state or target == 0:
        return
    side = "BUY" if target == 1 else "SELL"
    raw = {
        "symbol": symbol,
        "asset": asset,
        "side": side,
        "qty": settings.strategy_qty,
        "type": "MARKET",
        "ts": time.time(),
        "stop_distance": stop_distance,
    }
    sig = {
        "source": "strategy",
        **raw,
        "idem": compute_idem({**raw, "id": f"strat-{symbol}-{int(time.time())}"}),
    }
    common.log_event("signal_received", sig)
    common.publish(common.STREAM_SIGNALS, sig)
    common.r().hset(STATE_KEY, symbol, target)
    log.info("strategy signal", **log_extra(symbol=symbol, side=side, target=target))


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
                df = fetch_prices(ib, settings.strategy_symbol, settings.strategy_asset)
                if df is not None:
                    sig = ma_cross_signal(df, params.get())
                    if sig is not None:
                        emit_if_changed(
                            settings.strategy_symbol,
                            settings.strategy_asset,
                            sig["target"],
                            sig["stop_distance"],
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

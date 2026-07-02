#!/usr/bin/env python3
"""strategy_params の承認（昇格）・ロールバックツール。

auto_optimize.py が書き出した strategy_params.candidate.json を検証し、
合格した場合のみ strategy_params.json（配備用）へ昇格させる。

- 昇格前に現行の strategy_params.json を strategy_params.prev.json へ退避する。
- provenance に警告（overfit・取引数不足など）があれば既定で昇格を拒否する。
  内容を確認した上で受け入れる場合のみ --force を付ける。
- 事故時は `python3 promote_params.py --rollback` の1コマンドで直前の状態へ戻す。

使い方:
    python3 promote_params.py            # candidate を検証して昇格
    python3 promote_params.py --check    # 検証のみ（CI・事前確認用）
    python3 promote_params.py --rollback # 直前のパラメータへ戻す
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

import params_gate

BASE_DIR = Path(__file__).resolve().parent
ACTIVE_NAME = "strategy_params.json"
CANDIDATE_NAME = "strategy_params.candidate.json"
PREV_NAME = "strategy_params.prev.json"


def _atomic_write(path: Path, params: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(params, indent=2, ensure_ascii=False))
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _summarize(params: dict) -> str:
    prov = params.get("provenance", {})
    data = prov.get("data", {})
    lines = [
        f"  params: fast={params.get('fast_window')} slow={params.get('slow_window')} "
        f"atr={params.get('atr_window')} mult={params.get('atr_multiple')} "
        f"symbol={params.get('best_symbol')}",
        f"  IS: sharpe={params.get('sharpe')} pf={params.get('profit_factor')} "
        f"maxDD={params.get('max_drawdown_pct')}%",
        f"  OOS: {prov.get('oos', {})}",
        f"  data: {data.get('path')} ({data.get('rows')} rows, "
        f"{data.get('start')} .. {data.get('end')})",
        f"  trades: {prov.get('trade_count')}  updated_at: {params.get('updated_at')}",
    ]
    return "\n".join(lines)


def promote(base_dir: Path, *, force: bool = False, check_only: bool = False) -> int:
    candidate_path = base_dir / CANDIDATE_NAME
    active_path = base_dir / ACTIVE_NAME
    prev_path = base_dir / PREV_NAME

    params, errors = params_gate.load_validated_params(candidate_path)
    if params is None:
        print(f"[promote] ⛔ candidate が検証に不合格: {candidate_path}")
        for e in errors:
            print(f"[promote]   - {e}")
        print("[promote] strategy_params.json は変更しません。")
        return 1

    print(f"[promote] candidate 検証合格: {candidate_path}")
    print(_summarize(params))

    warnings = params.get("provenance", {}).get("warnings", [])
    for w in warnings:
        print(f"[promote] ⚠️ {w}")
    if warnings and not force and not check_only:
        print(
            "[promote] ⛔ 警告付きの candidate は既定で昇格しません。"
            "内容を確認して受け入れる場合は --force を付けてください。"
        )
        return 1

    if check_only:
        print("[promote] --check のため変更なし。")
        return 0

    if active_path.is_file():
        prev = json.loads(active_path.read_text(encoding="utf-8"))
        _atomic_write(prev_path, prev)
        print(f"[promote] 現行パラメータを退避: {prev_path}")
    _atomic_write(active_path, params)
    print(f"[promote] ✅ 昇格完了: {active_path}")
    print(f"[promote] 戻すには: python3 {Path(__file__).name} --rollback")
    return 0


def rollback(base_dir: Path, *, force: bool = False) -> int:
    active_path = base_dir / ACTIVE_NAME
    prev_path = base_dir / PREV_NAME

    if not prev_path.is_file():
        print(f"[promote] ⛔ 退避ファイルが無い: {prev_path}")
        return 1
    params, errors = params_gate.load_validated_params(prev_path)
    if params is None:
        print("[promote] ⚠️ 退避パラメータが現行ゲートでは不合格:")
        for e in errors:
            print(f"[promote]   - {e}")
        if not force:
            print(
                "[promote] ⛔ それでも戻す場合は --force を付けてください"
                "（読み込み側のゲートにも拒否される可能性があります）。"
            )
            return 1
        params = json.loads(prev_path.read_text(encoding="utf-8"))

    current = None
    if active_path.is_file():
        current = json.loads(active_path.read_text(encoding="utf-8"))
    _atomic_write(active_path, params)
    if current is not None:
        _atomic_write(prev_path, current)  # もう一度 --rollback すれば元に戻れる
    print(f"[promote] ✅ ロールバック完了: {active_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dir",
        type=Path,
        default=BASE_DIR,
        help="パラメータファイルのディレクトリ（既定: スクリプトと同じ場所）",
    )
    parser.add_argument("--check", action="store_true", help="検証のみで昇格しない")
    parser.add_argument(
        "--force",
        action="store_true",
        help="警告付き candidate の昇格 / 不合格 prev へのロールバックを許可",
    )
    parser.add_argument("--rollback", action="store_true", help="strategy_params.prev.json へ戻す")
    args = parser.parse_args(argv)

    if args.rollback:
        return rollback(args.dir, force=args.force)
    return promote(args.dir, force=args.force, check_only=args.check)


if __name__ == "__main__":
    raise SystemExit(main())

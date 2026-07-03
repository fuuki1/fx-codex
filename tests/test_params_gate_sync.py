"""リポジトリ直下 params_gate.py と trader/app/params_gate.py の同期を検証する。

読み込み側（trader コンテナ）と生成/検証側（flat リポジトリ）で判定ロジックが
ずれると、片方だけが弾く/通すという事故になる。両者はモジュール docstring を
除いて完全に同一でなければならない（docstring だけはミラーである旨の注記が入る）。
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ROOT_GATE = REPO_ROOT / "params_gate.py"
TRADER_GATE = REPO_ROOT / "trader" / "app" / "params_gate.py"


def _body_without_docstring(path: Path) -> str:
    """モジュール docstring を除いたソース（AST を unparse で正規化）を返す。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    body = tree.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    module = ast.Module(body=body, type_ignores=[])
    return ast.unparse(module)


def test_trader_copy_exists() -> None:
    assert TRADER_GATE.is_file(), "trader/app/params_gate.py が無い（コンテナに同梱されない）"


def test_gate_logic_is_identical() -> None:
    root = _body_without_docstring(ROOT_GATE)
    trader = _body_without_docstring(TRADER_GATE)
    assert root == trader, (
        "params_gate.py の検証ロジックが flat リポジトリと trader/app で乖離している。"
        "両方を同一内容に更新すること（docstring 以外は完全一致が必須）。"
    )

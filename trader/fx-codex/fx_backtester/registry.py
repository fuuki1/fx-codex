"""戦略レジストリ。名前から Strategy を生成する。"""
from __future__ import annotations

from typing import Any

from .strategy.base import Strategy
from .strategy.ma_cross import MaCrossStrategy

_REGISTRY: dict[str, type[Strategy]] = {
    "ma_cross": MaCrossStrategy,
}


def available() -> list[str]:
    return sorted(_REGISTRY)


def create(name: str, params: dict[str, Any]) -> Strategy:
    if name not in _REGISTRY:
        raise KeyError(f"unknown strategy {name!r}; available: {available()}")
    return _REGISTRY[name](**params)

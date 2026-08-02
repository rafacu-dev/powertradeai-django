"""Catalogo de reglas.

Cada modulo importado aqui auto-registra sus reglas via ``@register``.
"""
from .base import (  # noqa: F401
    BaseStrategy,
    ExitDecision,
    ScanContext,
    Signal,
    all_strategies,
    get_strategy_class,
    register,
)
from . import (  # noqa: F401  (auto-registro)
    aggression, bb_midpoint, e01e02, orb15, prevclose,
)

__all__ = [
    "BaseStrategy", "ExitDecision", "ScanContext", "Signal",
    "all_strategies", "get_strategy_class", "register",
]

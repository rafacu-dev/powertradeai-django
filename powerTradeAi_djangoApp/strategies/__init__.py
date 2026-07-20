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
from . import aggression, bb_midpoint, orb15, prevclose  # noqa: F401  (auto-registro)

__all__ = [
    "BaseStrategy", "ExitDecision", "ScanContext", "Signal",
    "all_strategies", "get_strategy_class", "register",
]

"""Indicadores. Copia autocontenida de ``core/indicators.py`` y ``core/levels.py``.

Se duplican a proposito: la app tiene que ser instalable en un proyecto Django
cualquiera sin arrastrar todo LocalQuantAI. El precio de esa decision es que si
alguien cambia una formula alli, aqui no cambia sola — por eso
``tests/test_bb_parity.py`` compara ambas implementaciones sobre los mismos
datos y falla si divergen.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

PIVOT_LEN = 7
MAX_PIVOTS = 25
MIN_TOUCHES = 3
TOL_ATR = 0.5


def bollinger_bands(close: pd.Series, period: int = 20, std: float = 2.0):
    """Devuelve (banda_superior, media, banda_inferior)."""
    mid = close.rolling(period).mean()
    sd = close.rolling(period).std()
    return mid + std * sd, mid, mid - std * sd


def atr(high, low, close, period: int = 14) -> pd.Series:
    """ATR de Wilder, suavizado exponencial con ``com = period - 1``."""
    prev_close = close.shift(1)
    true_range = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return true_range.ewm(com=period - 1, adjust=False).mean()


def pivot_prices(high, low, n: int = PIVOT_LEN,
                 maxpiv: int = MAX_PIVOTS) -> list[float]:
    """Precios de los ultimos swing highs/lows confirmados, combinados.

    Un pivote exige ser el maximo (o minimo) ESTRICTO de su ventana: un empate
    dentro de la ventana lo descarta.
    """
    highs = np.asarray(high, dtype=float)
    lows = np.asarray(low, dtype=float)
    out: list[float] = []
    for i in range(n, len(highs) - n):
        window_high = highs[i - n:i + n + 1]
        if highs[i] == window_high.max() and (window_high == highs[i]).sum() == 1:
            out.append(float(highs[i]))
        window_low = lows[i - n:i + n + 1]
        if lows[i] == window_low.min() and (window_low == lows[i]).sum() == 1:
            out.append(float(lows[i]))
    return out[-maxpiv:]


def cluster(prices: list[float], tol: float, mintouch: int = MIN_TOUCHES,
            maxlevels: int = 6) -> list[float]:
    """Agrupacion greedy: el nivel con mas pivotes cerca, repetido."""
    prices = list(prices)
    used = [False] * len(prices)
    levels: list[float] = []
    while len(levels) < maxlevels:
        best_count, center, best_index = 0, None, -1
        for j in range(len(prices)):
            if used[j]:
                continue
            candidate = prices[j]
            count, total = 0, 0.0
            for k in range(len(prices)):
                if abs(prices[k] - candidate) <= tol:
                    count += 1
                    total += prices[k]
            if count > best_count:
                best_count, center, best_index = count, total / count, j
        if best_index < 0 or best_count < mintouch:
            break
        for k in range(len(prices)):
            if abs(prices[k] - center) <= tol:
                used[k] = True
        levels.append(center)
    return levels


def horizontal_levels(high, low, atr_value: float | None, n: int = PIVOT_LEN,
                      maxpiv: int = MAX_PIVOTS, mintouch: int = MIN_TOUCHES,
                      tol_atr: float = TOL_ATR,
                      maxlevels: int = 6) -> list[float]:
    if atr_value is None or not (atr_value > 0):
        return []
    pivots = pivot_prices(high, low, n, maxpiv)
    if not pivots:
        return []
    return cluster(pivots, atr_value * tol_atr, mintouch, maxlevels)

"""Causalidad de las skills del agente en entrenamiento.

Regla: en entrenamiento (con ``as_of``) ninguna skill puede mirar dentro de la
vela EN CURSO — solo velas cuyo cierre ya ocurrio. Estos tests fijan esa
condicion para que el look-ahead no vuelva por descuido.
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd

from powerTradeAi_djangoApp.agent import skills
from powerTradeAi_djangoApp.strategies.base import NY


def _frame(times_et, highs):
    idx = pd.DatetimeIndex(
        [pd.Timestamp(f"2026-07-15 {t}", tz=NY) for t in times_et]
    ).tz_convert("UTC")
    n = len(times_et)
    return pd.DataFrame(
        {"open": [1.0] * n, "high": highs, "low": [1.0] * n,
         "close": [1.0] * n, "volume": [100] * n},
        index=idx)


class Fake15m:
    """Dos velas de 15m: 13:00 (con una mecha extrema) y 13:15."""
    def bars(self, sym, start, end, tf):
        return _frame(["13:00", "13:15"], highs=[999.0, 5.0])


def test_bars_upto_excluye_la_vela_en_curso():
    # 13:05: la vela 13:00 cierra a las 13:15 -> aun NO observable.
    ctx = {"as_of": datetime(2026, 7, 15, 13, 5, tzinfo=NY)}
    out = skills._bars_upto(ctx, Fake15m(), "TSLA", None, None, "15m")
    assert out.empty, "la vela en curso (cierra 13:15) no debe verse a las 13:05"


def test_bars_upto_incluye_la_vela_ya_cerrada():
    # 13:20: la vela 13:00 (cierre 13:15) ya es observable; la 13:15 aun no.
    ctx = {"as_of": datetime(2026, 7, 15, 13, 20, tzinfo=NY)}
    out = skills._bars_upto(ctx, Fake15m(), "TSLA", None, None, "15m")
    assert len(out) == 1
    assert float(out["high"].iloc[0]) == 999.0


def test_bars_upto_en_vivo_no_trunca():
    # Sin as_of (en vivo): el provider ya da la vela en curso real, no se toca.
    ctx = {"as_of": None}
    out = skills._bars_upto(ctx, Fake15m(), "TSLA", None, None, "15m")
    assert len(out) == 2


class Fake1m:
    def bars(self, sym, start, end, tf):
        # 13:04 cerro a 100; 13:05 (en curso a las 13:05:30) marca 200.
        idx = pd.DatetimeIndex(
            [pd.Timestamp("2026-07-15 13:04", tz=NY),
             pd.Timestamp("2026-07-15 13:05", tz=NY)]).tz_convert("UTC")
        return pd.DataFrame(
            {"open": [100.0, 100.0], "high": [100.0, 200.0],
             "low": [100.0, 100.0], "close": [100.0, 200.0], "volume": [1, 1]},
            index=idx)


def test_price_asof_no_adelanta_el_1m_en_curso():
    # A las 13:05:30 el precio causal es el cierre de 13:04 (100), no el 200
    # de la vela de 13:05 que aun no cierra.
    px = skills._price_asof(Fake1m(), "TSLA",
                            datetime(2026, 7, 15, 13, 5, 30, tzinfo=NY))
    assert px == 100.0

"""Ninguna regla debe alimentar sus indicadores con premarket/after-hours.

``ScanContext.history`` devuelve las barras CRUDAS del proveedor. Medido sobre
datos reales: hasta un 49% mas de velas de 1h y un 25% mas de 15m, que desplazan
el punto medio hasta 0.44% y cambian el ancho de banda entre -11% y +27%.

Los tests de paridad NO detectan esto: usan datos sinteticos ya limpios, y tanto
la regla como su referencia compartian el punto ciego. Por eso hace falta este.
"""
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from powerTradeAi_djangoApp.strategies.base import solo_rth

NY = ZoneInfo("America/New_York")


def _con_extendido(dias=3, tf_min=60):
    """Barras de 04:00 a 19:45: como las devuelve el proveedor de verdad."""
    idx, dia = [], datetime(2026, 7, 1)
    por_dia = int((19 * 60 + 45 - 4 * 60) / tf_min) + 1
    for _ in range(dias):
        base = pd.Timestamp(datetime(dia.year, dia.month, dia.day, 4, 0), tz=NY)
        idx += [(base + pd.Timedelta(minutes=tf_min * k)).tz_convert("UTC")
                for k in range(por_dia)]
        dia += timedelta(days=1)
    idx = pd.DatetimeIndex(sorted(idx))
    c = np.linspace(100.0, 101.0, len(idx))
    return pd.DataFrame({"open": c, "high": c + 0.1, "low": c - 0.1,
                         "close": c, "volume": 1}, index=idx)


def test_solo_rth_deja_unicamente_la_sesion_regular():
    crudo = _con_extendido(tf_min=60)
    limpio = solo_rth(crudo)
    tiempos = limpio.tz_convert(NY).index.time
    assert all(time(9, 30) <= t < time(16, 0) for t in tiempos), (
        "quedaron barras fuera de la sesion regular")
    assert len(limpio) < len(crudo), "no filtro nada"
    # y no debe tirar las de dentro
    crudos_rth = sum(1 for t in crudo.tz_convert(NY).index.time
                     if time(9, 30) <= t < time(16, 0))
    assert len(limpio) == crudos_rth


def test_solo_rth_tolera_vacio_y_none():
    assert solo_rth(None) is None
    vacio = pd.DataFrame(columns=["open", "high", "low", "close"],
                         index=pd.DatetimeIndex([], tz="UTC"))
    assert solo_rth(vacio).empty


@pytest.mark.parametrize("modulo,funcion", [
    ("bb_midpoint", "_frames"),
    ("aggression", "_h1_support"),
    ("e01e02", "evaluate"),
])
def test_las_reglas_afectadas_filtran_el_historial(modulo, funcion):
    """El filtrado debe ser VISIBLE en el codigo de cada regla.

    No se filtra dentro de ``history`` a proposito: alguna regla podria querer
    el extendido legitimamente. Pero quien no lo quiera tiene que decirlo.
    """
    import importlib
    import inspect
    m = importlib.import_module(f"powerTradeAi_djangoApp.strategies.{modulo}")
    fuente = inspect.getsource(m)
    assert "solo_rth(ctx.history" in fuente, (
        f"{modulo}.{funcion} usa ctx.history sin filtrar horario extendido")

"""E01/E02 rama de apertura y la convencion FORMING_15M.

Lo critico: esta es la UNICA regla del proyecto que mira la vela en curso.
Dos errores opuestos, ambos faciles y ambos con resultados plausibles:
  - usar el OHLC final de las 09:45  -> look-ahead
  - exigir que la vela cierre        -> se pierde la rama entera
"""
from datetime import date, datetime, time as dtime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from powerTradeAi_djangoApp.strategies.base import ScanContext
from powerTradeAi_djangoApp.strategies.e01e02 import (
    E01E02AperturaBase, _bollinger, _linea_max_contactos)

NY = ZoneInfo("America/New_York")
DIA = date(2026, 7, 6)


def _min1(inicio_ny, n, precio, paso=0.0):
    idx = pd.date_range(pd.Timestamp(inicio_ny, tz=NY).tz_convert("UTC"),
                        periods=n, freq="1min")
    c = np.array([precio + paso * i for i in range(n)], dtype=float)
    return pd.DataFrame({"open": c, "high": c + 0.05, "low": c - 0.05,
                         "close": c, "volume": 100}, index=idx)


def _hist15(n=40, desde=104.0, hasta=100.0):
    """Tramo bajista SUAVE que termina AYER (bandas estrechas)."""
    idx = pd.date_range(pd.Timestamp(datetime(2026, 7, 2, 9, 30), tz=NY)
                        .tz_convert("UTC"), periods=n, freq="15min")
    c = np.linspace(desde, hasta, n)
    return pd.DataFrame({"open": c, "high": c + 0.05, "low": c - 0.05,
                         "close": c, "volume": 1000}, index=idx)


class _Prov:
    def __init__(self, h15):
        self.h15 = h15

    def bars(self, symbol, start, end, tf):
        return self.h15 if tf == "15m" else pd.DataFrame()

    def option_quote(self, occ, at=None):
        return None


def _ctx(hoy, h15, minuto=31):
    return ScanContext(
        provider=_Prov(h15), symbol="TSLA", session_date=DIA,
        now=datetime.combine(DIA, dtime(9, minuto), tzinfo=NY), bars=hoy)


def _regla(direccion="CALL"):
    return type("R", (E01E02AperturaBase,),
                {"symbol": "TSLA", "direction": direccion,
                 "strategy_id": "T", "rule_version": "t"})()


# --- forming_bar -----------------------------------------------------------

def test_forming_bar_solo_ve_minutos_cerrados():
    hoy = _min1(datetime(2026, 7, 6, 9, 30), 15, 100.0, paso=1.0)
    f = _ctx(hoy, _hist15()).forming_bar(15)
    assert f["minutes"] == 1              # a las 09:31 solo cerro el de 09:30
    assert f["open"] == 100.0
    assert f["close"] == 100.0
    assert f["high"] < 101.0              # no ve el minuto 09:31


def test_forming_bar_crece_con_los_minutos():
    hoy = _min1(datetime(2026, 7, 6, 9, 30), 15, 100.0, paso=1.0)
    f = _ctx(hoy, _hist15(), minuto=36).forming_bar(15)
    assert f["minutes"] == 6
    assert f["close"] == 105.0


def test_forming_bar_none_sin_minutos():
    hoy = _min1(datetime(2026, 7, 6, 9, 30), 15, 100.0)
    ctx = ScanContext(provider=_Prov(_hist15()), symbol="TSLA", session_date=DIA,
                      now=datetime.combine(DIA, dtime(9, 30), tzinfo=NY), bars=hoy)
    assert ctx.forming_bar(15) is None


# --- señal -----------------------------------------------------------------

def test_e01_dispara_con_gap_alcista():
    hoy = _min1(datetime(2026, 7, 6, 9, 30), 15, 103.0, paso=0.05)
    s = _regla("CALL").evaluate(_ctx(hoy, _hist15()))
    assert s is not None
    assert s.direction == "CALL"
    assert s.meta["rama"] == "OPENING_GAP"
    assert s.meta["bar_state"] == "FORMING_15M"
    assert s.meta["gap_pct"] > 0


def test_sin_gap_no_dispara():
    hoy = _min1(datetime(2026, 7, 6, 9, 30), 15, 100.0, paso=0.05)
    assert _regla("CALL").evaluate(_ctx(hoy, _hist15())) is None


def test_gap_pequeño_no_abre_bandas():
    hoy = _min1(datetime(2026, 7, 6, 9, 30), 15, 100.5, paso=0.05)
    assert _regla("CALL").evaluate(_ctx(hoy, _hist15())) is None


def test_e01_no_dispara_si_ya_venia_alcista():
    """Invalidacion publicada: 'si el activo ya venia alcista'."""
    hoy = _min1(datetime(2026, 7, 6, 9, 30), 15, 107.0, paso=0.05)
    alcista = _hist15(desde=100.0, hasta=104.0)
    assert _regla("CALL").evaluate(_ctx(hoy, alcista)) is None


def test_solo_evalua_en_la_hora_de_decision():
    hoy = _min1(datetime(2026, 7, 6, 9, 30), 30, 103.0, paso=0.05)
    assert _regla("CALL").evaluate(_ctx(hoy, _hist15(), minuto=45)) is None


# --- causalidad (lo critico) ----------------------------------------------

def test_no_usa_el_ohlc_final_de_las_0945():
    hoy = _min1(datetime(2026, 7, 6, 9, 30), 15, 103.0, paso=0.05)
    base = _regla("CALL").evaluate(_ctx(hoy, _hist15()))
    assert base is not None
    manip = hoy.copy()
    tras = manip.index >= pd.Timestamp(
        datetime(2026, 7, 6, 9, 31), tz=NY).tz_convert("UTC")
    for col in ("open", "high", "low", "close"):
        manip.loc[tras, col] *= 10          # futuro absurdo
    otro = _regla("CALL").evaluate(_ctx(manip, _hist15()))
    assert otro is not None
    assert base.meta == otro.meta, "la decision cambio al alterar el futuro"


def test_el_historial_no_incluye_la_sesion_viva():
    """``history`` termina ayer; si colara hoy, el contexto leeria el futuro."""
    hoy = _min1(datetime(2026, 7, 6, 9, 30), 15, 103.0, paso=0.05)
    h = _ctx(hoy, _hist15()).history("15m", days=20)
    assert h.index.max().tz_convert(NY).date() < DIA


# --- piezas ----------------------------------------------------------------

def test_linea_e01_descendente_min_dos_contactos():
    ln = _linea_max_contactos(_hist15(), "CALL")
    assert ln is not None and ln["contactos"] >= 2


def test_bollinger_se_abre_al_sumar_la_barra_en_formacion():
    base = np.full(20, 100.0)
    sin_ = _bollinger(base); con_ = _bollinger(np.append(base, 105.0))
    assert (con_[2] - con_[0]) > (sin_[2] - sin_[0])


def test_las_doce_reglas_estan_registradas():
    from powerTradeAi_djangoApp.strategies.base import all_strategies
    ids = set(all_strategies())
    for sym in ("TSLA", "NVDA", "AAPL", "MSFT", "AMZN", "META"):
        assert f"{sym}_E01_APERTURA" in ids
        assert f"{sym}_E02_APERTURA" in ids

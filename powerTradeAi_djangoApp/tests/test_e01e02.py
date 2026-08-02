"""E01/E02 rama de apertura y la convencion FORMING_15M.

Lo critico: esta es la UNICA regla del proyecto que mira la vela en curso.
Dos errores opuestos, ambos faciles y ambos con resultados plausibles:
  - usar el OHLC final de las 09:45  -> look-ahead
  - exigir que la vela cierre        -> se pierde la rama entera
"""
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from powerTradeAi_djangoApp.strategies.base import ScanContext
from powerTradeAi_djangoApp.strategies.e01e02 import (
    E01E02AperturaBase, _bollinger, _escala, _linea_max_contactos)

NY = ZoneInfo("America/New_York")
DIA = date(2026, 7, 6)


def _min1(inicio_ny, n, precio, paso=0.0):
    idx = pd.date_range(pd.Timestamp(inicio_ny, tz=NY).tz_convert("UTC"),
                        periods=n, freq="1min")
    c = np.array([precio + paso * i for i in range(n)], dtype=float)
    return pd.DataFrame({"open": c, "high": c + 0.05, "low": c - 0.05,
                         "close": c, "volume": 100}, index=idx)


def _hist15(n=52, desde=104.0, hasta=100.0):
    """Tramo bajista SUAVE que termina AYER, SOLO en horario regular.

    Importante: 26 velas de 15m por sesion (09:30-16:00) repartidas en varios
    dias. Generarlas seguidas desde las 09:30 las metia en after-hours, que la
    regla descarta — igual que hace con los datos reales.
    """
    idx = []
    dia = datetime(2026, 7, 1)
    while len(idx) < n:
        base = pd.Timestamp(datetime(dia.year, dia.month, dia.day, 9, 30), tz=NY)
        for k in range(26):                       # 09:30..15:45
            if len(idx) < n:
                idx.append((base + pd.Timedelta(minutes=15 * k)).tz_convert("UTC"))
        dia += timedelta(days=1)
    idx = pd.DatetimeIndex(sorted(idx))
    c = np.linspace(desde, hasta, len(idx))
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


def test_dispara_cuando_se_cumple_no_a_una_hora_fija():
    """La academia NO fija hora: la señal se toma cuando se cumple la condicion.
    El 09:31 de los ejemplos es una observacion, no un requisito."""
    hoy = _min1(datetime(2026, 7, 6, 9, 30), 15, 103.0, paso=0.05)
    for minuto in (31, 33, 36, 40, 44):
        s = _regla("CALL").evaluate(_ctx(hoy, _hist15(), minuto=minuto))
        assert s is not None, f"deberia poder disparar a las 09:{minuto}"


def test_fuera_de_la_ventana_de_apertura_no_evalua():
    """Pasada la formacion de la primera vela de 15m, esta rama se cierra:
    a partir de ahi corresponde la rama intradia, que es otra cosa."""
    hoy = _min1(datetime(2026, 7, 6, 9, 30), 40, 103.0, paso=0.05)
    assert _regla("CALL").evaluate(_ctx(hoy, _hist15(), minuto=50)) is None


def test_el_gap_se_mide_siempre_contra_la_apertura():
    """Dispare a las 09:31 o a las 09:44, el gap es el mismo hecho: la
    discontinuidad entre el cierre regular anterior y la apertura."""
    hoy = _min1(datetime(2026, 7, 6, 9, 30), 15, 103.0, paso=0.05)
    a = _regla("CALL").evaluate(_ctx(hoy, _hist15(), minuto=31))
    b = _regla("CALL").evaluate(_ctx(hoy, _hist15(), minuto=44))
    assert a.meta["gap_pct"] == b.meta["gap_pct"]


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
    h = _hist15()
    ln = _linea_max_contactos(h, "CALL", 0.42 * _escala(h))
    assert ln is not None and ln["contactos"] >= 2


def test_la_escala_es_la_volatilidad_propia_del_simbolo():
    """Un simbolo mas volatil debe producir una escala mayor: es lo que hace
    que la misma tolerancia signifique lo mismo en TSLA que en AAPL."""
    tranquilo = _hist15()
    idx = tranquilo.index
    c = tranquilo["close"].to_numpy(float)
    volatil = pd.DataFrame({"open": c, "high": c * 1.01, "low": c * 0.99,
                            "close": c, "volume": 1000}, index=idx)
    assert _escala(volatil) > _escala(tranquilo) * 3


def test_la_señal_reporta_la_escala_usada():
    """Trazabilidad: hay que poder auditar con que umbral se disparo."""
    hoy = _min1(datetime(2026, 7, 6, 9, 30), 15, 103.0, paso=0.05)
    s = _regla("CALL").evaluate(_ctx(hoy, _hist15()))
    assert s.meta["escala_rango15m_pct"] > 0
    assert s.meta["umbral_lateral_bps"] > 0


def test_bollinger_se_abre_al_sumar_la_barra_en_formacion():
    base = np.full(20, 100.0)
    sin_ = _bollinger(base); con_ = _bollinger(np.append(base, 105.0))
    assert (con_[2] - con_[0]) > (sin_[2] - sin_[0])


def test_el_universo_esta_registrado_en_ambas_direcciones():
    from powerTradeAi_djangoApp.strategies.base import all_strategies
    from powerTradeAi_djangoApp.strategies.e01e02 import UNIVERSO
    ids = set(all_strategies())
    for sym in UNIVERSO:
        assert f"{sym}_E01_APERTURA" in ids
        assert f"{sym}_E02_APERTURA" in ids
    assert len(UNIVERSO) >= 25, "el universo debe ser amplio: el limite de tres"
    " instrumentos es de ancho de banda humano, no del software"


def test_descarta_contratos_con_spread_ancho():
    """Al ampliar el universo entran nombres ilíquidos donde el spread se come
    cualquier objetivo de 10-15% de prima."""
    class Q:
        is_live = True
        def __init__(self, bid, ask): self.bid, self.ask = bid, ask

    class ProvSpread(_Prov):
        def __init__(self, h15, bid, ask):
            super().__init__(h15); self.q = Q(bid, ask)
        def option_quote(self, occ, at=None):
            return self.q

    hoy = _min1(datetime(2026, 7, 6, 9, 30), 15, 103.0, paso=0.05)
    regla = _regla("CALL")
    s = regla.evaluate(_ctx(hoy, _hist15()))
    assert s is not None

    # spread del 2%: aceptable
    ctx_ok = ScanContext(provider=ProvSpread(_hist15(), 4.90, 5.00), symbol="TSLA",
                         session_date=DIA, bars=hoy,
                         now=datetime.combine(DIA, dtime(9, 31), tzinfo=NY))
    occ, _, _, _ = regla.select_contract(ctx_ok, s)
    assert occ is not None

    # spread del 20%: rechazado
    ctx_mal = ScanContext(provider=ProvSpread(_hist15(), 4.00, 5.00), symbol="TSLA",
                          session_date=DIA, bars=hoy,
                          now=datetime.combine(DIA, dtime(9, 31), tzinfo=NY))
    occ, _, _, _ = regla.select_contract(ctx_mal, s)
    assert occ is None, "un spread del 20% deberia descartarse"


def test_el_historial_se_comparte_entre_reglas_del_mismo_simbolo():
    """Sin cache compartida, cada regla repite la misma descarga.

    Con dos reglas por simbolo eso duplica las peticiones por pasada, y el coste
    crece linealmente al ampliar el universo: es lo que impedia pasar de 6
    simbolos a decenas.
    """
    llamadas = {"n": 0}

    class Contador(_Prov):
        def bars(self, symbol, start, end, tf):
            llamadas["n"] += 1
            return super().bars(symbol, start, end, tf)

    hoy = _min1(datetime(2026, 7, 6, 9, 30), 15, 103.0, paso=0.05)
    compartida: dict = {}
    prov = Contador(_hist15())
    for _ in range(2):                       # E01 y E02 del mismo simbolo
        ctx = ScanContext(
            provider=prov, symbol="TSLA", session_date=DIA,
            now=datetime.combine(DIA, dtime(9, 31), tzinfo=NY), bars=hoy,
            _history_cache=compartida.setdefault("TSLA", {}))
        ctx.history("15m", days=20)
    assert llamadas["n"] == 1, (
        f"el historial se pidio {llamadas['n']} veces; deberia ser 1")


def test_descarta_premarket_y_afterhours_del_historial():
    """``ctx.history`` devuelve barras CRUDAS: 32 velas de 15m por dia (08:00 a
    16:45), no las 26 de la sesion regular.

    Sin filtrar, el "cierre anterior" seria una vela de after-hours y el gap se
    mediria contra el precio equivocado. Es el fallo que invalidó un replay
    entero: el detector daba 11 señales de QQQ en 3 meses cuando la medicion
    correcta daba 2 al año.
    """
    from powerTradeAi_djangoApp.strategies.base import solo_rth as _solo_rth

    idx, dia = [], datetime(2026, 7, 1)
    for _ in range(3):                      # 08:00..16:45 = 36 barras/dia
        base = pd.Timestamp(datetime(dia.year, dia.month, dia.day, 8, 0), tz=NY)
        idx += [(base + pd.Timedelta(minutes=15 * k)).tz_convert("UTC")
                for k in range(36)]
        dia += timedelta(days=1)
    idx = pd.DatetimeIndex(sorted(idx))
    c = np.linspace(100.0, 101.0, len(idx))
    crudo = pd.DataFrame({"open": c, "high": c, "low": c, "close": c,
                          "volume": 1}, index=idx)

    limpio = _solo_rth(crudo)
    horas = {t.strftime("%H:%M") for t in limpio.tz_convert(NY).index.time}
    assert min(horas) == "09:30"
    assert max(horas) == "15:45"
    assert len(limpio) == 26 * 3, f"deberian quedar 26 velas por sesion, hay {len(limpio)/3:.0f}"
    assert len(limpio) < len(crudo)

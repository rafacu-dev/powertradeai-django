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
    E01E02AperturaBase, E01E02IntradiaBase, _bollinger, _escala,
    _linea_max_contactos,
)

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
                 "strategy_id": "T", "rule_version": "t"})(params={
                     "require_event_clear": False,
                     "require_terrain_model": False,
                 })


def _regla_intradia(direccion="CALL"):
    return type("RI", (E01E02IntradiaBase,),
                {"symbol": "TSLA", "direction": direccion,
                 "strategy_id": "TI", "rule_version": "ti"})(params={
                     "require_event_clear": False,
                     "require_terrain_model": False,
                 })


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


def test_resample_1h_se_ancla_a_la_apertura_de_0930():
    hoy = _min1(datetime(2026, 7, 6, 9, 30), 60, 100.0, paso=0.01)
    before_close = ScanContext(
        provider=_Prov(_hist15()), symbol="TSLA", session_date=DIA,
        now=datetime.combine(DIA, dtime(10, 0), tzinfo=NY), bars=hoy,
    )
    assert before_close.resample("1h").empty

    at_close = ScanContext(
        provider=_Prov(_hist15()), symbol="TSLA", session_date=DIA,
        now=datetime.combine(DIA, dtime(10, 30), tzinfo=NY), bars=hoy,
    )
    hourly = at_close.resample("1h")
    assert len(hourly) == 1
    assert hourly.index[0].tz_convert(NY).time() == dtime(9, 30)


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
    assert s.signal_ts.astimezone(NY).time() == dtime(9, 31)


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


def test_apertura_solo_evalua_el_primer_minuto_cerrado():
    """09:31 es la rama de gap; luego corresponde evaluar INTRADAY_BREAK."""
    hoy = _min1(datetime(2026, 7, 6, 9, 30), 15, 103.0, paso=0.05)
    assert _regla("CALL").evaluate(
        _ctx(hoy, _hist15(), minuto=31)) is not None
    for minuto in (32, 33, 36, 40, 44):
        assert _regla("CALL").evaluate(
            _ctx(hoy, _hist15(), minuto=minuto)) is None


def test_fuera_de_la_ventana_de_apertura_no_evalua():
    """Pasada la formacion de la primera vela de 15m, esta rama se cierra:
    a partir de ahi corresponde la rama intradia, que es otra cosa."""
    hoy = _min1(datetime(2026, 7, 6, 9, 30), 40, 103.0, paso=0.05)
    assert _regla("CALL").evaluate(_ctx(hoy, _hist15(), minuto=50)) is None


def test_el_gap_se_mide_contra_el_cierre_regular_anterior():
    hoy = _min1(datetime(2026, 7, 6, 9, 30), 15, 103.0, paso=0.05)
    signal = _regla("CALL").evaluate(_ctx(hoy, _hist15(), minuto=31))
    expected = (103.0 - 100.0) / 100.0 * 100
    assert signal.meta["gap_pct"] == pytest.approx(expected)


def test_intradia_confirma_con_cierre_de_15m(monkeypatch):
    """La rama frecuente usa una vela cerrada, no el gap de las 09:31."""
    monkeypatch.setattr(
        "powerTradeAi_djangoApp.strategies.e01e02._linea_max_contactos",
        lambda *args, **kwargs: {
            "contactos": 2, "nivel_ultimo": 101.0, "nivel_siguiente": 101.0,
        },
    )
    hoy = _min1(datetime(2026, 7, 6, 9, 30), 15, 100.0, paso=0.20)
    ctx = ScanContext(
        provider=_Prov(_hist15()), symbol="TSLA", session_date=DIA,
        now=datetime.combine(DIA, dtime(9, 45), tzinfo=NY), bars=hoy,
    )
    signal = _regla_intradia("CALL").evaluate(ctx)
    assert signal is not None
    assert signal.signal_ts.astimezone(NY).time() == dtime(9, 45)
    assert signal.meta["rama"] == "INTRADAY_BREAK"
    assert signal.meta["bar_state"] == "CLOSED_15M"


def test_intradia_no_acepta_solo_una_mecha(monkeypatch):
    monkeypatch.setattr(
        "powerTradeAi_djangoApp.strategies.e01e02._linea_max_contactos",
        lambda *args, **kwargs: {
            "contactos": 2, "nivel_ultimo": 101.0, "nivel_siguiente": 101.0,
        },
    )
    hoy = _min1(datetime(2026, 7, 6, 9, 30), 15, 100.0, paso=0.01)
    hoy.loc[hoy.index[-1], "high"] = 103.0
    ctx = ScanContext(
        provider=_Prov(_hist15()), symbol="TSLA", session_date=DIA,
        now=datetime.combine(DIA, dtime(9, 45), tzinfo=NY), bars=hoy,
    )
    assert _regla_intradia("CALL").evaluate(ctx) is None


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
        assert f"{sym}_E01_INTRADIA" in ids
        assert f"{sym}_E02_INTRADIA" in ids
    assert len(UNIVERSO) >= 25, "el catalogo de investigacion debe conservarse"


def test_descarta_contratos_con_spread_ancho():
    """Al ampliar el universo entran nombres ilíquidos donde el spread se come
    cualquier objetivo de 10-15% de prima."""
    class Q:
        is_live = True
        def __init__(self, bid, ask):
            self.bid, self.ask = bid, ask
            self.ts = datetime.combine(DIA, dtime(9, 31), tzinfo=NY)

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


def test_gestion_publicada_target_15_stop_20():
    """La fuente publica 10%-15% sobre la PRIMA y el Plan 10 aporta el -20%.

    Se usa 15 porque el escaner comprueba la prima cada minuto y el nivel se
    atraviesa: pidiendo 10 la mediana de salida real ya era +15.29%.
    """
    from powerTradeAi_djangoApp.strategies.base import all_strategies
    c = all_strategies()["TSLA_E01_APERTURA"]
    assert c.default_params["target_premium_pct"] == 15.0
    assert c.default_params["stop_premium_pct"] == 20.0


def test_el_target_y_el_stop_se_miden_sobre_la_prima_no_el_subyacente():
    """Error de unidades que ya invalidó un backtest: 2%-10% del SUBYACENTE no
    es lo mismo que de la PRIMA, y mezclarlos no representa el Plan 10."""
    import inspect
    from powerTradeAi_djangoApp.strategies import e01e02
    fuente = inspect.getsource(e01e02.E01E02AperturaBase.check_exit)
    assert "entry_premium" in fuente
    assert "bid" in fuente

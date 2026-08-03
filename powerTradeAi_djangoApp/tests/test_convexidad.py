"""Convexidad: que contrato se duplica con el menor movimiento.

Lo que se prueba aqui NO es que el numero sea bonito, sino tres cosas que ya
han fallado antes en este proyecto:

  1. El umbral se calcula ask->bid, no mid->mid. El sesgo de fills favorable
     invalido cuatro resultados; si alguien "simplifica" a mid, estos tests caen.
  2. El filtro de liquidez existe y muerde. Sin el, el ranking lo ganan
     contratos fantasma que parecen baratisimos porque nadie los cotiza.
  3. El aviso sobre el suelo teorico viaja con el dato. Publicar el 0.93% sin
     decir que el real medido ronda 1.4x seria enganoso.
"""
from datetime import date

import pytest

from powerTradeAi_djangoApp.agent import convexidad as cx


def _cadena(spot=100.0):
    """Cadena sintetica simetrica alrededor del dinero."""
    filas = []
    for k in range(90, 111, 2):
        dist = abs(k - spot) / spot
        prima = max(0.05, 3.0 * (1 - dist * 8))
        filas.append({"strike": float(k), "right": "CALL",
                      "bid": round(prima, 2), "ask": round(prima * 1.06, 2)})
        filas.append({"strike": float(k), "right": "PUT",
                      "bid": round(prima, 2), "ask": round(prima * 1.06, 2)})
    return filas


HOY, EXP = date(2026, 8, 3), date(2026, 8, 5)


# --- Black-Scholes y su inversion ----------------------------------------

def test_paridad_put_call():
    """Si la valoracion esta mal, todo lo demas es decoracion."""
    import math
    s, k, t, v = 100.0, 105.0, 0.05, 0.35
    c = cx.precio_bs(s, k, t, v, "CALL")
    p = cx.precio_bs(s, k, t, v, "PUT")
    assert abs((c - p) - (s - k * math.exp(-cx.R_LIBRE * t))) < 1e-6


def test_iv_recupera_la_volatilidad_usada():
    precio = cx.precio_bs(100.0, 105.0, 0.05, 0.42, "CALL")
    iv = cx.volatilidad_implicita(precio, 100.0, 105.0, 0.05, "CALL")
    assert abs(iv - 0.42) < 1e-3


def test_sin_valor_extrinseco_no_hay_iv():
    """Una opcion a su valor intrinseco no tiene volatilidad que invertir."""
    assert cx.volatilidad_implicita(10.0, 110.0, 100.0, 0.05, "CALL") is None


# --- el umbral ------------------------------------------------------------

def test_la_call_exige_subida_y_la_put_bajada():
    mc = cx.movimiento_para_doblar(100.0, 105.0, 0.05, 0.4, "CALL", 1.0, 0.05)
    mp = cx.movimiento_para_doblar(100.0, 95.0, 0.05, 0.4, "PUT", 1.0, 0.05)
    assert mc > 0 and mp < 0


def test_el_precio_objetivo_realmente_duplica():
    """Comprobacion directa: al mover el spot ese tanto, la prima dobla."""
    spot, k, t, v, ask, spread = 100.0, 105.0, 0.05, 0.4, 1.00, 0.06
    mov = cx.movimiento_para_doblar(spot, k, t, v, "CALL", ask, spread)
    valor = cx.precio_bs(spot + mov, k, t, v, "CALL")
    assert abs(valor - (2 * ask + spread / 2)) < 1e-3


def test_la_horquilla_encarece_el_umbral():
    """El nucleo del asunto: entrar al ask y salir al bid NO es gratis."""
    base = dict(spot=100.0, strike=105.0, t_anos=0.05, sigma=0.4, right="CALL")
    estrecha = cx.movimiento_para_doblar(**base, ask=1.00, spread=0.02)
    ancha = cx.movimiento_para_doblar(**base, ask=1.00, spread=0.30)
    assert ancha > estrecha, "una horquilla mas ancha debe exigir MAS movimiento"


def test_mid_a_mid_subestima_el_umbral():
    """Si alguien 'simplifica' a mid->mid, el umbral sale artificialmente bajo.
    Este test es el que impide que esa simplificacion pase inadvertida."""
    spot, k, t, v = 100.0, 105.0, 0.05, 0.4
    bid, ask = 0.94, 1.06
    mid, spread = (bid + ask) / 2, ask - bid
    real = cx.movimiento_para_doblar(spot, k, t, v, "CALL", ask, spread)
    ingenuo = cx.movimiento_para_doblar(spot, k, t, v, "CALL", mid, 0.0)
    assert real > ingenuo


# --- tiempo ---------------------------------------------------------------

def test_el_tiempo_se_mide_en_dias_habiles():
    """Viernes -> lunes es UN dia de negociacion, no tres."""
    viernes, lunes = date(2026, 8, 7), date(2026, 8, 10)
    t = cx.anos_hasta(lunes, viernes, fraccion_sesion=0.0)
    assert abs(t - 1 / cx.DIAS_NEGOCIACION_ANO) < 1e-9


def test_la_fraccion_de_sesion_suma_tiempo():
    a = cx.anos_hasta(EXP, HOY, fraccion_sesion=0.0)
    b = cx.anos_hasta(EXP, HOY, fraccion_sesion=1.0)
    assert b > a


# --- evaluacion de la cadena ---------------------------------------------

def test_ordena_de_menor_a_mayor_movimiento():
    r = cx.evaluar(100.0, _cadena(), EXP, HOY)
    movs = [abs(x["movimiento_pct"]) for x in r]
    assert movs == sorted(movs)


def test_descarta_los_ilíquidos():
    filas = _cadena() + [
        {"strike": 130.0, "right": "CALL", "bid": 0.01, "ask": 0.02},   # bid bajo
        {"strike": 135.0, "right": "CALL", "bid": 0.10, "ask": 0.90},   # horquilla
    ]
    r = cx.evaluar(100.0, filas, EXP, HOY)
    strikes = {x["strike"] for x in r}
    assert 130.0 not in strikes and 135.0 not in strikes


def test_descarta_lo_que_no_tiene_valor_extrinseco():
    """Una call de strike 50 con el spot a 100 vale 50 de intrinseco. Si el mid
    no lo supera, no hay volatilidad que invertir ni convexidad que comprar:
    es un sustituto de la accion, no una opcion."""
    filas = [{"strike": 50.0, "right": "CALL", "bid": 49.90, "ask": 50.00}]
    assert cx.evaluar(100.0, filas, EXP, HOY) == []


def test_conserva_la_itm_que_si_tiene_extrinseco():
    """El corte es el valor extrinseco, no el estar dentro del dinero."""
    filas = [{"strike": 50.0, "right": "CALL", "bid": 50.60, "ask": 51.00}]
    assert len(cx.evaluar(100.0, filas, EXP, HOY)) == 1


def test_mejor_par_devuelve_una_de_cada_lado():
    par = cx.mejor_par(100.0, _cadena(), EXP, HOY)
    assert par["call"]["right"] == "CALL"
    assert par["put"]["right"] == "PUT"
    assert par["evaluados"] > 0


def test_cada_fila_lleva_el_strike_y_el_precio_objetivo():
    """Es lo que se pidio ver en pantalla: strike y a cuanto tiene que llegar."""
    r = cx.evaluar(100.0, _cadena(), EXP, HOY)[0]
    for campo in ("strike", "ask", "coste_contrato", "movimiento_pct",
                  "movimiento_dolares", "spot_objetivo"):
        assert campo in r
    assert abs(r["spot_objetivo"] - (100.0 + r["movimiento_dolares"])) < 0.02


# --- el aviso de calibracion ---------------------------------------------

def test_el_ajuste_medido_viaja_con_el_dato():
    """El umbral teorico subestima: medido sobre 102 sesiones 0DTE de TSLA, el
    subyacente lo alcanzo el 67% de los dias y la opcion doblo el 31%. Si el
    ajuste desapareciera, la pantalla prometeria algo que no ocurre."""
    r = cx.evaluar(100.0, _cadena(), EXP, HOY)[0]
    assert abs(r["movimiento_pct_ajustado"]) > abs(r["movimiento_pct"])
    assert cx.FACTOR_REAL > 1.0


def test_el_ajuste_es_coherente_con_el_objetivo_ajustado():
    r = cx.evaluar(100.0, _cadena(), EXP, HOY)[0]
    esperado = 100.0 + r["movimiento_dolares"] * cx.FACTOR_REAL
    assert abs(r["spot_objetivo_ajustado"] - esperado) < 0.02


def test_la_elasticidad_acompana_pero_no_ordena():
    """omega tiene formula cerrada y sirve de referencia, pero ignora gamma y
    sobreestima ~27%: no puede ser quien ordene."""
    r = cx.evaluar(100.0, _cadena(), EXP, HOY)
    assert all(x["elasticidad"] is None or x["elasticidad"] > 0 for x in r)


# --- strikes a explorar ---------------------------------------------------

def test_la_banda_de_strikes_cubre_el_ganador_tipico():
    """Los ganadores reales estan al 5-6% del dinero; una banda mas estrecha
    los dejaria fuera y el ranking seria falso."""
    ks = cx.strikes_a_explorar(300.0, 5.0)
    assert min(ks) <= 300.0 * 0.93 and max(ks) >= 300.0 * 1.07


def test_la_banda_esta_acotada():
    ks = cx.strikes_a_explorar(7500.0, 5.0)
    assert len(ks) <= cx.MAX_STRIKES_LADO * 2 + 1


# --- escaneo (con proveedor falso) ---------------------------------------

@pytest.mark.django_db
def test_escaneo_usa_el_proveedor_y_no_revienta_sin_cadena(monkeypatch):
    from powerTradeAi_djangoApp.agent import convexidad_scan as cs
    from powerTradeAi_djangoApp.engine.session import now_ny

    class SinCadena:
        def latest_price(self, symbol):
            return 100.0

        def option_quote(self, occ, at=None):
            return None

    assert cs.escanear_simbolo(SinCadena(), "TSLA", now_ny()) is None


@pytest.mark.django_db
def test_escaneo_devuelve_mejor_call_y_put(monkeypatch):
    from types import SimpleNamespace

    from powerTradeAi_djangoApp.agent import convexidad_scan as cs
    from powerTradeAi_djangoApp.data import parse_occ
    from powerTradeAi_djangoApp.engine.session import now_ny

    class Falso:
        def latest_price(self, symbol):
            return 100.0

        def option_quote(self, occ, at=None):
            _, _, direccion, strike = parse_occ(occ)
            dist = abs(strike - 100.0) / 100.0
            prima = max(0.06, 3.0 * (1 - dist * 8))
            return SimpleNamespace(bid=round(prima, 2), ask=round(prima * 1.06, 2))

    r = cs.escanear_simbolo(Falso(), "TSLA", now_ny())
    assert r["symbol"] == "TSLA"
    assert r["call"]["right"] == "CALL" and r["call"]["movimiento_pct"] > 0
    assert r["put"]["right"] == "PUT" and r["put"]["movimiento_pct"] < 0
    assert r["contratos_evaluados"] > 0


# --- golden: cadena REAL ---------------------------------------------------

# TSLA, miercoles 29-jul-2026, 09:31:00 ET, spot 306.20 (VWAP del minuto).
# Bid/ask NBBO reales de ThetaData. Este caso se calculo aparte, con scipy y un
# script independiente, y dio CALL 325 (+0.95%) y PUT 290 (-0.91%). Si el modulo
# de la app deja de reproducirlo, algo se rompio en la valoracion.
GOLDEN_SPOT = 306.20
GOLDEN = [
    (282.5, "C", 24.05, 25.05), (285, "C", 21.55, 22.55),
    (287.5, "C", 19.05, 20.05), (290, "C", 16.60, 17.55),
    (292.5, "C", 14.15, 15.10), (295, "C", 11.85, 12.70),
    (297.5, "C", 9.55, 10.10), (300, "C", 7.40, 7.90),
    (302.5, "C", 5.60, 5.90), (305, "C", 4.00, 4.15),
    (307.5, "C", 2.71, 2.79), (310, "C", 1.74, 1.81),
    (312.5, "C", 1.10, 1.15), (315, "C", 0.67, 0.71),
    (317.5, "C", 0.41, 0.43), (320, "C", 0.24, 0.27),
    (322.5, "C", 0.15, 0.17), (325, "C", 0.10, 0.11),
    (327.5, "C", 0.07, 0.08), (330, "C", 0.05, 0.06),
    (287.5, "P", 0.06, 0.07), (290, "P", 0.10, 0.11),
    (292.5, "P", 0.14, 0.16), (295, "P", 0.24, 0.26),
    (297.5, "P", 0.43, 0.46), (300, "P", 0.76, 0.79),
    (302.5, "P", 1.30, 1.36), (305, "P", 2.11, 2.21),
    (307.5, "P", 3.25, 3.40), (310, "P", 4.75, 4.95),
    (312.5, "P", 6.55, 6.90),
]


def _golden_par():
    filas = [{"strike": k, "right": r, "bid": b, "ask": a}
             for k, r, b, a in GOLDEN]
    return cx.mejor_par(GOLDEN_SPOT, filas, date(2026, 7, 29),
                        date(2026, 7, 29), fraccion_sesion=389 / 390)


def test_golden_reproduce_los_ganadores_reales():
    par = _golden_par()
    assert par["call"]["strike"] == 325.0
    assert par["put"]["strike"] == 290.0


def test_golden_reproduce_los_umbrales_reales():
    par = _golden_par()
    assert abs(par["call"]["movimiento_pct"] - 0.95) < 0.03
    assert abs(par["put"]["movimiento_pct"] - (-0.91)) < 0.03


def test_golden_el_ganador_es_0dte_no_el_mas_barato_sin_mas():
    """La 330 es mas barata que la 325 y aun asi pierde: el ranking no es
    'el mas barato', es la relacion entre prima, distancia y volatilidad."""
    par = _golden_par()
    assert par["call"]["strike"] == 325.0
    assert par["call"]["ask"] > 0.05


def test_la_fraccion_de_sesion_no_pasa_de_uno():
    from datetime import datetime

    from powerTradeAi_djangoApp.agent import convexidad_scan as cs
    from powerTradeAi_djangoApp.engine.session import NY

    for h, m in ((4, 0), (9, 0), (9, 30), (12, 0), (16, 0), (20, 0)):
        f = cs._fraccion_sesion(datetime(2026, 8, 3, h, m, tzinfo=NY))
        assert 0.0 <= f <= 1.0

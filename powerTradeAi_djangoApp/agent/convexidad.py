"""Que contrato se duplica con el menor movimiento del subyacente.

Dado un strike, su cotizacion y su vencimiento, se resuelve el precio del
subyacente al que la opcion valdria el doble de lo pagado:

    encontrar S'  tal que   BS(S', K, T, sigma) = 2*ask + horquilla/2

Entrando al ASK y saliendo al BID. La version "mid a mid" que se ve al mirar
una cadena subestima el umbral entre un 5% y un 30% segun la horquilla, y ese
sesgo de fills ya invalido cuatro resultados de este proyecto.

La elasticidad (omega = |delta|*S/P) da el mismo orden en el 90% de los casos y
tiene formula cerrada, pero ignora gamma y sobreestima el umbral un 27%. Se
publica como referencia, no como respuesta.

═══════════════════════════════════════════════════════════════════════
AVISO SOBRE EL NUMERO QUE ESTE MODULO DEVUELVE

``movimiento_pct`` es un SUELO TEORICO, no una prediccion. Asume que el
movimiento ocurre de forma instantanea y con la volatilidad implicita
congelada. Ninguna de las dos cosas se cumple operando.

Medicion propia sobre TSLA, 102 sesiones 0DTE con bid/ask reales de ThetaData
(01-ago-2025 a 31-jul-2026), comprando a las 09:31 y siguiendo hasta las 09:46:

  - la IV de los contratos elegidos cae un 5.1% de media en esos 15 minutos
    (73% de los dias), y en un 0DTE fuera del dinero eso vale ~26% de la prima;
  - el subyacente alcanzo el umbral teorico el 67% de los dias, pero una pata
    solo doblo el 31%: de los dias que SI se movieron lo suficiente, doblo el 46%;
  - el umbral real dentro de la vela ronda 1.4-1.5x el teorico.

De ahi ``FACTOR_REAL``. Es calibracion EXTERNA medida sobre TSLA en la apertura;
no se ha validado en otros simbolos ni en otros tramos de la sesion, y por eso
viaja como campo aparte y etiquetado, nunca sustituyendo al dato crudo.
═══════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import math
from datetime import date

R_LIBRE = 0.04
FACTOR_REAL = 1.45
BANDA_STRIKES = 0.12       # se exploran strikes a +-12% del spot
MAX_STRIKES_LADO = 14
SPREAD_MAXIMO_PCT = 25.0
BID_MINIMO = 0.05
DIAS_NEGOCIACION_ANO = 252


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def precio_bs(spot, strike, t_anos, sigma, right) -> float:
    """Black-Scholes europeo. Las opciones sobre acciones son americanas, pero
    sin dividendo por medio el ejercicio anticipado no aporta valor a una call
    y en una put OTM a pocos dias el sesgo es despreciable."""
    es_call = right.upper().startswith("C")
    if t_anos <= 1e-9 or sigma <= 1e-9:
        return max(0.0, spot - strike) if es_call else max(0.0, strike - spot)
    d1 = ((math.log(spot / strike) + (R_LIBRE + sigma * sigma / 2) * t_anos)
          / (sigma * math.sqrt(t_anos)))
    d2 = d1 - sigma * math.sqrt(t_anos)
    desc = strike * math.exp(-R_LIBRE * t_anos)
    if es_call:
        return spot * _norm_cdf(d1) - desc * _norm_cdf(d2)
    return desc * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def _biseccion(f, lo, hi, tol=1e-6, max_iter=200):
    f_lo, f_hi = f(lo), f(hi)
    if f_lo * f_hi > 0:
        return None
    for _ in range(max_iter):
        mid = (lo + hi) / 2
        f_mid = f(mid)
        if abs(f_mid) < tol or (hi - lo) / 2 < tol:
            return mid
        if f_lo * f_mid <= 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2


def volatilidad_implicita(precio, spot, strike, t_anos, right) -> float | None:
    """Sin scipy a proposito: el despliegue no lo tiene garantizado y una
    biseccion sobre una funcion monotona no necesita mas."""
    intrinseco = (max(0.0, spot - strike) if right.upper().startswith("C")
                  else max(0.0, strike - spot))
    if precio <= intrinseco + 1e-6 or t_anos <= 0:
        return None
    return _biseccion(
        lambda v: precio_bs(spot, strike, t_anos, v, right) - precio, 1e-4, 8.0)


def movimiento_para_doblar(spot, strike, t_anos, sigma, right, ask, spread):
    """Cuanto debe moverse el subyacente para que la posicion valga el doble.

    Devuelve el movimiento CON SIGNO (negativo = el activo debe caer), o None
    si ni siquiera un movimiento extremo lo consigue.
    """
    objetivo = 2 * ask + spread / 2
    es_call = right.upper().startswith("C")
    f = lambda s: precio_bs(s, strike, t_anos, sigma, right) - objetivo
    lo, hi = (spot, spot * 3.0) if es_call else (spot * 0.05, spot)
    s_final = _biseccion(f, lo, hi, tol=1e-5)
    if s_final is None:
        return None
    return s_final - spot


def elasticidad(spot, strike, t_anos, sigma, right, precio) -> float | None:
    """omega = |delta| * S / P. Referencia con formula cerrada."""
    if t_anos <= 1e-9 or sigma <= 1e-9 or precio <= 0:
        return None
    d1 = ((math.log(spot / strike) + (R_LIBRE + sigma * sigma / 2) * t_anos)
          / (sigma * math.sqrt(t_anos)))
    delta = _norm_cdf(d1) if right.upper().startswith("C") else _norm_cdf(d1) - 1
    return abs(delta) * spot / precio


def anos_hasta(expiracion: date, hoy: date, fraccion_sesion: float = 1.0) -> float:
    """Tiempo en anos de NEGOCIACION (252), no calendario.

    ``fraccion_sesion`` es la parte de la sesion de hoy que queda por delante.
    """
    habiles = 0
    d = hoy
    while d < expiracion:
        d = date.fromordinal(d.toordinal() + 1)
        if d.weekday() < 5:
            habiles += 1
    return (habiles + max(0.0, min(1.0, fraccion_sesion))) / DIAS_NEGOCIACION_ANO


def evaluar(spot, filas, expiracion, hoy, fraccion_sesion=1.0):
    """Ordena una cadena por el movimiento necesario para doblar.

    ``filas``: iterable de dicts con ``strike``, ``right``, ``bid``, ``ask``.
    Devuelve la lista ordenada de menor a mayor movimiento (en valor absoluto),
    ya filtrada por liquidez.
    """
    t = anos_hasta(expiracion, hoy, fraccion_sesion)
    fuera = []
    for f in filas:
        bid, ask = f.get("bid"), f.get("ask")
        if not bid or not ask or ask <= bid or bid < BID_MINIMO:
            continue
        strike, right = float(f["strike"]), f["right"]
        mid, spread = (bid + ask) / 2, ask - bid
        if mid <= 0 or spread / mid * 100 > SPREAD_MAXIMO_PCT:
            continue
        intrinseco = (max(0.0, spot - strike) if right.upper().startswith("C")
                      else max(0.0, strike - spot))
        if mid <= intrinseco + 1e-6:      # sin valor extrinseco no hay convexidad
            continue
        sigma = volatilidad_implicita(mid, spot, strike, t, right)
        if sigma is None or sigma > 5.0:
            continue
        mov = movimiento_para_doblar(spot, strike, t, sigma, right, ask, spread)
        if mov is None:
            continue
        fuera.append({
            "strike": strike,
            "right": "CALL" if right.upper().startswith("C") else "PUT",
            "bid": round(bid, 2),
            "ask": round(ask, 2),
            "coste_contrato": round(ask * 100, 2),
            "iv_pct": round(sigma * 100, 1),
            "spread_pct": round(spread / mid * 100, 1),
            "movimiento_dolares": round(mov, 2),
            "movimiento_pct": round(mov / spot * 100, 2),
            "spot_objetivo": round(spot + mov, 2),
            # calibracion externa, etiquetada: ver el aviso de la cabecera
            "movimiento_pct_ajustado": round(mov / spot * 100 * FACTOR_REAL, 2),
            "spot_objetivo_ajustado": round(spot + mov * FACTOR_REAL, 2),
            "elasticidad": (round(e, 1)
                            if (e := elasticidad(spot, strike, t, sigma,
                                                 right, mid)) else None),
        })
    fuera.sort(key=lambda r: abs(r["movimiento_pct"]))
    return fuera


def mejor_par(spot, filas, expiracion, hoy, fraccion_sesion=1.0):
    """La mejor CALL y la mejor PUT, cada una por su propio umbral."""
    todo = evaluar(spot, filas, expiracion, hoy, fraccion_sesion)
    call = next((r for r in todo if r["right"] == "CALL"), None)
    put = next((r for r in todo if r["right"] == "PUT"), None)
    return {"call": call, "put": put, "evaluados": len(todo)}


def strikes_a_explorar(spot: float, paso: float) -> list[float]:
    atm = round(spot / paso) * paso
    n = min(int(spot * BANDA_STRIKES / paso), MAX_STRIKES_LADO)
    return [round(atm + i * paso, 2) for i in range(-n, n + 1)]

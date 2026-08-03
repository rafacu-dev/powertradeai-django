"""Escaneo de convexidad sobre la watchlist: baja cadenas y ordena.

Separado de ``convexidad`` a proposito: ahi vive el calculo, que es puro y se
prueba sin red; aqui vive el acceso al proveedor, que es lento y falla.

El resultado se cachea y se refresca en un HILO. Un escaneo son ~30 llamadas de
cotizacion por simbolo, y el servicio web de Render corre con dos hilos de
gunicorn: hacerlo dentro del request es exactamente el fallo que dejo la
corrida #1944 colgada.
"""
from __future__ import annotations

import logging
import threading

from django.core.cache import cache

from . import convexidad

log = logging.getLogger(__name__)

CLAVE = "powertradeai:convexidad"
CLAVE_REFRESCANDO = "powertradeai:convexidad:refrescando"
TTL = 60 * 30
UNIVERSO = ("SPX", "SPY", "QQQ", "IWM", "DIA", "TSLA", "NVDA", "AAPL",
            "MSFT", "AMZN", "META", "GOOGL", "AMD")

# Indices: no cotizan como accion (``latest_price`` pega al endpoint de stocks
# y falla), y su root de opciones no coincide con el del indice.
#
#   root  : SPX son las mensuales AM-settled; los vencimientos cercanos que
#           escaneamos son SPXW. Usar 'SPX' devuelve cadena vacia.
#   proxy : solo SIEMBRA la primera rejilla. El spot bueno sale de la paridad
#           put-call sobre la propia cadena, no de este cociente: SPY no es
#           SPX/10 (acumula dividendos) y el cociente deriva con los anos.
INDICES = {
    "SPX": {"root": "SPXW", "proxy": "SPY", "ratio": 10.03, "paso": 5.0},
}
# Margen de la rejilla de siembra, en tanto por uno sobre la semilla. Ancho a
# proposito: el ratio SPX/SPY deriva con los anos (SPY arrastra dividendos) y
# la paridad solo corrige si el spot verdadero cae DENTRO de la rejilla. Si
# queda fuera, el indice desaparece de la tabla sin error visible.
MARGEN_SIEMBRA = 0.20


def _paso_strike(spot: float) -> float:
    return 5.0 if spot >= 200 else 2.5 if spot >= 50 else 1.0


def _fraccion_sesion(ahora) -> float:
    """Parte de la sesion que queda por delante. Fuera de RTH devuelve 1.0:
    con el mercado cerrado el contrato conserva la sesion siguiente entera."""
    from ..engine.session import NY

    local = ahora.astimezone(NY)
    minutos = local.hour * 60 + local.minute
    apertura, cierre = 9 * 60 + 30, 16 * 60
    if minutos <= apertura or minutos >= cierre:
        return 1.0
    return (cierre - minutos) / (cierre - apertura)


def _cotizar(provider, root, exp, strikes) -> list[dict]:
    from ..data import occ_symbol

    filas = []
    for k in strikes:
        for right in ("CALL", "PUT"):
            try:
                q = provider.option_quote(occ_symbol(root, exp, right, k))
            except Exception:
                continue
            if q is None:
                continue
            bid, ask = getattr(q, "bid", None), getattr(q, "ask", None)
            if bid and ask:
                filas.append({"strike": k, "right": right,
                              "bid": float(bid), "ask": float(ask)})
    return filas


def _semilla_indice(provider, cfg) -> float | None:
    """Precio aproximado para sembrar la primera rejilla. Solo eso: el spot
    bueno lo da la paridad."""
    try:
        return float(provider.latest_price(cfg["proxy"])) * cfg["ratio"]
    except Exception:
        return None


def escanear_simbolo(provider, symbol: str, ahora, dte: int = 0) -> dict | None:
    """Mejor CALL y mejor PUT del simbolo, o None si no hay cadena utilizable."""
    from ..data import candidate_expirations

    sym = symbol.upper()
    cfg = INDICES.get(sym)
    hoy = ahora.date()
    fraccion = _fraccion_sesion(ahora)
    root = cfg["root"] if cfg else sym

    if cfg is None:
        try:
            spot = float(provider.latest_price(sym))
        except Exception as exc:
            log.warning("convexidad: sin precio de %s (%s)", sym, exc)
            return None
        if not spot or spot <= 0:
            return None
    else:
        spot = _semilla_indice(provider, cfg)
        if not spot:
            log.warning("convexidad: sin semilla para el indice %s", sym)
            return None

    for exp in candidate_expirations(hoy, max_dte=max(dte, 2)):
        paso = cfg["paso"] if cfg else _paso_strike(spot)

        if cfg is not None:
            # Pasada 1: rejilla ancha y basta solo para fijar el spot por
            # paridad. La paridad vale en CUALQUIER strike con las dos patas
            # cotizadas, asi que no hace falta afinar aqui.
            n_pasos = 12
            grueso = max(paso, round(spot * MARGEN_SIEMBRA / n_pasos / paso) * paso)
            base = round(spot / grueso) * grueso
            rejilla = [base + i * grueso for i in range(-n_pasos, n_pasos + 1)]
            real = convexidad.spot_por_paridad(
                _cotizar(provider, root, exp, rejilla), exp, hoy, fraccion)
            if real is None:
                continue          # ese vencimiento no existe para el indice
            spot = real

        filas = _cotizar(provider, root, exp,
                         convexidad.strikes_a_explorar(spot, paso))
        if not filas:
            continue              # ese vencimiento no existe para este simbolo
        par = convexidad.mejor_par(spot, filas, exp, hoy, fraccion)
        if par["call"] or par["put"]:
            return {"symbol": sym, "spot": round(spot, 2),
                    "expiration": str(exp), "dte": (exp - hoy).days,
                    "contratos_evaluados": par["evaluados"],
                    "spot_por_paridad": cfg is not None,
                    "call": par["call"], "put": par["put"]}
    return None


def escanear(provider, symbols=None, ahora=None) -> dict:
    from ..engine.session import now_ny

    ahora = ahora or now_ny()
    filas = []
    for sym in (symbols or UNIVERSO):
        try:
            r = escanear_simbolo(provider, sym, ahora)
        except Exception:
            log.exception("convexidad: fallo escaneando %s", sym)
            r = None
        if r:
            filas.append(r)
    return {"generado": ahora.isoformat(), "filas": filas,
            "factor_real": convexidad.FACTOR_REAL}


def cacheado() -> dict | None:
    return cache.get(CLAVE)


def refrescar_en_fondo(symbols=None) -> bool:
    """Lanza un refresco si no hay otro en curso. True si lo lanzo."""
    if cache.get(CLAVE_REFRESCANDO):
        return False
    cache.set(CLAVE_REFRESCANDO, True, 300)

    def _worker():
        from django.db import close_old_connections

        from ..data import get_provider
        try:
            cache.set(CLAVE, escanear(get_provider(), symbols), TTL)
        except Exception:
            log.exception("convexidad: fallo el refresco en fondo")
        finally:
            cache.delete(CLAVE_REFRESCANDO)
            close_old_connections()

    threading.Thread(target=_worker, daemon=True).start()
    return True

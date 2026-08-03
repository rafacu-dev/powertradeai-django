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
UNIVERSO = ("SPY", "QQQ", "IWM", "DIA", "TSLA", "NVDA", "AAPL",
            "MSFT", "AMZN", "META", "GOOGL", "AMD")


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


def escanear_simbolo(provider, symbol: str, ahora, dte: int = 0) -> dict | None:
    """Mejor CALL y mejor PUT del simbolo, o None si no hay cadena utilizable."""
    from ..data import candidate_expirations, occ_symbol

    sym = symbol.upper()
    try:
        spot = float(provider.latest_price(sym))
    except Exception as exc:
        log.warning("convexidad: sin precio de %s (%s)", sym, exc)
        return None
    if not spot or spot <= 0:
        return None

    hoy = ahora.date()
    paso = _paso_strike(spot)
    strikes = convexidad.strikes_a_explorar(spot, paso)

    for exp in candidate_expirations(hoy, max_dte=max(dte, 2)):
        filas = []
        for k in strikes:
            for right in ("CALL", "PUT"):
                try:
                    q = provider.option_quote(occ_symbol(sym, exp, right, k))
                except Exception:
                    continue
                if q is None:
                    continue
                bid, ask = getattr(q, "bid", None), getattr(q, "ask", None)
                if bid and ask:
                    filas.append({"strike": k, "right": right,
                                  "bid": float(bid), "ask": float(ask)})
        if not filas:
            continue        # ese vencimiento no existe para este simbolo
        par = convexidad.mejor_par(spot, filas, exp, hoy, _fraccion_sesion(ahora))
        if par["call"] or par["put"]:
            return {"symbol": sym, "spot": round(spot, 2),
                    "expiration": str(exp), "dte": (exp - hoy).days,
                    "contratos_evaluados": par["evaluados"],
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

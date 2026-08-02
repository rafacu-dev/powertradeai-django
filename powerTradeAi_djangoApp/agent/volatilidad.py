"""Estado de volatilidad Bollinger: la secuencia de calibracion del manual.

El manual (seccion 7, "Secuencia transversal de calibracion") define cinco
estados y los aplica a E01-E08 y E11/E12:

    CERRADA -> ABRIENDO_LEVE -> CONFIRMADA -> EXPUESTO -> REGRESO_A_BANDA

Y dice explicitamente: "No existen umbrales academicos para leve, alta, extrema,
expuesto ni sobreexpuesto." Tampoco hay un BBWidth publicado.

Por eso este modulo:
  1. Devuelve SIEMPRE los numeros crudos (ancho, expansion, posicion del precio)
     para que el agente pueda discrepar de la clasificacion.
  2. Calcula los cortes contra la PROPIA historia del simbolo (percentiles), no
     con un porcentaje fijo. Un 15% de expansion no significa lo mismo en TSLA
     que en AAPL: sus rangos de vela difieren en 1.6x.
  3. Etiqueta la clasificacion como CALIBRACION EXTERNA en la respuesta.

Un detector previo del proyecto exigia "BBWidth > su media de 20" — un umbral
que no existe en el material — y esa sola invencion fue una de las causas de que
solo encontrara 6 señales donde la academia describe una configuracion frecuente.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

BB_PERIODO, BB_K = 20, 2.0

# --- CALIBRACION EXTERNA: el material no publica estos cortes ---
PCT_LEVE = 33.0        # percentil de expansion por debajo del cual es "cerrada"
PCT_CONFIRMA = 66.0    # percentil a partir del cual la apertura es "confirmada"
VENTANA_PERCENTIL = 120   # barras de referencia para los percentiles
TOCANDO_FRAC = 0.15    # a menos de este % del ancho de la banda = "tocando"


def _bandas(cierres: np.ndarray):
    if len(cierres) < BB_PERIODO:
        return None
    v = cierres[-BB_PERIODO:]
    mid = float(v.mean()); sd = float(v.std(ddof=0))
    return mid - BB_K * sd, mid, mid + BB_K * sd


def _serie_ancho(cierres: np.ndarray) -> np.ndarray:
    """Ancho de banda normalizado por el punto medio, barra a barra."""
    out = []
    for i in range(BB_PERIODO, len(cierres) + 1):
        b = _bandas(cierres[:i])
        out.append((b[2] - b[0]) / b[1] if b and b[1] else np.nan)
    return np.array(out, dtype=float)


def _posicion(precio: float, lo: float, mid: float, up: float) -> str:
    ancho = up - lo
    if ancho <= 0:
        return "indefinida"
    if precio > up:
        return "fuera_arriba"
    if precio < lo:
        return "fuera_abajo"
    if (up - precio) / ancho <= TOCANDO_FRAC:
        return "tocando_superior"
    if (precio - lo) / ancho <= TOCANDO_FRAC:
        return "tocando_inferior"
    return "dentro"


def evaluar(cierres: np.ndarray, precio_actual: float | None = None) -> dict:
    """Estado de volatilidad a partir de una serie de cierres YA CERRADOS.

    ``precio_actual`` permite pasar el precio observado ahora (por ejemplo el de
    una vela en formacion) sin meterlo en el calculo de las bandas.
    """
    c = np.asarray(cierres, dtype=float)
    c = c[np.isfinite(c)]
    if len(c) < BB_PERIODO + 2:
        return {"estado": "DATOS_INSUFICIENTES",
                "motivo": f"hacen falta {BB_PERIODO + 2} cierres, hay {len(c)}"}

    b_now = _bandas(c)
    b_prev = _bandas(c[:-1])
    b_ant = _bandas(c[:-2])
    lo, mid, up = b_now
    ancho = (up - lo) / mid
    ancho_prev = (b_prev[2] - b_prev[0]) / b_prev[1]
    expansion = ((ancho / ancho_prev - 1) * 100
                 if ancho_prev and np.isfinite(ancho_prev) else 0.0)

    # percentil de la expansion contra la propia historia del simbolo
    serie = _serie_ancho(c[-(VENTANA_PERCENTIL + BB_PERIODO):])
    # Una serie plana da ancho 0: dividir ahi produce inf/nan. Se descartan esos
    # tramos en vez de propagarlos, que es como un percentil se vuelve basura.
    prev = serie[:-1]
    validos = np.isfinite(prev) & (prev > 0)
    cambios = np.full(len(prev), np.nan)
    cambios[validos] = np.diff(serie)[validos] / prev[validos] * 100
    cambios = cambios[np.isfinite(cambios)]
    pctl = (float((cambios < expansion).mean()) * 100
            if len(cambios) >= 20 else None)

    # direccion y giro del punto medio
    dm_now = mid - b_prev[1]
    dm_prev = b_prev[1] - b_ant[1]
    if abs(dm_now) < 1e-12:
        direccion = "plano"
    else:
        direccion = "ascendente" if dm_now > 0 else "descendente"
    giro = (dm_now > 0) != (dm_prev > 0) and abs(dm_prev) > 1e-12

    precio = float(precio_actual) if precio_actual is not None else float(c[-1])
    pos = _posicion(precio, lo, mid, up)
    pos_prev = _posicion(float(c[-2]), b_prev[0], b_prev[1], b_prev[2])

    # --- clasificacion (CALIBRACION EXTERNA) ---
    if pos in ("fuera_arriba", "fuera_abajo"):
        estado = "EXPUESTO"
        nota = "el precio ya esta fuera de banda: el manual dice no perseguir"
    elif pos_prev in ("fuera_arriba", "fuera_abajo") and pos == "dentro":
        estado = "REGRESO_A_BANDA"
        nota = "exige nuevo toque y revalidacion antes de operar"
    elif pctl is not None and pctl < PCT_LEVE:
        estado = "CERRADA"
        nota = "sin apertura: no entrar por una vela aislada"
    elif pctl is not None and pctl >= PCT_CONFIRMA and expansion > 0:
        estado = "CONFIRMADA"
        nota = "hay apertura; comprobar contexto, estrategia, terreno y contrato"
    else:
        estado = "ABRIENDO_LEVE"
        nota = "esperar: todavia puede cerrarse"

    return {
        "estado": estado,
        "nota": nota,
        # --- crudo: para que el agente pueda discrepar de la clasificacion ---
        "ancho_pct": round(ancho * 100, 4),
        "ancho_previo_pct": round(ancho_prev * 100, 4),
        "expansion_pct": round(expansion, 3),
        "expansion_percentil": round(pctl, 1) if pctl is not None else None,
        "punto_medio": round(mid, 4),
        "punto_medio_direccion": direccion,
        "punto_medio_giro": bool(giro),
        "banda_superior": round(up, 4),
        "banda_inferior": round(lo, 4),
        "precio": round(precio, 4),
        "posicion_precio": pos,
        "posicion_previa": pos_prev,
        "calibracion_externa": {
            "aviso": ("El manual NO publica umbrales para leve/alta/extrema. "
                      "Los cortes de abajo son calibracion de software: si "
                      "discrepas, usa expansion_percentil y decide tu."),
            "percentil_cerrada": PCT_LEVE,
            "percentil_confirmada": PCT_CONFIRMA,
            "ventana_percentil": VENTANA_PERCENTIL,
        },
    }

"""La pantalla de convexidad: que pinte lo que se pidio y lo que hay que avisar.

Se renderiza la plantilla directamente en vez de pasar por la vista: el Django
minimo de los tests no monta sesiones ni admin, y lo que hay que proteger es el
contenido, no el decorador de staff.
"""
import re

import pytest
from django.template.loader import render_to_string

from powerTradeAi_djangoApp.agent import convexidad as cx

CONTEXTO = {
    "datos": {
        "generado": "2026-08-02T18:00:00-04:00",
        "factor_real": cx.FACTOR_REAL,
        "filas": [{
            "symbol": "TSLA", "spot": 306.2, "expiration": "2026-07-29",
            "dte": 0, "contratos_evaluados": 45,
            "call": {"strike": 325.0, "ask": 0.11, "coste_contrato": 11.0,
                     "movimiento_pct": 0.95, "spot_objetivo": 309.11,
                     "movimiento_pct_ajustado": 1.38,
                     "spot_objetivo_ajustado": 310.43},
            "put": {"strike": 290.0, "ask": 0.11, "coste_contrato": 11.0,
                    "movimiento_pct": -0.91, "spot_objetivo": 303.41,
                    "movimiento_pct_ajustado": -1.32,
                    "spot_objetivo_ajustado": 302.15},
        }],
    },
    "refrescando": False,
    "universo": ("TSLA",),
}


def _html(ctx=None):
    return render_to_string("powertradeai/convexidad.html", ctx or CONTEXTO)


@pytest.mark.parametrize("esperado", [
    "TSLA", "325.0", "290.0",          # simbolo y los dos strikes
    "0.95", "-0.91",                   # movimiento de cada lado
    "309.11", "303.41",                # a cuanto tiene que llegar el activo
    "$11.0",                           # lo que cuesta el contrato
])
def test_pinta_lo_que_se_pidio(esperado):
    assert esperado in _html()


def test_muestra_el_ajuste_medido_junto_al_teorico():
    """Publicar solo el suelo teorico prometeria algo que no ocurre: medido
    sobre 102 sesiones 0DTE, el activo alcanzo el umbral el 67% de los dias y
    la opcion doblo el 31%."""
    h = _html()
    assert "1.38" in h and "310.43" in h
    assert "suelo teórico" in h
    assert "31" in h and "67" in h


def test_el_aviso_no_se_puede_borrar_sin_romper_el_test():
    h = _html()
    for frase in ("volatilidad implícita congelada", "ask", "bid"):
        assert frase in h


def test_sin_datos_no_revienta_y_avisa_de_que_calcula():
    h = _html({"datos": None, "refrescando": True, "universo": ("TSLA",)})
    assert "Calculando" in h


def test_una_fila_sin_cadena_no_rompe_la_tabla():
    ctx = {**CONTEXTO}
    ctx["datos"] = {**CONTEXTO["datos"], "filas": [
        {"symbol": "IWM", "spot": 240.0, "expiration": "2026-08-03", "dte": 1,
         "contratos_evaluados": 0, "call": None, "put": None}]}
    h = _html(ctx)
    assert "IWM" in h and "sin cadena utilizable" in h


def test_no_deja_etiquetas_de_plantilla_sin_resolver():
    h = _html()
    assert "{{" not in h and "{%" not in h


def test_la_tabla_cabe_en_movil():
    """Contenido ancho dentro de su propio contenedor con scroll: el body no
    debe desplazarse en horizontal."""
    h = _html()
    assert "overflow-x: auto" in h


def test_tiene_los_dos_temas():
    assert "prefers-color-scheme: dark" in _html()


@pytest.mark.parametrize("plantilla", ["dashboard.html", "agent.html"])
def test_se_llega_desde_el_resto_del_panel(plantilla):
    """Una pantalla sin enlace es una pantalla que nadie encuentra: se quedo
    fuera del primer commit y solo se llegaba escribiendo la URL a mano.

    Se lee el fuente en vez de renderizar porque dashboard.html usa
    ``{% url 'admin:logout' %}`` y el Django minimo de los tests no monta admin.
    """
    from pathlib import Path

    import powerTradeAi_djangoApp
    ruta = (Path(powerTradeAi_djangoApp.__file__).parent
            / "templates" / "powertradeai" / plantilla)
    assert "powertradeai:convexidad" in ruta.read_text(encoding="utf-8")

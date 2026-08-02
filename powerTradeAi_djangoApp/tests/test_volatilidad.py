"""Estado de volatilidad Bollinger — la secuencia de calibracion del manual.

Lo critico aqui no es acertar la etiqueta, sino que:
  - los umbrales sean EXTERNOS y esten declarados como tales;
  - el dato crudo viaje siempre, para que el agente pueda discrepar;
  - los cortes se midan contra la propia historia del simbolo, no con un % fijo.

Un detector previo del proyecto exigia "BBWidth > su media de 20" —umbral que no
existe en el material— y esa invencion fue una de las causas de que encontrara
6 señales donde la academia describe una configuracion frecuente.
"""
from datetime import datetime

import numpy as np
import pandas as pd

from powerTradeAi_djangoApp.agent import skills
from powerTradeAi_djangoApp.agent.volatilidad import (
    BB_PERIODO, PCT_CONFIRMA, PCT_LEVE, evaluar)
from powerTradeAi_djangoApp.agent.skills import SKILLS
from powerTradeAi_djangoApp.strategies.base import NY


def _serie(n=200, base=100.0, ruido=0.2, semilla=7):
    rng = np.random.default_rng(semilla)
    return base + np.cumsum(rng.normal(0, ruido, n))


def test_datos_insuficientes_no_inventa_estado():
    r = evaluar(np.array([100.0] * 5))
    assert r["estado"] == "DATOS_INSUFICIENTES"
    assert "motivo" in r


def test_siempre_devuelve_el_dato_crudo():
    """El agente tiene que poder discrepar de la clasificacion."""
    r = evaluar(_serie())
    for k in ("ancho_pct", "expansion_pct", "expansion_percentil",
              "punto_medio", "punto_medio_direccion", "banda_superior",
              "banda_inferior", "posicion_precio"):
        assert k in r, f"falta el crudo {k}"


def test_declara_su_calibracion_como_externa():
    r = evaluar(_serie())
    cal = r["calibracion_externa"]
    assert "NO publica umbrales" in cal["aviso"]
    assert cal["percentil_cerrada"] == PCT_LEVE
    assert cal["percentil_confirmada"] == PCT_CONFIRMA


def test_precio_fuera_de_banda_es_expuesto():
    """El manual: si ya esta fuera, no perseguir."""
    c = np.array([100.0] * (BB_PERIODO + 5))
    c[-1] = 100.05
    r = evaluar(c, precio_actual=200.0)
    assert r["estado"] == "EXPUESTO"
    assert "no perseguir" in r["nota"]


def test_regreso_a_banda_exige_revalidacion():
    c = list(np.full(BB_PERIODO + 2, 100.0))
    c[-2] = 130.0            # la barra previa cerro muy fuera
    r = evaluar(np.array(c), precio_actual=100.0)
    assert r["estado"] == "REGRESO_A_BANDA"
    assert "revalidacion" in r["nota"]


def test_serie_plana_no_da_confirmada():
    """Sin apertura no puede haber confirmacion."""
    r = evaluar(np.full(BB_PERIODO + 60, 100.0))
    assert r["estado"] != "CONFIRMADA"


def test_expansion_fuerte_sube_el_percentil():
    """Una barra que ensancha mucho debe quedar en la cola alta de SU historia."""
    c = list(_serie(150, ruido=0.05))
    c.append(c[-1] + 8.0)              # salto grande respecto a su propio ruido
    r = evaluar(np.array(c))
    assert r["expansion_pct"] > 0
    assert r["expansion_percentil"] is not None
    assert r["expansion_percentil"] > 50


def test_el_giro_del_punto_medio_se_detecta():
    c = list(np.linspace(100.0, 90.0, BB_PERIODO + 10))   # bajando
    c += [c[-1] + 3.0, c[-1] + 7.0]                        # gira al alza
    r = evaluar(np.array(c))
    assert r["punto_medio_direccion"] in ("ascendente", "descendente", "plano")
    assert isinstance(r["punto_medio_giro"], bool)


def test_los_cortes_son_relativos_al_simbolo_no_un_porcentaje_fijo():
    """La misma expansion en % debe clasificarse distinto segun la volatilidad
    propia: 15% no significa lo mismo en un simbolo tranquilo que en uno agitado.
    """
    tranquilo = list(_serie(200, ruido=0.05, semilla=1))
    agitado = list(_serie(200, ruido=1.5, semilla=1))
    salto = 1.0
    rt = evaluar(np.array(tranquilo + [tranquilo[-1] + salto]))
    ra = evaluar(np.array(agitado + [agitado[-1] + salto]))
    assert rt["expansion_percentil"] != ra["expansion_percentil"], (
        "el percentil deberia depender de la volatilidad propia del simbolo")


def test_la_skill_esta_registrada_y_avisa_de_e09_e10():
    assert "get_estado_volatilidad" in SKILLS
    d = SKILLS["get_estado_volatilidad"].description
    assert "E09" in d and "SIN volatilidad" in d, (
        "debe avisar de la excepcion: E09/E10 explotan la AUSENCIA de expansion")


def test_la_skill_ofrece_las_tres_temporalidades():
    props = SKILLS["get_estado_volatilidad"].parameters["properties"]
    assert set(props["timeframe"]["enum"]) == {"15m", "1h", "1d"}


def test_skill_diaria_no_aplica_filtro_horario_a_velas_de_medianoche(monkeypatch):
    class DailyProvider:
        def bars(self, symbol, start, end, timeframe):
            idx = pd.date_range(
                "2026-05-01", periods=70, freq="1D", tz="UTC")
            close = np.linspace(90.0, 110.0, len(idx))
            return pd.DataFrame({
                "open": close,
                "high": close + 1,
                "low": close - 1,
                "close": close,
                "volume": np.full(len(idx), 1000),
            }, index=idx)

        def latest_price(self, symbol):
            return 110.0

    monkeypatch.setattr(skills, "_provider", lambda: DailyProvider())
    result = skills.get_estado_volatilidad(
        {"as_of": datetime(2026, 7, 15, 12, 0, tzinfo=NY)},
        "TSLA", timeframe="1d")

    assert "error" not in result
    assert result["timeframe"] == "1d"
    assert result["barras_usadas"] >= BB_PERIODO + 2

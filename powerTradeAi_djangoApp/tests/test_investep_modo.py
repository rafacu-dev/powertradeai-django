"""Modo Investep: el agente solo puede operar las estrategias del manual.

Lo que se prueba aqui no es el prompt (un modelo puede ignorarlo) sino la PUERTA
DURA: ``create_alert`` rechaza cualquier alerta sin una estrategia documentada.
Sin ese rechazo, "opera solo las del manual" seria una sugerencia.
"""
import hashlib
from pathlib import Path

import pytest

from powerTradeAi_djangoApp.agent import investep
from powerTradeAi_djangoApp.agent.skills import SKILLS


# --- catalogo -------------------------------------------------------------

def test_las_diez_operables_y_las_dos_corregidas():
    assert set(investep.ESTRATEGIAS) == {f"E{i:02d}" for i in range(1, 11)}
    assert set(investep.NO_OPERABLES) == {"E11", "E12"}


def test_e11_y_e12_no_son_operables():
    """La auditoria las marco CORREGIDAS: el Worden no tiene direccion, formula
    ni umbral publicados, y su proxy quedo desactivado."""
    for c in ("E11", "E12"):
        ok, motivo = investep.es_operable(c)
        assert ok is False
        assert "NO es operable" in motivo


@pytest.mark.parametrize("codigo", ["E01", "e01", " E05 ", "E10"])
def test_acepta_codigos_validos_con_ruido(codigo):
    ok, nombre = investep.es_operable(codigo)
    assert ok is True and nombre


@pytest.mark.parametrize("codigo", ["", None, "E99", "scalping", "mi corazonada"])
def test_rechaza_lo_que_no_esta_en_el_manual(codigo):
    ok, motivo = investep.es_operable(codigo)
    assert ok is False
    assert motivo


# --- manual ---------------------------------------------------------------

def test_el_manual_viaja_dentro_del_paquete():
    """Debe desplegarse a Render: el corpus vive fuera del repo."""
    assert investep.RUTA.exists()
    assert len(investep.texto()) > 10_000


def test_el_manual_declara_su_origen_y_hash():
    """Es una COPIA. Sin hash, quedaria obsoleta en silencio si cambia el
    original."""
    cabecera = investep.texto()[:1200]
    assert "Investepia" in cabecera
    assert "sha256" in cabecera


def test_la_copia_coincide_con_el_corpus_si_esta_disponible():
    """Solo corre en la maquina que tiene el corpus; en CI se salta."""
    origen = Path.home() / "Desktop/Investepia/MANUAL_AGENTE_INVESTEPACADEMY.md"
    if not origen.exists():
        pytest.skip("corpus no disponible en esta maquina")
    esperado = hashlib.sha256(origen.read_text().encode()).hexdigest()[:16]
    assert esperado in investep.texto()[:1200], (
        "la copia del manual quedo obsoleta: vuelve a copiarla del corpus")


def test_busca_secciones_por_estrategia_y_por_tema():
    assert investep.buscar("E01")
    assert investep.buscar("volatilidad")
    assert investep.buscar("xyz-que-no-existe-123") == []


# --- prompt ---------------------------------------------------------------

def test_el_prompt_lleva_la_restriccion_y_el_indice():
    b = investep.bloque_prompt()
    assert "EXCLUSIVAMENTE" in b
    for c in investep.ESTRATEGIAS:
        assert c in b
    assert "E11" in b and "NO OPERABLE" in b


def test_el_prompt_prohibe_inventar_umbrales():
    """El material deja vacios reconocidos; rellenarlos por intuicion es el
    error que ya invalido varios resultados del proyecto."""
    b = investep.bloque_prompt()
    assert "NO inventes" in b


def test_el_prompt_del_agente_incluye_el_bloque():
    from powerTradeAi_djangoApp.agent.runner import _system_prompt
    p = _system_prompt()
    assert "MODO INVESTEP" in p
    assert "create_alert" in p


def test_el_prompt_no_arrastra_el_manual_entero():
    """~12.000 tokens en cada llamada serian caros y diluirian lo importante:
    el detalle se consulta con la skill."""
    from powerTradeAi_djangoApp.agent.runner import _system_prompt
    assert len(_system_prompt()) < 8000


# --- skills ---------------------------------------------------------------

def test_la_skill_de_consulta_esta_registrada():
    assert "consultar_manual" in SKILLS


def test_create_alert_exige_la_estrategia():
    esquema = SKILLS["create_alert"].parameters["properties"]
    assert "estrategia" in esquema


def test_create_alert_rechaza_sin_estrategia_valida():
    from powerTradeAi_djangoApp.agent.skills import create_alert
    r = create_alert({}, symbol="TSLA", direction="CALL", thesis="me late")
    assert r.get("error") == "estrategia no valida"
    r = create_alert({}, symbol="TSLA", direction="CALL", thesis="x",
                     estrategia="E11")
    assert r.get("error") == "estrategia no valida", "E11 no deberia operarse"


def test_consultar_manual_avisa_cuando_no_hay_regla():
    from powerTradeAi_djangoApp.agent.skills import consultar_manual
    r = consultar_manual({}, consulta="tecnica-inventada-999")
    assert "aviso" in r and "NO inventes" in r["aviso"]

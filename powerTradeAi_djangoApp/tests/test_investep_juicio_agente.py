"""Dos niveles de validacion: DETERMINISTA y JUICIO_AGENTE.

En fase de investigacion el agente puede operar las diez estrategias
documentadas. Ocho de ellas no tienen validador mecanico, asi que el servidor no
puede confirmar el setup — pero bloquearlas significaria no aprender nunca si
sirven.

Lo que hace esto investigacion y no ruido es la SEPARABILIDAD: si las dos clases
de alerta fueran indistinguibles, ante un resultado malo no se podria saber si
fallo la estrategia o la lectura que el agente hizo de ella. Estos tests fijan
esa separacion.
"""
import pytest

from powerTradeAi_djangoApp.agent import investep
from powerTradeAi_djangoApp.models import InvestepDecision


# --- catalogo -------------------------------------------------------------

@pytest.mark.parametrize("codigo", sorted(investep.ESTRATEGIAS))
def test_las_diez_documentadas_son_operables(codigo):
    ok, _ = investep.es_operable(codigo)
    assert ok is True, f"{codigo} deberia poder investigarse"


def test_e01_e02_no_advierten_de_falta_de_validador():
    for c in sorted(investep.AUTOMATIZADAS):
        _, motivo = investep.es_operable(c)
        assert "JUICIO_AGENTE" not in motivo


@pytest.mark.parametrize("codigo", ["E03", "E05", "E07", "E09"])
def test_las_ocho_sin_validador_lo_advierten(codigo):
    ok, motivo = investep.es_operable(codigo)
    assert ok is True
    assert "JUICIO_AGENTE" in motivo, (
        "debe quedar claro que el servidor no verifica el setup")


def test_e11_y_e12_siguen_bloqueadas():
    """Distinto caso: no es falta de validador, es que la REGLA esta incompleta.
    El Worden no tiene direccion, formula ni umbral publicados: no hay nada que
    el agente pueda juzgar sin inventarlo."""
    for c in ("E11", "E12"):
        ok, motivo = investep.es_operable(c)
        assert ok is False
        assert "NO es operable" in motivo


# --- modelo ---------------------------------------------------------------

def test_el_modelo_distingue_los_dos_niveles():
    v = InvestepDecision.Validacion
    assert v.DETERMINISTA != v.JUICIO_AGENTE
    assert InvestepDecision._meta.get_field("validacion").db_index, (
        "debe indexarse: todo analisis va a separar por este campo")


def test_el_nivel_por_defecto_es_el_estricto():
    """Si alguien añade una decision sin declararlo, que no se cuele como
    verificada por el servidor sin serlo... o mejor dicho: el default debe ser
    explicito y conocido."""
    campo = InvestepDecision._meta.get_field("validacion")
    assert campo.default == InvestepDecision.Validacion.DETERMINISTA


# --- prompt ---------------------------------------------------------------

def test_el_prompt_explica_los_dos_niveles():
    b = investep.bloque_prompt()
    assert "DETERMINISTA" in b and "JUICIO_AGENTE" in b
    assert "E03-E10" in b


def test_el_prompt_exige_mas_evidencia_sin_validador():
    b = investep.bloque_prompt()
    assert "condicion por condicion" in b.lower() or "condicion por" in b.lower()


def test_el_prompt_sigue_prohibiendo_inventar():
    assert "NO inventes" in investep.bloque_prompt()

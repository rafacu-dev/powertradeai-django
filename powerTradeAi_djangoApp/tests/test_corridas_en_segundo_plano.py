"""Lanzamiento en segundo plano y barrido de corridas colgadas.

Origen: la corrida #1944 (02-ago-2026, TSLA) quedo en RUNNING para siempre.
Dos causas encadenadas, y cada una necesita su propia prueba:

  1. ``agent_launch`` corria el agente DENTRO del request HTTP. Un ciclo son
     varias llamadas al LLM y puede pasar del timeout de gunicorn; ese SIGKILL
     se salta el ``finally`` de ``ejecutar_corrida``, que es lo unico que cierra
     la corrida. De paso ocupaba uno de los dos hilos del servicio web.

  2. La limpieza existia pero era letra muerta en produccion: solo la llamaba
     ``agent_loop`` al arrancar, y el worker de Render arranca ``scan_loop``.
"""
import threading
from types import SimpleNamespace

import pytest

from powerTradeAi_djangoApp.agent import runner
from powerTradeAi_djangoApp.models import AgentRun

pytestmark = pytest.mark.django_db(transaction=True)


# --- lanzamiento en segundo plano ----------------------------------------

def test_lanzar_devuelve_la_corrida_sin_esperar_al_modelo(monkeypatch):
    """El request debe volver mientras el LLM sigue trabajando."""
    soltar = threading.Event()
    entro = threading.Event()

    def lento(messages, tools):
        entro.set()
        soltar.wait(timeout=5)
        return SimpleNamespace(content="listo", tool_calls=None)

    monkeypatch.setattr(runner.llm, "chat", lento)
    try:
        run = runner.lanzar_corrida("evalua", symbols=["TSLA"])
        assert entro.wait(timeout=5), "el hilo de fondo no arranco"
        # Aqui el modelo sigue bloqueado y aun asi ya tenemos id y estado.
        assert run.id is not None
        assert run.status == AgentRun.Status.RUNNING
    finally:
        soltar.set()


def test_la_corrida_de_fondo_termina_cerrada(monkeypatch):
    monkeypatch.setattr(
        runner.llm, "chat",
        lambda messages, tools: SimpleNamespace(content="sin setup",
                                                tool_calls=None))
    run = runner.lanzar_corrida("evalua", symbols=["TSLA"])
    for _ in range(100):
        run.refresh_from_db()
        if run.status != AgentRun.Status.RUNNING:
            break
        threading.Event().wait(0.05)
    assert run.status == AgentRun.Status.DONE
    assert run.finished_at is not None


def test_un_fallo_del_modelo_no_deja_la_corrida_colgada(monkeypatch):
    """Lo unico que deberia dejar una corrida en RUNNING es la muerte del
    proceso, nunca una excepcion."""
    def revienta(messages, tools):
        raise RuntimeError("el proveedor devolvio 500")

    monkeypatch.setattr(runner.llm, "chat", revienta)
    run = runner.lanzar_corrida("evalua", symbols=["TSLA"])
    for _ in range(100):
        run.refresh_from_db()
        if run.status != AgentRun.Status.RUNNING:
            break
        threading.Event().wait(0.05)
    assert run.status == AgentRun.Status.ERROR
    assert "500" in run.error
    assert run.finished_at is not None


def test_la_vista_del_panel_ya_no_corre_el_agente_en_el_request():
    """Si volviera a ser sincrona, reaparece la #1944."""
    import inspect

    from powerTradeAi_djangoApp import dashboard

    fuente = inspect.getsource(dashboard.agent_launch)
    assert "lanzar_corrida" in fuente
    assert "run_agent(" not in fuente


# --- barrido periodico ----------------------------------------------------

def test_scan_loop_barre_las_colgadas():
    """La limpieza tiene que vivir en el proceso que SI corre en produccion."""
    import inspect

    from powerTradeAi_djangoApp.management.commands import scan_loop

    fuente = inspect.getsource(scan_loop)
    assert "close_stale_runs" in fuente, (
        "scan_loop es el unico proceso que corre siempre: si la limpieza no "
        "esta aqui, no se ejecuta nunca en Render")
    assert scan_loop.LIMPIEZA_CADA_S > 0


def test_el_barredor_no_lanza_corridas_propias():
    """Es lo que hace seguro barrer desde ahi: scan_loop no puede matar una
    corrida suya en vuelo porque nunca crea ninguna."""
    import inspect

    from powerTradeAi_djangoApp.management.commands import scan_loop

    fuente = inspect.getsource(scan_loop)
    assert "run_agent" not in fuente
    assert "lanzar_corrida" not in fuente

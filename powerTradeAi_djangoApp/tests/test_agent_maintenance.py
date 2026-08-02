"""Limpieza de corridas huerfanas del agente.

Lo que hace segura esta limpieza es el corte por antiguedad: una corrida real
dura minutos, asi que jamas debe tocar una que este en vuelo.

El corte se mide en MINUTOS desde la corrida #1944 (02-ago-2026). El umbral
anterior de 6 horas venia de cuando la limpieza solo corria al arrancar
``agent_loop``; con un barrido periodico ese margen solo servia para que una
corrida muerta figurara como viva media jornada.
"""
from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from powerTradeAi_djangoApp.agent.maintenance import (
    DEFAULT_STALE_MINUTES, close_stale_runs, stale_runs)
from powerTradeAi_djangoApp.models import AgentRun

pytestmark = pytest.mark.django_db


def _run(minutes_ago, status=AgentRun.Status.RUNNING, **kw):
    run = AgentRun.objects.create(
        trigger=AgentRun.Trigger.MANUAL, status=status,
        model_name="deepseek-v4-pro", symbols=["QQQ"], goal="test", **kw)
    # started_at suele ser auto_now_add: se reescribe sin disparar auto_now
    AgentRun.objects.filter(pk=run.pk).update(
        started_at=timezone.now() - timedelta(minutes=minutes_ago))
    run.refresh_from_db()
    return run


def test_cierra_la_corrida_colgada():
    viejo = _run(180)
    assert close_stale_runs() == 1
    viejo.refresh_from_db()
    assert viejo.status == AgentRun.Status.ERROR
    assert "interrumpido" in viejo.error
    assert viejo.finished_at is not None


def test_no_toca_una_corrida_en_vuelo():
    """Una corrida arrancada hace minutos puede estar ejecutandose AHORA."""
    fresco = _run(0)
    assert close_stale_runs() == 0
    fresco.refresh_from_db()
    assert fresco.status == AgentRun.Status.RUNNING
    assert fresco.finished_at is None


def test_respeta_el_limite_de_minutos():
    _run(DEFAULT_STALE_MINUTES - 1)   # justo por debajo del corte
    assert close_stale_runs() == 0
    _run(DEFAULT_STALE_MINUTES + 1)   # justo por encima
    assert close_stale_runs() == 1


def test_el_umbral_por_defecto_da_margen_a_un_ciclo_real():
    """Un ciclo son como mucho MAX_STEPS llamadas al LLM. Si el corte bajara de
    ahi, el barrido periodico mataria corridas vivas."""
    assert DEFAULT_STALE_MINUTES >= 15


def test_no_toca_corridas_ya_cerradas():
    hecho = _run(180, status=AgentRun.Status.DONE)
    error = _run(180, status=AgentRun.Status.ERROR, error="fallo original")
    assert close_stale_runs() == 0
    hecho.refresh_from_db(); error.refresh_from_db()
    assert hecho.status == AgentRun.Status.DONE
    assert error.error == "fallo original"


def test_comando_dry_run_no_escribe():
    viejo = _run(180)
    out = StringIO()
    call_command("cleanup_agent_runs", "--dry-run", stdout=out)
    assert "dry-run" in out.getvalue()
    viejo.refresh_from_db()
    assert viejo.status == AgentRun.Status.RUNNING


def test_comando_cierra_y_reporta():
    _run(180)
    out = StringIO()
    call_command("cleanup_agent_runs", stdout=out)
    assert "Cerradas 1" in out.getvalue()
    assert stale_runs().count() == 0


def test_comando_sin_nada_que_hacer():
    out = StringIO()
    call_command("cleanup_agent_runs", stdout=out)
    assert "Sin corridas colgadas" in out.getvalue()


def test_umbral_configurable_en_minutos():
    _run(20)
    out = StringIO()
    call_command("cleanup_agent_runs", "--older-than-minutes", "10", stdout=out)
    assert "Cerradas 1" in out.getvalue()


def test_el_flag_legado_en_horas_sigue_funcionando():
    """Los runbooks escritos hasta hoy usan --older-than-hours."""
    _run(180)
    out = StringIO()
    call_command("cleanup_agent_runs", "--older-than-hours", "2", stdout=out)
    assert "Cerradas 1" in out.getvalue()


def test_cero_cierra_incluso_una_recien_arrancada():
    """Es la via de escape manual, y por eso el comando avisa en su ayuda: con
    0 no queda ninguna proteccion para las corridas en vuelo."""
    fresco = _run(0)
    call_command("cleanup_agent_runs", "--older-than-minutes", "0",
                 stdout=StringIO())
    fresco.refresh_from_db()
    assert fresco.status == AgentRun.Status.ERROR


def test_los_dos_flags_a_la_vez_se_rechazan():
    _run(180)
    err = StringIO()
    call_command("cleanup_agent_runs", "--older-than-minutes", "10",
                 "--older-than-hours", "1", stdout=StringIO(), stderr=err)
    assert "no ambos" in err.getvalue()
    assert stale_runs().count() == 1, "no debio escribir nada"

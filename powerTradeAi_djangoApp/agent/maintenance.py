"""Mantenimiento de las corridas del agente.

Una ``AgentRun`` nace en estado ``RUNNING`` y solo pasa a ``DONE``/``ERROR`` al
terminar el ciclo (``runner``). Ese cierre vive en un ``finally``, asi que
ninguna excepcion deja una corrida colgada: solo la muerte del proceso sin
desenrollar la pila -- SIGKILL por timeout de gunicorn, OOM, un deploy a mitad
de ciclo. Entonces queda ``RUNNING`` para siempre. No bloquea nada (nadie
consulta ese estado), pero ensucia el historial y falsea cualquier metrica de
completitud.

El corte por antiguedad es lo que hace segura la limpieza. Se mide en MINUTOS y
no en horas por lo que se aprendio con la corrida #1944: el umbral de 6h se
eligio cuando la limpieza solo corria al arrancar ``agent_loop``, donde esperar
era gratis. Con un barrido periodico ese margen solo sirve para que una corrida
muerta siga figurando como viva media jornada. Una corrida real dura minutos, y
el barrido corre en un proceso que no lanza corridas, asi que 30 minutos ya
distinguen sin ambiguedad "en vuelo" de "muerta".
"""
from __future__ import annotations

from datetime import timedelta

DEFAULT_STALE_MINUTES = 30
REASON = "proceso interrumpido (worker reiniciado o caido)"


def stale_runs(older_than_minutes: int = DEFAULT_STALE_MINUTES):
    """QuerySet de corridas ``RUNNING`` arrancadas hace mas de N minutos."""
    from django.utils import timezone

    from ..models import AgentRun

    cutoff = timezone.now() - timedelta(minutes=older_than_minutes)
    return AgentRun.objects.filter(
        status=AgentRun.Status.RUNNING, started_at__lt=cutoff)


def close_stale_runs(older_than_minutes: int = DEFAULT_STALE_MINUTES) -> int:
    """Marca como ERROR las corridas huerfanas. Devuelve cuantas cerro."""
    from django.utils import timezone

    from ..models import AgentRun

    return stale_runs(older_than_minutes).update(
        status=AgentRun.Status.ERROR,
        error=REASON,
        finished_at=timezone.now(),
    )

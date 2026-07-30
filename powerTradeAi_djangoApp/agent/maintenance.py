"""Mantenimiento de las corridas del agente.

Una ``AgentRun`` nace en estado ``RUNNING`` y solo pasa a ``DONE``/``ERROR`` al
terminar el ciclo (``runner``). Si el proceso muere antes -- reinicio del worker
por un deploy, OOM, SIGKILL tras el periodo de gracia -- ese cierre nunca corre y
la corrida queda ``RUNNING`` para siempre. No bloquea nada (nadie consulta ese
estado), pero ensucia el historial y falsea cualquier metrica de completitud.

El corte por antiguedad es lo que hace segura la limpieza: una corrida real dura
minutos, asi que exigir varias horas garantiza que no se toca una en vuelo.
"""
from __future__ import annotations

from datetime import timedelta

DEFAULT_STALE_HOURS = 6
REASON = "proceso interrumpido (worker reiniciado o caido)"


def stale_runs(older_than_hours: int = DEFAULT_STALE_HOURS):
    """QuerySet de corridas ``RUNNING`` arrancadas hace mas de N horas."""
    from django.utils import timezone

    from ..models import AgentRun

    cutoff = timezone.now() - timedelta(hours=older_than_hours)
    return AgentRun.objects.filter(
        status=AgentRun.Status.RUNNING, started_at__lt=cutoff)


def close_stale_runs(older_than_hours: int = DEFAULT_STALE_HOURS) -> int:
    """Marca como ERROR las corridas huerfanas. Devuelve cuantas cerro."""
    from django.utils import timezone

    from ..models import AgentRun

    return stale_runs(older_than_hours).update(
        status=AgentRun.Status.ERROR,
        error=REASON,
        finished_at=timezone.now(),
    )

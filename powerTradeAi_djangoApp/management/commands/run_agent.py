"""Corre el agente una vez sobre una watchlist.

    python manage.py run_agent --symbols SPY,QQQ,TSLA
    python manage.py run_agent --goal "Evalua E01/E02 en TSLA"

Pensado para el modo autonomo: engancharlo a un cron o a un worker que lo
dispare cada cierto tiempo durante la sesion. Cada corrida queda registrada en
AgentRun con todo su razonamiento.
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand

DEFAULT_GOAL = (
    "Revisa E01 y E02 en sus ramas OPENING_GAP e INTRADAY_TREND_CHANGE. "
    "Consulta el manual de cada candidato, ejecuta validate_investep_setup y "
    "crea una alerta solo si el servidor devuelve VALID. Si no, informa el "
    "estado WAIT/BLOCKED y sus blockers exactos.")


class Command(BaseCommand):
    help = "Corre el agente una vez sobre una watchlist."

    def add_arguments(self, parser):
        parser.add_argument("--symbols", type=str, default="")
        parser.add_argument("--goal", type=str, default=DEFAULT_GOAL)

    def handle(self, *args, **options):
        from ...agent.runner import run_agent

        configured = getattr(settings, "INVESTEP_WATCHLIST", ["TSLA", "SPY", "QQQ"])
        symbols = [s.strip().upper() for s in options["symbols"].split(",")
                   if s.strip()] or [str(s).strip().upper() for s in configured]
        self.stdout.write(f"Agente arrancando sobre: {', '.join(symbols)}")
        run = run_agent(options["goal"], symbols=symbols, trigger="manual")

        style = self.style.SUCCESS if run.status == "done" else self.style.ERROR
        self.stdout.write(style(
            f"Corrida #{run.id} [{run.status}] · "
            f"{run.alerts_created} alertas · {len(run.transcript)} pasos"))
        if run.summary:
            self.stdout.write(f"Resumen: {run.summary}")
        if run.error:
            self.stdout.write(self.style.ERROR(run.error))

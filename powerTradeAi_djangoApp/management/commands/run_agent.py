"""Corre el agente una vez sobre una watchlist.

    python manage.py run_agent --symbols SPY,QQQ,TSLA
    python manage.py run_agent --goal "Busca setups de reversion en la apertura"

Pensado para el modo autonomo: engancharlo a un cron o a un worker que lo
dispare cada cierto tiempo durante la sesion. Cada corrida queda registrada en
AgentRun con todo su razonamiento.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

DEFAULT_SYMBOLS = ["NVDA", "AAPL", "MSFT", "AMZN", "META",
                   "TSLA", "QQQ", "SPY", "DIA"]
DEFAULT_GOAL = (
    "Revisa la watchlist, actualiza tu analisis de cada activo y lanza una "
    "alerta solo si encuentras una tesis clara de reversion o continuacion.")


class Command(BaseCommand):
    help = "Corre el agente una vez sobre una watchlist."

    def add_arguments(self, parser):
        parser.add_argument("--symbols", type=str, default="")
        parser.add_argument("--goal", type=str, default=DEFAULT_GOAL)

    def handle(self, *args, **options):
        from ...agent.runner import run_agent

        symbols = [s.strip().upper() for s in options["symbols"].split(",")
                   if s.strip()] or DEFAULT_SYMBOLS
        self.stdout.write(f"Agente arrancando sobre: {', '.join(symbols)}")
        run = run_agent(options["goal"], symbols=symbols, trigger="scan_loop")

        style = self.style.SUCCESS if run.status == "done" else self.style.ERROR
        self.stdout.write(style(
            f"Corrida #{run.id} [{run.status}] · "
            f"{run.alerts_created} alertas · {len(run.transcript)} pasos"))
        if run.summary:
            self.stdout.write(f"Resumen: {run.summary}")
        if run.error:
            self.stdout.write(self.style.ERROR(run.error))

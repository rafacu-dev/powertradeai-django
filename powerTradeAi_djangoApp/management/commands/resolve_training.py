"""Liquida al cierre de sesion las posiciones de ENTRENAMIENTO que quedaron
abiertas (p.ej. abiertas tarde, con horizonte mas alla del fin del bucle).

    python manage.py resolve_training --date 2026-07-20 [--symbol TSLA]
"""
from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand, CommandError

NY = ZoneInfo("America/New_York")


class Command(BaseCommand):
    help = "Cierra al final de sesion las posiciones de entrenamiento abiertas."

    def add_arguments(self, parser):
        parser.add_argument("--date", type=str, required=True)
        parser.add_argument("--symbol", type=str, default="")

    def handle(self, *args, **options):
        from ...agent.resolver import resolve_agent_alerts
        from ...models import Alert

        try:
            day = datetime.strptime(options["date"], "%Y-%m-%d").date()
        except ValueError:
            raise CommandError("Fecha invalida (YYYY-MM-DD).")

        close_ny = datetime.combine(day, time(16, 0), tzinfo=NY)
        closed = resolve_agent_alerts(
            now=close_ny, source=Alert.Source.AGENT_TRAIN, force=True)
        if options["symbol"]:
            closed = [a for a in closed if a.symbol == options["symbol"].upper()]
        self.stdout.write(self.style.SUCCESS(
            f"{len(closed)} posicion(es) de entrenamiento liquidada(s) al cierre."))
        for a in closed:
            self.stdout.write(
                f"  {a.symbol} {a.direction} -> {a.net_pct}% ({a.exit_reason})")

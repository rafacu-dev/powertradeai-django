"""Cierra las corridas del agente que quedaron colgadas en ``RUNNING``.

Una corrida queda huerfana cuando el worker muere a mitad del ciclo (deploy,
OOM, SIGKILL). El ``agent_loop`` ya limpia al arrancar; este comando sirve para
hacerlo a mano o desde un cron.

    python manage.py cleanup_agent_runs --dry-run
    python manage.py cleanup_agent_runs --older-than-hours 12
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from ...agent.maintenance import DEFAULT_STALE_HOURS, close_stale_runs, stale_runs


class Command(BaseCommand):
    help = "Marca como ERROR las corridas del agente colgadas en RUNNING."

    def add_arguments(self, parser):
        parser.add_argument(
            "--older-than-hours", type=int, default=DEFAULT_STALE_HOURS,
            help=f"Antiguedad minima para considerarla colgada (def. "
                 f"{DEFAULT_STALE_HOURS}). Protege a las que estan en vuelo.")
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Solo lista lo que cerraria, sin escribir.")

    def handle(self, *args, **options):
        hours = options["older_than_hours"]
        pendientes = list(stale_runs(hours).values_list(
            "id", "started_at", "model_name", "symbols"))

        if not pendientes:
            self.stdout.write(self.style.SUCCESS(
                f"Sin corridas colgadas de mas de {hours}h."))
            return

        for run_id, started, model, symbols in pendientes:
            self.stdout.write(
                f"  #{run_id} {started:%Y-%m-%d %H:%M} {model or '?'} {symbols}")

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING(
                f"[dry-run] cerraria {len(pendientes)} corrida(s)."))
            return

        n = close_stale_runs(hours)
        self.stdout.write(self.style.SUCCESS(f"Cerradas {n} corrida(s)."))

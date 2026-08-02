"""Cierra las corridas del agente que quedaron colgadas en ``RUNNING``.

Una corrida queda huerfana cuando el proceso muere a mitad del ciclo (deploy,
OOM, SIGKILL por timeout de gunicorn). El ``scan_loop`` ya barre periodicamente;
este comando sirve para hacerlo a mano o desde un cron.

    python manage.py cleanup_agent_runs --dry-run
    python manage.py cleanup_agent_runs --older-than-minutes 10
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from ...agent.maintenance import (
    DEFAULT_STALE_MINUTES, close_stale_runs, stale_runs)


class Command(BaseCommand):
    help = "Marca como ERROR las corridas del agente colgadas en RUNNING."

    def add_arguments(self, parser):
        parser.add_argument(
            "--older-than-minutes", type=int, default=None,
            help=f"Antiguedad minima para considerarla colgada, en minutos "
                 f"(def. {DEFAULT_STALE_MINUTES}). Es lo que protege a las que "
                 f"estan en vuelo: con 0 cierra TODAS, incluida una corriendo "
                 f"ahora mismo.")
        parser.add_argument(
            "--older-than-hours", type=int, default=None,
            help="Igual pero en horas (legado, se conserva por los runbooks).")
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Solo lista lo que cerraria, sin escribir.")

    def handle(self, *args, **options):
        minutes, hours = options["older_than_minutes"], options["older_than_hours"]
        if minutes is not None and hours is not None:
            self.stderr.write(self.style.ERROR(
                "Usa --older-than-minutes o --older-than-hours, no ambos."))
            return
        if minutes is None:
            minutes = hours * 60 if hours is not None else DEFAULT_STALE_MINUTES

        pendientes = list(stale_runs(minutes).values_list(
            "id", "started_at", "model_name", "symbols"))

        if not pendientes:
            self.stdout.write(self.style.SUCCESS(
                f"Sin corridas colgadas de mas de {minutes} min."))
            return

        for run_id, started, model, symbols in pendientes:
            self.stdout.write(
                f"  #{run_id} {started:%Y-%m-%d %H:%M} {model or '?'} {symbols}")

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING(
                f"[dry-run] cerraria {len(pendientes)} corrida(s)."))
            return

        n = close_stale_runs(minutes)
        self.stdout.write(self.style.SUCCESS(f"Cerradas {n} corrida(s)."))

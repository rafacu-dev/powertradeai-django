"""Una pasada de escaneo.

    python manage.py scan_once             # escribe alertas
    python manage.py scan_once --dry-run   # solo muestra que dispararia
"""
from __future__ import annotations

from datetime import datetime

from django.core.management.base import BaseCommand, CommandError

from ...engine.scanner import dry_run, scan_once
from ...engine.session import NY, is_market_open, now_ny


class Command(BaseCommand):
    help = "Ejecuta una pasada del scanner."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Evalua las reglas sin escribir nada en base de datos.")
        parser.add_argument(
            "--at", default=None, metavar="'YYYY-MM-DD HH:MM'",
            help="Evalua como si fuera ese instante ET. Solo con --dry-run: "
                 "sirve para comprobar las reglas contra una sesion pasada.")

    def handle(self, *args, **options):
        moment = None
        if options["at"]:
            if not options["dry_run"]:
                raise CommandError(
                    "--at solo tiene sentido con --dry-run: reevaluar el pasado "
                    "y escribir alertas con fecha de hoy corrompe el historial.")
            moment = datetime.strptime(
                options["at"], "%Y-%m-%d %H:%M").replace(tzinfo=NY)
            self.stdout.write(self.style.WARNING(
                f"Evaluando como si fueran las {moment:%Y-%m-%d %H:%M} ET.\n"))

        if moment is None and not is_market_open():
            self.stdout.write(self.style.WARNING(
                f"[{now_ny():%H:%M:%S} ET] mercado cerrado: las reglas "
                "intradia no encontraran senal."))

        if not options["dry_run"]:
            run = scan_once()
            if not run.ok:
                self.stdout.write(self.style.ERROR(f"scan fallo: {run.error}"))
                return
            self.stdout.write(self.style.SUCCESS(
                f"{run.strategies_evaluated} reglas | "
                f"+{run.alerts_created} alertas | {run.alerts_closed} cerradas"))
            return

        rows = dry_run(moment)
        if not rows:
            self.stdout.write("No hay reglas activas. Corre seed_strategies.")
            return

        styles = {
            "dispararia": self.style.SUCCESS,
            "sin_senal": lambda text: text,
            "sin_contrato": self.style.WARNING,
        }
        for row in sorted(rows, key=lambda r: r["strategy_id"]):
            status = row["status"]
            style = styles.get(status, self.style.ERROR)
            line = f"  {row['strategy_id']:<40} {status}"
            if status == "dispararia":
                line += (f"  {row['direction']} {row['occ'].strip()} "
                         f"ask {row['ask']:.2f} coste ${row['coste']:.0f}")
            elif row.get("detail"):
                line += f"  ({row['detail']})"
            self.stdout.write(style(line))

        fired = sum(1 for r in rows if r["status"] == "dispararia")
        errors = sum(1 for r in rows if r["status"].startswith("error"))
        self.stdout.write(
            f"\n{len(rows)} reglas | {fired} dispararian | {errors} con error")
        self.stdout.write(self.style.WARNING(
            "dry-run: no se ha escrito nada en base de datos.\n"))

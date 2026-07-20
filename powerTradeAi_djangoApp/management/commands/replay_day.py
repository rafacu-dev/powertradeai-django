"""Reconstruye una sesion pasada y guarda las alertas como ``replay``.

    python manage.py replay_day --date 2026-07-17
    python manage.py replay_day --date 2026-07-17 --strategy SPY_ORB15_BASE
    python manage.py replay_day --date 2026-07-17 --overwrite

Las alertas quedan marcadas y NO se mezclan con las de produccion: la API las
sirve solo con ``?source=replay``.
"""
from __future__ import annotations

from datetime import datetime

from django.core.management.base import BaseCommand, CommandError

from ...engine.replay import replay_day
from ...models import Alert


class Command(BaseCommand):
    help = "Reconstruye una sesion pasada con datos historicos."

    def add_arguments(self, parser):
        parser.add_argument("--date", required=True, metavar="YYYY-MM-DD")
        parser.add_argument(
            "--strategy", action="append", dest="strategies",
            help="Limita a una regla (repetible).")
        parser.add_argument(
            "--overwrite", action="store_true",
            help="Rehace las reconstrucciones que ya existan de ese dia.")

    def handle(self, *args, **options):
        try:
            day = datetime.strptime(options["date"], "%Y-%m-%d").date()
        except ValueError:
            raise CommandError("--date debe tener el formato YYYY-MM-DD")

        self.stdout.write(f"\nReconstruyendo {day}...\n")
        try:
            result = replay_day(
                day, strategy_ids=options["strategies"],
                overwrite=options["overwrite"])
        except ValueError as exc:
            raise CommandError(str(exc))

        if result.alerts:
            self.stdout.write("")
            for alert in sorted(result.alerts,
                                key=lambda a: a.strategy.strategy_id):
                self._print_alert(alert)

        if result.skipped:
            self.stdout.write("\nSin alerta:")
            for strategy_id, why in sorted(result.skipped):
                self.stdout.write(f"  {strategy_id:<40} {why}")

        if result.errors:
            self.stdout.write(self.style.ERROR("\nErrores:"))
            for strategy_id, detail in sorted(result.errors):
                self.stdout.write(self.style.ERROR(
                    f"  {strategy_id:<40} {detail}"))

        self._print_summary(result)

    def _print_alert(self, alert: Alert) -> None:
        head = f"  {alert.strategy.strategy_id:<40} {alert.direction}"
        if alert.net_dollars is None:
            self.stdout.write(self.style.WARNING(
                f"{head}  {alert.status}: {alert.exit_reason or 'sin resolver'}"))
            return

        style = (self.style.SUCCESS if alert.net_dollars > 0
                 else self.style.ERROR)
        self.stdout.write(style(
            f"{head}  {alert.occ_symbol.strip()}"
            f"  compra {alert.entry_premium:.2f}"
            f"  venta {alert.exit_premium:.2f}"
            f"  [{alert.exit_reason}]"
            f"  {alert.net_dollars:+.2f}$ ({alert.net_pct:+.2f}%)"))

    def _print_summary(self, result) -> None:
        closed = result.closed
        self.stdout.write(
            f"\n{len(result.alerts)} alertas | {len(closed)} resueltas | "
            f"{len(result.skipped)} sin senal | {len(result.errors)} con error")

        if closed:
            winners = [a for a in closed if a.net_dollars > 0]
            self.stdout.write(
                f"neto {result.net_total:+.2f}$ | "
                f"{len(winners)}/{len(closed)} ganadoras")

        self.stdout.write(self.style.WARNING(
            "\nGuardadas como source=replay. Son una reconstruccion: no "
            "sufrieron latencia\nni competencia por el fill, y usan la quote "
            "del instante teorico. Su P&L es\nun limite superior optimista, no "
            "un resultado. Consultalas con ?source=replay.\n"))

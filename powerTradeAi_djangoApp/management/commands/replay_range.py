"""Reconstruye un rango de sesiones.

    python manage.py replay_range --desde 2026-07-01 --hasta 2026-07-17
    python manage.py replay_range --desde 2026-07-01 --hasta 2026-07-17 \\
        --strategy SPY_ORB15_BASE

Igual que ``replay_day`` pero encadenando dias habiles. Una sesion que falle no
aborta el rango: se registra y se sigue.

El P&L agregado que imprime al final sigue siendo una reconstruccion —limite
superior optimista, no un resultado. Para verificar si la app reproduce un
backtest, usa ``compare_golden``, que compara senales en vez de dinero.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from ...engine.replay import replay_day
from ...engine.session import is_trading_day


class Command(BaseCommand):
    help = "Reconstruye todas las sesiones habiles de un rango."

    def add_arguments(self, parser):
        parser.add_argument("--desde", required=True, metavar="YYYY-MM-DD")
        parser.add_argument("--hasta", required=True, metavar="YYYY-MM-DD")
        parser.add_argument("--strategy", action="append", dest="strategies")
        parser.add_argument("--overwrite", action="store_true")

    def handle(self, *args, **options):
        start = self._date(options["desde"], "--desde")
        end = self._date(options["hasta"], "--hasta")
        if start > end:
            raise CommandError("--desde no puede ser posterior a --hasta")

        days = [d for d in self._days(start, end) if is_trading_day(d)]
        if not days:
            raise CommandError("el rango no contiene ningun dia habil")

        self.stdout.write(
            f"\n{len(days)} sesiones habiles entre {start} y {end}\n")

        total_alerts = total_closed = total_errors = 0
        net = Decimal("0.00")
        failed_days: list[tuple[str, str]] = []

        for day in days:
            try:
                result = replay_day(
                    day, strategy_ids=options["strategies"],
                    overwrite=options["overwrite"])
            except Exception as exc:
                # Una sesion sin datos no debe tumbar el rango entero.
                failed_days.append((str(day), f"{type(exc).__name__}: {exc}"))
                self.stdout.write(self.style.ERROR(f"  {day}  FALLO"))
                continue

            closed = result.closed
            total_alerts += len(result.alerts)
            total_closed += len(closed)
            total_errors += len(result.errors)
            net += result.net_total

            if result.alerts:
                self.stdout.write(
                    f"  {day}  {len(result.alerts)} alertas, "
                    f"{len(closed)} resueltas, neto {result.net_total:+.2f}$")
            else:
                self.stdout.write(f"  {day}  sin señal")

        self.stdout.write(
            f"\n{total_alerts} alertas | {total_closed} resueltas | "
            f"{total_errors} con error | neto reconstruido {net:+.2f}$")

        if failed_days:
            self.stdout.write(self.style.ERROR(
                f"\n{len(failed_days)} sesiones fallaron:"))
            for day, detail in failed_days[:10]:
                self.stdout.write(f"  {day}  {detail}")

        self.stdout.write(self.style.WARNING(
            "\nTodo esto es source=replay: sin latencia, sin competencia por el\n"
            "fill y con la quote del instante teorico. Ese neto es un limite\n"
            "superior optimista, no un resultado. Para verificar fidelidad al\n"
            "backtest usa compare_golden, que compara señales, no dinero.\n"))

    def _date(self, raw: str, flag: str):
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            raise CommandError(f"{flag} debe tener el formato YYYY-MM-DD")

    def _days(self, start, end):
        cursor = start
        while cursor <= end:
            yield cursor
            cursor += timedelta(days=1)

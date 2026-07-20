"""Worker de escaneo. Es el proceso que corre en el Background Worker de Render.

Un proceso vivo, no un cron: el estado en memoria (velas de la sesion) se
reutiliza entre pasadas y no hay cold start en cada vela.

    python manage.py scan_loop --interval 30
"""
from __future__ import annotations

import logging
import signal
import time

from django.core.management.base import BaseCommand

from ...engine.scanner import resolve_pending, scan_once
from ...engine.session import is_market_open, now_ny, seconds_until_open

log = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Escanea el mercado durante RTH y resuelve las alertas pendientes."

    def add_arguments(self, parser):
        parser.add_argument(
            "--interval", type=int, default=30,
            help="Segundos entre pasadas con el mercado abierto (def. 30).")
        parser.add_argument(
            "--once", action="store_true",
            help="Una sola pasada y salir (util para probar o para un cron).")
        parser.add_argument(
            "--ignore-market-hours", action="store_true",
            help="Escanea aunque el mercado este cerrado.")

    def handle(self, *args, **options):
        interval = options["interval"]
        ignore_hours = options["ignore_market_hours"]

        if options["once"]:
            run = scan_once()
            self._report(run)
            return

        stopping = {"now": False}

        def _stop(signum, frame):
            # Render envia SIGTERM al redeployar: hay que salir limpio, no a
            # media escritura.
            self.stdout.write(self.style.WARNING("\nSenal recibida, parando..."))
            stopping["now"] = True

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

        self.stdout.write(self.style.SUCCESS(
            f"scan_loop arrancado (intervalo {interval}s)"))

        while not stopping["now"]:
            if not ignore_hours and not is_market_open():
                wait = min(seconds_until_open(), 900)
                self.stdout.write(
                    f"[{now_ny():%H:%M:%S}] mercado cerrado; "
                    f"durmiendo {wait / 60:.0f} min")
                # Trocear la espera para atender SIGTERM sin latencia.
                self._sleep(wait, stopping)
                continue

            run = scan_once()
            self._report(run)
            # Aunque el scan falle, seguimos: un fallo de red no debe tumbar
            # el worker y dejar alertas vivas sin resolver.
            self._sleep(interval, stopping)

        # Ultimo intento de dejar la casa ordenada antes de morir.
        try:
            resolve_pending()
        except Exception:
            log.exception("fallo el resolve final")
        self.stdout.write(self.style.SUCCESS("scan_loop terminado."))

    def _sleep(self, seconds: float, stopping: dict) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not stopping["now"]:
            time.sleep(min(1.0, deadline - time.monotonic()))

    def _report(self, run) -> None:
        stamp = f"[{now_ny():%H:%M:%S}]"
        if not run.ok:
            self.stdout.write(self.style.ERROR(f"{stamp} scan fallo: {run.error}"))
            return
        self.stdout.write(
            f"{stamp} {run.strategies_evaluated} reglas | "
            f"+{run.alerts_created} alertas | {run.alerts_closed} cerradas")

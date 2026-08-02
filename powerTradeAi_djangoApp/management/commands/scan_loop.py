"""Worker de escaneo. Es el proceso que corre en el Background Worker de Render.

Un proceso vivo, no un cron: el historial anterior a la sesion se reutiliza
entre pasadas. Las velas de la sesion se refrescan en cada scan.

    python manage.py scan_loop --interval 30
"""
from __future__ import annotations

import logging
import signal
import time

from django.core.management.base import BaseCommand

from ...agent.maintenance import close_stale_runs
from ...engine.scanner import resolve_pending, scan_once
from ...engine.session import is_market_open, now_ny, seconds_until_open

LIMPIEZA_CADA_S = 300

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
        # Argumentos legados: el flag solo emite una advertencia; el agente
        # corre en su propio worker.
        parser.add_argument(
            "--agent", action="store_true",
            help="Ademas del scanner, corre el agente autonomo en este proceso.")
        parser.add_argument("--agent-symbols", type=str, default="TSLA")
        parser.add_argument("--agent-interval", type=int, default=1800)
        parser.add_argument("--agent-move-threshold", type=float, default=0.7)
        parser.add_argument("--agent-min-gap", type=int, default=600)

    def handle(self, *args, **options):
        interval = options["interval"]
        ignore_hours = options["ignore_market_hours"]

        if options["once"]:
            run = scan_once()
            self._report(run)
            return

        # El LLM no comparte proceso con el scanner: una llamada lenta no puede
        # retrasar stops ni cierres. Se conserva el flag para fallar de forma
        # explicable en despliegues antiguos, pero no se ejecuta aqui.
        if options["agent"]:
            self.stdout.write(self.style.WARNING(
                "--agent fue desactivado por seguridad; ejecuta agent_loop en "
                "un worker separado."))

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
        history_cache: dict = {}
        cache_day = None
        proxima_limpieza = 0.0

        while not stopping["now"]:
            # Este proceso NO lanza corridas del agente, y por eso es el unico
            # que puede barrerlas sin riesgo de matar una propia en vuelo. Vive
            # aqui porque es el unico proceso que corre siempre en produccion:
            # el worker arranca ``scan_loop``, no ``agent_loop``, asi que la
            # limpieza que colgaba de aquel nunca llegaba a ejecutarse.
            if time.monotonic() >= proxima_limpieza:
                proxima_limpieza = time.monotonic() + LIMPIEZA_CADA_S
                try:
                    n = close_stale_runs()
                    if n:
                        self.stdout.write(self.style.WARNING(
                            f"[{now_ny():%H:%M:%S}] cerradas {n} corrida(s) "
                            f"colgada(s) del agente"))
                except Exception:
                    log.exception("fallo la limpieza de corridas colgadas")

            if not ignore_hours and not is_market_open():
                # El cambio a "cerrado" ocurre antes de la siguiente pasada.
                # Resolver aqui evita dejar una salida de las 16:00 pendiente
                # hasta la apertura siguiente.
                try:
                    resolve_pending(moment=now_ny())
                except Exception:
                    log.exception("fallo el resolve con mercado cerrado")
                wait = min(seconds_until_open(), 900)
                self.stdout.write(
                    f"[{now_ny():%H:%M:%S}] mercado cerrado; "
                    f"durmiendo {wait / 60:.0f} min")
                # Trocear la espera para atender SIGTERM sin latencia.
                self._sleep(wait, stopping)
                continue

            current_day = now_ny().date()
            if cache_day != current_day:
                history_cache.clear()
                cache_day = current_day
            run = scan_once(history_cache=history_cache)
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

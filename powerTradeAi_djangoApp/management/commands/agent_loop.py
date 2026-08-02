"""Loop autonomo del agente: corre toda la jornada.

Dispara al agente por dos vias:
  1. Revision periodica: cada ``--interval`` segundos revisa cada activo.
  2. Por evento: si el precio se mueve mas de ``--move-threshold`` por ciento
     (en cualquier direccion) desde la ultima vez que el agente lo miro, lo
     analiza en el acto.

Un ``--min-gap`` limita cuantas veces por activo puede correr, para no disparar
al LLM sin parar. Cada corrida queda en AgentRun con su razonamiento, y el
analisis se acumula en AgentAnalysis para dar continuidad.

    python manage.py agent_loop --symbols SPY,QQQ,TSLA
    python manage.py agent_loop --interval 1800 --move-threshold 0.7

Pensado para un Background Worker aparte del scanner de reglas.
"""
from __future__ import annotations

import logging
import signal
import time

from django.core.management.base import BaseCommand

DEFAULT_SYMBOLS = ["TSLA", "SPY", "QQQ"]
log = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Corre el agente de forma autonoma durante la sesion."

    def add_arguments(self, parser):
        parser.add_argument("--symbols", type=str, default="")
        parser.add_argument(
            "--interval", type=int, default=1800,
            help="Segundos entre revisiones periodicas por activo (def. 1800).")
        parser.add_argument(
            "--move-threshold", type=float, default=0.7,
            help="%% de movimiento del precio que dispara un analisis (def 0.7).")
        parser.add_argument(
            "--min-gap", type=int, default=600,
            help="Segundos minimos entre corridas del mismo activo (def. 600).")
        parser.add_argument(
            "--poll", type=int, default=60,
            help="Segundos entre chequeos de precio (def. 60).")
        parser.add_argument("--ignore-market-hours", action="store_true")

    def handle(self, *args, **options):
        from ...agent.autopilot import AgentAutopilot
        from ...agent.maintenance import close_stale_runs
        from ...data import get_provider
        from ...engine.session import is_market_open, now_ny, seconds_until_open

        # Si el worker anterior murio a mitad de un ciclo, su AgentRun quedo en
        # RUNNING para siempre. Este es el momento natural de cerrarlas: acabamos
        # de arrancar, asi que ninguna corrida propia puede estar en vuelo.
        # El barrido periodico vive en ``scan_loop``, que es el proceso que si
        # corre siempre en produccion; esto solo cubre el arranque de este.
        huerfanas = close_stale_runs()
        if huerfanas:
            self.stdout.write(self.style.WARNING(
                f"Cerradas {huerfanas} corrida(s) colgadas de un arranque previo."))

        from django.conf import settings
        configured = getattr(settings, "POWERTRADEAI", {}).get(
            "INVESTEP_WATCHLIST", DEFAULT_SYMBOLS)
        if isinstance(configured, str):
            configured = configured.split(",")
        default_symbols = [str(s).strip().upper() for s in configured if str(s).strip()]
        symbols = [s.strip().upper() for s in options["symbols"].split(",")
                   if s.strip()] or default_symbols
        poll = options["poll"]
        ignore_hours = options["ignore_market_hours"]

        provider = get_provider()
        autopilot = AgentAutopilot(
            symbols, interval=options["interval"],
            move_threshold=options["move_threshold"], min_gap=options["min_gap"])

        stopping = {"now": False}

        def _stop(signum, frame):
            self.stdout.write(self.style.WARNING("\nSenal recibida, parando..."))
            stopping["now"] = True

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

        self.stdout.write(self.style.SUCCESS(
            f"agent_loop arrancado · {len(symbols)} activos · "
            f"periodico {options['interval']}s · evento "
            f"{options['move_threshold']}% · min_gap {options['min_gap']}s"))

        while not stopping["now"]:
            if not ignore_hours and not is_market_open():
                try:
                    from ...agent.resolver import resolve_agent_alerts
                    for alert in resolve_agent_alerts(
                            now=now_ny(), force=True, provider=provider):
                        self.stdout.write(
                            f"[{now_ny():%H:%M:%S}] resuelta alerta #{alert.id}")
                except Exception:
                    log.exception("fallo el resolver con mercado cerrado")
                wait = min(seconds_until_open(), 900)
                self.stdout.write(
                    f"[{now_ny():%H:%M:%S}] mercado cerrado; "
                    f"durmiendo {wait / 60:.0f} min")
                self._sleep(wait, stopping)
                continue

            # Este worker es dueno tanto de las decisiones generativas como de
            # resolver sus alertas. El scanner determinista no espera al LLM.
            try:
                from ...agent.resolver import resolve_agent_alerts
                for alert in resolve_agent_alerts(provider=provider):
                    self.stdout.write(
                        f"[{now_ny():%H:%M:%S}] resuelta alerta #{alert.id}")
            except Exception:
                log.exception("fallo el resolver del agente")
            try:
                for sym, msg in autopilot.tick(provider):
                    self.stdout.write(f"[{now_ny():%H:%M:%S}] {sym} {msg}")
            except Exception:
                log.exception("fallo el tick del agente")
            self._sleep(poll, stopping)

        self.stdout.write(self.style.SUCCESS("agent_loop terminado."))

    def _sleep(self, seconds: float, stopping: dict) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not stopping["now"]:
            time.sleep(min(1.0, deadline - time.monotonic()))

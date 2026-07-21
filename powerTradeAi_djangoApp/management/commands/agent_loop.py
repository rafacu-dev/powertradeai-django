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

import signal
import time

from django.core.management.base import BaseCommand

DEFAULT_SYMBOLS = ["NVDA", "AAPL", "MSFT", "AMZN", "META",
                   "TSLA", "QQQ", "SPY", "DIA"]


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
        from ...agent.runner import run_agent
        from ...data import get_provider
        from ...engine.session import is_market_open, now_ny, seconds_until_open

        symbols = [s.strip().upper() for s in options["symbols"].split(",")
                   if s.strip()] or DEFAULT_SYMBOLS
        interval = options["interval"]
        move_threshold = options["move_threshold"]
        min_gap = options["min_gap"]
        poll = options["poll"]
        ignore_hours = options["ignore_market_hours"]

        provider = get_provider()
        # Estado por activo: cuando corrio por ultima vez y a que precio.
        state: dict[str, dict] = {s: {"last_run": None, "last_price": None}
                                  for s in symbols}

        stopping = {"now": False}

        def _stop(signum, frame):
            self.stdout.write(self.style.WARNING("\nSenal recibida, parando..."))
            stopping["now"] = True

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

        self.stdout.write(self.style.SUCCESS(
            f"agent_loop arrancado · {len(symbols)} activos · "
            f"periodico {interval}s · evento {move_threshold}% · "
            f"min_gap {min_gap}s"))

        while not stopping["now"]:
            if not ignore_hours and not is_market_open():
                wait = min(seconds_until_open(), 900)
                self.stdout.write(
                    f"[{now_ny():%H:%M:%S}] mercado cerrado; "
                    f"durmiendo {wait / 60:.0f} min")
                self._sleep(wait, stopping)
                continue

            now = time.monotonic()
            for sym in symbols:
                if stopping["now"]:
                    break
                try:
                    price = float(provider.latest_price(sym))
                except Exception as exc:  # noqa: BLE001
                    self.stdout.write(self.style.ERROR(
                        f"[{now_ny():%H:%M:%S}] {sym}: sin precio ({exc})"))
                    continue

                st = state[sym]
                last_run = st["last_run"]
                base_price = st["last_price"]

                # Rate limit por activo.
                if last_run is not None and (now - last_run) < min_gap:
                    continue

                periodic = last_run is None or (now - last_run) >= interval
                move_pct = (
                    (price / base_price - 1) * 100
                    if base_price else 0.0)
                event = base_price is not None and abs(move_pct) >= move_threshold

                if not (periodic or event):
                    continue

                if periodic and last_run is None:
                    reason = "primera revision de la sesion"
                elif event:
                    reason = f"el precio se movio {move_pct:+.2f}% desde tu ultima mirada"
                else:
                    reason = "revision periodica programada"

                goal = (
                    f"Analiza {sym} ahora mismo (precio {price:.2f}). "
                    f"Motivo del disparo: {reason}. Compara con tu analisis "
                    f"previo, proyecta la direccion mas probable a corto plazo "
                    f"y guarda tu vision. Lanza una alerta solo si hay una "
                    f"tesis clara y accionable.")
                try:
                    run = run_agent(goal, symbols=[sym], trigger="scan_loop")
                    self.stdout.write(
                        f"[{now_ny():%H:%M:%S}] {sym} @ {price:.2f} "
                        f"({reason}) -> corrida #{run.id} [{run.status}] "
                        f"{run.alerts_created} alerta(s)")
                except Exception as exc:  # noqa: BLE001
                    self.stdout.write(self.style.ERROR(
                        f"[{now_ny():%H:%M:%S}] {sym}: agente fallo ({exc})"))

                st["last_run"] = time.monotonic()
                st["last_price"] = price

            self._sleep(poll, stopping)

        self.stdout.write(self.style.SUCCESS("agent_loop terminado."))

    def _sleep(self, seconds: float, stopping: dict) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not stopping["now"]:
            time.sleep(min(1.0, deadline - time.monotonic()))

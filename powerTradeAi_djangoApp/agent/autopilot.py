"""Piloto automatico del agente.

Encapsula la decision de 'cuando disparar al agente': por periodicidad y por
evento de movimiento, con rate-limit por activo. Lo usan tanto ``agent_loop``
(worker dedicado) como ``scan_loop --agent`` (mismo worker que el scanner de
reglas), para no duplicar la logica.
"""
from __future__ import annotations

import time


class AgentAutopilot:
    def __init__(self, symbols: list[str], interval: int = 1800,
                 move_threshold: float = 0.7, min_gap: int = 600):
        self.symbols = symbols
        self.interval = interval            # revision periodica (s)
        self.move_threshold = move_threshold  # % de movimiento que dispara
        self.min_gap = min_gap              # rate-limit por activo (s)
        self.state = {s: {"last_run": None, "last_price": None} for s in symbols}

    def tick(self, provider) -> list[tuple[str, str]]:
        """Revisa los activos y corre el agente en los que toque. Devuelve una
        lista de (symbol, mensaje) de lo que hizo, para loguear."""
        from .runner import run_agent

        events: list[tuple[str, str]] = []
        now = time.monotonic()
        for sym in self.symbols:
            try:
                price = float(provider.latest_price(sym))
            except Exception as exc:  # noqa: BLE001
                events.append((sym, f"sin precio ({exc})"))
                continue

            st = self.state[sym]
            last_run, base = st["last_run"], st["last_price"]
            if last_run is not None and (now - last_run) < self.min_gap:
                continue

            periodic = last_run is None or (now - last_run) >= self.interval
            move_pct = (price / base - 1) * 100 if base else 0.0
            event = base is not None and abs(move_pct) >= self.move_threshold
            if not (periodic or event):
                continue

            if last_run is None:
                reason = "primera revision de la sesion"
            elif event:
                reason = f"el precio se movio {move_pct:+.2f}% desde tu ultima mirada"
            else:
                reason = "revision periodica programada"

            goal = (
                f"Analiza {sym} ahora mismo (precio {price:.2f}). "
                f"Motivo del disparo: {reason}. Recupera tu contexto previo, "
                f"lee el estado intradia, valida cualquier idea con backtest, "
                f"proyecta la direccion mas probable y guarda tu vision. Lanza "
                f"una alerta solo si hay una tesis clara y accionable.")
            try:
                run = run_agent(goal, symbols=[sym], trigger="scan_loop")
                events.append((sym, f"@ {price:.2f} ({reason}) -> corrida "
                                    f"#{run.id} [{run.status}] "
                                    f"{run.alerts_created} alerta(s)"))
            except Exception as exc:  # noqa: BLE001
                events.append((sym, f"agente fallo ({exc})"))

            st["last_run"] = time.monotonic()
            st["last_price"] = price
        return events

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
                 move_threshold: float = 0.7, min_gap: int = 600,
                 review_interval: int = 300):
        self.symbols = symbols
        self.interval = interval            # revision periodica (s)
        self.move_threshold = move_threshold  # % de movimiento que dispara
        self.min_gap = min_gap              # rate-limit por activo (s)
        # Con posicion ABIERTA se revisa mas seguido para gestionarla.
        self.review_interval = review_interval
        self.state = {s: {"last_run": None, "last_price": None} for s in symbols}

    def tick(self, provider) -> list[tuple[str, str]]:
        """Revisa los activos y corre el agente en los que toque. Devuelve una
        lista de (symbol, mensaje) de lo que hizo, para loguear."""
        from ..models import AgentTrigger, Alert

        events: list[tuple[str, str]] = []

        # Niveles de vigilancia que fijo el propio agente, agrupados por activo.
        triggers: dict[str, list] = {}
        for t in AgentTrigger.objects.filter(active=True):
            triggers.setdefault(t.symbol, []).append(t)

        # Activos con posicion abierta del agente (para gestionarla mas seguido).
        open_syms = set(Alert.objects.filter(
            source=Alert.Source.AGENT, status=Alert.Status.PENDING,
        ).values_list("symbol", flat=True))

        # Se vigilan los activos base MAS cualquiera con triggers o posicion.
        symbols = list(dict.fromkeys(
            self.symbols + list(triggers.keys()) + list(open_syms)))

        now = time.monotonic()
        for sym in symbols:
            try:
                price = float(provider.latest_price(sym))
            except Exception as exc:  # noqa: BLE001
                events.append((sym, f"sin precio ({exc})"))
                continue

            st = self.state.setdefault(sym, {"last_run": None, "last_price": None})

            # 1) Triggers del agente: prioridad, disparan aunque haya min_gap.
            hit = [t for t in triggers.get(sym, []) if t.is_hit(price)]
            if hit:
                self._run(provider, sym, price, self._trigger_reason(hit, price),
                          st, events, hit_triggers=hit)
                continue

            last_run, base = st["last_run"], st["last_price"]

            # 2) Posicion abierta: revisar mas seguido para gestionarla.
            if sym in open_syms and (last_run is None or
                                     (now - last_run) >= self.review_interval):
                self._run(provider, sym, price,
                          "tienes una posicion ABIERTA en este activo: "
                          "revisala y gestionala (mantener, ajustar stop/target "
                          "o cerrar)", st, events)
                continue

            # 3) Periodico + evento de movimiento, con rate-limit.
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
            self._run(provider, sym, price, reason, st, events)

        return events

    def _trigger_reason(self, hit, price: float) -> str:
        parts = [f"nivel {float(t.price):.2f} ({t.get_direction_display().lower()})"
                 f"{' — ' + t.reason if t.reason else ''}" for t in hit]
        return (f"toco un nivel que TU marcaste para vigilar: "
                f"{'; '.join(parts)}")

    def _run(self, provider, sym, price, reason, st, events, hit_triggers=None):
        from django.utils import timezone

        from .runner import run_agent

        goal = (
            f"Analiza {sym} ahora mismo (precio {price:.2f}). "
            f"Motivo del disparo: {reason}. Recupera tu contexto previo, lee el "
            f"estado intradia, valida cualquier idea con backtest, proyecta la "
            f"direccion mas probable y guarda tu vision. Si sigue teniendo "
            f"sentido, puedes fijar nuevos niveles de vigilancia. Lanza una "
            f"alerta solo si hay una tesis clara y accionable.")
        try:
            run = run_agent(goal, symbols=[sym], trigger="scan_loop")
            events.append((sym, f"@ {price:.2f} ({reason}) -> corrida "
                                f"#{run.id} [{run.status}] "
                                f"{run.alerts_created} alerta(s)"))
        except Exception as exc:  # noqa: BLE001
            events.append((sym, f"agente fallo ({exc})"))

        # Consumir los triggers tocados (una sola vez).
        if hit_triggers:
            ids = [t.id for t in hit_triggers]
            from ..models import AgentTrigger
            AgentTrigger.objects.filter(id__in=ids).update(
                active=False, triggered_at=timezone.now())

        st["last_run"] = time.monotonic()
        st["last_price"] = price

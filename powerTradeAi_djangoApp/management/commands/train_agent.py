"""Entrenamiento del agente: opera un dia PASADO en tiempo simulado.

Recorre la sesion de un dia en pasos finos (por defecto 5 min). En cada paso
despierta al agente con un reloj ``as_of``: las skills solo ven datos hasta ese
instante, nunca el futuro (sin look-ahead). El agente analiza, abre, gestiona y
cierra posiciones; cada operacion se marca como ``agent_train`` y se puntua
causalmente contra el precio real.

    python manage.py train_agent --symbol TSLA --date 2026-07-21 --step 5

Cuanto mas fino el paso, mas realista y mas llamadas al LLM (mas costo).
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand, CommandError

NY = ZoneInfo("America/New_York")


class Command(BaseCommand):
    help = "Entrena al agente operando un dia pasado en tiempo simulado."

    def add_arguments(self, parser):
        parser.add_argument("--symbol", type=str, default="TSLA")
        parser.add_argument("--date", type=str, required=True,
                            help="Dia a operar, YYYY-MM-DD.")
        parser.add_argument("--step", type=int, default=5,
                            help="Minutos de tiempo simulado entre pasos (def 5).")
        parser.add_argument("--start", type=str, default="09:35",
                            help="Hora de inicio ET (def 09:35).")
        parser.add_argument("--end", type=str, default="15:55",
                            help="Hora de fin ET (def 15:55).")

    def handle(self, *args, **options):
        from ...agent.resolver import resolve_agent_alerts
        from ...agent.runner import run_agent
        from ...engine.session import is_trading_day
        from ...models import Alert

        sym = options["symbol"].upper()
        try:
            day = datetime.strptime(options["date"], "%Y-%m-%d").date()
        except ValueError:
            raise CommandError("Fecha invalida (YYYY-MM-DD).")
        if not is_trading_day(day):
            raise CommandError(f"{day} no es dia habil de mercado.")

        step = max(int(options["step"]), 1)
        h1, m1 = map(int, options["start"].split(":"))
        h2, m2 = map(int, options["end"].split(":"))
        t = datetime.combine(day, time(h1, m1), tzinfo=NY)
        end = datetime.combine(day, time(h2, m2), tzinfo=NY)

        self.stdout.write(self.style.SUCCESS(
            f"Entrenamiento {sym} {day} · pasos de {step} min · "
            f"{options['start']}–{options['end']} ET"))

        n_steps = 0
        while t <= end:
            goal = (
                f"Estas operando {sym} el {day} a las {t:%H:%M} ET "
                f"(ENTRENAMIENTO en tiempo pasado; solo ves datos hasta ahora). "
                f"Gestiona tus posiciones abiertas y busca oportunidades como un "
                f"day-trader. Define riesgo (target/stop) en cada entrada.")
            try:
                run = run_agent(goal, symbols=[sym], trigger="training", as_of=t)
                closed = resolve_agent_alerts(now=t, source=Alert.Source.AGENT_TRAIN)
                msg = f"[{t:%H:%M}] corrida #{run.id} [{run.status}]"
                if run.alerts_created:
                    msg += f" +{run.alerts_created} op"
                if closed:
                    msg += f" · {len(closed)} cerrada(s)"
                self.stdout.write(msg)
            except Exception as exc:  # noqa: BLE001
                self.stdout.write(self.style.ERROR(f"[{t:%H:%M}] fallo: {exc}"))
            n_steps += 1
            t += timedelta(minutes=step)

        # Cierre final: liquidar lo que siga abierto al cierre de la sesion.
        final = resolve_agent_alerts(now=end, source=Alert.Source.AGENT_TRAIN)

        from django.db.models import Avg
        closed_all = Alert.objects.filter(
            source=Alert.Source.AGENT_TRAIN, symbol=sym,
            session_date=day, status=Alert.Status.CLOSED)
        n = closed_all.count()
        wins = closed_all.filter(net_pct__gt=0).count()
        avg = closed_all.aggregate(a=Avg("net_pct"))["a"]
        self.stdout.write(self.style.SUCCESS(
            f"\nEntrenamiento terminado · {n_steps} pasos · {n} operaciones · "
            f"win-rate {round(wins / n * 100, 1) if n else 0}% · "
            f"retorno medio {round(avg, 2) if avg is not None else 0}%"))
        if final:
            self.stdout.write(f"({len(final)} liquidadas al cierre)")

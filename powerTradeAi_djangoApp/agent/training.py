"""Bucle de entrenamiento del agente sobre un dia pasado.

Extraido aqui para que lo usen igual el comando ``train_agent`` (terminal) y el
boton de la pagina del Agente (via endpoint, en un hilo de fondo).
"""
from __future__ import annotations

from datetime import date as date_cls, datetime, time, timedelta
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")


def train_day(symbol: str, day: date_cls, step: int = 5,
              start: str = "09:35", end: str = "15:55", log=None) -> dict:
    """Opera ``symbol`` el dia ``day`` en tiempo simulado. Devuelve un resumen.

    ``log`` (opcional): callable(str) para reportar progreso."""
    from django.db.models import Avg

    from .resolver import resolve_agent_alerts
    from .runner import run_agent
    from ..engine.session import is_trading_day
    from ..models import Alert

    def _say(msg):
        if log:
            log(msg)

    sym = symbol.upper()
    if not is_trading_day(day):
        raise ValueError(f"{day} no es dia habil de mercado.")

    step = max(int(step), 1)
    h1, m1 = map(int, start.split(":"))
    h2, m2 = map(int, end.split(":"))
    t = datetime.combine(day, time(h1, m1), tzinfo=NY)
    stop = datetime.combine(day, time(h2, m2), tzinfo=NY)

    _say(f"Entrenamiento {sym} {day} · pasos {step} min · {start}-{end} ET")
    n_steps = 0
    while t <= stop:
        goal = (
            f"Estas operando {sym} el {day} a las {t:%H:%M} ET (ENTRENAMIENTO "
            f"en tiempo pasado; solo ves datos hasta ahora). Gestiona tus "
            f"posiciones abiertas y busca oportunidades como day-trader. "
            f"Define riesgo (target/stop) en cada entrada.")
        try:
            run = run_agent(goal, symbols=[sym], trigger="training", as_of=t)
            closed = resolve_agent_alerts(now=t, source=Alert.Source.AGENT_TRAIN)
            line = f"[{t:%H:%M}] #{run.id} [{run.status}]"
            if run.alerts_created:
                line += f" +{run.alerts_created} op"
            if closed:
                line += f" · {len(closed)} cerrada(s)"
            _say(line)
        except Exception as exc:  # noqa: BLE001
            _say(f"[{t:%H:%M}] fallo: {exc}")
        n_steps += 1
        t += timedelta(minutes=step)

    resolve_agent_alerts(now=stop, source=Alert.Source.AGENT_TRAIN)

    closed_all = Alert.objects.filter(
        source=Alert.Source.AGENT_TRAIN, symbol=sym,
        session_date=day, status=Alert.Status.CLOSED)
    n = closed_all.count()
    wins = closed_all.filter(net_pct__gt=0).count()
    avg = closed_all.aggregate(a=Avg("net_pct"))["a"]
    summary = {
        "symbol": sym, "date": str(day), "steps": n_steps, "trades": n,
        "win_rate": round(wins / n * 100, 1) if n else None,
        "avg_pct": round(float(avg), 2) if avg is not None else None,
    }
    _say(f"Terminado · {n_steps} pasos · {n} operaciones · "
         f"win {summary['win_rate']}% · medio {summary['avg_pct']}%")
    return summary

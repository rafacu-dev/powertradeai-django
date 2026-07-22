"""Resolver de las alertas del agente.

Cada alerta del agente es una prediccion direccional con un horizonte. Cuando el
horizonte vence, la cerramos con el precio REAL del subyacente en ese instante
(causal: el precio en el momento del vencimiento, no el ultimo) y guardamos su
retorno direccional. Asi el agente pasa de opinar a tener un expediente medible.

Se mide el movimiento del SUBYACENTE en %, no el P&L de la opcion: es lo que
prueba si el agente acierta la DIRECCION, sin el ruido de theta y spread. El
P&L de opcion real es una segunda capa para mas adelante.
"""
from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")


def _price_at(provider, symbol: str, ts):
    """Precio del subyacente en (o justo despues de) ``ts``, causal.

    Devuelve el cierre de la vela de 1m en/tras el timestamp; si el horizonte
    cae tras el cierre, usa la ultima vela de la sesion. None si no hay datos.
    """
    day = ts.astimezone(NY).date()
    try:
        bars = provider.bars(symbol, day, day, "1m")
    except Exception:
        return None
    if bars.empty:
        return None
    idx = bars.index  # UTC, tz-aware
    at_or_after = bars[idx >= ts]
    row = at_or_after.iloc[0] if not at_or_after.empty else bars.iloc[-1]
    return float(row["close"])


def resolve_agent_alerts(now=None) -> list:
    """Cierra las alertas del agente cuyo horizonte ya vencio. Devuelve las
    cerradas."""
    from django.utils import timezone

    from ..data import get_provider
    from ..models import Alert

    now = now or timezone.now()
    pending = Alert.objects.filter(
        source=Alert.Source.AGENT, status=Alert.Status.PENDING,
        scheduled_exit_ts__isnull=False, scheduled_exit_ts__lte=now,
    )
    if not pending.exists():
        return []

    provider = get_provider()
    closed = []
    for a in pending:
        entry = a.underlying_at_signal
        if entry is None:
            entry = (a.meta or {}).get("entry_price")
        entry = float(entry) if entry is not None else None
        if not entry:
            # Sin precio de entrada no hay como puntuar: la marcamos error.
            a.status = Alert.Status.ERROR
            a.exit_reason = "sin_entrada"
            a.save(update_fields=["status", "exit_reason", "updated_at"])
            continue

        exit_price = _price_at(provider, a.symbol, a.scheduled_exit_ts)
        if exit_price is None:
            continue  # aun no hay dato; se reintenta en la proxima pasada

        move_pct = (exit_price - entry) / entry * 100
        ret = move_pct if a.direction == Alert.Direction.CALL else -move_pct

        a.status = Alert.Status.CLOSED
        a.exit_ts = a.scheduled_exit_ts
        a.exit_reason = "horizonte"
        a.net_pct = round(ret, 2)
        meta = dict(a.meta or {})
        meta.update({"exit_price": round(exit_price, 2),
                     "move_pct": round(move_pct, 2),
                     "return_pct": round(ret, 2),
                     "win": ret > 0})
        a.meta = meta
        a.save(update_fields=[
            "status", "exit_ts", "exit_reason", "net_pct", "meta", "updated_at"])
        closed.append(a)
    return closed

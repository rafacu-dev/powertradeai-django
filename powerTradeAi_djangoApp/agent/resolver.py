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


def _walk_target_stop(direction, entry, seg, target_pct, stop_pct):
    """Recorre las velas de ``seg`` buscando el primer toque de objetivo o stop.

    Devuelve (reason, exit_price, exit_ts) o None si no se toca ninguno. Si en
    la misma vela se tocan ambos, asume el STOP (conservador, sin over-fit)."""
    if not target_pct and not stop_pct:
        return None
    is_call = direction == "CALL"
    tgt = stp = None
    if target_pct:
        tgt = entry * (1 + target_pct / 100) if is_call else entry * (1 - target_pct / 100)
    if stop_pct:
        stp = entry * (1 - stop_pct / 100) if is_call else entry * (1 + stop_pct / 100)

    for ts, row in seg.iterrows():
        hi, lo = float(row["high"]), float(row["low"])
        if is_call:
            stop_hit = stp is not None and lo <= stp
            tgt_hit = tgt is not None and hi >= tgt
        else:
            stop_hit = stp is not None and hi >= stp
            tgt_hit = tgt is not None and lo <= tgt
        if stop_hit and tgt_hit:
            return ("stop", stp, ts)
        if stop_hit:
            return ("stop", stp, ts)
        if tgt_hit:
            return ("target", tgt, ts)
    return None


def resolve_agent_alerts(now=None, source=None, force=False) -> list:
    """Cierra las alertas del agente por objetivo/stop (lo que ocurra primero)
    o, si no se tocan, al vencer el horizonte. Devuelve las cerradas.

    ``source`` acota a alertas en vivo (``agent``) o de entrenamiento
    (``agent_train``); ``now`` es el reloj (el as_of en entrenamiento).
    ``force``: liquida TODA posicion abierta al precio de ``now`` aunque su
    horizonte no haya vencido (cierre de sesion)."""
    from django.utils import timezone

    from ..data import get_provider
    from ..models import Alert

    now = now or timezone.now()
    source = source or Alert.Source.AGENT
    pending = list(Alert.objects.filter(
        source=source, status=Alert.Status.PENDING,
        entry_ts__isnull=False, scheduled_exit_ts__isnull=False,
    ))
    if not pending:
        return []

    provider = get_provider()
    bars_cache: dict = {}
    closed = []
    for a in pending:
        meta = dict(a.meta or {})
        target_pct = meta.get("target_pct")
        stop_pct = meta.get("stop_pct")

        # ── Ruta OPCION: target/stop sobre la PRIMA del contrato ──
        if a.occ_symbol and a.entry_ask:
            entry_ask = float(a.entry_ask)
            window_end = min(a.scheduled_exit_ts, now)
            try:
                series = provider.option_quotes(
                    a.occ_symbol, a.entry_ts, window_end, interval="1m")
            except Exception:
                series = None

            outcome = _walk_option_premium(series, entry_ask, target_pct, stop_pct)
            if outcome is None:
                if now < a.scheduled_exit_ts and not force:
                    continue  # sigue viva
                # Horizonte o cierre forzado: salir al ultimo bid conocido.
                exit_bid = _last_bid(series)
                if exit_bid is None:
                    exit_bid = _option_bid(provider, a.occ_symbol, window_end)
                if exit_bid is None:
                    continue
                reason = ("cierre_sesion"
                          if force and now < a.scheduled_exit_ts else "horizonte")
                exit_ts = window_end
            else:
                reason, exit_bid, exit_ts = outcome

            n = a.contracts or 1
            opt_ret = (exit_bid - entry_ask) / entry_ask * 100
            net_d = (exit_bid - entry_ask) * 100 * n - float(a.commission) * n
            a.status = Alert.Status.CLOSED
            a.exit_ts = exit_ts
            a.exit_reason = reason
            a.exit_premium = round(exit_bid, 4)
            a.net_pct = round(opt_ret, 2)
            a.net_dollars = round(net_d, 2)
            meta.update({"exit_premium": round(exit_bid, 4),
                         "option_return_pct": round(opt_ret, 2),
                         "net_dollars": round(net_d, 2),
                         "win": opt_ret > 0, "exit_reason": reason})
            a.meta = meta
            a.save(update_fields=[
                "status", "exit_ts", "exit_reason", "exit_premium", "net_pct",
                "net_dollars", "meta", "updated_at"])
            closed.append(a)
            continue

        # ── Ruta LEGACY (sin contrato): target/stop sobre el subyacente ──
        entry = a.underlying_at_signal or meta.get("entry_price")
        entry = float(entry) if entry is not None else None
        if not entry:
            a.status = Alert.Status.ERROR
            a.exit_reason = "sin_entrada"
            a.save(update_fields=["status", "exit_reason", "updated_at"])
            continue
        key = (a.symbol, a.session_date)
        if key not in bars_cache:
            try:
                bars_cache[key] = provider.bars(
                    a.symbol, a.session_date, a.session_date, "1m")
            except Exception:
                bars_cache[key] = None
        bars = bars_cache[key]
        if bars is None or bars.empty:
            continue
        window_end = min(a.scheduled_exit_ts, now)
        seg = bars[(bars.index >= a.entry_ts) & (bars.index <= window_end)]
        outcome = _walk_target_stop(a.direction, entry, seg, target_pct, stop_pct)
        if outcome is None:
            if now < a.scheduled_exit_ts and not force:
                continue
            close_ts = min(a.scheduled_exit_ts, now)
            exit_price = _price_at(provider, a.symbol, close_ts)
            if exit_price is None:
                continue
            reason = ("cierre_sesion"
                      if force and now < a.scheduled_exit_ts else "horizonte")
            exit_ts = close_ts
        else:
            reason, exit_price, exit_ts = outcome
        move_pct = (exit_price - entry) / entry * 100
        und_ret = move_pct if a.direction == Alert.Direction.CALL else -move_pct
        a.status = Alert.Status.CLOSED
        a.exit_ts = exit_ts
        a.exit_reason = reason
        a.net_pct = round(und_ret, 2)
        meta.update({"exit_price": round(exit_price, 2), "move_pct": round(move_pct, 2),
                     "underlying_return_pct": round(und_ret, 2),
                     "win": und_ret > 0, "exit_reason": reason})
        a.meta = meta
        a.save(update_fields=["status", "exit_ts", "exit_reason", "net_pct",
                              "meta", "updated_at"])
        closed.append(a)
    return closed


def _walk_option_premium(series, entry_ask, target_pct, stop_pct):
    """Recorre la serie de primas buscando el primer toque de objetivo o stop
    SOBRE LA PRIMA (bid). Objetivo = subir target_pct%; stop = caer stop_pct%.
    Si ambos en la misma vela, asume el STOP (conservador). Devuelve
    (reason, exit_bid, ts) o None."""
    if series is None or getattr(series, "empty", True):
        return None
    if not target_pct and not stop_pct:
        return None
    tgt = entry_ask * (1 + target_pct / 100) if target_pct else None
    stp = entry_ask * (1 - stop_pct / 100) if stop_pct else None
    for ts, row in series.iterrows():
        bid = float(row.get("bid", 0) or 0)
        if bid <= 0:
            continue
        stop_hit = stp is not None and bid <= stp
        tgt_hit = tgt is not None and bid >= tgt
        if stop_hit:
            return ("stop", bid, ts)
        if tgt_hit:
            return ("target", bid, ts)
    return None


def _last_bid(series):
    if series is None or getattr(series, "empty", True):
        return None
    valid = series[series["bid"] > 0]
    return float(valid["bid"].iloc[-1]) if not valid.empty else None


def _option_bid(provider, occ, at):
    """Bid del contrato en ``at`` (lo que cobrarias al vender). None si no hay."""
    try:
        q = provider.option_quote(occ, at=at)
    except Exception:
        return None
    if q is None:
        return None
    bid = getattr(q, "bid", None)
    return float(bid) if bid else None

"""Escaneo y resolucion.

``scan_once`` evalua las reglas activas y abre alertas. ``resolve_pending``
cierra las que ya vencieron o fueron invalidadas. Las dos son idempotentes: si
el worker se reinicia a media sesion, volver a llamarlas no duplica ni inventa.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from ..data import get_provider
from ..models import Alert, ScanRun, Strategy
from ..strategies import ScanContext, get_strategy_class
from .session import NY, now_ny, session_close

log = logging.getLogger(__name__)


def _context(provider, symbol: str, day: date, moment: datetime, cache: dict,
             hist_cache: dict | None = None):
    """Un solo fetch de velas por simbolo y pasada.

    El fallo tambien se cachea y se re-lanza: si el proveedor esta caido, la
    primera regla del simbolo se lleva el error y las demas no repiten la
    llamada. Sin esto, varias reglas sobre el mismo simbolo repiten la peticion.

    ``hist_cache`` comparte ademas el HISTORIAL entre las reglas de un mismo
    simbolo. Sin el, cada regla creaba un ScanContext con su cache vacia y
    volvia a pedir los mismos dias: con dos reglas por simbolo eran el doble de
    peticiones por pasada, y crece linealmente al ampliar el universo.
    """
    if symbol not in cache:
        try:
            cache[symbol] = provider.bars_1m(symbol, day)
        except Exception as exc:
            cache[symbol] = exc
    cached = cache[symbol]
    if isinstance(cached, Exception):
        raise cached
    if hist_cache is None:
        hist_cache = {}
    return ScanContext(
        provider=provider, symbol=symbol, session_date=day,
        now=moment, bars=cached,
        _history_cache=hist_cache.setdefault(symbol, {}),
    )


def _build(strategy_row: Strategy):
    cls = get_strategy_class(strategy_row.strategy_id)
    return cls(strategy_row.params)


def dry_run(moment: datetime | None = None, provider=None) -> list[dict]:
    """Evalua las reglas activas SIN escribir nada. Devuelve lo que dispararia.

    Es el paso previo a soltar el worker: permite ver que reglas encuentran
    senal y con que contrato, sin crear alertas ni ScanRun.
    """
    # Si se pide un instante explicito, es una reconstruccion del pasado.
    replay = moment is not None
    moment = (moment or now_ny()).astimezone(NY)
    day = moment.date()
    provider = provider or get_provider()
    bars_cache: dict[str, object] = {}
    hist_cache: dict[str, dict] = {}
    out: list[dict] = []

    for row in Strategy.objects.filter(enabled=True):
        entry: dict = {"strategy_id": row.strategy_id, "symbol": row.symbol}
        try:
            ctx = _context(provider, row.symbol, day, moment, bars_cache,
                           hist_cache)
            signal = _build(row).evaluate(ctx)
        except Exception as exc:
            out.append({**entry, "status": "error",
                        "detail": f"{type(exc).__name__}: {exc}"})
            continue

        if signal is None:
            out.append({**entry, "status": "sin_senal"})
            continue

        entry.update(direction=signal.direction, signal_ts=signal.signal_ts,
                     underlying=signal.underlying)
        try:
            # En replay (``--at``) se piden quotes del instante de la señal: el
            # snapshot en vivo de un contrato ya vencido no existe.
            occ, _, strike, quote = _build(row).select_contract(
                ctx, signal, at=signal.signal_ts if replay else None)
        except Exception as exc:
            out.append({**entry, "status": "error_contrato",
                        "detail": f"{type(exc).__name__}: {exc}"})
            continue

        if occ is None:
            out.append({**entry, "status": "sin_contrato",
                        "detail": "ningun strike paso los filtros"})
        else:
            out.append({**entry, "status": "dispararia", "occ": occ,
                        "strike": strike, "bid": quote.bid, "ask": quote.ask,
                        "coste": quote.ask * 100 * row.contracts})
    return out


def scan_once(moment: datetime | None = None, provider=None,
              history_cache: dict | None = None) -> ScanRun:
    """Una pasada completa: abre lo que dispare, cierra lo que toque."""
    moment = (moment or now_ny()).astimezone(NY)
    day = moment.date()
    provider = provider or get_provider()
    run = ScanRun.objects.create(started_at=timezone.now())
    bars_cache: dict[str, object] = {}
    hist_cache = history_cache if history_cache is not None else {}

    try:
        # Gestionar riesgo va antes de buscar entradas. Un fallo posterior de un
        # ticker no debe impedir que se cierren posiciones ya abiertas.
        closed = resolve_pending(moment=moment, provider=provider,
                                 bars_cache=bars_cache)

        rows = list(Strategy.objects.filter(enabled=True))
        # Agrupar por simbolo mantiene el cache de velas util.
        by_symbol = defaultdict(list)
        for row in rows:
            by_symbol[row.symbol].append(row)

        created = 0
        symbol_errors = []
        for symbol, strategy_rows in by_symbol.items():
            try:
                ctx = _context(provider, symbol, day, moment, bars_cache,
                               hist_cache)
            except Exception as exc:
                log.exception("datos de %s fallaron; continua el resto", symbol)
                symbol_errors.append(f"{symbol}:{type(exc).__name__}")
                continue
            for strategy_row in strategy_rows:
                if _open_alert(strategy_row, ctx):
                    created += 1

        run.strategies_evaluated = len(rows)
        run.alerts_created = created
        run.alerts_closed = closed
        run.ok = True
        if symbol_errors:
            run.error = "errores aislados: " + ", ".join(symbol_errors)
    except Exception as exc:
        log.exception("scan_once fallo")
        run.ok = False
        run.error = f"{type(exc).__name__}: {exc}"
    finally:
        run.finished_at = timezone.now()
        run.save()
    return run


def _open_alert(strategy_row: Strategy, ctx: ScanContext) -> bool:
    """Evalua una regla y abre la alerta si dispara. True si la creo."""
    # Una regla dispara una vez por sesion: si ya hay alerta, no se reevalua.
    if Alert.objects.filter(
        strategy=strategy_row, session_date=ctx.session_date,
        source=Alert.Source.LIVE,
    ).exists():
        return False

    strategy = _build(strategy_row)
    try:
        signal = strategy.evaluate(ctx)
    except Exception:
        log.exception("regla %s fallo al evaluar", strategy_row.strategy_id)
        return False
    if signal is None:
        return False

    try:
        occ, expiration, strike, quote = strategy.select_contract(ctx, signal)
    except Exception:
        log.exception("regla %s fallo al seleccionar contrato",
                      strategy_row.strategy_id)
        return False
    if occ is None:
        log.warning(
            "%s disparo %s pero no hay contrato con quote viva; no se registra",
            strategy_row.strategy_id, signal.direction)
        return False

    # Un unico reloj para la entrada. Si la quote no trae timestamp se usa el
    # instante del escaneo, NUNCA el reloj de pared: en un replay o en un
    # reproceso, ``timezone.now()`` situaria la entrada fuera de la sesion y la
    # invalidacion no encontraria ninguna vela posterior.
    entry_ts = quote.ts or ctx.now

    try:
        with transaction.atomic():
            Alert.objects.create(
                strategy=strategy_row,
                rule_version=strategy_row.rule_version,
                symbol=strategy_row.symbol,
                session_date=ctx.session_date,
                direction=signal.direction,
                evaluation_version=(
                    "investep_v2" if signal.meta.get("rama") else "legacy_v1"),
                status=Alert.Status.PENDING,
                signal_ts=signal.signal_ts,
                underlying_at_signal=Decimal(str(signal.underlying)),
                occ_symbol=occ,
                expiration=expiration,
                strike=Decimal(str(strike)),
                contracts=strategy_row.contracts,
                commission=strategy_row.commission,
                entry_ts=entry_ts,
                entry_bid=Decimal(str(quote.bid)),
                entry_ask=Decimal(str(quote.ask)),
                # Se paga el ASK: misma convencion que el replay causal.
                entry_premium=Decimal(str(quote.ask)),
                scheduled_exit_ts=strategy.scheduled_exit(entry_ts),
                meta=signal.meta,
                academy_strategy=(
                    strategy_row.strategy_id.split("_")[1]
                    if any(marker in strategy_row.strategy_id
                           for marker in ("_E01_", "_E02_"))
                    else ""),
                strategy_branch=str(signal.meta.get("rama", "")),
            )
    except IntegrityError:
        # Otra pasada gano la carrera. No es un error.
        return False

    log.info("%s abrio %s %s @ %.2f",
             strategy_row.strategy_id, signal.direction, occ, quote.ask)
    return True


def resolve_pending(moment: datetime | None = None, provider=None,
                    bars_cache: dict | None = None) -> int:
    """Cierra las alertas vivas que ya vencieron o fueron invalidadas."""
    moment = (moment or now_ny()).astimezone(NY)
    provider = provider or get_provider()
    bars_cache = bars_cache if bars_cache is not None else {}
    closed = 0

    pending = Alert.objects.filter(
        status=Alert.Status.PENDING,
        source=Alert.Source.LIVE,
    ).select_related("strategy")
    for alert in pending:
        try:
            if _resolve_one(alert, moment, provider, bars_cache):
                closed += 1
        except Exception:
            log.exception("fallo al resolver la alerta %s", alert.pk)
    return closed


def _resolve_one(alert: Alert, moment: datetime, provider, bars_cache: dict) -> bool:
    strategy = _build(alert.strategy)
    ctx = _context(provider, alert.symbol, alert.session_date, moment, bars_cache)

    # 1) Salida anticipada propia de la regla (p.ej. regreso al rango).
    decision = strategy.check_exit(ctx, alert)
    exit_at, reason = None, ""
    if decision.should_exit:
        exit_at, reason = decision.at or moment, decision.reason

    # 2) Salida por reloj.
    elif alert.scheduled_exit_ts and moment >= alert.scheduled_exit_ts:
        exit_at, reason = alert.scheduled_exit_ts, "time_exit"

    # 3) Cierre de sesion: nunca se deja una alerta viva de un dia a otro.
    elif moment.date() > alert.session_date or (
        moment.time() >= session_close(alert.session_date)
    ):
        exit_at = datetime.combine(
            alert.session_date, session_close(alert.session_date), tzinfo=NY)
        reason = "session_close"

    if exit_at is None:
        return False

    quote = provider.option_quote(alert.occ_symbol, at=exit_at)
    if quote is None or quote.bid <= 0:
        # Sin quote utilizable no se inventa un cierre. Se marca para revisar
        # y se deja constancia: un P&L fabricado es peor que un hueco.
        if moment.date() > alert.session_date:
            alert.status = Alert.Status.EXPIRED
            alert.exit_reason = f"{reason}:sin_quote"
            alert.save(update_fields=["status", "exit_reason", "updated_at"])
            return True
        return False
    if alert.academy_strategy:
        from django.conf import settings

        from ..strategies.gates import validate_quote

        config = getattr(settings, "POWERTRADEAI", {})
        checked = validate_quote(
            quote,
            as_of=exit_at,
            max_spread_pct=float(config.get("MAX_OPTION_SPREAD_PCT", 5.0)),
            allow_after_seconds=float(config.get(
                "MAX_HISTORICAL_OPTION_QUOTE_DELAY_SECONDS", 90)),
        )
        if checked["status"] != "VALID":
            log.warning(
                "quote de salida rechazada para alerta %s: %s",
                alert.pk, checked.get("blocker"))
            return False

    # Se cobra el BID: misma convencion que el replay causal.
    alert.close(exit_premium=quote.bid, exit_ts=exit_at, reason=reason)
    log.info("%s cerro por %s: %s$ (%s%%)",
             alert.strategy.strategy_id, reason, alert.net_dollars, alert.net_pct)
    return True

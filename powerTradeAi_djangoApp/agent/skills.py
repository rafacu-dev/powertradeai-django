"""Skills del agente: las herramientas que puede consultar.

Cada skill se registra con ``@skill`` y expone: nombre, descripcion y un esquema
JSON de parametros. El runner las traduce al formato de 'tools' de la API de
OpenAI y ejecuta la que el modelo pida. Anadir una capacidad nueva = anadir una
funcion con ``@skill``; el agente la ve automaticamente.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")


@dataclass
class Skill:
    name: str
    description: str
    parameters: dict
    func: Callable


SKILLS: dict[str, Skill] = {}


def skill(name: str, description: str, parameters: dict):
    def deco(func: Callable) -> Callable:
        SKILLS[name] = Skill(name, description, parameters, func)
        return func
    return deco


def tool_schemas() -> list[dict]:
    """Las skills en el formato ``tools`` de la API de OpenAI."""
    return [
        {
            "type": "function",
            "function": {
                "name": s.name,
                "description": s.description,
                "parameters": s.parameters,
            },
        }
        for s in SKILLS.values()
    ]


# ── Helpers de datos ────────────────────────────────────────────────

def _provider():
    from ..data import get_provider
    return get_provider()


def _bollinger_and_mas(closes, period=20, k=2):
    import pandas as pd  # noqa: F401
    n = len(closes)
    out = {}
    if n >= period:
        window = closes.iloc[-period:]
        mid = float(window.mean())
        std = float(window.std(ddof=0))
        out["bollinger"] = {
            "upper": round(mid + k * std, 2),
            "middle": round(mid, 2),
            "lower": round(mid - k * std, 2),
        }
    for p in (9, 20, 50, 100, 200):
        if n >= p:
            out[f"ma{p}"] = round(float(closes.iloc[-p:].mean()), 2)
    return out


# ── Skills de mercado ───────────────────────────────────────────────

@skill(
    "get_market_data",
    "Datos recientes del subyacente: ultimo precio, ultimas velas OHLC y las "
    "medias moviles y bandas de Bollinger calculadas sobre el timeframe pedido.",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Ticker, p.ej. SPY."},
            "timeframe": {"type": "string", "enum": ["15m", "1h", "1d"],
                          "description": "Temporalidad de las velas."},
            "lookback_days": {"type": "integer",
                              "description": "Dias hacia atras (max 60)."},
        },
        "required": ["symbol"],
    },
)
def get_market_data(ctx, symbol: str, timeframe: str = "15m",
                    lookback_days: int = 15):
    provider = _provider()
    end = datetime.now(NY).date()
    start = end - timedelta(days=min(int(lookback_days), 60))
    bars = provider.bars(symbol.upper(), start, end, timeframe)
    if bars.empty:
        return {"symbol": symbol.upper(), "error": "sin datos"}
    closes = bars["close"]
    last = bars.iloc[-1]
    tail = [
        {
            "t": ts.tz_convert(NY).strftime("%Y-%m-%d %H:%M"),
            "o": round(float(r["open"]), 2), "h": round(float(r["high"]), 2),
            "l": round(float(r["low"]), 2), "c": round(float(r["close"]), 2),
        }
        for ts, r in bars.tail(8).iterrows()
    ]
    result = {
        "symbol": symbol.upper(),
        "timeframe": timeframe,
        "last_close": round(float(last["close"]), 2),
        "recent_bars": tail,
    }
    result.update(_bollinger_and_mas(closes))
    try:
        result["last_price"] = round(float(provider.latest_price(symbol.upper())), 2)
    except Exception:
        pass
    return result


@skill(
    "scan_bollinger",
    "Escanea una lista de activos y devuelve, para cada uno, si el precio esta "
    "por encima o por debajo de sus bandas de Bollinger de 15m y la tendencia "
    "en 1h (MA20 vs MA40).",
    {
        "type": "object",
        "properties": {
            "symbols": {
                "type": "array", "items": {"type": "string"},
                "description": "Tickers a escanear.",
            },
        },
        "required": ["symbols"],
    },
)
def scan_bollinger(ctx, symbols: list[str]):
    provider = _provider()
    end = datetime.now(NY).date()
    rows = []
    for symbol in symbols:
        sym = symbol.upper()
        try:
            bars = provider.bars(sym, end - timedelta(days=15), end, "15m")
            h1 = provider.bars(sym, end - timedelta(days=40), end, "1h")
        except Exception as exc:
            rows.append({"symbol": sym, "error": str(exc)})
            continue
        if bars.empty or len(bars) < 20:
            rows.append({"symbol": sym, "error": "sin datos"})
            continue
        closes = bars["close"]
        bb = _bollinger_and_mas(closes).get("bollinger")
        try:
            price = float(provider.latest_price(sym))
        except Exception:
            price = float(closes.iloc[-1])
        status = "dentro"
        if bb and price > bb["upper"]:
            status = "sobre_banda_superior"
        elif bb and price < bb["lower"]:
            status = "bajo_banda_inferior"
        trend = "n/d"
        if not h1.empty and len(h1) >= 40:
            ma20 = float(h1["close"].iloc[-20:].mean())
            ma40 = float(h1["close"].iloc[-40:].mean())
            trend = "alcista" if ma20 > ma40 else "bajista" if ma20 < ma40 else "plano"
        rows.append({
            "symbol": sym, "price": round(price, 2),
            "bollinger": bb, "status": status, "trend_1h": trend,
        })
    return {"scanned": rows}


@skill(
    "get_option_quote",
    "Quote de un contrato de opciones cercano al dinero (ATM) para el activo, "
    "eligiendo la expiracion mas proxima segun los dias al vencimiento pedidos.",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "right": {"type": "string", "enum": ["CALL", "PUT"]},
            "dte": {"type": "integer",
                    "description": "Dias al vencimiento objetivo (0 = mismo dia)."},
        },
        "required": ["symbol", "right"],
    },
)
def get_option_quote(ctx, symbol: str, right: str, dte: int = 0):
    from ..data import candidate_expirations, occ_symbol
    provider = _provider()
    sym = symbol.upper()
    try:
        spot = float(provider.latest_price(sym))
    except Exception as exc:
        return {"error": f"sin precio del subyacente: {exc}"}
    strike = round(spot)
    today = datetime.now(NY).date()
    exps = candidate_expirations(today, max_dte=max(int(dte) + 5, 7))
    if not exps:
        return {"error": "sin expiraciones candidatas"}
    target = min(exps, key=lambda e: abs((e - today).days - int(dte)))
    occ = occ_symbol(sym, target, right, strike)
    try:
        q = provider.option_quote(occ)
    except Exception as exc:
        return {"error": f"sin quote: {exc}", "occ": occ}
    if q is None:
        return {"error": "quote vacia", "occ": occ}
    return {
        "occ": occ, "expiration": str(target), "strike": strike, "right": right,
        "bid": getattr(q, "bid", None), "ask": getattr(q, "ask", None),
    }


# ── Skills de memoria y continuidad ─────────────────────────────────

@skill(
    "get_prior_analysis",
    "Tu propio analisis previo sobre un activo, para dar continuidad y no "
    "empezar de cero. Devuelve las ultimas entradas mas recientes primero.",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "limit": {"type": "integer", "description": "Cuantas entradas (max 5)."},
        },
        "required": ["symbol"],
    },
)
def get_prior_analysis(ctx, symbol: str, limit: int = 3):
    from ..models import AgentAnalysis
    qs = AgentAnalysis.objects.filter(symbol=symbol.upper())[: min(int(limit), 5)]
    return {
        "symbol": symbol.upper(),
        "prior": [
            {
                "when": a.created_at.astimezone(NY).strftime("%Y-%m-%d %H:%M"),
                "stance": a.stance, "analysis": a.analysis,
            }
            for a in qs
        ],
    }


@skill(
    "save_analysis",
    "Guarda tu analisis actual sobre un activo para futuras corridas. Usalo "
    "para dejar constancia de tu vision aunque no lances una alerta.",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "analysis": {"type": "string",
                         "description": "Tu conclusion razonada sobre el activo."},
            "stance": {"type": "string",
                       "enum": ["alcista", "bajista", "neutral", "observando"]},
        },
        "required": ["symbol", "analysis", "stance"],
    },
)
def save_analysis(ctx, symbol: str, analysis: str, stance: str = "neutral"):
    from ..models import AgentAnalysis
    AgentAnalysis.objects.create(
        symbol=symbol.upper(), analysis=analysis, stance=stance,
        agent_run=ctx["run"],
    )
    return {"saved": True, "symbol": symbol.upper()}


@skill(
    "create_alert",
    "Registra una alerta marcada como generada por el agente: es una PREDICCION "
    "con fecha de vencimiento que luego se puntua sola. Indica el horizonte en "
    "minutos (cuanto vale tu tesis). Aparece en el dashboard con tu razonamiento. "
    "Usala solo cuando tengas una tesis clara y accionable.",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "direction": {"type": "string", "enum": ["CALL", "PUT"]},
            "thesis": {"type": "string",
                       "description": "Por que lanzas la alerta, en breve."},
            "horizon_minutes": {"type": "integer",
                                "description": "Cuanto tiempo vale tu tesis "
                                               "(def. 120; se acota al cierre)."},
        },
        "required": ["symbol", "direction", "thesis"],
    },
)
def create_alert(ctx, symbol: str, direction: str, thesis: str,
                 horizon_minutes: int = 120):
    from django.utils import timezone

    from ..models import Alert, Strategy
    sym = symbol.upper()
    provider = _provider()
    try:
        spot = float(provider.latest_price(sym))
    except Exception:
        spot = None

    strategy, _ = Strategy.objects.get_or_create(
        strategy_id=f"AGENT:{sym}",
        defaults={
            "name": f"Agente {sym}", "symbol": sym,
            "rule_version": "agent_v1", "enabled": False,
        },
    )
    now = timezone.now()
    today = now.astimezone(NY).date()
    horizon = max(int(horizon_minutes or 120), 5)
    # El horizonte se acota al cierre de la sesion (16:00 NY).
    close_dt = datetime.combine(today, datetime(2000, 1, 1, 16, 0).time(),
                                tzinfo=NY)
    exit_at = min(now + timedelta(minutes=horizon), close_dt)

    alert, created = Alert.objects.update_or_create(
        strategy=strategy, session_date=today,
        direction=direction, source=Alert.Source.AGENT,
        defaults={
            "rule_version": "agent_v1", "symbol": sym,
            "status": Alert.Status.PENDING, "signal_ts": now,
            "detected_at": now, "entry_ts": now,
            "scheduled_exit_ts": exit_at, "agent_run": ctx["run"],
            "underlying_at_signal": spot,
            "meta": {"thesis": thesis, "by": "agent",
                     "entry_price": spot, "horizon_minutes": horizon},
        },
    )
    return {"alert_id": alert.id, "created": created, "symbol": sym,
            "direction": direction, "entry_price": spot,
            "resolves_at": exit_at.astimezone(NY).strftime("%H:%M")}


# ── Skills de day-trader: indicadores, historicos, backtest, notas ──

def _rsi(closes, period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    delta = closes.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, float("nan"))
    rsi = 100 - 100 / (1 + rs)
    val = rsi.iloc[-1]
    return round(float(val), 1) if val == val else None  # nan check


@skill(
    "get_intraday_stats",
    "Radar intradia del activo: apertura, precio actual, rango del dia y donde "
    "esta dentro de el, gap contra el cierre previo, VWAP, ATR(14) diario y "
    "RSI(14) en 15m. Lo esencial para decidir de day-trader.",
    {
        "type": "object",
        "properties": {"symbol": {"type": "string"}},
        "required": ["symbol"],
    },
)
def get_intraday_stats(ctx, symbol: str):
    import pandas as pd  # noqa: F401
    provider = _provider()
    sym = symbol.upper()
    end = datetime.now(NY).date()

    daily = provider.bars(sym, end - timedelta(days=40), end, "1d")
    if daily.empty:
        return {"symbol": sym, "error": "sin datos diarios"}
    ddates = daily.index.tz_convert(NY).date
    today_daily = daily[ddates == end]
    prev = daily[ddates < end]
    prev_close = float(prev["close"].iloc[-1]) if not prev.empty else None

    # ATR(14) diario.
    atr = None
    if len(daily) >= 15:
        h, l, c = daily["high"], daily["low"], daily["close"]
        pc = c.shift(1)
        tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
        atr = round(float(tr.rolling(14).mean().iloc[-1]), 2)

    bars15 = provider.bars(sym, end - timedelta(days=5), end, "15m")
    ny = bars15.index.tz_convert(NY)
    today15 = bars15[(ny.date == end) &
                     (ny.time >= datetime(2000, 1, 1, 9, 30).time())]
    try:
        price = float(provider.latest_price(sym))
    except Exception:
        price = float(bars15["close"].iloc[-1]) if not bars15.empty else None

    out = {"symbol": sym, "price": round(price, 2) if price else None,
           "prev_close": round(prev_close, 2) if prev_close else None,
           "atr14_daily": atr, "rsi14_15m": _rsi(bars15["close"]) if not bars15.empty else None}

    if not today15.empty:
        o = float(today15["open"].iloc[0])
        hi = float(today15["high"].max())
        lo = float(today15["low"].min())
        out["open"] = round(o, 2)
        out["day_high"] = round(hi, 2)
        out["day_low"] = round(lo, 2)
        out["day_range_pct"] = round((hi - lo) / o * 100, 2) if o else None
        if hi > lo and price:
            out["pos_in_range_pct"] = round((price - lo) / (hi - lo) * 100, 1)
        if "volume" in today15:
            tp = (today15["high"] + today15["low"] + today15["close"]) / 3
            vol = today15["volume"]
            if vol.sum() > 0:
                out["vwap"] = round(float((tp * vol).sum() / vol.sum()), 2)
        if prev_close and o:
            out["gap_pct"] = round((o - prev_close) / prev_close * 100, 2)
    return out


@skill(
    "get_historical_bars",
    "Historico DIARIO resumido del activo para estudiar su comportamiento: por "
    "cada dia el OHLC, el rango en %% y el gap de apertura. Util para ver "
    "patrones y contexto.",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "days": {"type": "integer", "description": "Dias hacia atras (max 90)."},
        },
        "required": ["symbol"],
    },
)
def get_historical_bars(ctx, symbol: str, days: int = 20):
    provider = _provider()
    sym = symbol.upper()
    end = datetime.now(NY).date()
    daily = provider.bars(sym, end - timedelta(days=min(int(days), 90) + 5), end, "1d")
    if daily.empty:
        return {"symbol": sym, "error": "sin datos"}
    rows, prev_c = [], None
    for ts, r in daily.tail(min(int(days), 90)).iterrows():
        o, h, l, c = (float(r["open"]), float(r["high"]),
                      float(r["low"]), float(r["close"]))
        rows.append({
            "date": ts.tz_convert(NY).strftime("%Y-%m-%d"),
            "o": round(o, 2), "h": round(h, 2), "l": round(l, 2), "c": round(c, 2),
            "range_pct": round((h - l) / o * 100, 2) if o else None,
            "gap_pct": round((o - prev_c) / prev_c * 100, 2) if prev_c else None,
        })
        prev_c = c
    return {"symbol": sym, "days": len(rows), "bars": rows}


@skill(
    "backtest_reversion",
    "Backtest simple de reversion con Bollinger en velas de 15m: entra cuando "
    "el cierre perfora la banda (inferior=long, superior=short) y sale al "
    "volver a la media o tras un maximo de velas. Devuelve n, aciertos y "
    "retorno medio del SUBYACENTE (no de opciones). Es orientativo.",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "days": {"type": "integer", "description": "Dias a testear (max 30)."},
            "max_hold_bars": {"type": "integer",
                              "description": "Maximo de velas 15m en la posicion."},
        },
        "required": ["symbol"],
    },
)
def backtest_reversion(ctx, symbol: str, days: int = 15, max_hold_bars: int = 8):
    provider = _provider()
    sym = symbol.upper()
    end = datetime.now(NY).date()
    bars = provider.bars(sym, end - timedelta(days=min(int(days), 30) + 5), end, "15m")
    ny = bars.index.tz_convert(NY)
    rth = bars[(ny.time >= datetime(2000, 1, 1, 9, 30).time()) &
               (ny.time < datetime(2000, 1, 1, 16, 0).time())]
    closes = rth["close"].reset_index(drop=True)
    if len(closes) < 40:
        return {"symbol": sym, "error": "pocas velas"}
    period, k = 20, 2
    mid = closes.rolling(period).mean()
    std = closes.rolling(period).std(ddof=0)
    upper, lower = mid + k * std, mid - k * std

    trades, i, n = [], period, len(closes)
    hold = int(max_hold_bars)
    while i < n - 1:
        entry = None
        if closes[i] < lower[i]:
            entry = ("long", closes[i])
        elif closes[i] > upper[i]:
            entry = ("short", closes[i])
        if not entry:
            i += 1
            continue
        side, px = entry
        exit_px, j = closes[min(i + hold, n - 1)], i + 1
        while j <= min(i + hold, n - 1):
            if side == "long" and closes[j] >= mid[j]:
                exit_px = closes[j]
                break
            if side == "short" and closes[j] <= mid[j]:
                exit_px = closes[j]
                break
            j += 1
        ret = ((exit_px - px) / px if side == "long"
               else (px - exit_px) / px) * 100
        trades.append(round(ret, 3))
        i = j + 1

    if not trades:
        return {"symbol": sym, "trades": 0, "note": "sin señales en el periodo"}
    wins = sum(1 for t in trades if t > 0)
    return {
        "symbol": sym, "days": min(int(days), 30), "trades": len(trades),
        "win_rate_pct": round(wins / len(trades) * 100, 1),
        "avg_return_pct": round(sum(trades) / len(trades), 3),
        "best_pct": max(trades), "worst_pct": min(trades),
        "note": "retorno del subyacente, orientativo; no incluye opciones ni costes",
    }


@skill(
    "save_note",
    "Guarda una nota en tu cuaderno de day-trader (ideas, patrones, reglas que "
    "quieres recordar), indexada por tema. Persiste entre corridas.",
    {
        "type": "object",
        "properties": {
            "topic": {"type": "string", "description": "Tema, p.ej. 'TSLA' o 'reversion'."},
            "note": {"type": "string"},
        },
        "required": ["topic", "note"],
    },
)
def save_note(ctx, topic: str, note: str):
    from ..models import AgentNote
    AgentNote.objects.create(topic=topic.strip()[:80], note=note, agent_run=ctx["run"])
    return {"saved": True, "topic": topic.strip()[:80]}


@skill(
    "get_my_track_record",
    "Tu expediente real: como te fue con las alertas que YA lanzaste y se "
    "cerraron (acierto direccional del subyacente). Consultalo para ser honesto "
    "contigo mismo y ajustar tu exigencia. Opcional filtrar por activo.",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Opcional."},
        },
    },
)
def get_my_track_record(ctx, symbol: str | None = None):
    from ..models import Alert
    qs = Alert.objects.filter(source=Alert.Source.AGENT,
                              status=Alert.Status.CLOSED)
    if symbol:
        qs = qs.filter(symbol=symbol.upper())
    closed = list(qs.order_by("-signal_ts")[:200])
    n = len(closed)
    if not n:
        return {"closed": 0, "note": "aun no tienes alertas cerradas"}

    def stats(items):
        if not items:
            return None
        rets = [float(a.net_pct or 0) for a in items]
        wins = sum(1 for r in rets if r > 0)
        return {"n": len(items), "win_rate_pct": round(wins / len(items) * 100, 1),
                "avg_return_pct": round(sum(rets) / len(rets), 2),
                "best_pct": round(max(rets), 2), "worst_pct": round(min(rets), 2)}

    calls = [a for a in closed if a.direction == "CALL"]
    puts = [a for a in closed if a.direction == "PUT"]
    recent = [
        {"when": a.signal_ts.astimezone(NY).strftime("%m-%d %H:%M"),
         "symbol": a.symbol, "dir": a.direction,
         "return_pct": float(a.net_pct or 0),
         "thesis": (a.meta or {}).get("thesis", "")[:80]}
        for a in closed[:8]
    ]
    return {"overall": stats(closed), "calls": stats(calls),
            "puts": stats(puts), "recent": recent}


@skill(
    "set_price_trigger",
    "Fija un nivel de precio en el que quieres que te despierten. Cuando el "
    "precio lo toque, el loop te llamara de nuevo con ese contexto para que "
    "decidas. Usalo para vigilar soportes, resistencias o puntos de ruptura sin "
    "tener que estar mirando. Si no indicas direccion, se deduce del precio "
    "actual (arriba si el nivel esta por encima, abajo si esta por debajo).",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "price": {"type": "number", "description": "Nivel a vigilar."},
            "reason": {"type": "string",
                       "description": "Que esperas en ese nivel y que haras."},
            "direction": {"type": "string", "enum": ["above", "below"],
                          "description": "Opcional. Disparar al subir o al bajar."},
        },
        "required": ["symbol", "price", "reason"],
    },
)
def set_price_trigger(ctx, symbol: str, price: float, reason: str,
                      direction: str | None = None):
    from ..models import AgentTrigger
    provider = _provider()
    sym = symbol.upper()
    try:
        ref = float(provider.latest_price(sym))
    except Exception:
        ref = None
    if direction not in ("above", "below"):
        direction = "above" if (ref is None or float(price) >= ref) else "below"
    t = AgentTrigger.objects.create(
        symbol=sym, price=round(float(price), 2), direction=direction,
        reason=reason, ref_price=round(ref, 2) if ref else None,
        agent_run=ctx["run"],
    )
    return {"trigger_id": t.id, "symbol": sym, "price": t.price,
            "direction": direction, "ref_price": t.ref_price}


@skill(
    "list_price_triggers",
    "Lista tus niveles de vigilancia activos para un activo, para no duplicar "
    "ni olvidar los que ya pusiste.",
    {
        "type": "object",
        "properties": {"symbol": {"type": "string"}},
        "required": ["symbol"],
    },
)
def list_price_triggers(ctx, symbol: str):
    from ..models import AgentTrigger
    qs = AgentTrigger.objects.filter(symbol=symbol.upper(), active=True)
    return {
        "symbol": symbol.upper(),
        "triggers": [
            {"id": t.id, "price": float(t.price), "direction": t.direction,
             "reason": t.reason}
            for t in qs
        ],
    }


@skill(
    "cancel_price_trigger",
    "Desactiva un nivel de vigilancia que ya no te interesa, por su id.",
    {
        "type": "object",
        "properties": {"trigger_id": {"type": "integer"}},
        "required": ["trigger_id"],
    },
)
def cancel_price_trigger(ctx, trigger_id: int):
    from ..models import AgentTrigger
    n = AgentTrigger.objects.filter(id=trigger_id, active=True).update(active=False)
    return {"cancelled": bool(n), "trigger_id": trigger_id}


@skill(
    "get_notes",
    "Lee tus notas previas por tema, para no perder tus propias ideas y reglas.",
    {
        "type": "object",
        "properties": {
            "topic": {"type": "string"},
            "limit": {"type": "integer", "description": "Cuantas (max 10)."},
        },
        "required": ["topic"],
    },
)
def get_notes(ctx, topic: str, limit: int = 5):
    from ..models import AgentNote
    qs = AgentNote.objects.filter(topic=topic.strip()[:80])[: min(int(limit), 10)]
    return {
        "topic": topic.strip()[:80],
        "notes": [
            {"when": n.created_at.astimezone(NY).strftime("%Y-%m-%d %H:%M"),
             "note": n.note}
            for n in qs
        ],
    }

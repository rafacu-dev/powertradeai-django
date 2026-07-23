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


# ── Reloj causal (entrenamiento en tiempo pasado) ───────────────────
# Cuando ctx trae ``as_of`` estamos en entrenamiento: NINGUNA skill puede ver
# datos posteriores a ese instante. ``_now``/``_spot``/``_bars_upto`` acotan
# todo a ese reloj; en vivo (``as_of`` None) se comportan como siempre.

def _now(ctx):
    from django.utils import timezone
    return ctx.get("as_of") or timezone.now()


def _is_training(ctx) -> bool:
    return ctx.get("as_of") is not None


def _alert_source(ctx):
    from ..models import Alert
    return Alert.Source.AGENT_TRAIN if _is_training(ctx) else Alert.Source.AGENT


def _mode(ctx) -> str:
    """Aisla la memoria: 'train' en entrenamiento, 'live' en vivo."""
    return "train" if _is_training(ctx) else "live"


def _price_asof(provider, symbol, as_of):
    """Ultimo precio conocido en/antes de ``as_of`` (cierre de la vela de 1m).
    Antes de la apertura, el ultimo cierre diario previo. None si no hay dato."""
    from datetime import timedelta
    day = as_of.astimezone(NY).date()
    try:
        bars = provider.bars(symbol, day, day, "1m")
    except Exception:
        bars = None
    if bars is not None and not bars.empty:
        upto = bars[bars.index <= as_of]
        if not upto.empty:
            return float(upto.iloc[-1]["close"])
    try:
        d = provider.bars(symbol, day - timedelta(days=7), day, "1d")
        prev = d[d.index.tz_convert(NY).date < day]
        if not prev.empty:
            return float(prev["close"].iloc[-1])
    except Exception:
        pass
    return None


def _spot(ctx, provider, symbol):
    as_of = ctx.get("as_of")
    if as_of is None:
        return float(provider.latest_price(symbol))
    px = _price_asof(provider, symbol, as_of)
    if px is None:
        from ..data import MarketDataError
        raise MarketDataError(f"sin precio as-of para {symbol}")
    return px


def _bars_upto(ctx, provider, symbol, start, end, tf):
    """Velas hasta ``end``, truncadas al reloj causal si estamos entrenando."""
    df = provider.bars(symbol, start, end, tf)
    as_of = ctx.get("as_of")
    if as_of is not None and not df.empty:
        df = df[df.index <= as_of]
    return df


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
    sym = symbol.upper()
    end = _now(ctx).date()
    start = end - timedelta(days=min(int(lookback_days), 60))
    bars = _bars_upto(ctx, provider, sym, start, end, timeframe)
    if bars.empty:
        return {"symbol": sym, "error": "sin datos"}
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
        "symbol": sym,
        "timeframe": timeframe,
        "last_close": round(float(last["close"]), 2),
        "recent_bars": tail,
    }
    result.update(_bollinger_and_mas(closes))
    try:
        result["last_price"] = round(_spot(ctx, provider, sym), 2)
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
    end = _now(ctx).date()
    rows = []
    for symbol in symbols:
        sym = symbol.upper()
        try:
            bars = _bars_upto(ctx, provider, sym, end - timedelta(days=15), end, "15m")
            h1 = _bars_upto(ctx, provider, sym, end - timedelta(days=40), end, "1h")
        except Exception as exc:
            rows.append({"symbol": sym, "error": str(exc)})
            continue
        if bars.empty or len(bars) < 20:
            rows.append({"symbol": sym, "error": "sin datos"})
            continue
        closes = bars["close"]
        bb = _bollinger_and_mas(closes).get("bollinger")
        try:
            price = _spot(ctx, provider, sym)
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


def _strike_step(spot: float) -> float:
    return 5.0 if spot >= 200 else 2.5 if spot >= 50 else 1.0


def _pick_expiration(ctx, sym, dte):
    from ..data import candidate_expirations
    today = _now(ctx).date()
    exps = candidate_expirations(today, max_dte=max(int(dte) + 5, 7))
    if not exps:
        return None, None
    target = min(exps, key=lambda e: abs((e - today).days - int(dte)))
    return target, (target - today).days


@skill(
    "get_option_chain",
    "Cadena de opciones: varios strikes alrededor del dinero para el activo, "
    "con su bid/ask y prima, para que ELIJAS el contrato. Devuelve tambien los "
    "dias al vencimiento (DTE): a menos DTE, mas theta (decae rapido). Con esto "
    "decides strike y expiracion segun tu tesis y el riesgo.",
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
def get_option_chain(ctx, symbol: str, right: str, dte: int = 0):
    from ..data import occ_symbol
    provider = _provider()
    sym = symbol.upper()
    try:
        spot = _spot(ctx, provider, sym)
    except Exception as exc:
        return {"error": f"sin precio del subyacente: {exc}"}
    target, real_dte = _pick_expiration(ctx, sym, dte)
    if target is None:
        return {"error": "sin expiraciones candidatas"}
    at = ctx.get("as_of")
    step = _strike_step(spot)
    atm = round(spot / step) * step
    strikes = [atm + i * step for i in range(-2, 3)]
    rows = []
    for k in strikes:
        occ = occ_symbol(sym, target, right, k)
        try:
            q = provider.option_quote(occ, at=at) if at else provider.option_quote(occ)
        except Exception:
            q = None
        if q is None:
            continue
        bid, ask = getattr(q, "bid", None), getattr(q, "ask", None)
        rows.append({
            "strike": k, "bid": bid, "ask": ask,
            "mid": round((bid + ask) / 2, 2) if bid and ask else None,
            "cost_1_contrato": round(ask * 100, 2) if ask else None,
            "moneyness": ("ITM" if (right == "CALL" and k < spot) or
                          (right == "PUT" and k > spot) else
                          "ATM" if abs(k - spot) < step else "OTM"),
        })
    return {"symbol": sym, "right": right, "spot": round(spot, 2),
            "expiration": str(target), "dte": real_dte,
            "theta_aviso": "0 DTE = maximo theta, decae en horas" if real_dte == 0
            else f"{real_dte} DTE",
            "strikes": rows}


@skill(
    "get_account",
    "Estado de tu cuenta (papel): tamano, capital ya desplegado en posiciones "
    "abiertas, disponible, y el riesgo maximo sugerido por operacion. Usalo "
    "para dimensionar cuantos contratos comprar sin arriesgar de mas.",
    {"type": "object", "properties": {}},
)
def get_account(ctx):
    from django.conf import settings

    from ..models import Alert
    cfg = getattr(settings, "POWERTRADEAI", {})
    size = float(cfg.get("PAPER_ACCOUNT", 10000))
    risk_pct = float(cfg.get("RISK_PCT_PER_TRADE", 2.0))
    open_qs = Alert.objects.filter(source=_alert_source(ctx),
                                   status=Alert.Status.PENDING)
    deployed = 0.0
    for a in open_qs:
        cost = (a.meta or {}).get("cost")
        if cost:
            deployed += float(cost)
    return {
        "account_size": round(size, 2),
        "deployed": round(deployed, 2),
        "available": round(size - deployed, 2),
        "risk_pct_per_trade": risk_pct,
        "max_risk_per_trade": round(size * risk_pct / 100, 2),
        "nota": "El maximo que puedes perder en una opcion comprada es la prima "
                "pagada; dimensiona los contratos para no arriesgar mas del "
                "maximo sugerido.",
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
    qs = AgentAnalysis.objects.filter(symbol=symbol.upper(), mode=_mode(ctx))
    as_of = ctx.get("as_of")
    if as_of is not None:
        # Solo lo escrito hasta este instante simulado (sin futuro).
        qs = qs.filter(as_of__isnull=False, as_of__lte=as_of).order_by("-as_of")
    qs = qs[: min(int(limit), 5)]

    def _when(a):
        t = a.as_of or a.created_at
        return t.astimezone(NY).strftime("%Y-%m-%d %H:%M")
    return {
        "symbol": symbol.upper(),
        "prior": [
            {"when": _when(a), "stance": a.stance, "analysis": a.analysis}
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
        agent_run=ctx["run"], mode=_mode(ctx), as_of=ctx.get("as_of"),
    )
    return {"saved": True, "symbol": symbol.upper()}


@skill(
    "create_alert",
    "Compra una OPCION real (CALL o PUT) y registra la operacion. TU eliges el "
    "contrato: strike, dias al vencimiento (dte) y cuantos contratos, segun tu "
    "tesis y la gestion de riesgo (mira get_option_chain y get_account primero). "
    "Se registra la prima de entrada (ask) real de ThetaData; al cerrar se mide "
    "el P&L de la opcion (bid), con su apalancamiento. target/stop son sobre el "
    "movimiento del SUBYACENTE (tu tesis de precio); el resultado es de la opcion.",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "direction": {"type": "string", "enum": ["CALL", "PUT"]},
            "thesis": {"type": "string", "description": "Por que operas, en breve."},
            "strike": {"type": "number",
                       "description": "Strike del contrato (si lo omites, ATM)."},
            "dte": {"type": "integer",
                    "description": "Dias al vencimiento (0 = mismo dia; a menos "
                                   "DTE mas theta)."},
            "contracts": {"type": "integer",
                          "description": "Cuantos contratos (sizing/riesgo)."},
            "horizon_minutes": {"type": "integer",
                                "description": "Cuanto vale tu tesis (def. 120)."},
            "target_pct": {"type": "number",
                           "description": "Objetivo en %% del SUBYACENTE (opcional)."},
            "stop_pct": {"type": "number",
                         "description": "Stop en %% del SUBYACENTE (opcional)."},
        },
        "required": ["symbol", "direction", "thesis"],
    },
)
def create_alert(ctx, symbol: str, direction: str, thesis: str,
                 strike: float | None = None, dte: int = 0, contracts: int = 1,
                 horizon_minutes: int = 120,
                 target_pct: float | None = None,
                 stop_pct: float | None = None):
    from ..data import occ_symbol
    from ..models import Alert, Strategy
    sym = symbol.upper()
    provider = _provider()
    try:
        spot = _spot(ctx, provider, sym)
    except Exception:
        return {"error": "sin precio del subyacente"}

    # Elegir el contrato: strike (ATM si no se da) + expiracion segun dte.
    step = _strike_step(spot)
    if strike is None:
        strike = round(spot / step) * step
    strike = round(float(strike), 2)
    target_exp, real_dte = _pick_expiration(ctx, sym, dte)
    if target_exp is None:
        return {"error": "sin expiraciones candidatas"}
    occ = occ_symbol(sym, target_exp, direction, strike)

    # Prima de entrada REAL (ask) via ThetaData.
    at = ctx.get("as_of")
    try:
        q = provider.option_quote(occ, at=at) if at else provider.option_quote(occ)
    except Exception as exc:
        return {"error": f"sin quote del contrato: {exc}", "occ": occ}
    if q is None or not getattr(q, "ask", None):
        return {"error": "el contrato no tiene quote utilizable; prueba otro "
                         "strike o dte", "occ": occ}
    entry_ask = float(q.ask)
    entry_bid = float(getattr(q, "bid", 0) or 0)
    contracts = max(int(contracts or 1), 1)
    cost = round(entry_ask * 100 * contracts, 2)

    training = _is_training(ctx)
    source = _alert_source(ctx)
    strategy, _ = Strategy.objects.get_or_create(
        strategy_id=f"AGENT:{sym}",
        defaults={"name": f"Agente {sym}", "symbol": sym,
                  "rule_version": "agent_v1", "enabled": False})
    now = _now(ctx)
    today = now.astimezone(NY).date()
    horizon = max(int(horizon_minutes or 120), 5)
    close_dt = datetime.combine(today, datetime(2000, 1, 1, 16, 0).time(),
                                tzinfo=NY)
    exit_at = min(now + timedelta(minutes=horizon), close_dt)
    meta = {"thesis": thesis, "by": "agent", "entry_price": spot,
            "horizon_minutes": horizon, "dte": real_dte, "cost": cost,
            "target_pct": round(float(target_pct), 3) if target_pct else None,
            "stop_pct": round(abs(float(stop_pct)), 3) if stop_pct else None,
            "training": training}
    common = {
        "rule_version": "agent_v1", "symbol": sym,
        "status": Alert.Status.PENDING, "signal_ts": now, "detected_at": now,
        "entry_ts": now, "scheduled_exit_ts": exit_at, "agent_run": ctx["run"],
        "underlying_at_signal": spot, "occ_symbol": occ,
        "expiration": target_exp, "strike": strike, "contracts": contracts,
        "entry_ask": entry_ask, "entry_bid": entry_bid,
        "entry_premium": entry_ask, "meta": meta,
    }
    if training:
        alert = Alert.objects.create(
            strategy=strategy, session_date=today, direction=direction,
            source=source, **common)
        created = True
    else:
        alert, created = Alert.objects.update_or_create(
            strategy=strategy, session_date=today, direction=direction,
            source=source, defaults=common)
    return {"alert_id": alert.id, "created": created, "symbol": sym,
            "direction": direction, "contract": occ, "strike": strike,
            "dte": real_dte, "contracts": contracts,
            "entry_premium": entry_ask, "cost": cost,
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
    end = _now(ctx).date()

    daily = provider.bars(sym, end - timedelta(days=40), end, "1d")
    if daily.empty:
        return {"symbol": sym, "error": "sin datos diarios"}
    ddates = daily.index.tz_convert(NY).date
    # La vela diaria de HOY resume toda la jornada (incluye el futuro): para el
    # ATR y el cierre previo solo se usan dias ya cerrados (< hoy).
    prev = daily[ddates < end]
    prev_close = float(prev["close"].iloc[-1]) if not prev.empty else None

    # ATR(14) diario, sobre dias cerrados.
    atr = None
    if len(prev) >= 15:
        h, l, c = prev["high"], prev["low"], prev["close"]
        pc = c.shift(1)
        tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
        atr = round(float(tr.rolling(14).mean().iloc[-1]), 2)

    bars15 = _bars_upto(ctx, provider, sym, end - timedelta(days=5), end, "15m")
    ny = bars15.index.tz_convert(NY) if not bars15.empty else None
    today15 = (bars15[(ny.date == end) &
                      (ny.time >= datetime(2000, 1, 1, 9, 30).time())]
               if ny is not None else bars15)
    try:
        price = _spot(ctx, provider, sym)
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
    end = _now(ctx).date()
    daily = provider.bars(sym, end - timedelta(days=min(int(days), 90) + 5), end, "1d")
    if daily.empty:
        return {"symbol": sym, "error": "sin datos"}
    # Solo dias ya cerrados: la vela de hoy incluiria el futuro.
    daily = daily[daily.index.tz_convert(NY).date < end]
    if daily.empty:
        return {"symbol": sym, "error": "sin dias previos"}
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
    end = _now(ctx).date()
    bars = _bars_upto(ctx, provider, sym,
                      end - timedelta(days=min(int(days), 30) + 5), end, "15m")
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


def _cluster_levels(prices, tol=0.004):
    """Agrupa precios cercanos en niveles horizontales (soporte/resistencia).
    Un nivel con >=2 toques es relevante."""
    prices = sorted(prices)
    clusters = []
    for p in prices:
        if clusters and abs(p - clusters[-1]["p"]) / clusters[-1]["p"] <= tol:
            c = clusters[-1]
            c["vals"].append(p)
            c["p"] = sum(c["vals"]) / len(c["vals"])
        else:
            clusters.append({"p": p, "vals": [p]})
    return [{"price": round(c["p"], 2), "touches": len(c["vals"])}
            for c in clusters if len(c["vals"]) >= 2]


@skill(
    "get_trendlines",
    "Detecta lineas de tendencia DIAGONALES (resistencia bajista uniendo maximos "
    "descendentes, soporte alcista uniendo minimos ascendentes) y niveles "
    "horizontales de soporte/resistencia. Devuelve donde esta cada linea AHORA y "
    "el precio respecto a ellas, para buscar rechazos, rupturas y puntos de "
    "entrada.",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "timeframe": {"type": "string", "enum": ["15m", "1h", "1d"],
                          "description": "Temporalidad (1h capta la tendencia del dia)."},
            "lookback_days": {"type": "integer", "description": "Dias atras (max 30)."},
        },
        "required": ["symbol"],
    },
)
def get_trendlines(ctx, symbol: str, timeframe: str = "1h",
                   lookback_days: int = 15):
    import numpy as np

    provider = _provider()
    sym = symbol.upper()
    end = _now(ctx).date()
    bars = _bars_upto(ctx, provider, sym,
                      end - timedelta(days=min(int(lookback_days), 30)), end, timeframe)
    if bars.empty or len(bars) < 15:
        return {"symbol": sym, "error": "pocas velas"}

    highs = bars["high"].values.astype(float)
    lows = bars["low"].values.astype(float)
    n = len(bars)
    w = 3
    sh, sl = [], []  # (indice, precio) de swings
    for i in range(w, n - w):
        if highs[i] == highs[i - w:i + w + 1].max():
            sh.append((i, highs[i]))
        if lows[i] == lows[i - w:i + w + 1].min():
            sl.append((i, lows[i]))

    def fit(points, kind):
        if len(points) < 2:
            return None
        xs = np.array([p[0] for p in points], float)
        ys = np.array([p[1] for p in points], float)
        slope, intercept = np.polyfit(xs, ys, 1)
        current = slope * (n - 1) + intercept
        pct_per_bar = slope / current * 100 if current else 0
        if kind == "resistencia":
            direction = "bajista" if slope < 0 else "alcista/plana"
        else:
            direction = "alcista" if slope > 0 else "bajista/plana"
        return {"current_value": round(float(current), 2),
                "slope_per_bar": round(float(slope), 3),
                "pct_por_vela": round(float(pct_per_bar), 3),
                "toques": len(points), "direccion": direction}

    res = fit(sh[-4:], "resistencia")
    sup = fit(sl[-4:], "soporte")
    levels = _cluster_levels([p[1] for p in sh] + [p[1] for p in sl])

    try:
        price = _spot(ctx, provider, sym)
    except Exception:
        price = float(bars["close"].iloc[-1])
    above = sorted([l for l in levels if l["price"] > price], key=lambda x: x["price"])
    below = sorted([l for l in levels if l["price"] < price],
                   key=lambda x: -x["price"])

    def rel(line):
        if not line:
            return None
        d = (price - line["current_value"]) / line["current_value"] * 100
        pos = "por encima" if d > 0.1 else "por debajo" if d < -0.1 else "justo en"
        return f"{pos} ({d:+.2f}%)"

    return {
        "symbol": sym, "timeframe": timeframe, "price": round(price, 2),
        "resistencia_diagonal": res, "precio_vs_resistencia": rel(res),
        "soporte_diagonal": sup, "precio_vs_soporte": rel(sup),
        "resistencia_horizontal_mas_cercana": above[0] if above else None,
        "soporte_horizontal_mas_cercano": below[0] if below else None,
        "niveles_horizontales": levels,
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
    AgentNote.objects.create(
        topic=topic.strip()[:80], note=note, agent_run=ctx["run"],
        mode=_mode(ctx), as_of=ctx.get("as_of"))
    return {"saved": True, "topic": topic.strip()[:80]}


@skill(
    "get_open_positions",
    "Tus posiciones ABIERTAS (alertas aun sin cerrar): entrada, precio actual, "
    "ganancia/perdida no realizada, tu objetivo y stop vigentes y el tiempo en "
    "el trade. Revisalas para gestionarlas. Opcional filtrar por activo.",
    {
        "type": "object",
        "properties": {"symbol": {"type": "string", "description": "Opcional."}},
    },
)
def get_open_positions(ctx, symbol: str | None = None):
    from ..models import Alert
    provider = _provider()
    qs = Alert.objects.filter(source=_alert_source(ctx),
                              status=Alert.Status.PENDING)
    if symbol:
        qs = qs.filter(symbol=symbol.upper())
    as_of = ctx.get("as_of")
    if as_of is not None:
        qs = qs.filter(entry_ts__isnull=False, entry_ts__lte=as_of)
    out = []
    now = _now(ctx)
    at = ctx.get("as_of")
    for a in qs:
        try:
            und = _spot(ctx, provider, a.symbol)
        except Exception:
            und = None
        # P&L NO REALIZADO DE LA OPCION: bid actual del contrato vs prima pagada.
        opt_bid = opt_unreal = None
        if a.occ_symbol and a.entry_ask:
            try:
                q = provider.option_quote(a.occ_symbol, at=at) if at \
                    else provider.option_quote(a.occ_symbol)
                opt_bid = float(getattr(q, "bid", 0) or 0) if q else None
            except Exception:
                opt_bid = None
            if opt_bid:
                opt_unreal = round((opt_bid - float(a.entry_ask)) /
                                   float(a.entry_ask) * 100, 2)
        mins = int((now - a.entry_ts).total_seconds() / 60) if a.entry_ts else None
        meta = a.meta or {}
        out.append({
            "alert_id": a.id, "symbol": a.symbol, "direction": a.direction,
            "contrato": a.occ_symbol, "strike": float(a.strike) if a.strike else None,
            "contratos": a.contracts,
            "prima_entrada": float(a.entry_ask) if a.entry_ask else None,
            "prima_actual_bid": opt_bid, "coste": meta.get("cost"),
            "opcion_unrealized_pct": opt_unreal,   # <- lo que importa
            "subyacente_actual": round(und, 2) if und else None,
            "target_pct": meta.get("target_pct"), "stop_pct": meta.get("stop_pct"),
            "minutes_open": mins, "thesis": meta.get("thesis", ""),
        })
    return {"open": out}


@skill(
    "adjust_position",
    "Ajusta el plan de una posicion ABIERTA sin cerrarla: nuevo objetivo, nuevo "
    "stop (p.ej. moverlo a break-even tras ir a favor) o nuevo horizonte. Solo "
    "los campos que pases cambian.",
    {
        "type": "object",
        "properties": {
            "alert_id": {"type": "integer"},
            "target_pct": {"type": "number"},
            "stop_pct": {"type": "number"},
            "horizon_minutes": {"type": "integer"},
        },
        "required": ["alert_id"],
    },
)
def adjust_position(ctx, alert_id: int, target_pct: float | None = None,
                    stop_pct: float | None = None,
                    horizon_minutes: int | None = None):
    from ..models import Alert
    try:
        a = Alert.objects.get(id=alert_id, source=_alert_source(ctx),
                              status=Alert.Status.PENDING)
    except Alert.DoesNotExist:
        return {"error": "posicion no encontrada o ya cerrada"}
    meta = dict(a.meta or {})
    changed = {}
    if target_pct is not None:
        meta["target_pct"] = round(float(target_pct), 3)
        changed["target_pct"] = meta["target_pct"]
    if stop_pct is not None:
        meta["stop_pct"] = round(abs(float(stop_pct)), 3)
        changed["stop_pct"] = meta["stop_pct"]
    fields = ["meta", "updated_at"]
    if horizon_minutes is not None:
        now = _now(ctx)
        close_dt = datetime.combine(
            now.astimezone(NY).date(),
            datetime(2000, 1, 1, 16, 0).time(), tzinfo=NY)
        a.scheduled_exit_ts = min(
            now + timedelta(minutes=int(horizon_minutes)), close_dt)
        changed["resolves_at"] = a.scheduled_exit_ts.astimezone(NY).strftime("%H:%M")
        fields.append("scheduled_exit_ts")
    meta.setdefault("adjustments", []).append(
        {"at": _now(ctx).astimezone(NY).strftime("%H:%M"), **changed})
    a.meta = meta
    a.save(update_fields=fields)
    return {"alert_id": alert_id, "changed": changed}


@skill(
    "close_position",
    "Cierra YA una posicion abierta al precio actual (no esperas al objetivo ni "
    "al stop). Usalo si la tesis se rompio o ya conseguiste lo que querias.",
    {
        "type": "object",
        "properties": {
            "alert_id": {"type": "integer"},
            "reason": {"type": "string", "description": "Por que cierras ahora."},
        },
        "required": ["alert_id", "reason"],
    },
)
def close_position(ctx, alert_id: int, reason: str):
    from ..models import Alert
    provider = _provider()
    try:
        a = Alert.objects.get(id=alert_id, source=_alert_source(ctx),
                              status=Alert.Status.PENDING)
    except Alert.DoesNotExist:
        return {"error": "posicion no encontrada o ya cerrada"}
    if not a.occ_symbol or not a.entry_ask:
        return {"error": "posicion sin contrato de opcion asociado"}

    # Cerrar la OPCION: se vende al bid actual del contrato.
    at = ctx.get("as_of")
    try:
        q = provider.option_quote(a.occ_symbol, at=at) if at \
            else provider.option_quote(a.occ_symbol)
    except Exception as exc:
        return {"error": f"sin quote para cerrar: {exc}"}
    exit_bid = float(getattr(q, "bid", 0) or 0) if q else 0
    if not exit_bid:
        return {"error": "sin bid de la opcion para cerrar ahora"}

    entry_ask = float(a.entry_ask)
    n = a.contracts or 1
    opt_ret = (exit_bid - entry_ask) / entry_ask * 100
    net_d = (exit_bid - entry_ask) * 100 * n - float(a.commission) * n

    a.status = Alert.Status.CLOSED
    a.exit_ts = _now(ctx)
    a.exit_reason = f"agente: {reason}"[:40]
    a.exit_premium = round(exit_bid, 4)
    a.net_pct = round(opt_ret, 2)
    a.net_dollars = round(net_d, 2)
    meta = dict(a.meta or {})
    meta.update({"exit_premium": round(exit_bid, 4),
                 "option_return_pct": round(opt_ret, 2),
                 "net_dollars": round(net_d, 2), "win": opt_ret > 0,
                 "exit_reason": "agente"})
    a.meta = meta
    a.save(update_fields=["status", "exit_ts", "exit_reason", "exit_premium",
                          "net_pct", "net_dollars", "meta", "updated_at"])
    return {"alert_id": alert_id, "closed": True,
            "option_return_pct": round(opt_ret, 2), "net_dollars": round(net_d, 2)}


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
    qs = Alert.objects.filter(source=_alert_source(ctx),
                              status=Alert.Status.CLOSED)
    if symbol:
        qs = qs.filter(symbol=symbol.upper())
    as_of = ctx.get("as_of")
    if as_of is not None:
        # Solo lo cerrado hasta este instante simulado.
        qs = qs.filter(exit_ts__isnull=False, exit_ts__lte=as_of)
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
        ref = _spot(ctx, provider, sym)
    except Exception:
        ref = None
    if direction not in ("above", "below"):
        direction = "above" if (ref is None or float(price) >= ref) else "below"
    t = AgentTrigger.objects.create(
        symbol=sym, price=round(float(price), 2), direction=direction,
        reason=reason, ref_price=round(ref, 2) if ref else None,
        agent_run=ctx["run"], mode=_mode(ctx),
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
    qs = AgentTrigger.objects.filter(symbol=symbol.upper(), active=True,
                                     mode=_mode(ctx))
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
    n = AgentTrigger.objects.filter(
        id=trigger_id, active=True, mode=_mode(ctx)).update(active=False)
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
    qs = AgentNote.objects.filter(topic=topic.strip()[:80], mode=_mode(ctx))
    as_of = ctx.get("as_of")
    if as_of is not None:
        qs = qs.filter(as_of__isnull=False, as_of__lte=as_of).order_by("-as_of")
    qs = qs[: min(int(limit), 10)]
    return {
        "topic": topic.strip()[:80],
        "notes": [
            {"when": (n.as_of or n.created_at).astimezone(NY).strftime("%Y-%m-%d %H:%M"),
             "note": n.note}
            for n in qs
        ],
    }

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
    "Registra una alerta marcada como generada por el agente. Aparece en el "
    "dashboard con tu razonamiento adjunto. Usala solo cuando tengas una tesis "
    "clara y accionable sobre un activo.",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "direction": {"type": "string", "enum": ["CALL", "PUT"]},
            "thesis": {"type": "string",
                       "description": "Por que lanzas la alerta, en breve."},
        },
        "required": ["symbol", "direction", "thesis"],
    },
)
def create_alert(ctx, symbol: str, direction: str, thesis: str):
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
    today = datetime.now(NY).date()
    now = timezone.now()
    alert, created = Alert.objects.update_or_create(
        strategy=strategy, session_date=today,
        direction=direction, source=Alert.Source.AGENT,
        defaults={
            "rule_version": "agent_v1", "symbol": sym,
            "status": Alert.Status.PENDING, "signal_ts": now,
            "detected_at": now, "agent_run": ctx["run"],
            "underlying_at_signal": spot,
            "meta": {"thesis": thesis, "by": "agent"},
        },
    )
    return {"alert_id": alert.id, "created": created,
            "symbol": sym, "direction": direction}

"""Skills del agente: las herramientas que puede consultar.

Cada skill se registra con ``@skill`` y expone: nombre, descripcion y un esquema
JSON de parametros. El runner las traduce al formato de 'tools' de la API de
OpenAI y ejecuta la que el modelo pida. Anadir una capacidad nueva = anadir una
funcion con ``@skill``; el agente la ve automaticamente.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
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
    import pandas as pd
    from datetime import timedelta
    day = as_of.astimezone(NY).date()
    try:
        bars = provider.bars(symbol, day, day, "1m")
    except Exception:
        bars = None
    if bars is not None and not bars.empty:
        # Excluir la vela de 1m en curso: su cierre esta 1 min en el futuro.
        cutoff = pd.Timestamp(as_of) - pd.Timedelta(minutes=1)
        upto = bars[bars.index <= cutoff]
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


_TF_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60,
               "1d": 1440, "1w": 10080}


def _tf_minutes(tf: str) -> int:
    return _TF_MINUTES.get(tf, 1)


def _bars_upto(ctx, provider, symbol, start, end, tf):
    """Velas cuyo CIERRE ya ocurrio en el reloj causal (entrenamiento).

    El indice de una vela es su INICIO. Una vela de 15m que empieza a las 13:00
    no cierra hasta las 13:15, asi que a las 13:05 aun NO es observable: mirar su
    OHLC seria leer 10 min del futuro. Se exige ``inicio + timeframe <= as_of``,
    la misma condicion que ``ScanContext.causal_bars`` del motor de reglas. En
    vivo (``as_of`` None) no se toca: el provider ya da la vela en curso real.
    """
    import pandas as pd

    df = provider.bars(symbol, start, end, tf)
    as_of = ctx.get("as_of")
    if as_of is not None and not df.empty:
        cutoff = pd.Timestamp(as_of) - pd.Timedelta(minutes=_tf_minutes(tf))
        df = df[df.index <= cutoff]
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
    for p in (20, 40, 100, 200):
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


# ── Internos del indice (que lo mueve por dentro) ───────────────────
# Los pesos son aproximados y solo sirven para ponderar el voto: el objetivo no
# es replicar el NAV del ETF sino saber quien empuja mas fuerte.

_SECTOR_PROXIES = {
    "SOXX": 0.20, "SMH": 0.15, "XLK": 0.20, "IGV": 0.15,
    "XLY": 0.10, "XLC": 0.10, "FDN": 0.05, "IBB": 0.05,
}
_MEGACAPS = {
    "NVDA": 0.085, "AAPL": 0.080, "MSFT": 0.075, "AMZN": 0.055,
    "AVGO": 0.050, "GOOGL": 0.050, "META": 0.045, "TSLA": 0.030,
}
_SLOPE_MIN = 0.0003   # mismo umbral que la investigacion de regimen


def _trend_1h(bars):
    """(etiqueta, signo, pendiente_bps) de la tendencia 1h: MA20 vs MA40 + pendiente.

    Misma definicion que el backtest de regimen: se exige que la pendiente de la
    MA20 supere ``_SLOPE_MIN`` para declarar tendencia; si no, es 'plano'.
    """
    if bars is None or bars.empty or len(bars) < 45:
        return "n/d", 0, None
    closes = bars["close"]
    maf = closes.rolling(20).mean()
    mas = closes.rolling(40).mean()
    if maf.isna().iloc[-1] or mas.isna().iloc[-1] or len(maf.dropna()) < 6:
        return "n/d", 0, None
    f, s = float(maf.iloc[-1]), float(mas.iloc[-1])
    prev = float(maf.iloc[-6])
    slope = (f - prev) / f if f else 0.0
    if f > s and slope > _SLOPE_MIN:
        return "alcista", 1, round(slope * 10000, 1)
    if f < s and slope < -_SLOPE_MIN:
        return "bajista", -1, round(slope * 10000, 1)
    return "plano", 0, round(slope * 10000, 1)


@skill(
    "get_index_internals",
    "Como se esta moviendo un indice POR DENTRO: la tendencia 1h de los "
    "componentes/sectores que mas lo empujan (QQQ: semis, software, tech, "
    "consumo...), cuantos estan alineados con el indice (breadth), si el "
    "movimiento es 'en bloque' o disperso, y quien lidera o rezaga. Sirve para "
    "juzgar la CALIDAD del regimen antes de operar el indice: NO es una senal "
    "de entrada. Usar en QQQ (tambien SPY) junto a la senal de precio.",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string",
                       "description": "Indice a analizar: QQQ o SPY."},
            "group": {"type": "string", "enum": ["sectores", "megacaps", "ambos"],
                      "description": "Que componentes usar. Por defecto sectores "
                                     "(ETFs), que dan una lectura mas limpia."},
        },
        "required": ["symbol"],
    },
)
def get_index_internals(ctx, symbol: str, group: str = "sectores"):
    """Breadth + dispersion + liderazgo de los componentes, causal.

    Todo se calcula con velas ya cerradas (``_bars_upto``), asi que en
    entrenamiento no puede ver el futuro. La dispersion se compara contra su
    propia historia PREVIA (percentil), no contra un umbral fijo: su escala
    depende del regimen de volatilidad.
    """
    import pandas as pd

    provider = _provider()
    sym = symbol.upper()
    end = _now(ctx).date()
    comps: dict[str, float] = {}
    if group in ("sectores", "ambos"):
        comps.update(_SECTOR_PROXIES)
    if group in ("megacaps", "ambos"):
        comps.update(_MEGACAPS)
    if not comps:
        comps = dict(_SECTOR_PROXIES)

    idx_h1 = _bars_upto(ctx, provider, sym, end - timedelta(days=45), end, "1h")
    idx_label, idx_sign, idx_slope = _trend_1h(idx_h1)
    if idx_sign == 0:
        nota = ("El indice NO tiene tendencia 1h definida: el breadth no es "
                "interpretable como confirmacion. Trata el regimen como neutro.")
    else:
        nota = None

    idx_m15 = _bars_upto(ctx, provider, sym, end - timedelta(days=20), end, "15m")

    rows, rets, wsum, waligned, aligned = [], {}, 0.0, 0.0, 0
    for comp, weight in comps.items():
        try:
            h1 = _bars_upto(ctx, provider, comp, end - timedelta(days=45), end, "1h")
            m15 = _bars_upto(ctx, provider, comp, end - timedelta(days=20), end, "15m")
        except Exception as exc:
            rows.append({"symbol": comp, "error": str(exc)})
            continue
        label, sign, slope = _trend_1h(h1)
        if label == "n/d":
            rows.append({"symbol": comp, "error": "sin datos"})
            continue
        is_al = bool(idx_sign != 0 and sign == idx_sign)
        aligned += int(is_al)
        wsum += weight
        waligned += weight * (1.0 if is_al else 0.0)
        r15 = None
        if not m15.empty and len(m15) >= 2:
            c = m15["close"]
            r15 = round(float((c.iloc[-1] / c.iloc[-2] - 1) * 100), 3)
            rets[comp] = c.pct_change()
        rows.append({
            "symbol": comp, "peso_aprox": weight, "trend_1h": label,
            "alineado": is_al, "pendiente_bps": slope, "ret_15m_pct": r15,
        })

    total = sum(1 for r in rows if "error" not in r)
    breadth = round(aligned / total, 3) if total else None
    strength = round(waligned / wsum, 3) if wsum else None

    # Dispersion transversal: std de los retornos 15m entre componentes, y su
    # percentil frente a la historia previa (excluida la barra actual).
    disp_val = disp_pct = None
    if rets and not idx_m15.empty:
        R = pd.DataFrame(rets).reindex(idx_m15.index).ffill(limit=2)
        ser = R.std(axis=1).dropna()
        if len(ser) >= 50:
            disp_val = float(ser.iloc[-1])
            hist = ser.iloc[:-1]
            disp_pct = int(round(float((hist < disp_val).mean()) * 100))

    disp_label = None
    if disp_pct is not None:
        disp_label = "baja" if disp_pct <= 33 else "alta" if disp_pct >= 67 else "media"

    regimen = "indefinido"
    if breadth is not None and disp_label and idx_sign != 0:
        alto = breadth >= 0.75
        if alto and disp_label == "baja":
            regimen = f"bloque_{idx_label}"
        elif alto and disp_label == "alta":
            regimen = f"fuerte_pero_disperso_{idx_label}"
        elif alto:
            regimen = f"alineado_{idx_label}"
        elif disp_label == "alta":
            regimen = "fragmentado"
        else:
            regimen = "mixto"

    ok = [r for r in rows if "error" not in r and r.get("pendiente_bps") is not None]
    lideres = sorted(ok, key=lambda r: -abs(r["pendiente_bps"]))[:3]
    rezagados = [r for r in ok if not r["alineado"]][:3]

    return {
        "symbol": sym,
        "as_of": _now(ctx).astimezone(NY).strftime("%Y-%m-%d %H:%M"),
        "grupo": group,
        "trend_1h_indice": idx_label,
        "pendiente_indice_bps": idx_slope,
        "breadth": {"alineados": aligned, "total": total, "fraccion": breadth},
        "alineacion_ponderada": strength,
        "dispersion": {"valor": round(disp_val, 6) if disp_val is not None else None,
                       "percentil_historico": disp_pct, "etiqueta": disp_label},
        "regimen": regimen,
        "componentes": rows,
        "lideres_por_fuerza": [r["symbol"] for r in lideres],
        "no_confirman": [r["symbol"] for r in rezagados],
        "nota": nota,
        "como_interpretar": {
            "breadth_alto": "6/8+ alineados: el movimiento siguiente tiende a ser "
                            "~44% mas amplio y la direccion acierta ~54% (vs 53% "
                            "base). Es contexto, no ventaja suficiente por si sola.",
            "dispersion_baja": "movimiento 'en bloque': la mejor asimetria a favor "
                               "de la tendencia, pero recorridos CHICOS.",
            "dispersion_alta": "movimiento ~2x mas grande PERO la tendencia falla: "
                               "acierto ~50% y el recorrido en contra supera al "
                               "favorable. NO es regimen para seguir tendencia.",
            "limite_medido": "Ninguna combinacion de breadth/dispersion resulto "
                             "rentable como regla mecanica comprando opciones de "
                             "QQQ intradia (5 backtests, -$25 a -$75 por operacion). "
                             "Usa esto para DESCARTAR entradas y dimensionar, no "
                             "para justificar una entrada.",
        },
    }


@skill(
    "consultar_manual",
    "Consulta el manual operativo de Investep Academy: la regla exacta de una "
    "estrategia (E01-E12), sus condiciones, invalidaciones y gestion, o "
    "cualquier concepto (volatilidad, terreno, linea de tendencia, planes). "
    "OBLIGATORIO antes de operar: hay que verificar la regla, no recordarla.",
    {
        "type": "object",
        "properties": {
            "consulta": {
                "type": "string",
                "description": "Codigo de estrategia (E01) o tema "
                               "(volatilidad, terreno, punto medio, plan 10).",
            },
        },
        "required": ["consulta"],
    },
)
def consultar_manual(ctx, consulta: str):
    from . import investep

    c = (consulta or "").strip()
    res = investep.buscar(c)
    salida = {"consulta": c, "secciones": res}
    cod = c.upper()
    if cod in investep.ESTRATEGIAS:
        ctx.setdefault("manual_consulted", set()).add(cod)
        salida["documentada"] = True
        salida["operable"] = cod in investep.AUTOMATIZADAS
        if not salida["operable"]:
            salida["motivo"] = "NO_DETERMINISTIC_VALIDATOR"
        salida["nombre"] = investep.ESTRATEGIAS[cod]
    elif cod in investep.NO_OPERABLES:
        salida["operable"] = False
        salida["motivo"] = investep.NO_OPERABLES[cod]
    if not res:
        salida["aviso"] = ("Sin coincidencias. Si el manual no lo documenta, NO "
                           "inventes la regla: descarta la operacion.")
    return salida


@skill(
    "validate_investep_setup",
    "Recalcula en el servidor una propuesta Investep y aplica direccion, reloj, "
    "setup mecanico, calendario, terreno y Plan 10. Devuelve un decision_id. "
    "Solo un resultado VALID puede pasar a create_alert; WAIT o BLOCKED son "
    "resultados finales y no se pueden justificar con narrativa.",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "strategy_code": {
                "type": "string", "enum": [
                    "E01", "E02", "E03", "E04", "E05",
                    "E06", "E07", "E08", "E09", "E10",
                ],
            },
            "branch": {
                "type": "string",
                "description": "Rama exacta. E01/E02: OPENING_GAP o "
                               "INTRADAY_BREAK.",
            },
            "thesis": {
                "type": "string",
                "description": "Condiciones observadas, invalidacion y barrera.",
            },
            "target_pct": {
                "type": "number",
                "description": "Objetivo Plan 10 sobre prima, entre 10 y 15.",
            },
            "stop_pct": {
                "type": "number",
                "description": "Stop Plan 10 sobre prima; debe ser 20.",
            },
        },
        "required": ["symbol", "strategy_code", "branch", "thesis"],
    },
)
def validate_investep_setup(
    ctx,
    symbol: str,
    strategy_code: str,
    branch: str,
    thesis: str,
    target_pct: float = 15.0,
    stop_pct: float = 20.0,
):
    from .decision import public_result, validate_setup

    decision = validate_setup(
        ctx,
        _provider(),
        symbol=symbol,
        strategy_code=strategy_code,
        branch=branch,
        thesis=thesis,
        target_pct=target_pct,
        stop_pct=stop_pct,
    )
    return public_result(decision)


@skill(
    "get_estado_volatilidad",
    "Estado de las bandas de Bollinger en la secuencia del manual: CERRADA, "
    "ABRIENDO_LEVE, CONFIRMADA, EXPUESTO o REGRESO_A_BANDA. Devuelve tambien el "
    "ancho, la expansion y su PERCENTIL contra la propia historia del simbolo, "
    "la direccion y el giro del punto medio, y donde esta el precio respecto a "
    "las bandas. Es el requisito central de E01-E08: usalo antes de operarlas. "
    "OJO: no aplicar la exigencia de expansion a E09/E10, que explotan "
    "precisamente una apertura extrema SIN volatilidad.",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "timeframe": {"type": "string", "enum": ["15m", "1h", "1d"],
                          "description": "Temporalidad. E01/E02 usan 15m; "
                                         "E03/E04 usan 1h."},
            "include_forming": {
                "type": "boolean",
                "description": "Solo para OPENING_GAP E01/E02: recalcula 15m "
                               "con el cierre parcial observable.",
            },
        },
        "required": ["symbol"],
    },
)
def get_estado_volatilidad(ctx, symbol: str, timeframe: str = "15m",
                           include_forming: bool = False):
    import numpy as np
    import pandas as pd

    from .volatilidad import evaluar

    provider = _provider()
    sym = symbol.upper()
    end = _now(ctx).date()
    dias = {"15m": 30, "1h": 90, "1d": 400}.get(timeframe, 30)
    try:
        bars = _bars_upto(ctx, provider, sym, end - timedelta(days=dias), end,
                          timeframe)
    except Exception as exc:
        return {"symbol": sym, "error": f"{type(exc).__name__}: {exc}"}
    if bars is None or bars.empty:
        return {"symbol": sym, "error": "sin datos"}

    # Solo los marcos intradia se filtran por hora. Una vela diaria suele estar
    # fechada a medianoche; aplicarle 09:30-16:00 elimina toda la serie.
    if timeframe in {"15m", "1h"}:
        from ..strategies.base import solo_rth
        bars = solo_rth(bars)
    if bars.empty:
        return {"symbol": sym, "error": "sin barras de sesion regular"}

    now = _now(ctx)
    if timeframe in {"15m", "1h"}:
        minutes = 15 if timeframe == "15m" else 60
        cutoff = pd.Timestamp(now) - pd.Timedelta(minutes=minutes)
        bars = bars[bars.index <= cutoff]
    elif timeframe == "1d":
        bars = bars[bars.index.tz_convert(NY).date < now.astimezone(NY).date()]
    if bars.empty:
        return {"symbol": sym, "error": "sin barras cerradas observables"}

    precio = None
    try:
        precio = _spot(ctx, provider, sym)
    except Exception:
        pass
    closes = bars["close"].to_numpy(float)
    bar_state = "CLOSED"
    partial_close = None
    if include_forming:
        if timeframe != "15m":
            return {"symbol": sym, "error": "include_forming solo admite 15m"}
        try:
            minute = provider.bars_1m(sym, now.astimezone(NY).date())
            minute_cutoff = pd.Timestamp(now) - pd.Timedelta(minutes=1)
            minute = minute[minute.index <= minute_cutoff]
            period_start = pd.Timestamp(now).tz_convert("UTC").floor("15min")
            partial = minute[minute.index >= period_start]
            if not partial.empty:
                partial_close = float(partial["close"].iloc[-1])
                closes = np.append(closes, partial_close)
                precio = partial_close
                bar_state = "FORMING_15M"
        except Exception as exc:
            return {"symbol": sym, "error": f"sin barra parcial: {exc}"}
        if partial_close is None:
            return {"symbol": sym, "error": "sin minuto cerrado para 15m parcial"}

    out = evaluar(closes, precio)
    out.update(symbol=sym, timeframe=timeframe,
               barras_usadas=int(len(bars)),
               ultima_barra=bars.index[-1].tz_convert(NY).strftime("%Y-%m-%d %H:%M"),
               bar_state=bar_state,
               partial_close=round(partial_close, 4) if partial_close is not None else None)
    return out


@skill(
    "get_event_risk",
    "Comprueba earnings y eventos macro contra la cobertura configurada. "
    "Devuelve CLEAR, BLOCKED o UNKNOWN; UNKNOWN bloquea una decision Investep.",
    {
        "type": "object",
        "properties": {"symbol": {"type": "string"}},
        "required": ["symbol"],
    },
)
def get_event_risk(ctx, symbol: str):
    from ..strategies.gates import event_gate

    return {"symbol": symbol.upper(), **event_gate(symbol, _now(ctx))}


@skill(
    "get_available_terrain",
    "Calcula la primera barrera en la direccion propuesta usando MA20/40/100/200 "
    "y pivotes, y la compara con el modelo empirico spot-prima del ticker. "
    "Sin modelo devuelve PENDING_EMPIRICAL_MOVE_MODEL.",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "direction": {"type": "string", "enum": ["CALL", "PUT"]},
            "target_pct": {"type": "number"},
        },
        "required": ["symbol", "direction"],
    },
)
def get_available_terrain(ctx, symbol: str, direction: str,
                          target_pct: float = 15.0):
    import math

    from ..strategies.base import ScanContext
    from ..strategies.gates import assess_terrain

    provider = _provider()
    sym = symbol.upper()
    direction = str(direction).upper()
    if direction not in {"CALL", "PUT"}:
        return {"symbol": sym, "status": "BLOCKED",
                "blocker": "INVALID_DIRECTION"}
    try:
        target = float(target_pct)
    except (TypeError, ValueError, OverflowError):
        target = float("nan")
    if not math.isfinite(target) or not 10 <= target <= 15:
        return {"symbol": sym, "status": "BLOCKED",
                "blocker": "PLAN10_TARGET_OUT_OF_RANGE"}
    now = _now(ctx).astimezone(NY)
    try:
        bars = provider.bars_1m(sym, now.date())
        spot = _spot(ctx, provider, sym)
    except Exception as exc:
        return {"symbol": sym, "status": "UNKNOWN",
                "blocker": "MARKET_DATA_ERROR", "error": str(exc)}
    if not math.isfinite(float(spot)) or float(spot) <= 0:
        return {"symbol": sym, "status": "UNKNOWN",
                "blocker": "INVALID_SPOT_PRICE"}
    scan_ctx = ScanContext(
        provider=provider, symbol=sym, session_date=now.date(),
        now=now, bars=bars)
    return {
        "symbol": sym,
        "direction": direction,
        "spot": round(float(spot), 4),
        **assess_terrain(
            scan_ctx, direction, float(spot),
            target_premium_pct=target),
    }


@skill(
    "calculate_option_price_range",
    "Calcula el rango academico de costo por contrato comparando hasta ocho "
    "strikes cercanos al dinero con sus excursiones intradia de prima. Usa "
    "quotes de ThetaData a traves del provider configurado.",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "direction": {"type": "string", "enum": ["CALL", "PUT"]},
            "dte": {"type": "integer"},
            "minimum_contract_cost": {"type": "number"},
            "maximum_contract_cost": {"type": "number"},
        },
        "required": ["symbol", "direction"],
    },
)
def calculate_option_price_range_skill(
    ctx,
    symbol: str,
    direction: str,
    dte: int = 0,
    minimum_contract_cost: float = 20.0,
    maximum_contract_cost: float | None = None,
):
    import math

    import pandas as pd

    from ..data import candidate_expirations, occ_symbol
    from .option_range import calculate_option_price_range

    provider = _provider()
    sym = symbol.upper()
    direction = str(direction).upper()
    if direction not in {"CALL", "PUT"}:
        return {"status": "blocked", "blocker": "INVALID_DIRECTION"}
    try:
        requested_dte = int(dte)
        minimum_cost = float(minimum_contract_cost)
        maximum_cost = (
            float(maximum_contract_cost)
            if maximum_contract_cost is not None else None)
    except (TypeError, ValueError, OverflowError):
        return {"status": "blocked", "blocker": "INVALID_RANGE_INPUT"}
    if (not 0 <= requested_dte <= 30
            or not math.isfinite(minimum_cost) or minimum_cost <= 0
            or (maximum_cost is not None
                and (not math.isfinite(maximum_cost)
                     or maximum_cost < minimum_cost))):
        return {"status": "blocked", "blocker": "INVALID_RANGE_INPUT"}
    now = _now(ctx).astimezone(NY)
    try:
        spot = _spot(ctx, provider, sym)
    except Exception as exc:
        return {"status": "blocked", "blocker": "MARKET_DATA_ERROR",
                "error": str(exc)}
    if not math.isfinite(float(spot)) or float(spot) <= 0:
        return {"status": "blocked", "blocker": "INVALID_SPOT_PRICE"}
    step = _strike_step(float(spot))
    atm = round(float(spot) / step) * step
    strikes = sorted(
        [atm + offset * step for offset in range(-5, 6)],
        key=lambda value: abs(value - float(spot)))
    expirations = candidate_expirations(
        now.date(), max_dte=max(requested_dte + 7, 7))
    expirations.sort(
        key=lambda value: abs((value - now.date()).days - requested_dte))
    start = datetime.combine(
        now.date(), datetime(2000, 1, 1, 9, 30).time(), tzinfo=NY)

    last_error = None
    for expiration in expirations:
        rows = []
        for strike in strikes:
            occ = occ_symbol(sym, expiration, direction, strike)
            try:
                quotes = provider.option_quotes(occ, start, now, interval="1m")
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                continue
            if quotes is None or quotes.empty or "ask" not in quotes:
                continue
            quotes = quotes[quotes.index <= pd.Timestamp(now)]
            asks = pd.to_numeric(quotes["ask"], errors="coerce")
            asks = asks[asks > 0]
            if asks.empty:
                continue
            latest_index = asks.index[-1]
            bid = None
            if "bid" in quotes:
                raw_bid = pd.to_numeric(quotes.loc[[latest_index], "bid"], errors="coerce")
                bid = float(raw_bid.iloc[-1]) if not raw_bid.empty else None
            ask = float(asks.iloc[-1])
            if bid is not None and (bid <= 0 or ask < bid):
                continue
            rows.append({
                "occ_symbol": occ,
                "expiration": expiration,
                "strike": strike,
                "ask": ask,
                "bid": bid,
                "low": float(asks.min()),
                "high": float(asks.max()),
                "spread_pct": (
                    round((ask - bid) / ask * 100, 4)
                    if bid is not None and ask else None),
                "quote_timestamp": latest_index.isoformat(),
            })
        if len(rows) < 2:
            continue
        try:
            result = calculate_option_price_range(
                pd.DataFrame(rows),
                spot=float(spot),
                direction=direction,
                minimum_contract_cost=minimum_cost,
                maximum_contract_cost=maximum_cost,
            )
        except ValueError as exc:
            last_error = str(exc)
            continue
        result.update({
            "symbol": sym,
            "direction": direction,
            "spot": round(float(spot), 4),
            "expiration": expiration.isoformat(),
            "observed_at": now.isoformat(),
        })
        return result
    return {
        "status": "blocked",
        "blocker": "OPTION_RANGE_DATA_INSUFFICIENT",
        "error": last_error,
    }


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
    "Estado de la cuenta interna: tamano, capital ya desplegado en posiciones "
    "abiertas, disponible, y el riesgo maximo sugerido por operacion. Usalo "
    "para dimensionar cuantos contratos comprar sin arriesgar de mas.",
    {"type": "object", "properties": {}},
)
def get_account(ctx):
    from django.conf import settings

    from ..models import Alert
    cfg = getattr(settings, "POWERTRADEAI", {})
    size = float(cfg.get("ACCOUNT_SIZE", cfg.get("PAPER_ACCOUNT", 10000)))
    risk_pct = float(cfg.get("RISK_PCT_PER_TRADE", 2.0))
    open_qs = Alert.objects.filter(source=_alert_source(ctx),
                                   status=Alert.Status.PENDING,
                                   evaluation_version="investep_v2")
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
    "Registra una operacion interna a partir de un decision_id VALID emitido por "
    "validate_investep_setup. El servidor toma estrategia, direccion, Plan 10 y "
    "cantidad de contratos de la decision y de los limites de cuenta; el modelo "
    "no puede reemplazarlos.",
    {
        "type": "object",
        "properties": {
            "decision_id": {
                "type": "integer",
                "description": "Decision VALID devuelta por validate_investep_setup.",
            },
            "strike": {"type": "number",
                       "description": "Strike del contrato (si lo omites, ATM)."},
            "dte": {"type": "integer",
                    "description": "DTE preferido; el servidor busca un "
                                   "vencimiento real cercano."},
            "horizon_minutes": {"type": "integer",
                                "description": "Cuanto vale tu tesis (def. 120)."},
        },
        "required": ["decision_id"],
    },
)
def create_alert(ctx, decision_id: int | None = None,
                 strike: float | None = None, dte: int = 0,
                 horizon_minutes: int = 120, **legacy):
    import math

    from django.conf import settings
    from django.db import transaction

    from ..data import occ_symbol
    from ..models import Alert, InvestepDecision, RiskControl, Strategy
    from ..strategies.gates import validate_quote
    from .decision import PROMPT_VERSION, manual_hash

    if decision_id is None:
        return {
            "error": "decision_id requerido",
            "detalle": "llama primero a validate_investep_setup",
        }
    try:
        decision = InvestepDecision.objects.get(pk=int(decision_id))
    except (InvestepDecision.DoesNotExist, TypeError, ValueError):
        return {"error": "decision Investep inexistente"}
    expected_source = _alert_source(ctx)
    if decision.source != expected_source:
        return {"error": "decision de otra modalidad", "status": decision.status}
    if decision.agent_run_id != ctx["run"].id:
        return {"error": "decision de otra corrida"}
    if decision.status != InvestepDecision.Status.VALID:
        return {
            "error": "decision no validada",
            "status": decision.status,
            "blockers": decision.blockers,
        }
    if (decision.manual_hash != manual_hash()
            or decision.prompt_version != PROMPT_VERSION):
        return {"error": "decision de una version obsoleta"}
    if decision.signal_ts is None:
        return {"error": "decision valida sin signal_ts auditable"}

    existing = Alert.objects.filter(investep_decision=decision).first()
    if existing is not None:
        return {
            "alert_id": existing.id,
            "created": False,
            "decision_id": decision.id,
            "status": existing.status,
        }

    now = _now(ctx).astimezone(NY)
    cfg = getattr(settings, "POWERTRADEAI", {})
    if not _is_training(ctx):
        age = (now - decision.as_of.astimezone(NY)).total_seconds()
        if age < 0 or age > int(cfg.get("MAX_DECISION_AGE_SECONDS", 180)):
            return {"error": "decision expirada", "age_seconds": round(age, 2)}
    if not (datetime(2000, 1, 1, 9, 30).time()
            <= now.time() < datetime(2000, 1, 1, 16, 0).time()):
        return {"error": "fuera de sesion regular"}

    sym = decision.symbol
    direction = decision.direction
    thesis = decision.thesis
    target_pct = float(decision.evidence.get("target_pct", 15.0))
    stop_pct = float(decision.evidence.get("stop_pct", 20.0))
    provider = _provider()
    try:
        spot = _spot(ctx, provider, sym)
    except Exception:
        return {"error": "sin precio del subyacente"}
    if not math.isfinite(float(spot)) or float(spot) <= 0:
        return {"error": "precio del subyacente invalido"}

    try:
        requested_dte = int(dte)
    except (TypeError, ValueError):
        return {"error": "dte invalido"}
    if not 0 <= requested_dte <= 30:
        return {"error": "dte fuera de rango", "allowed": "0-30"}
    try:
        requested_horizon = int(horizon_minutes or 120)
    except (TypeError, ValueError):
        return {"error": "horizon_minutes invalido"}
    if not 5 <= requested_horizon <= 390:
        return {
            "error": "horizon_minutes fuera de rango",
            "allowed": "5-390",
        }

    # Elegir el contrato: strike (ATM si no se da) + expiracion segun dte.
    step = _strike_step(spot)
    if strike is None:
        strike = round(spot / step) * step
    try:
        strike = round(float(strike), 2)
    except (TypeError, ValueError):
        return {"error": "strike invalido"}
    if not math.isfinite(strike) or strike <= 0:
        return {"error": "strike invalido"}
    # Se prueban vencimientos cercanos al DTE solicitado. Un dia habil no implica
    # que el simbolo tenga vencimiento; una quote ausente hace avanzar al siguiente.
    from ..data import candidate_expirations
    today = now.date()
    expirations = candidate_expirations(
        today, max_dte=max(requested_dte + 7, 7))
    expirations.sort(
        key=lambda value: abs((value - today).days - requested_dte))
    at = ctx.get("as_of")
    q = None
    quote_check = None
    target_exp = None
    occ = ""
    for candidate in expirations:
        candidate_occ = occ_symbol(sym, candidate, direction, strike)
        try:
            candidate_quote = (
                provider.option_quote(candidate_occ, at=at)
                if at else provider.option_quote(candidate_occ)
            )
        except Exception:
            continue
        check = validate_quote(
            candidate_quote,
            as_of=at or now,
            max_spread_pct=float(cfg.get("MAX_OPTION_SPREAD_PCT", 5.0)),
            allow_after_seconds=(
                float(cfg.get(
                    "MAX_HISTORICAL_OPTION_QUOTE_DELAY_SECONDS", 90))
                if at else None
            ),
        )
        if check["status"] == "VALID":
            q, quote_check, target_exp, occ = (
                candidate_quote, check, candidate, candidate_occ)
            break
    if q is None or target_exp is None:
        return {
            "error": "sin vencimiento y quote utilizables",
            "dte": requested_dte,
        }
    real_dte = (target_exp - today).days
    entry_ask = float(q.ask)
    entry_bid = float(q.bid)

    training = _is_training(ctx)
    source = expected_source
    strategy, _ = Strategy.objects.get_or_create(
        strategy_id=(
            f"AGENT:{sym}:{decision.strategy_code}:{decision.branch}"[:80]
        ),
        defaults={
            "name": (
                f"Agente {sym} {decision.strategy_code} {decision.branch}"
            ),
            "symbol": sym,
            "rule_version": decision.rule_version or "agent_investep_v2",
            "enabled": False,
        })
    today = now.astimezone(NY).date()
    horizon = requested_horizon
    entry_ts = getattr(q, "ts", None) or now
    close_dt = datetime.combine(today, datetime(2000, 1, 1, 16, 0).time(),
                                tzinfo=NY)
    exit_at = min(entry_ts + timedelta(minutes=horizon), close_dt)
    with transaction.atomic():
        RiskControl.objects.select_for_update().get_or_create(key="agent")
        locked = InvestepDecision.objects.select_for_update().get(pk=decision.pk)
        existing = Alert.objects.filter(investep_decision=locked).first()
        if existing is not None:
            return {
                "alert_id": existing.id,
                "created": False,
                "decision_id": locked.id,
                "status": existing.status,
            }
        duplicate = Alert.objects.filter(
            source=expected_source,
            symbol=locked.symbol,
            academy_strategy=locked.strategy_code,
            strategy_branch=locked.branch,
            direction=locked.direction,
            signal_ts=locked.signal_ts,
            evaluation_version="investep_v2",
        ).first()
        if duplicate is not None:
            return {
                "alert_id": duplicate.id,
                "created": False,
                "decision_id": locked.id,
                "status": duplicate.status,
                "reason": "duplicate_academic_signal",
            }
        account_size = float(
            cfg.get("ACCOUNT_SIZE", cfg.get("PAPER_ACCOUNT", 10000)))
        risk_pct = float(cfg.get("RISK_PCT_PER_TRADE", 2.0))
        max_contracts = max(int(cfg.get("MAX_CONTRACTS_PER_TRADE", 5)), 1)
        deployed = sum(
            float((item.meta or {}).get("cost", 0) or 0)
            for item in Alert.objects.filter(
                source=expected_source, status=Alert.Status.PENDING)
        )
        budget = min(
            account_size * risk_pct / 100,
            max(account_size - deployed, 0),
        )
        one_contract_cost = entry_ask * 100
        contracts = min(math.floor(budget / one_contract_cost), max_contracts)
        if contracts < 1:
            return {
                "error": "contrato excede el riesgo disponible",
                "contract_cost": round(one_contract_cost, 2),
                "risk_budget": round(budget, 2),
            }
        cost = round(entry_ask * 100 * contracts, 2)
        meta = {
            "thesis": thesis, "by": "agent", "entry_price": spot,
            "horizon_minutes": horizon, "dte": real_dte, "cost": cost,
            "target_pct": round(target_pct, 3),
            "stop_pct": round(stop_pct, 3),
            "training": training,
            "strategy_code": decision.strategy_code,
            "strategy_branch": decision.branch,
            "decision_id": decision.id,
            "manual_hash": decision.manual_hash,
            "prompt_version": decision.prompt_version,
            "quote_validation": quote_check,
        }
        alert = Alert.objects.create(
            strategy=strategy, symbol=sym, session_date=today,
            direction=direction,
            source=source,
            rule_version=decision.rule_version or "agent_investep_v2",
            evaluation_version="investep_v2",
            status=Alert.Status.PENDING,
            signal_ts=locked.signal_ts,
            detected_at=now,
            entry_ts=entry_ts,
            scheduled_exit_ts=exit_at,
            agent_run=ctx["run"],
            underlying_at_signal=spot,
            occ_symbol=occ,
            expiration=target_exp,
            strike=strike,
            contracts=contracts,
            entry_ask=entry_ask,
            entry_bid=entry_bid,
            entry_premium=entry_ask,
            meta=meta,
            investep_decision=locked,
            academy_strategy=decision.strategy_code,
            strategy_branch=decision.branch,
        )
        created = True
    return {"alert_id": alert.id, "created": created, "symbol": sym,
            "direction": direction, "contract": occ, "strike": strike,
            "dte": real_dte, "contracts": contracts,
            "entry_premium": entry_ask, "cost": cost,
            "decision_id": decision.id,
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
    "RSI(14) en 15m. Es contexto descriptivo; no valida una estrategia.",
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
    "IMPLEMENTACION auxiliar: detecta lineas DIAGONALES (resistencia bajista uniendo maximos "
    "descendentes, soporte alcista uniendo minimos ascendentes) y niveles "
    "horizontales de soporte/resistencia. Devuelve donde esta cada linea AHORA y "
    "el precio respecto a ellas, para buscar rechazos, rupturas y puntos de "
    "entrada. Sus pivotes 3/3 no son una regla publicada y no sustituyen a "
    "validate_investep_setup.",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "timeframe": {"type": "string", "enum": ["15m", "1h", "1d"],
                          "description": "Temporalidad; E01/E02 requieren 15m."},
            "lookback_days": {"type": "integer", "description": "Dias atras (max 30)."},
        },
        "required": ["symbol"],
    },
)
def get_trendlines(ctx, symbol: str, timeframe: str = "15m",
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
    "get_daily_briefing",
    "Briefing de PRE-MERCADO: resumen multi-dia para saber que esperar hoy. "
    "Tendencia (velas seguidas del mismo lado, MA20/40 diarias), la vela de AYER "
    "(color, fuerza, donde cerro), maximo/minimo de 3 y 10 dias y donde esta el "
    "precio, cercania al punto medio de Bollinger en 1h (pista de rebote o cambio "
    "de tendencia), RSI diario, y TU historial (dias operados, win rate, P&L, "
    "ultima leccion). Llamalo LO PRIMERO cada dia para tener contexto.",
    {"type": "object", "properties": {"symbol": {"type": "string"}},
     "required": ["symbol"]},
)
def get_daily_briefing(ctx, symbol: str):
    import pandas as pd

    from ..models import Alert, AgentNote
    provider = _provider()
    sym = symbol.upper()
    end = _now(ctx).date()

    # --- Diario: SOLO dias ya cerrados (la vela de hoy incluiria el futuro) ---
    daily = provider.bars(sym, end - timedelta(days=90), end, "1d")
    if daily.empty:
        return {"symbol": sym, "error": "sin datos diarios"}
    d = daily[daily.index.tz_convert(NY).date < end]
    if len(d) < 5:
        return {"symbol": sym, "error": "historial diario insuficiente"}
    o, h, l, c = (d["open"].astype(float), d["high"].astype(float),
                  d["low"].astype(float), d["close"].astype(float))
    last_o, last_h, last_l, last_c = (float(o.iloc[-1]), float(h.iloc[-1]),
                                      float(l.iloc[-1]), float(c.iloc[-1]))
    prev_c = float(c.iloc[-2])

    # ATR(14) diario para medir "fuerza".
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    atr = float(tr.rolling(14).mean().iloc[-1]) if len(d) >= 15 else float(tr.mean())

    # Vela de AYER (ultima cerrada).
    rng = last_h - last_l
    close_pos = (last_c - last_l) / rng if rng > 0 else 0.5
    tercio = ("tercio alto" if close_pos >= 0.66 else
              "tercio bajo" if close_pos <= 0.33 else "tercio medio")
    ret = (last_c - prev_c) / prev_c * 100 if prev_c else 0.0
    ayer = {"color": "roja" if last_c < last_o else "verde",
            "rango_pct": round(rng / last_o * 100, 2) if last_o else None,
            "cierre_en": tercio, "cambio_pct": round(ret, 2),
            "movimiento": "fuerte" if atr and abs(last_c - prev_c) >= atr else "normal"}

    # Tendencia: velas seguidas del mismo color + sesgo de medias.
    streak = 1
    for i in range(len(d) - 1, 0, -1):
        if (float(c.iloc[i]) < float(o.iloc[i])) == (float(c.iloc[i - 1]) < float(o.iloc[i - 1])):
            streak += 1
        else:
            break
    ma20 = round(float(c.iloc[-20:].mean()), 2) if len(d) >= 20 else None
    ma40 = round(float(c.iloc[-40:].mean()), 2) if len(d) >= 40 else None
    sesgo = ("alcista" if ma20 and ma40 and ma20 > ma40 else
             "bajista" if ma20 and ma40 and ma20 < ma40 else "lateral")
    tendencia = {"sesgo_ma": sesgo, "ma20_d": ma20, "ma40_d": ma40,
                 "velas_seguidas": streak,
                 "color_racha": "rojas" if last_c < last_o else "verdes"}

    # Rango reciente.
    hi3, lo3 = float(h.iloc[-3:].max()), float(l.iloc[-3:].min())
    hi10, lo10 = float(h.iloc[-10:].max()), float(l.iloc[-10:].min())
    try:
        price = _spot(ctx, provider, sym)
    except Exception:
        price = last_c
    rango = {"max_3d": round(hi3, 2), "min_3d": round(lo3, 2),
             "max_10d": round(hi10, 2), "min_10d": round(lo10, 2),
             "pos_en_rango_10d_pct": round((price - lo10) / (hi10 - lo10) * 100, 1)
             if hi10 > lo10 else None}

    # Efecto iman: cercania al punto medio de Bollinger en 1h (causal).
    iman = None
    h1 = _bars_upto(ctx, provider, sym, end - timedelta(days=15), end, "1h")
    if not h1.empty and len(h1) >= 20:
        cc = h1["close"].astype(float)
        mid = float(cc.iloc[-20:].mean())
        std = float(cc.iloc[-20:].std(ddof=0))
        up, dn = mid + 2 * std, mid - 2 * std
        dist = (price - mid) / mid * 100 if mid else 0.0
        if abs(dist) <= 0.3:
            pista = "cerca del punto medio 1h: zona de rebote o cambio de tendencia"
        elif price > up:
            pista = "extendido sobre la banda superior 1h: posible reversion a la media"
        elif price < dn:
            pista = "extendido bajo la banda inferior 1h: posible rebote a la media"
        else:
            pista = "dentro de las bandas 1h, sin extremo"
        iman = {"punto_medio_1h": round(mid, 2), "dist_pct": round(dist, 2),
                "zona": ("sobre banda sup" if price > up else
                         "bajo banda inf" if price < dn else "dentro"),
                "pista": pista}

    # Tu historial (causal): dias operados, win rate, P&L, ultima leccion.
    qs = Alert.objects.filter(
        source=_alert_source(ctx), symbol=sym,
        status=Alert.Status.CLOSED, evaluation_version="investep_v2")
    as_of = ctx.get("as_of")
    if as_of is not None:
        qs = qs.filter(exit_ts__isnull=False, exit_ts__lte=as_of)
    closed = list(qs)
    ncl = len(closed)
    wins = sum(1 for a in closed if float(a.net_dollars or 0) > 0)
    nq = AgentNote.objects.filter(mode=_mode(ctx))
    nq = (nq.filter(as_of__isnull=False, as_of__lte=as_of).order_by("-as_of")
          if as_of is not None else nq.order_by("-created_at"))
    last_note = nq.first()
    historial = {"dias_operados": len({a.session_date for a in closed}),
                 "operaciones_cerradas": ncl,
                 "win_rate_pct": round(wins / ncl * 100, 1) if ncl else None,
                 "pnl_neto": round(sum(float(a.net_dollars or 0) for a in closed), 2),
                 "ultima_leccion": last_note.note[:200] if last_note else None}

    return {
        "symbol": sym, "fecha": str(end), "precio": round(price, 2),
        "tendencia": tendencia, "ayer": ayer, "rango_reciente": rango,
        "rsi14_diario": _rsi(c, 14),
        "dist_ma20_d_pct": round((price - ma20) / ma20 * 100, 2) if ma20 else None,
        "dist_ma40_d_pct": round((price - ma40) / ma40 * 100, 2) if ma40 else None,
        "efecto_iman_1h": iman, "tu_historial": historial,
    }


@skill(
    "save_note",
    "Guarda una observacion o hipotesis, indexada por tema. No convierte la "
    "nota en regla academica ni en condicion operable. Persiste entre corridas.",
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
                              status=Alert.Status.PENDING,
                              evaluation_version="investep_v2")
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
                              status=Alert.Status.PENDING,
                              evaluation_version="investep_v2")
    except Alert.DoesNotExist:
        return {"error": "posicion no encontrada o ya cerrada"}
    meta = dict(a.meta or {})
    if a.academy_strategy:
        proposed_target = (
            float(target_pct) if target_pct is not None
            else float(meta.get("target_pct", 15.0)))
        proposed_stop = (
            abs(float(stop_pct)) if stop_pct is not None
            else float(meta.get("stop_pct", 20.0)))
        if not 10.0 <= proposed_target <= 15.0 or proposed_stop != 20.0:
            return {
                "error": "ajuste fuera de Plan 10",
                "allowed_target_pct": "10-15",
                "required_stop_pct": 20,
            }
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
    from django.conf import settings

    from ..models import Alert
    from ..strategies.gates import validate_quote
    provider = _provider()
    try:
        a = Alert.objects.get(id=alert_id, source=_alert_source(ctx),
                              status=Alert.Status.PENDING,
                              evaluation_version="investep_v2")
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
    config = getattr(settings, "POWERTRADEAI", {})
    checked = validate_quote(
        q,
        as_of=at or _now(ctx),
        max_spread_pct=float(config.get("MAX_OPTION_SPREAD_PCT", 5.0)),
        allow_after_seconds=(
            float(config.get(
                "MAX_HISTORICAL_OPTION_QUOTE_DELAY_SECONDS", 90))
            if at else None
        ),
    )
    if checked["status"] != "VALID":
        return {
            "error": "quote no utilizable para cerrar",
            "quote_validation": checked,
        }
    exit_bid = float(q.bid)

    entry_ask = float(a.entry_ask)
    opt_ret = (exit_bid - entry_ask) / entry_ask * 100
    a.close(
        exit_premium=round(exit_bid, 4),
        exit_ts=(getattr(q, "ts", None) or _now(ctx)) if at else _now(ctx),
        reason=f"agente: {reason}"[:40],
    )
    net_d = float(a.net_dollars or 0)
    meta = dict(a.meta or {})
    meta.update({"exit_premium": round(exit_bid, 4),
                 "option_return_pct": round(opt_ret, 2),
                 "net_return_pct": float(a.net_pct or 0),
                 "net_dollars": round(net_d, 2), "win": net_d > 0,
                 "exit_reason": "agente"})
    a.meta = meta
    a.save(update_fields=["meta", "updated_at"])
    return {"alert_id": alert_id, "closed": True,
            "option_return_pct": round(opt_ret, 2), "net_dollars": round(net_d, 2)}


@skill(
    "reinforce_position",
    "REFUERZA una posicion abierta que ya toco su stop: compra la MISMA cantidad "
    "de contratos del mismo contrato al precio actual y promedia la prima. Es la "
    "rama 'la tesis sigue viva' de la decision en el stop; la otra rama, la normal, "
    "es close_position. AVISO de tu propia evidencia: en el backtest, reforzar al "
    "tocar el stop fue PERDEDOR el 94%% de las veces (cerrar gano en PF y retorno). "
    "Solo refuerza con una CONFIRMACION nueva y objetiva (un cierre 15m mas alla "
    "del nivel, un rechazo de VWAP, una diagonal que aguanta), no 'sigo creyendo'. "
    "Gates: solo si el stop esta tocado, una sola vez por posicion, nunca en 0DTE, "
    "y sin pasarte del riesgo maximo por operacion.",
    {
        "type": "object",
        "properties": {
            "alert_id": {"type": "integer"},
            "confirmation": {
                "type": "string",
                "description": "El evento NUEVO y objetivo que confirma la tesis "
                               "(nivel, precio, vela). Se registra para auditoria."},
        },
        "required": ["alert_id", "confirmation"],
    },
)
def reinforce_position(ctx, alert_id: int, confirmation: str):
    from django.conf import settings

    from ..models import Alert

    # Gate 0: por ahora la capacidad esta EN EVALUACION -> solo entrenamiento.
    if not _is_training(ctx):
        return {"error": "reinforce_position esta en evaluacion; por ahora solo "
                         "en entrenamiento. En vivo, usa close_position."}
    if not confirmation or not confirmation.strip():
        return {"error": "reforzar exige una confirmacion nueva y objetiva; sin "
                         "ella, cierra con close_position"}

    provider = _provider()
    try:
        a = Alert.objects.get(id=alert_id, source=_alert_source(ctx),
                              status=Alert.Status.PENDING)
    except Alert.DoesNotExist:
        return {"error": "posicion no encontrada o ya cerrada"}
    if a.academy_strategy:
        return {
            "error": "refuerzo no permitido para Plan 10",
            "strategy": a.academy_strategy,
        }
    if not a.occ_symbol or not a.entry_ask:
        return {"error": "posicion sin contrato de opcion asociado"}

    meta = dict(a.meta or {})
    # Gate 1: una sola vez por posicion.
    if meta.get("reinforced"):
        return {"error": "esta posicion ya se reforzo una vez; no se puede otra. "
                         "Si vuelve a tocar el stop, cierra."}
    # Gate 2: nunca en 0DTE (no hay tiempo de recuperar).
    if int(meta.get("dte") or 0) == 0:
        return {"error": "no se refuerza un 0DTE: sin tiempo para recuperar. Cierra."}
    stop_pct = meta.get("stop_pct")
    if not stop_pct:
        return {"error": "la posicion no tiene stop definido; reforzar solo tiene "
                         "sentido en el punto de decision del stop"}

    # Quote actual: sirve para el gate del stop y para el precio del refuerzo.
    at = ctx.get("as_of")
    try:
        q = provider.option_quote(a.occ_symbol, at=at) if at \
            else provider.option_quote(a.occ_symbol)
    except Exception as exc:
        return {"error": f"sin quote para reforzar: {exc}"}
    cur_bid = float(getattr(q, "bid", 0) or 0) if q else 0
    add_ask = float(getattr(q, "ask", 0) or 0) if q else 0
    if not cur_bid or not add_ask:
        return {"error": "sin bid/ask utilizable del contrato ahora mismo"}

    entry_ask = float(a.entry_ask)
    # Gate 3: el stop DEBE estar tocado (bid <= entrada*(1-stop%)). Reforzar antes
    # del stop no es esta decision; para eso esta adjust_position.
    stop_level = entry_ask * (1 - float(stop_pct) / 100.0)
    if cur_bid > stop_level:
        return {"error": f"el stop no esta tocado (bid {cur_bid:.2f} > umbral "
                         f"{stop_level:.2f}); reforzar solo en el toque del stop"}

    # Gate 4: riesgo total acotado. La prima total del trade (lo ya pagado + el
    # refuerzo) no puede superar el maximo por operacion. Obliga a que la entrada
    # inicial fuese un 'starter' pequeno; si no, no hay sitio para reforzar.
    n = a.contracts or 1
    add_cost = round(add_ask * 100 * n, 2)
    orig_cost = float(meta.get("cost") or entry_ask * 100 * n)
    cfg = getattr(settings, "POWERTRADEAI", {})
    max_risk = (
        float(cfg.get("ACCOUNT_SIZE", cfg.get("PAPER_ACCOUNT", 10000)))
        * float(cfg.get("RISK_PCT_PER_TRADE", 2.0)) / 100.0
    )
    if orig_cost + add_cost > max_risk:
        return {"error": f"reforzar (+${add_cost:.0f}) llevaria el riesgo del trade "
                         f"a ${orig_cost + add_cost:.0f}, por encima del maximo "
                         f"${max_risk:.0f}. Entra con menos contratos si quieres "
                         f"dejar sitio para reforzar."}

    # --- Refuerzo: promediar prima, duplicar contratos ---
    # Con el mismo nº de contratos por tramo, la prima mezclada es la media; el
    # resolver calcula el P&L combinado correcto con (entry_ask mezclado, 2n).
    blended = round((entry_ask + add_ask) / 2.0, 4)
    add_bid = round(cur_bid, 4)
    meta.setdefault("original_entry_ask", entry_ask)
    meta.setdefault("original_contracts", n)
    meta["reinforced"] = True
    meta["cost"] = round(orig_cost + add_cost, 2)
    meta.setdefault("reinforcements", []).append({
        "at": _now(ctx).astimezone(NY).strftime("%H:%M"),
        "confirmation": confirmation.strip()[:200],
        "add_ask": round(add_ask, 4), "add_contracts": n, "add_cost": add_cost,
        "bid_at_reinforce": add_bid, "blended_entry_ask": blended,
    })
    a.entry_ask = blended
    a.entry_premium = blended
    a.contracts = n * 2
    a.meta = meta
    a.save(update_fields=["entry_ask", "entry_premium", "contracts", "meta",
                          "updated_at"])
    return {"alert_id": alert_id, "reinforced": True,
            "add_contracts": n, "total_contracts": n * 2,
            "add_premium": round(add_ask, 4), "blended_entry_premium": blended,
            "add_cost": add_cost, "trade_risk_total": meta["cost"],
            "nota": "El stop ahora se mide sobre la prima media; si vuelve a "
                    "tocarlo, cierra (no hay segundo refuerzo)."}


@skill(
    "get_my_track_record",
    "Historial Investep v2: resultado neto de las opciones que ya cerraste. "
    "Sirve para auditar comportamiento, no para modificar reglas o umbrales. "
    "Opcional filtrar por activo.",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Opcional."},
        },
    },
)
def get_my_track_record(ctx, symbol: str | None = None):
    from ..models import Alert
    qs = Alert.objects.filter(
        source=_alert_source(ctx), status=Alert.Status.CLOSED,
        evaluation_version="investep_v2")
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
    "Lee observaciones previas por tema. Son contexto, no reglas academicas.",
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

"""E01/E02 — cambio de tendencia con Bollinger en 15m, RAMA DE APERTURA.

Metodologia Investep (corpus en ~/Desktop/Investepia). Regla academica:

  Contexto previo contrario o lateral en 15m -> se traza una linea de tendencia
  sobre el tramo vigente -> un GAP de apertura rompe la linea Y el punto medio
  de Bollinger -> las bandas se abren de inmediato -> CALL (E01) o PUT (E02).

RELOJ: la academia decide ~09:31 ET con la vela 09:30-09:45 EN FORMACION
(``bar_state = FORMING``, ``future_ohlc_allowed = false``). Por eso esta regla
usa ``ctx.forming_bar()``, la unica del proyecto que lo hace. Esperar al cierre
de las 09:45 perderia la rama entera; usar su OHLC final seria look-ahead.

GESTION: la fuente publica 10%-15% sobre la PRIMA. No hay banda opuesta ni MA40
como target estructural obligatorio: esas referencias sirven para validar
terreno ANTES de entrar. El stop -20% de prima viene del Plan 10.

=========================================================================
CALIBRACION EXTERNA — NO ES REGLA DE LA ACADEMIA
El material deja estos puntos como "pendiente de definir". Se agrupan aqui
para que ningun resultado se atribuya a la fuente. Ver
Investepia/estrategias_instrucciones.md y la investigacion de cierre.
=========================================================================
"""
from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd
from django.conf import settings

from .base import BaseStrategy, ExitDecision, ScanContext, Signal, register, solo_rth
from .gates import assess_terrain, event_gate, validate_quote

BB_PERIODO, BB_K = 20, 2.0

# --- calibracion externa (pendientes del material) ---
# Las magnitudes se expresan EN RANGOS DE VELA DEL PROPIO SIMBOLO, no en % fijo.
# Motivo: el rango tipico de 15m va de 0.293% (AAPL) a 0.480% (TSLA) y el gap
# tipico de 0.424% a 0.898%. Un 0.15% fijo vale 0.31 rangos en TSLA y 0.51 en
# AAPL: seria un 65% mas permisivo en AAPL sin haberlo decidido. Ademas
# confundiria simbolo con calibracion, y entonces un ranking de rentabilidad
# entre simbolos no significaria nada. La academia lo pide explicitamente:
# "conocer la liquidez, el spread normal y el comportamiento propio del
# instrumento".
VENTANA_TRAMO = 26           # velas 15m del tramo para trazar la linea
TOLERANCIA_CONTACTO_RANGOS = 0.42   # antes 0.15% fijo (= 0.42 rangos de media)
CONTACTOS_MINIMOS = 2        # unico valor con respaldo academico
PENDIENTE_LATERAL_RANGOS = 0.14     # antes 5 bps fijos (= 0.14 rangos de media)
BARRAS_CONTEXTO = 8
GAP_MINIMO_RANGOS = 0.0      # sin minimo publicado
GIRO_MEDIO_MODO = "desaceleracion"   # o "estricto"; ver docstring de _giro


def _hora(texto: str):
    return datetime.strptime(texto, "%H:%M").time()




def _escala(barras: pd.DataFrame) -> float | None:
    """Rango tipico de una vela de 15m del simbolo, en fraccion del precio.

    Es la unidad con la que se miden tolerancia, lateralidad y gap. Se calcula
    con el historial que termina AYER, asi que no lee la sesion viva.
    """
    if barras is None or len(barras) < 20:
        return None
    r = ((barras["high"] - barras["low"]) / barras["close"]).to_numpy(float)
    r = r[np.isfinite(r) & (r > 0)]
    if len(r) < 20:
        return None
    return float(np.median(r))


def _bollinger(cierres: np.ndarray):
    if len(cierres) < BB_PERIODO:
        return None
    v = cierres[-BB_PERIODO:]
    mid = float(v.mean()); sd = float(v.std(ddof=0))
    return mid - BB_K * sd, mid, mid + BB_K * sd


def _linea_max_contactos(barras: pd.DataFrame, direccion: str, tol_frac: float):
    """Linea con mas contactos sobre el tramo vigente. CALIBRACION EXTERNA.

    La fuente solo fija "minimo dos contactos, cuerpo o mecha, penetraciones
    leves". Generacion de candidatos y desempate son decisiones de software.

    ``tol_frac`` viene ya escalado por la volatilidad del simbolo.
    """
    seg = barras.tail(VENTANA_TRAMO)
    if len(seg) < 4:
        return None
    y = (seg["high"] if direccion == "CALL" else seg["low"]).to_numpy(float)
    x = np.arange(len(y), dtype=float)
    mejor = None
    for i in range(len(y) - 1):
        for j in range(i + 1, len(y)):
            m = (y[j] - y[i]) / (j - i)
            if (direccion == "CALL" and m >= 0) or (direccion == "PUT" and m <= 0):
                continue
            linea = m * x + (y[i] - m * i)
            tol = np.abs(y) * tol_frac
            fuera = (y > linea + tol) if direccion == "CALL" else (y < linea - tol)
            if np.any(fuera):
                continue
            contactos = int(np.sum(np.abs(y - linea) <= tol))
            if contactos < CONTACTOS_MINIMOS:
                continue
            clave = (contactos, -abs(m))
            if mejor is None or clave > mejor[0]:
                mejor = (clave, m, y[i] - m * i, len(seg))
    if mejor is None:
        return None
    _, m, b, n = mejor
    # Dos niveles, no uno: una linea descendente proyectada hacia adelante acaba
    # cruzando por debajo del precio. Hay que comparar cada punto con la linea EN
    # SU PROPIO MOMENTO: el cierre anterior contra el nivel de su barra, y la
    # apertura contra el nivel proyectado. Usar solo la proyeccion rechaza
    # rupturas validas.
    return {"contactos": mejor[0][0],
            "nivel_ultimo": m * (n - 1) + b,
            "nivel_siguiente": m * n + b}


def _giro(mid_ant, mid_prev, mid_now, direccion) -> bool:
    """'Giro del punto medio' a las 09:31. AMBIGUO en la fuente.

    El punto medio es una MA20: un solo minuto casi nunca invierte su signo,
    porque el valor que sale de la ventana suele pesar mas que el que entra.
    Dos lecturas posibles, ninguna publicada.
    """
    if GIRO_MEDIO_MODO == "estricto":
        return mid_now > mid_prev if direccion == "CALL" else mid_now < mid_prev
    ritmo_prev = mid_prev - mid_ant if mid_ant is not None else 0.0
    ritmo_now = mid_now - mid_prev
    return ritmo_now > ritmo_prev if direccion == "CALL" else ritmo_now < ritmo_prev


class E01E02AperturaBase(BaseStrategy):
    """Rama OPENING_GAP. ``direction`` decide E01 (CALL) o E02 (PUT)."""

    direction: str = "CALL"
    default_params = {
        # La fuente observa el indicador 15m tras terminar el primer minuto. El
        # intervalo es [09:31, 09:32): permite polls con segundos sin extender la
        # rama hasta 09:44, que no esta respaldado por los ejemplos revisados.
        "watch_from": "09:31",
        "watch_until": "09:32",
        # La fuente publica 10%-15%. Se usa 15 porque la mediana de las
        # salidas por target ya era +15.29%: el escaner comprueba la prima
        # cada minuto y el nivel se atraviesa, asi que pedir 10 no cerraba
        # en 10. Ambos valores son regla academica.
        "target_premium_pct": 15.0,
        "stop_premium_pct": 20.0,     # Plan 10
        # La academia no publica una salida a 30/120 minutos para E01/E02.
        # Se deja trabajar target/stop y solo se aplana antes del cierre como
        # control operativo externo, claramente separado de la regla.
        "hold_minutes": 390,
        "flatten_at": "15:55",
        # Se sondean siete dias calendario porque una accion puede no tener
        # vencimiento diario. La ausencia de quote confirma que una fecha
        # habil no es un vencimiento real.
        "max_dte": 7,
        "strike_depth": 6,
        # Liquidez: la evaluacion TSLA de la academia observo que "todos los
        # spreads fueron menores a 5%". Es un dato de sus propias operaciones,
        # no un umbral inventado. Para un bot es imprescindible: al ampliar el
        # universo entran nombres con opciones ilíquidas donde el spread se come
        # cualquier objetivo de 10-15% de prima.
        "max_spread_pct": 5.0,
        "require_event_clear": True,
        "require_terrain_model": True,
    }

    def evaluate(self, ctx: ScanContext) -> Signal | None:
        ahora = ctx.et(ctx.now).time()
        if not (_hora(self.params["watch_from"]) <= ahora
                < _hora(self.params["watch_until"])):
            return None

        # LIMITE DE DATOS: la barra en formacion se reconstruye con velas de 1m,
        # asi que el estado mas temprano observable es tras cerrar el minuto de
        # las 09:30. Un cumplimiento a las 09:30:20 no es visible con este
        # proveedor; haria falta tape de ticks. No es la regla, es el dato.
        formando = ctx.forming_bar(15)
        if formando is None or formando["minutes"] < 1:
            return None

        # historial 15m que termina AYER: nunca mezcla la sesion viva
        h = solo_rth(ctx.history("15m", days=20))
        if h is None or h.empty or len(h) < BB_PERIODO + BARRAS_CONTEXTO:
            return None
        cierres = h["close"].to_numpy(float)

        # unidad propia del simbolo: rango tipico de su vela de 15m
        escala = _escala(h)
        if escala is None:
            return None
        tol_frac = TOLERANCIA_CONTACTO_RANGOS * escala
        lateral_bps = PENDIENTE_LATERAL_RANGOS * escala * 10000
        gap_min_pct = GAP_MINIMO_RANGOS * escala * 100

        bb_prev = _bollinger(cierres)
        bb_now = _bollinger(np.append(cierres, formando["close"]))
        if bb_prev is None or bb_now is None:
            return None
        lo_p, mid_p, up_p = bb_prev
        lo_n, mid_n, up_n = bb_now
        cierre_ant = float(cierres[-1])
        apertura = formando["open"]
        d = self.direction

        # 1. GAP
        gap = apertura - cierre_ant
        if (d == "CALL" and gap <= 0) or (d == "PUT" and gap >= 0):
            return None
        if abs(gap) / cierre_ant * 100 < gap_min_pct:
            return None

        # 2. CONTEXTO PREVIO contrario o lateral (direccion del punto medio)
        mids = []
        for k in range(len(cierres) - BARRAS_CONTEXTO, len(cierres)):
            bb = _bollinger(cierres[:k + 1])
            if bb:
                mids.append(bb[1])
        if len(mids) < 2:
            return None
        pend_bps = (mids[-1] - mids[0]) / mids[0] * 10000
        lateral = abs(pend_bps) < lateral_bps
        if d == "CALL" and not (pend_bps < 0 or lateral):
            return None
        if d == "PUT" and not (pend_bps > 0 or lateral):
            return None

        # 3. LINEA DE TENDENCIA rota por el gap
        ln = _linea_max_contactos(h, d, tol_frac)
        if ln is None:
            return None
        nivel = ln["nivel_siguiente"]; nivel_ant = ln["nivel_ultimo"]
        if d == "CALL" and not (apertura > nivel and cierre_ant <= nivel_ant):
            return None
        if d == "PUT" and not (apertura < nivel and cierre_ant >= nivel_ant):
            return None

        # 4. PUNTO MEDIO superado/perdido
        if d == "CALL" and not (apertura > mid_p and cierre_ant <= mid_p):
            return None
        if d == "PUT" and not (apertura < mid_p and cierre_ant >= mid_p):
            return None

        # 5. EXPANSION DE VOLATILIDAD (tres condiciones, sin umbral inventado)
        bandas_abren = (up_n - lo_n) > (up_p - lo_p)
        bb_ant = _bollinger(cierres[:-1])
        medio_gira = _giro(bb_ant[1] if bb_ant else None, mid_p, mid_n, d)
        dentro_antes = lo_p <= cierre_ant <= up_p
        hacia_banda = (formando["close"] > cierre_ant if d == "CALL"
                       else formando["close"] < cierre_ant)
        if not (bandas_abren and medio_gira and dentro_antes and hacia_banda):
            return None

        if self.params.get("require_event_clear", True):
            event = event_gate(
                self.symbol, pd.Timestamp(ctx.now).to_pydatetime())
            if event["status"] != "CLEAR":
                return None
        else:
            event = {"status": "SKIPPED", "reason": "mechanical_only"}
        if self.params.get("require_terrain_model", True):
            terrain = assess_terrain(
                ctx, d, formando["close"],
                target_premium_pct=float(self.params["target_premium_pct"]))
            if terrain["status"] != "SUFFICIENT":
                return None
        else:
            terrain = {"status": "SKIPPED", "reason": "mechanical_only"}

        return Signal(
            direction=d,
            signal_ts=pd.Timestamp(formando["observed_through"]).to_pydatetime(),
            underlying=formando["close"],
            meta={
                "rama": "OPENING_GAP", "bar_state": "FORMING_15M",
                "gap_pct": round(gap / cierre_ant * 100, 4),
                "linea_nivel": round(nivel, 4), "contactos": ln["contactos"],
                "punto_medio": round(mid_p, 4),
                "expansion_pct": round(((up_n - lo_n) / (up_p - lo_p) - 1) * 100, 3),
                "contexto_pend_bps": round(pend_bps, 2),
                "contexto_lateral": lateral,
                "escala_rango15m_pct": round(escala * 100, 4),
                "umbral_lateral_bps": round(lateral_bps, 2),
                "target_premium_pct": self.params["target_premium_pct"],
                "stop_premium_pct": self.params["stop_premium_pct"],
                "event_gate": event,
                "terrain": terrain,
            },
        )

    def select_contract(self, ctx: ScanContext, signal: Signal, at=None):
        """ATM mas cercano con quote viva. La academia compara ocho strikes por
        rango de prima; aqui se usa ATM porque el modelo empirico de movimiento
        mostro que ATM alcanza el objetivo de prima mucho mas a menudo que OTM.
        Eso es CALIBRACION EXTERNA, no regla de la fuente."""
        from ..data import candidate_expirations, occ_symbol

        spot = signal.underlying
        paso = 5.0 if spot >= 200 else 2.5 if spot >= 50 else 1.0
        base = round(spot / paso) * paso
        depth = int(self.params["strike_depth"])
        strikes = [base + (i - depth // 2) * paso for i in range(depth)]
        strikes.sort(key=lambda k: abs(k - spot))
        for exp in candidate_expirations(ctx.session_date, int(self.params["max_dte"])):
            for k in strikes:
                occ = occ_symbol(self.symbol, exp, signal.direction, float(k))
                try:
                    q = ctx.provider.option_quote(occ, at=at)
                except Exception:
                    continue
                if q is None or not q.is_live:
                    continue
                checked = validate_quote(
                    q,
                    as_of=pd.Timestamp(at or ctx.now).to_pydatetime(),
                    max_spread_pct=float(self.params["max_spread_pct"]),
                    allow_after_seconds=(
                        float(getattr(settings, "POWERTRADEAI", {}).get(
                            "MAX_HISTORICAL_OPTION_QUOTE_DELAY_SECONDS", 90))
                        if at is not None else None
                    ),
                )
                if checked["status"] != "VALID":
                    continue
                return occ, exp, float(k), q
        return None, None, None, None

    def check_exit(self, ctx: ScanContext, alert) -> ExitDecision:
        """Salida por PRIMA: target y stop en % del ask de entrada.

        No hay salida estructural publicada para E01/E02; las barreras se usan
        para validar terreno antes de entrar, no para salir.
        """
        if alert.entry_premium is None or alert.entry_ts is None:
            return ExitDecision(should_exit=False)
        entrada = float(alert.entry_premium)
        meta = alert.meta or {}
        tgt = entrada * (1 + float(meta.get("target_premium_pct", 15.0)) / 100)
        stp = entrada * (1 - float(meta.get("stop_premium_pct", 20.0)) / 100)
        occ = alert.occ_symbol
        if not occ:
            return ExitDecision(should_exit=False)
        try:
            q = ctx.provider.option_quote(occ, at=pd.Timestamp(ctx.now).to_pydatetime())
        except Exception:
            return ExitDecision(should_exit=False)
        bid = float(getattr(q, "bid", 0) or 0)
        if bid <= 0:
            return ExitDecision(should_exit=False)
        if bid >= tgt:
            return ExitDecision(True, "target_premium", pd.Timestamp(ctx.now).to_pydatetime())
        if bid <= stp:
            return ExitDecision(True, "stop_premium", pd.Timestamp(ctx.now).to_pydatetime())
        return ExitDecision(should_exit=False)


class E01E02IntradiaBase(E01E02AperturaBase):
    """Rama INTRADAY_BREAK con confirmacion en cierre completo de 15m."""

    default_params = {
        **E01E02AperturaBase.default_params,
        "watch_from": "09:45",
        "watch_until": "15:46",
    }

    def evaluate(self, ctx: ScanContext) -> Signal | None:
        ahora = ctx.et(ctx.now).time()
        if not (_hora(self.params["watch_from"]) <= ahora
                < _hora(self.params["watch_until"])):
            return None

        today = ctx.resample("15m")
        if today is None or today.empty:
            return None
        history = solo_rth(ctx.history("15m", days=20))
        if history is None or history.empty:
            return None
        bars = pd.concat([history, today]).sort_index()
        bars = bars[~bars.index.duplicated(keep="last")]
        if len(bars) < BB_PERIODO + BARRAS_CONTEXTO + 1:
            return None

        trigger = bars.iloc[-1]
        prior = bars.iloc[:-1]
        closes = prior["close"].to_numpy(float)
        all_closes = bars["close"].to_numpy(float)
        escala = _escala(prior)
        if escala is None:
            return None
        tol_frac = TOLERANCIA_CONTACTO_RANGOS * escala
        lateral_bps = PENDIENTE_LATERAL_RANGOS * escala * 10000

        bb_prev = _bollinger(closes)
        bb_now = _bollinger(all_closes)
        if bb_prev is None or bb_now is None:
            return None
        lo_p, mid_p, up_p = bb_prev
        lo_n, mid_n, up_n = bb_now
        d = self.direction

        mids = []
        for k in range(len(closes) - BARRAS_CONTEXTO, len(closes)):
            bb = _bollinger(closes[:k + 1])
            if bb:
                mids.append(bb[1])
        if len(mids) < 2:
            return None
        pend_bps = (mids[-1] - mids[0]) / mids[0] * 10000
        lateral = abs(pend_bps) < lateral_bps
        if d == "CALL" and not (pend_bps < 0 or lateral):
            return None
        if d == "PUT" and not (pend_bps > 0 or lateral):
            return None

        line = _linea_max_contactos(prior, d, tol_frac)
        if line is None:
            return None
        level = line["nivel_siguiente"]
        previous_level = line["nivel_ultimo"]
        previous_close = float(prior["close"].iloc[-1])
        candle_open = float(trigger["open"])
        candle_close = float(trigger["close"])

        if d == "CALL":
            body_crosses = (
                candle_close > candle_open
                and candle_open <= level < candle_close
                and candle_open <= mid_p < candle_close
                and previous_close <= previous_level
            )
            volatility_opens = (
                (up_n - lo_n) > (up_p - lo_p)
                and up_n > up_p
                and mid_n > mid_p
            )
        else:
            body_crosses = (
                candle_close < candle_open
                and candle_open >= level > candle_close
                and candle_open >= mid_p > candle_close
                and previous_close >= previous_level
            )
            volatility_opens = (
                (up_n - lo_n) > (up_p - lo_p)
                and lo_n < lo_p
                and mid_n < mid_p
            )
        if not body_crosses or not volatility_opens:
            return None

        signal_ts = (today.index[-1] + pd.Timedelta(minutes=15)).to_pydatetime()
        if self.params.get("require_event_clear", True):
            event = event_gate(self.symbol, signal_ts)
            if event["status"] != "CLEAR":
                return None
        else:
            event = {"status": "SKIPPED", "reason": "mechanical_only"}
        if self.params.get("require_terrain_model", True):
            terrain = assess_terrain(
                ctx, d, candle_close,
                target_premium_pct=float(self.params["target_premium_pct"]))
            if terrain["status"] != "SUFFICIENT":
                return None
        else:
            terrain = {"status": "SKIPPED", "reason": "mechanical_only"}

        return Signal(
            direction=d,
            signal_ts=signal_ts,
            underlying=candle_close,
            meta={
                "rama": "INTRADAY_BREAK",
                "bar_state": "CLOSED_15M",
                "linea_nivel": round(level, 4),
                "contactos": line["contactos"],
                "punto_medio_previo": round(mid_p, 4),
                "punto_medio_actual": round(mid_n, 4),
                "expansion_pct": round(
                    ((up_n - lo_n) / (up_p - lo_p) - 1) * 100, 3),
                "contexto_pend_bps": round(pend_bps, 2),
                "contexto_lateral": lateral,
                "escala_rango15m_pct": round(escala * 100, 4),
                "umbral_lateral_bps": round(lateral_bps, 2),
                "target_premium_pct": self.params["target_premium_pct"],
                "stop_premium_pct": self.params["stop_premium_pct"],
                "event_gate": event,
                "terrain": terrain,
            },
        )


def _crear(sym: str, direccion: str, branch: str):
    eid = "E01" if direccion == "CALL" else "E02"
    nombre = ("cambio al alza" if direccion == "CALL" else "cambio a la baja")
    intraday = branch == "INTRADAY_BREAK"
    suffix = "INTRADIA" if intraday else "APERTURA"
    base = E01E02IntradiaBase if intraday else E01E02AperturaBase
    return register(type(
        f"{eid}{suffix.title()}{sym}", (base,),
        {
            "strategy_id": f"{sym}_{eid}_{suffix}",
            "name": (
                f"{sym} {eid} {nombre} Bollinger 15m "
                f"({'intradia' if intraday else 'apertura'})"
            ),
            "symbol": sym,
            "direction": direccion,
            "rule_version": (
                "e01e02_intraday_closed15m_gated_v6" if intraday
                else "e01e02_opening_gap_first_minute_gated_v6"
            ),
        }))


# Catalogo disponible para investigacion. Registrar una clase no la activa: el
# comando ``seed_strategies`` limita la ejecucion a ``INVESTEP_WATCHLIST`` (tres
# instrumentos por defecto) y deja el resto deshabilitado. Asi se puede ampliar
# el estudio sin presentar un universo mecanico como la lista oficial perdida.
UNIVERSO = (
    # megacaps
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AVGO", "NFLX",
    # alta beta y volumen de opciones
    "AMD", "PLTR", "COIN", "MU", "INTC", "UBER", "ORCL", "CRM", "QCOM",
    "BA", "DIS", "JPM", "XOM", "GS", "CAT",
    # indices y ETF (control)
    "SPY", "QQQ", "IWM", "DIA",
)

for _sym in UNIVERSO:
    _crear(_sym, "CALL", "OPENING_GAP")
    _crear(_sym, "PUT", "OPENING_GAP")
    _crear(_sym, "CALL", "INTRADAY_BREAK")
    _crear(_sym, "PUT", "INTRADAY_BREAK")

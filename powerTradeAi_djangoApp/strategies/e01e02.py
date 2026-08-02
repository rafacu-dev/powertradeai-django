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

from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from .base import NY, BaseStrategy, ExitDecision, ScanContext, Signal, register

BB_PERIODO, BB_K = 20, 2.0

# --- calibracion externa (pendientes del material) ---
VENTANA_TRAMO = 26          # velas 15m del tramo para trazar la linea
TOLERANCIA_CONTACTO_PCT = 0.15
CONTACTOS_MINIMOS = 2       # unico valor con respaldo academico
PENDIENTE_LATERAL_BPS = 5.0
BARRAS_CONTEXTO = 8
GAP_MINIMO_PCT = 0.0        # sin minimo publicado
GIRO_MEDIO_MODO = "desaceleracion"   # o "estricto"; ver docstring de _giro


def _hora(texto: str):
    return datetime.strptime(texto, "%H:%M").time()


def _bollinger(cierres: np.ndarray):
    if len(cierres) < BB_PERIODO:
        return None
    v = cierres[-BB_PERIODO:]
    mid = float(v.mean()); sd = float(v.std(ddof=0))
    return mid - BB_K * sd, mid, mid + BB_K * sd


def _linea_max_contactos(barras: pd.DataFrame, direccion: str):
    """Linea con mas contactos sobre el tramo vigente. CALIBRACION EXTERNA.

    La fuente solo fija "minimo dos contactos, cuerpo o mecha, penetraciones
    leves". Generacion de candidatos y desempate son decisiones de software.
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
            tol = np.abs(y) * TOLERANCIA_CONTACTO_PCT / 100.0
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
        # La academia NO fija una hora: la señal se toma CUANDO SE CUMPLE la
        # condicion, que puede ser a las 09:30:20, 09:31 o 09:32. El "09:31" de
        # los ejemplos (PLTR, Apple) es una OBSERVACION de cuando ocurrio en esos
        # casos, no un requisito. Aqui se vigila toda la formacion de la primera
        # vela de 15m y se dispara en la primera evaluacion que cumpla todo.
        "watch_from": "09:30",
        "watch_until": "09:45",
        "target_premium_pct": 10.0,   # publicado: 10%-15% sobre la prima
        "stop_premium_pct": 20.0,     # Plan 10
        "max_dte": 2,
        "strike_depth": 6,
    }

    def evaluate(self, ctx: ScanContext) -> Signal | None:
        ahora = ctx.et(ctx.now).time()
        if not (_hora(self.params["watch_from"]) <= ahora
                <= _hora(self.params["watch_until"])):
            return None

        # LIMITE DE DATOS: la barra en formacion se reconstruye con velas de 1m,
        # asi que el estado mas temprano observable es tras cerrar el minuto de
        # las 09:30. Un cumplimiento a las 09:30:20 no es visible con este
        # proveedor; haria falta tape de ticks. No es la regla, es el dato.
        formando = ctx.forming_bar(15)
        if formando is None or formando["minutes"] < 1:
            return None

        # historial 15m que termina AYER: nunca mezcla la sesion viva
        h = ctx.history("15m", days=20)
        if h is None or h.empty or len(h) < BB_PERIODO + BARRAS_CONTEXTO:
            return None
        cierres = h["close"].to_numpy(float)

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
        if abs(gap) / cierre_ant * 100 < GAP_MINIMO_PCT:
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
        lateral = abs(pend_bps) < PENDIENTE_LATERAL_BPS
        if d == "CALL" and not (pend_bps < 0 or lateral):
            return None
        if d == "PUT" and not (pend_bps > 0 or lateral):
            return None

        # 3. LINEA DE TENDENCIA rota por el gap
        ln = _linea_max_contactos(h, d)
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

        return Signal(
            direction=d,
            signal_ts=pd.Timestamp(ctx.now).to_pydatetime(),
            underlying=formando["close"],
            meta={
                "rama": "OPENING_GAP", "bar_state": "FORMING_15M",
                "gap_pct": round(gap / cierre_ant * 100, 4),
                "linea_nivel": round(nivel, 4), "contactos": ln["contactos"],
                "punto_medio": round(mid_p, 4),
                "expansion_pct": round(((up_n - lo_n) / (up_p - lo_p) - 1) * 100, 3),
                "contexto_pend_bps": round(pend_bps, 2),
                "contexto_lateral": lateral,
                "target_premium_pct": self.params["target_premium_pct"],
                "stop_premium_pct": self.params["stop_premium_pct"],
            },
        )

    def select_contract(self, ctx: ScanContext, signal: Signal, at=None):
        """ATM mas cercano con quote viva. La academia compara ocho strikes por
        rango de prima; aqui se usa ATM porque el modelo empirico de movimiento
        mostro que ATM alcanza el objetivo de prima mucho mas a menudo que OTM.
        Eso es CALIBRACION EXTERNA, no regla de la fuente."""
        import math
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
                if q is not None and q.is_live:
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
        tgt = entrada * (1 + float(meta.get("target_premium_pct", 10.0)) / 100)
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


def _crear(sym: str, direccion: str):
    eid = "E01" if direccion == "CALL" else "E02"
    nombre = ("cambio al alza" if direccion == "CALL" else "cambio a la baja")
    return register(type(
        f"{eid}Apertura{sym}", (E01E02AperturaBase,),
        {
            "strategy_id": f"{sym}_{eid}_APERTURA",
            "name": f"{sym} {eid} {nombre} Bollinger 15m (apertura)",
            "symbol": sym,
            "direction": direccion,
            "rule_version": "e01e02_opening_gap_forming15m_v2",
        }))


# Universo inicial: acciones individuales. El detector de investigacion midio
# que los indices casi no producen esta rama (QQQ 0 y SPY 3 candidatos en un
# año, frente a 15 de META): un indice diversificado rara vez abre con gap.
for _sym in ("TSLA", "NVDA", "AAPL", "MSFT", "AMZN", "META"):
    _crear(_sym, "CALL")
    _crear(_sym, "PUT")

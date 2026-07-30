"""BB midpoint: cierre por INVALIDACION ESTRUCTURAL.

Cuando el precio rompe el punto medio EN CONTRA de la posicion (una vela 15m
cierra al otro lado del medio), el trade se corta ya, sin esperar al stop de
40 bps mas alla. Reproduce el caso real: PUT que fadea un rebote, el precio
reclama el medio con confirmacion y sigue de largo -> debe cerrar como un stop.
"""
from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

import pandas as pd

from powerTradeAi_djangoApp.strategies.base import NY, ScanContext
from powerTradeAi_djangoApp.strategies.bb_midpoint import BBMidpointBase

SESSION = date(2026, 7, 15)


def _bars(reclaim: bool):
    idx, rows = [], []

    def add(hh, mm, c, h):
        idx.append(pd.Timestamp(datetime(2026, 7, 15, hh, mm, tzinfo=NY)))
        rows.append({"open": c, "high": h, "low": c - 0.1, "close": c, "volume": 1000})

    for m in range(30, 45):
        add(9, m, 99.0, 99.1)
    for m in range(45, 60):
        add(9, m, 99.3, 99.4)          # zona de entrada (bajo el medio 100)
    # 10:00-10:14: la vela 15m sube y CIERRA en 100.2 (reclaim) o 99.9 (no).
    top = 100.2 if reclaim else 99.9
    ramp = [round(99.4 + (top - 99.4) * k / 14, 3) for k in range(15)]
    for k, m in enumerate(range(0, 15)):
        add(10, m, ramp[k], min(ramp[k] + 0.1, 100.3))
    for m in range(15, 20):
        add(10, m, top, min(top + 0.1, 100.3))
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx).tz_convert("UTC"))


def _put():
    return type("X", (BBMidpointBase,),
                {"symbol": "SPY", "direction": "PUT",
                 "confirmation": "close_volume", "strategy_id": "SPY"})()


def _alert():
    return SimpleNamespace(
        meta={"bb_mid": 100.0, "target_underlying": 98.0, "stop_underlying": 100.4},
        direction="PUT", entry_ts=datetime(2026, 7, 15, 9, 45, tzinfo=NY))


def test_cierra_al_reclamar_el_medio_antes_del_stop():
    b = _bars(reclaim=True)
    ctx = ScanContext(None, "SPY", SESSION,
                      datetime(2026, 7, 15, 10, 20, tzinfo=NY), b)
    dec = _put().check_exit(ctx, _alert())
    assert dec.should_exit
    assert dec.reason == "midpoint_reclaim"
    # cierra en el cierre de la vela 15m que reclamo el medio (10:15), NO al
    # stop de 100.4 (que el precio nunca tocó: máximo 100.3).
    assert dec.at.astimezone(NY).strftime("%H:%M") == "10:15"


def test_no_cierra_si_no_reclama_el_medio():
    b = _bars(reclaim=False)     # cierra en 99.9, sigue bajo el medio
    ctx = ScanContext(None, "SPY", SESSION,
                      datetime(2026, 7, 15, 10, 20, tzinfo=NY), b)
    dec = _put().check_exit(ctx, _alert())
    assert dec.should_exit is False

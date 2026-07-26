"""get_daily_briefing: resumen multi-dia causal de pre-mercado.

Escenario controlado: ayer fue una vela roja fuerte que cerro en el tercio bajo,
tres velas rojas seguidas, y una vela de HOY con un maximo absurdo (999) que NO
debe filtrarse en el resumen (solo dias cerrados).
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from powerTradeAi_djangoApp.agent import skills
from powerTradeAi_djangoApp.strategies.base import NY


def _daily():
    rows = [
        ("2026-07-13", 95, 96, 94, 96),
        ("2026-07-14", 96, 97, 95, 97),
        ("2026-07-15", 97, 98, 96, 98),
        ("2026-07-16", 98, 99, 97, 99),
        ("2026-07-17", 99, 100, 98, 100),
        ("2026-07-18", 100, 101, 99, 101),
        ("2026-07-20", 102, 103, 101, 103),
        ("2026-07-21", 103, 104, 102, 104),   # verde: corta la racha
        ("2026-07-22", 104, 105, 102, 103),   # roja
        ("2026-07-23", 103, 104, 100, 101),   # roja (cierre 101 = prev de 07-24)
        ("2026-07-24", 100, 101, 89, 90),     # roja, fuerte, tercio bajo
        ("2026-07-25", 90, 999, 50, 90),      # HOY: debe excluirse
    ]
    idx = pd.DatetimeIndex(
        [pd.Timestamp(r[0], tz=NY) for r in rows]).tz_convert("UTC")
    return pd.DataFrame(
        {"open": [r[1] for r in rows], "high": [r[2] for r in rows],
         "low": [r[3] for r in rows], "close": [r[4] for r in rows],
         "volume": [1000] * len(rows)}, index=idx)


def _hourly():
    idx = pd.date_range(
        pd.Timestamp("2026-07-24 09:30", tz=NY).tz_convert("UTC"),
        periods=25, freq="1h")
    return pd.DataFrame(
        {"open": [90.0] * 25, "high": [90.2] * 25, "low": [89.8] * 25,
         "close": [90.0] * 25, "volume": [100] * 25}, index=idx)


class Fake:
    def bars(self, sym, start, end, tf):
        if tf == "1d":
            return _daily()
        if tf == "1h":
            return _hourly()
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"],
                            index=pd.DatetimeIndex([], tz="UTC"))

    def latest_price(self, sym):
        return 90.0


@pytest.mark.django_db
def test_daily_briefing_es_causal_y_resume_el_dia_anterior(monkeypatch):
    monkeypatch.setattr(skills, "_provider", lambda: Fake())
    ctx = {"as_of": datetime(2026, 7, 25, 8, 0, tzinfo=NY)}   # pre-apertura 07-25
    b = skills.get_daily_briefing(ctx, "TSLA")

    # Vela de ayer (07-24): roja, fuerte, cerro en el tercio bajo.
    assert b["ayer"]["color"] == "roja"
    assert b["ayer"]["movimiento"] == "fuerte"
    assert b["ayer"]["cierre_en"] == "tercio bajo"

    # Tres velas rojas seguidas.
    assert b["tendencia"]["velas_seguidas"] == 3

    # Rango de 3 dias: el maximo es 105, NO el 999 de hoy (dia sin cerrar).
    assert b["rango_reciente"]["max_3d"] == 105.0
    assert b["rango_reciente"]["min_3d"] == 89.0

    # Efecto iman: el precio (90) esta pegado al punto medio 1h (90).
    assert "punto medio" in b["efecto_iman_1h"]["pista"]

    # Sin operaciones aun.
    assert b["tu_historial"]["operaciones_cerradas"] == 0

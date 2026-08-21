from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
from django.template.loader import render_to_string
from django.urls import reverse


def _synthetic_long_reversal():
    from powerTradeAi_djangoApp.engine.session import NY

    anchors = [
        (0, 105.0), (5, 110.0), (10, 100.0), (15, 108.0),
        (20, 98.0), (25, 106.0), (30, 96.0), (35, 104.0),
        (40, 94.0), (42, 98.0), (44, 93.98), (45, 103.0),
        (46, 105.0), (47, 104.5), (52, 111.0), (70, 108.0),
    ]
    close = np.interp(np.arange(71), [p[0] for p in anchors], [p[1] for p in anchors])
    index = pd.DatetimeIndex([
        datetime(2026, 8, 20, 9, 30, tzinfo=NY) + timedelta(minutes=i)
        for i in range(len(close))
    ]).tz_convert("UTC")
    return pd.DataFrame({
        "open": close,
        "high": close + 0.20,
        "low": close - 0.20,
        "close": close,
        "volume": [100] * len(close),
    }, index=index)


def test_intraday_trendlines_tiene_rutas_y_calendario():
    assert reverse("powertradeai:intraday_trendlines") == "/panel/intraday-trendlines/"
    assert reverse("powertradeai:intraday_trendlines_data") == "/panel/intraday-trendlines/data/"
    html = render_to_string("powertradeai/intraday_trendlines.html", {"symbols": ["SPY"]})
    assert "calendar-grid" in html
    assert "Líneas de tendencia intradía · 1 minuto" in html
    assert "/panel/intraday-trendlines/data/" in html
    assert "rejection_line" in html
    assert "entry_time" in html
    assert "{{" not in html and "{%" not in html


def test_detector_1m_confirma_y_entra_en_la_vela_siguiente():
    from powerTradeAi_djangoApp.engine.intraday_trendlines import structural_reversal_setups

    frame = _synthetic_long_reversal()
    setups = structural_reversal_setups(frame)

    assert setups
    setup = setups[0]
    assert setup["direction"] == "LONG"
    assert setup["break_time"] < setup["confirm_time"] < setup["entry_time"]
    assert len(setup["trendline"]) == 2
    assert len(setup["rejection_line"]) == 2
    assert setup["target_price"] > setup["entry_price"] > setup["stop_price"]


def test_timeline_1m_recorta_solo_la_sesion_seleccionada():
    from powerTradeAi_djangoApp.engine.intraday_trendlines import intraday_trendline_timeline
    from powerTradeAi_djangoApp.engine.session import NY

    selected = date(2026, 8, 20)
    frame = _synthetic_long_reversal()
    before = frame.copy()
    before.index = before.index - pd.Timedelta(days=1)
    after_close = pd.DataFrame({
        "open": [100.0], "high": [101.0], "low": [99.0],
        "close": [100.0], "volume": [10],
    }, index=pd.DatetimeIndex([datetime(2026, 8, 20, 16, 1, tzinfo=NY)]).tz_convert("UTC"))

    class Provider:
        def bars_1m(self, symbol, day):
            return pd.concat([before, frame, after_close]).sort_index()

    timeline = intraday_trendline_timeline(selected, "spy", provider=Provider())

    assert timeline.symbol == "SPY"
    assert timeline.timeframe == "1m"
    assert len(timeline.candles) == len(frame)
    assert timeline.setups

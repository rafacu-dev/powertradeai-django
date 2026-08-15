from django.template.loader import render_to_string
from django.urls import reverse
import pytest


def test_replay_visual_tiene_rutas_separadas():
    assert reverse("powertradeai:replay") == "/panel/replay/"
    assert reverse("powertradeai:replay_data") == "/panel/replay/data/"
    assert reverse("powertradeai:replay_action") == "/panel/replay/run/"


def test_replay_visual_resuelve_endpoints_en_template():
    html = render_to_string("powertradeai/replay.html", {
        "symbols": ["SPY"],
        "strategies": [],
    })
    assert "/panel/replay/data/" in html
    assert "trendline-list" in html
    assert "Toques" in html
    assert "{{" not in html and "{%" not in html


@pytest.mark.django_db
def test_replay_visual_tiene_simbolos_por_defecto(rf, django_user_model):
    from powerTradeAi_djangoApp.dashboard import replay_view

    user = django_user_model.objects.create_user(
        username="staff", password="x", is_staff=True)
    request = rf.get(reverse("powertradeai:replay"))
    request.user = user

    html = replay_view(request).content.decode()

    for text in (
        "Amazon · AMZN", "Google · GOOGL", "Tesla · TSLA", "Apple · AAPL",
        "Nvidia · NVDA", "Microsoft · MSFT", "Nasdaq QQQ · QQQ",
        "S&P 500 SPY · SPY",
    ):
        assert text in html


def test_dashboard_enlaza_al_replay_visual():
    from pathlib import Path

    import powerTradeAi_djangoApp

    ruta = (Path(powerTradeAi_djangoApp.__file__).parent
            / "templates" / "powertradeai" / "dashboard.html")
    assert "powertradeai:replay" in ruta.read_text(encoding="utf-8")


@pytest.mark.django_db
def test_replay_timeline_grafica_15m_con_cinco_dias_previos_rth():
    import pandas as pd
    from datetime import date, datetime, time, timedelta

    from powerTradeAi_djangoApp.engine.replay import replay_timeline
    from powerTradeAi_djangoApp.engine.session import NY

    class Provider:
        name = "fake"

        def bars_1m(self, symbol, session_date):
            return self._bars_for_days([session_date])

        def bars(self, symbol, start, end, timeframe="1m"):
            days = []
            cursor = start
            while cursor <= end:
                if cursor.weekday() < 5:
                    days.append(cursor)
                cursor += timedelta(days=1)
            return self._bars_for_days(days)

        def _bars_for_days(self, days):
            idx = []
            for day in days:
                for hh, mm in [(8, 0), (9, 30), (9, 31), (9, 45), (15, 45), (16, 0)]:
                    idx.append(datetime.combine(day, time(hh, mm), tzinfo=NY))
            utc = pd.DatetimeIndex(idx).tz_convert("UTC")
            return pd.DataFrame({
                "open": range(len(utc)),
                "high": range(1, len(utc) + 1),
                "low": range(len(utc)),
                "close": range(len(utc)),
                "volume": [100] * len(utc),
            }, index=utc)

    day = date(2026, 8, 10)
    timeline = replay_timeline(day, "SPY", provider=Provider())

    assert timeline.timeframe == "15m"
    assert timeline.replay_start_time == int(
        datetime.combine(day, time(9, 30), tzinfo=NY).timestamp())
    local_times = [
        datetime.fromtimestamp(candle["time"], tz=NY)
        for candle in timeline.candles
    ]
    assert {ts.date() for ts in local_times} == {
        date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 5),
        date(2026, 8, 6), date(2026, 8, 7), day,
    }
    assert all(time(9, 30) <= ts.time() < time(16, 0) for ts in local_times)
    assert all(ts.minute in {0, 15, 30, 45} for ts in local_times)


def test_trendlines_devuelven_lineas_dibujables():
    import pandas as pd
    from datetime import datetime, timedelta

    from powerTradeAi_djangoApp.engine.replay import _trendlines_for_frame
    from powerTradeAi_djangoApp.engine.session import NY

    idx = pd.DatetimeIndex([
        datetime(2026, 8, 3, 9, 30, tzinfo=NY) + timedelta(minutes=15 * i)
        for i in range(18)
    ]).tz_convert("UTC")
    highs = [10, 11, 15, 12, 11, 14, 11, 10, 13, 10, 9, 12, 9, 8, 11, 8, 7, 10]
    lows = [8, 7, 6, 8, 9, 7, 9, 10, 8, 10, 11, 9, 11, 12, 10, 12, 13, 11]
    frame = pd.DataFrame({
        "open": lows,
        "high": highs,
        "low": lows,
        "close": [(h + l) / 2 for h, l in zip(highs, lows)],
        "volume": [100] * len(idx),
    }, index=idx)

    lines = _trendlines_for_frame(
        frame, "15m", int(idx[0].timestamp()), int(idx[-1].timestamp()))

    assert any(line["kind"] == "resistencia" for line in lines)
    assert any(line["kind"] == "soporte" for line in lines)
    for line in lines:
        assert line["timeframe"] == "15m"
        assert len(line["points"]) == 2
        assert all("time" in point and "value" in point for point in line["points"])


def test_breakout_confirmado_marca_la_vela_siguiente():
    import pandas as pd
    from datetime import date, datetime, timedelta

    from powerTradeAi_djangoApp.engine.replay import _confirmed_breakouts
    from powerTradeAi_djangoApp.engine.session import NY

    day = date(2026, 8, 10)
    idx = pd.DatetimeIndex([
        datetime(2026, 8, 7, 9, 30, tzinfo=NY) + timedelta(minutes=15 * i)
        for i in range(4)
    ]).tz_convert("UTC")
    frame = pd.DataFrame({
        "open": [99, 100, 102, 103],
        "high": [100, 103, 105, 106],
        "low": [98, 99, 101, 103],
        "close": [99, 102, 103.5, 105],
        "volume": [100] * 4,
    }, index=idx)
    line = {
        "timeframe": "15m", "kind": "resistencia",
        "points": [
            {"time": int(idx[0].timestamp()), "value": 100},
            {"time": int(idx[-1].timestamp()), "value": 100},
        ],
    }

    out = _confirmed_breakouts(frame, day, [line])

    assert out == [{
        "time": int(idx[1].timestamp()),
        "direction": "CALL",
        "price": 102.0,
        "line_price": 100.0,
        "timeframe": "15m",
        "kind": "resistencia",
        "label": "Confirmacion CALL 15m resistencia",
    }]


def test_breakout_confirmado_incluye_corte_lateral():
    import pandas as pd
    from datetime import date, datetime, timedelta

    from powerTradeAi_djangoApp.engine.replay import _confirmed_breakouts
    from powerTradeAi_djangoApp.engine.session import NY

    day = date(2026, 8, 10)
    idx = pd.DatetimeIndex([
        datetime(2026, 8, 7, 9, 30, tzinfo=NY) + timedelta(minutes=15 * i)
        for i in range(4)
    ]).tz_convert("UTC")
    frame = pd.DataFrame({
        "open": [102, 101, 99, 98],
        "high": [103, 102, 100, 99],
        "low": [101, 98, 96, 95],
        "close": [102, 99, 97, 96],
        "volume": [100] * 4,
    }, index=idx)
    line = {
        "timeframe": "15m", "kind": "corte",
        "points": [
            {"time": int(idx[0].timestamp()), "value": 100},
            {"time": int(idx[-1].timestamp()), "value": 100},
        ],
    }

    out = _confirmed_breakouts(frame, day, [line])

    assert out[0]["direction"] == "PUT"
    assert out[0]["time"] == int(idx[1].timestamp())
    assert out[0]["kind"] == "corte"


def test_breakout_no_marca_el_dia_del_replay():
    import pandas as pd
    from datetime import date, datetime, timedelta

    from powerTradeAi_djangoApp.engine.replay import _confirmed_breakouts
    from powerTradeAi_djangoApp.engine.session import NY

    day = date(2026, 8, 10)
    idx = pd.DatetimeIndex([
        datetime(2026, 8, 10, 9, 30, tzinfo=NY) + timedelta(minutes=15 * i)
        for i in range(4)
    ]).tz_convert("UTC")
    frame = pd.DataFrame({
        "open": [99, 100, 102, 103],
        "high": [100, 103, 105, 106],
        "low": [98, 99, 101, 103],
        "close": [99, 102, 103.5, 105],
        "volume": [100] * 4,
    }, index=idx)
    line = {
        "timeframe": "15m", "kind": "resistencia",
        "points": [
            {"time": int(idx[0].timestamp()), "value": 100},
            {"time": int(idx[-1].timestamp()), "value": 100},
        ],
    }

    assert _confirmed_breakouts(frame, day, [line]) == []

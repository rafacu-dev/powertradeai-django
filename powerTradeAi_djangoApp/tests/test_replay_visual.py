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


def test_trendlines_diagonales_exigen_tres_toques():
    from powerTradeAi_djangoApp.engine.replay import _best_diagonal_line

    line = _best_diagonal_line(
        [(0, 10.0), (4, 8.0)], n=6, timeframe="15m", kind="resistencia")

    assert line is None


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


def test_lineas_intradia_detectan_tendencias_cortas_del_dia():
    import pandas as pd
    from datetime import date, datetime, timedelta

    from powerTradeAi_djangoApp.engine.replay import (
        _confirmed_breakouts, _intraday_trendlines_15m,
    )
    from powerTradeAi_djangoApp.engine.session import NY

    replay_day = date(2026, 8, 10)
    idx = pd.DatetimeIndex([
        datetime(2026, 8, 7, 9, 30, tzinfo=NY) + timedelta(minutes=15 * i)
        for i in range(9)
    ]).tz_convert("UTC")
    frame = pd.DataFrame({
        "open":  [100, 99, 98, 97, 96, 95, 94, 95, 97],
        "high":  [101, 99, 98, 98, 96, 95, 95, 98, 100],
        "low":   [99, 97, 96, 95, 94, 93, 92, 94, 96],
        "close": [100, 98, 97, 96, 95, 94, 94, 97, 99],
        "volume": [100] * 9,
    }, index=idx)

    lines = _intraday_trendlines_15m(frame, replay_day)

    intraday_res = [
        line for line in lines
        if line["kind"] == "resistencia" and line.get("scope") == "intradia"
    ]
    assert intraday_res

    breakouts = _confirmed_breakouts(frame, replay_day, intraday_res)
    assert breakouts
    assert breakouts[0]["time"] == int(idx[7].timestamp())


def test_lineas_intradia_se_limitan_a_pocas_por_tipo_en_la_sesion():
    import pandas as pd
    from datetime import date, datetime, timedelta

    from powerTradeAi_djangoApp.engine.replay import _intraday_trendlines_15m
    from powerTradeAi_djangoApp.engine.session import NY

    replay_day = date(2026, 8, 10)
    idx = pd.DatetimeIndex([
        datetime(2026, 8, 7, 9, 30, tzinfo=NY) + timedelta(minutes=15 * i)
        for i in range(12)
    ]).tz_convert("UTC")
    frame = pd.DataFrame({
        "open":  [110, 108, 106, 104, 102, 100, 101, 102, 103, 104, 105, 106],
        "high":  [111, 109, 107, 105, 103, 101, 102, 103, 104, 105, 106, 107],
        "low":   [106, 104, 102, 100, 98, 96, 97, 98, 99, 100, 101, 102],
        "close": [108, 106, 104, 102, 100, 98, 100, 101, 102, 103, 104, 105],
        "volume": [100] * 12,
    }, index=idx)

    lines = _intraday_trendlines_15m(frame, replay_day)
    intraday = [line for line in lines if line.get("scope") == "intradia"]

    assert len([line for line in intraday if line["kind"] == "resistencia"]) <= 2
    assert len([line for line in intraday if line["kind"] == "soporte"]) <= 2


def test_lineas_intradia_usan_temporalidad_de_3m_y_duran_45_minutos():
    import pandas as pd
    from datetime import date, datetime, timedelta

    from powerTradeAi_djangoApp.engine.replay import _intraday_trendlines_3m
    from powerTradeAi_djangoApp.engine.session import NY

    replay_day = date(2026, 8, 10)
    idx = pd.DatetimeIndex([
        datetime(2026, 8, 7, 9, 30, tzinfo=NY) + timedelta(minutes=3 * i)
        for i in range(21)
    ]).tz_convert("UTC")
    frame = pd.DataFrame({
        "open":  [100, 101, 102, 103, 104, 105, 106, 107, 108, 109,
                  110, 111, 112, 113, 114, 115, 116, 117, 118, 119, 120],
        "high":  [101, 102, 103, 104, 105, 106, 107, 108, 109, 110,
                  111, 112, 113, 114, 115, 116, 117, 118, 119, 120, 121],
        "low":   [100, 102, 103, 104, 105, 106, 107, 108, 104, 106,
                  107, 108, 109, 110, 111, 112, 108, 110, 111, 112, 113],
        "close": [101, 102, 102, 104, 105, 106, 107, 108, 109, 110,
                  111, 112, 113, 114, 115, 116, 117, 118, 119, 120, 121],
        "volume": [100] * 21,
    }, index=idx)

    lines = _intraday_trendlines_3m(frame, replay_day)

    assert lines
    assert all(line["timeframe"] == "3m" for line in lines)
    assert all(line["touches"] >= 3 for line in lines)
    assert all(
        line["points"][-1]["time"] - line["points"][0]["time"] >= 45 * 60
        for line in lines
    )
    assert all(line["label"].startswith("3m intradia") for line in lines)


def test_lineas_intradia_no_cuentan_velas_consecutivas_como_tres_toques():
    import pandas as pd
    from datetime import date, datetime, timedelta

    from powerTradeAi_djangoApp.engine.replay import _intraday_trendlines_3m
    from powerTradeAi_djangoApp.engine.session import NY

    replay_day = date(2026, 8, 10)
    idx = pd.DatetimeIndex([
        datetime(2026, 8, 7, 9, 30, tzinfo=NY) + timedelta(minutes=3 * i)
        for i in range(8)
    ]).tz_convert("UTC")
    frame = pd.DataFrame({
        "open":  [99, 100, 101, 102, 103, 104, 105, 106],
        "high":  [101, 102, 103, 104, 105, 106, 107, 108],
        "low":   [100, 101, 102, 103, 110, 111, 112, 113],
        "close": [101, 102, 103, 104, 105, 106, 107, 108],
        "volume": [100] * 8,
    }, index=idx)

    support_lines = [
        line for line in _intraday_trendlines_3m(frame, replay_day)
        if line["kind"] == "soporte"
    ]

    assert support_lines == []


def test_lineas_intradia_rechazan_tendencias_menores_a_45_minutos():
    import pandas as pd
    from datetime import date, datetime, timedelta

    from powerTradeAi_djangoApp.engine.replay import _intraday_trendlines_3m
    from powerTradeAi_djangoApp.engine.session import NY

    replay_day = date(2026, 8, 10)
    idx = pd.DatetimeIndex([
        datetime(2026, 8, 7, 9, 30, tzinfo=NY) + timedelta(minutes=3 * i)
        for i in range(12)
    ]).tz_convert("UTC")
    frame = pd.DataFrame({
        "open":  list(range(100, 112)),
        "high":  list(range(101, 113)),
        "low":   [100, 101, 103, 104, 105, 106, 106, 107, 108, 109, 110, 111],
        "close": list(range(101, 113)),
        "volume": [100] * 12,
    }, index=idx)

    assert _intraday_trendlines_3m(frame, replay_day) == []


def test_lineas_intradia_exigen_dos_velas_intermedias_entre_toques():
    import pandas as pd
    from datetime import date, datetime, timedelta

    from powerTradeAi_djangoApp.engine.replay import _intraday_trendlines_3m
    from powerTradeAi_djangoApp.engine.session import NY

    replay_day = date(2026, 8, 10)
    idx = pd.DatetimeIndex([
        datetime(2026, 8, 7, 9, 30, tzinfo=NY) + timedelta(minutes=3 * i)
        for i in range(21)
    ]).tz_convert("UTC")
    frame = pd.DataFrame({
        "open":  list(range(100, 121)),
        "high":  list(range(101, 122)),
        "low":   [100, 101, 101, 103, 104, 105, 106, 107, 108, 109,
                  110, 111, 112, 113, 114, 115, 116, 112, 118, 119, 120],
        "close": list(range(101, 122)),
        "volume": [100] * 21,
    }, index=idx)

    assert _intraday_trendlines_3m(frame, replay_day) == []


def test_lineas_intradia_aceptan_toques_dentro_del_grosor_de_banda():
    import pandas as pd
    from datetime import date, datetime, timedelta

    from powerTradeAi_djangoApp.engine.replay import _intraday_trendlines_3m
    from powerTradeAi_djangoApp.engine.session import NY

    replay_day = date(2026, 8, 10)
    idx = pd.DatetimeIndex([
        datetime(2026, 8, 7, 9, 30, tzinfo=NY) + timedelta(minutes=3 * i)
        for i in range(21)
    ]).tz_convert("UTC")
    frame = pd.DataFrame({
        "open":  [780 + i * 0.2 for i in range(21)],
        "high":  [781 + i * 0.2 for i in range(21)],
        "low":   [780.0, 782.0, 783.0, 784.0, 785.0, 786.0, 787.0,
                  788.0, 781.1, 783.0, 784.0, 785.0, 786.0, 787.0,
                  788.0, 789.0, 782.2, 784.0, 785.0, 786.0, 787.0],
        "close": [780.5 + i * 0.2 for i in range(21)],
        "volume": [100] * 21,
    }, index=idx)

    support_lines = [
        line for line in _intraday_trendlines_3m(frame, replay_day)
        if line["kind"] == "soporte"
    ]

    assert support_lines


def test_lineas_intradia_detectan_impulsos_sin_pivotes_locales_clasicos():
    import pandas as pd
    from datetime import date, datetime, timedelta

    from powerTradeAi_djangoApp.engine.replay import _intraday_trendlines_3m
    from powerTradeAi_djangoApp.engine.session import NY

    replay_day = date(2026, 8, 10)
    idx = pd.DatetimeIndex([
        datetime(2026, 8, 7, 9, 30, tzinfo=NY) + timedelta(minutes=3 * i)
        for i in range(24)
    ]).tz_convert("UTC")
    lows = [
        100.0, 101.0, 102.0, 103.0, 104.0, 105.0,
        101.4, 103.0, 104.0, 105.0, 106.0, 107.0,
        102.8, 104.0, 105.0, 106.0, 107.0, 108.0,
        104.2, 106.0, 107.0, 108.0, 109.0, 110.0,
    ]
    frame = pd.DataFrame({
        "open":  [price + 0.7 for price in lows],
        "high":  [price + 1.2 for price in lows],
        "low":   lows,
        "close": [price + 0.9 for price in lows],
        "volume": [100] * len(lows),
    }, index=idx)

    support_lines = [
        line for line in _intraday_trendlines_3m(frame, replay_day)
        if line["kind"] == "soporte"
    ]

    assert support_lines

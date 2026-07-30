"""get_index_internals: breadth, dispersion y causalidad.

Lo critico es que en entrenamiento NO pueda ver velas cuyo cierre aun no
ocurrio: es el mismo fallo que la auditoria encontro en ``_bars_upto``.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from powerTradeAi_djangoApp.agent import skills

NY = ZoneInfo("America/New_York")


def _bars(start, n, tf_min, base, step):
    """Serie sintetica: ``step`` > 0 sube (tendencia alcista), < 0 baja."""
    idx = pd.date_range(start, periods=n, freq=f"{tf_min}min", tz="UTC")
    close = [base + step * i for i in range(n)]
    return pd.DataFrame(
        {"open": close, "high": [c + 0.1 for c in close],
         "low": [c - 0.1 for c in close], "close": close,
         "volume": [1000] * n},
        index=idx,
    )


class FakeProvider:
    """Todos suben salvo los de ``bajan``; registra los rangos pedidos."""

    def __init__(self, bajan=(), start=None):
        self.bajan = set(bajan)
        self.start = start or pd.Timestamp("2026-07-01 13:30", tz="UTC")

    def bars(self, symbol, start, end, tf):
        tf_min = {"1h": 60, "15m": 15, "1m": 1, "1d": 1440}[tf]
        n = 120 if tf == "1h" else 400
        step = -0.05 if symbol in self.bajan else 0.05
        # cada componente con un jitter distinto -> dispersion no nula
        jitter = (hash(symbol) % 7) * 0.002
        return _bars(self.start, n, tf_min, 100.0 + jitter, step + jitter / 100)

    def latest_price(self, symbol):
        return 100.0


@pytest.fixture(autouse=True)
def _fake(monkeypatch):
    monkeypatch.setattr(skills, "_provider", lambda: FakeProvider())


def test_breadth_total_cuando_todos_alineados():
    out = skills.get_index_internals({}, "QQQ", group="sectores")
    assert out["trend_1h_indice"] == "alcista"
    assert out["breadth"]["total"] == 8
    assert out["breadth"]["alineados"] == 8
    assert out["breadth"]["fraccion"] == 1.0
    assert out["alineacion_ponderada"] == 1.0
    assert out["regimen"].startswith(("bloque_", "fuerte_pero_disperso_",
                                     "alineado_"))


def test_breadth_parcial_marca_los_que_no_confirman(monkeypatch):
    monkeypatch.setattr(skills, "_provider",
                        lambda: FakeProvider(bajan={"SOXX", "SMH", "IBB"}))
    out = skills.get_index_internals({}, "QQQ", group="sectores")
    assert out["breadth"]["alineados"] == 5
    assert out["breadth"]["fraccion"] == round(5 / 8, 3)
    assert set(out["no_confirman"]) <= {"SOXX", "SMH", "IBB"}
    # la ponderada baja mas que la simple: SOXX+SMH pesan 0.35 de 1.0
    assert out["alineacion_ponderada"] < out["breadth"]["fraccion"]


def test_indice_plano_no_interpreta_breadth(monkeypatch):
    class Flat(FakeProvider):
        def bars(self, symbol, start, end, tf):
            df = super().bars(symbol, start, end, tf)
            if symbol == "QQQ":
                df["close"] = 100.0
                df["open"] = df["high"] = df["low"] = 100.0
            return df

    monkeypatch.setattr(skills, "_provider", lambda: Flat())
    out = skills.get_index_internals({}, "QQQ")
    assert out["trend_1h_indice"] == "plano"
    assert out["breadth"]["alineados"] == 0
    assert out["nota"] is not None
    assert out["regimen"] == "indefinido"


def test_grupo_megacaps_y_ambos():
    m = skills.get_index_internals({}, "QQQ", group="megacaps")
    assert m["breadth"]["total"] == 8
    assert {r["symbol"] for r in m["componentes"]} == set(skills._MEGACAPS)
    both = skills.get_index_internals({}, "QQQ", group="ambos")
    assert both["breadth"]["total"] == 16


def test_incluye_dispersion_y_como_interpretar():
    out = skills.get_index_internals({}, "QQQ")
    d = out["dispersion"]
    assert d["etiqueta"] in {"baja", "media", "alta"}
    assert 0 <= d["percentil_historico"] <= 100
    # la guia de interpretacion debe advertir el limite medido
    assert "limite_medido" in out["como_interpretar"]
    assert "opciones" in out["como_interpretar"]["limite_medido"]


def test_causalidad_no_mira_velas_sin_cerrar(monkeypatch):
    """En entrenamiento la ultima vela 1h usada debe haber CERRADO ya."""
    vistos = {}

    class Spy(FakeProvider):
        def bars(self, symbol, start, end, tf):
            df = super().bars(symbol, start, end, tf)
            if symbol == "QQQ" and tf == "1h":
                vistos["all"] = df
            return df

    monkeypatch.setattr(skills, "_provider", lambda: Spy())
    # as_of a mitad de una vela de 1h
    as_of = datetime(2026, 7, 6, 11, 40, tzinfo=NY)
    skills.get_index_internals({"as_of": as_of}, "QQQ")

    cutoff = pd.Timestamp(as_of) - pd.Timedelta(minutes=60)
    usadas = vistos["all"][vistos["all"].index <= cutoff]
    assert not usadas.empty
    # ninguna vela usada puede cerrar despues de as_of
    assert (usadas.index + pd.Timedelta(minutes=60) <= pd.Timestamp(as_of)).all()


def test_skill_registrada_en_el_catalogo():
    assert "get_index_internals" in skills.SKILLS
    names = {s["function"]["name"] for s in skills.tool_schemas()}
    assert "get_index_internals" in names

"""El comparador tiene que fallar cuando debe fallar.

Una herramienta de verificacion que pasa en vacio es peor que no tenerla: da
confianza sin fundamento. Estos tests fijan que ``compare_session`` detecte cada
tipo de divergencia, incluido el caso mas traicionero — que la app deje de
publicar un campo y la comparacion lo salte en silencio.
"""
from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest

from powerTradeAi_djangoApp.engine.golden import compare_session
from powerTradeAi_djangoApp.strategies.base import NY, Signal

SESSION = date(2026, 7, 17)


class FakeStrategy:
    """Devuelve una senal fija: aisla el comparador de la logica de la regla."""

    symbol = "SPY"

    def __init__(self, signal: Signal | None):
        self._signal = signal

    def evaluate(self, ctx):
        return self._signal


@pytest.fixture(autouse=True)
def _no_sweep(monkeypatch):
    """``detect_signal`` barre 390 minutos; aqui basta con una llamada."""
    def fake_detect(strategy, day, bars, provider, history_cache=None):
        signal = strategy.evaluate(None)
        return (signal, None) if signal is not None else (None, None)

    monkeypatch.setattr(
        "powerTradeAi_djangoApp.engine.golden.detect_signal", fake_detect)


class FakeProvider:
    name = "fake"

    def __init__(self, empty: bool = False):
        self._empty = empty

    def bars_1m(self, symbol, session_date):
        if self._empty:
            return pd.DataFrame()
        index = pd.DatetimeIndex(
            [pd.Timestamp(datetime(2026, 7, 17, 9, 30, tzinfo=NY))])
        return pd.DataFrame(
            {"open": [1.0], "high": [1.0], "low": [1.0],
             "close": [1.0], "volume": [1.0]}, index=index)


GOLDEN_ROW = {
    "day": "2026-07-17",
    "range_high": "742.66",
    "range_low": "740.80",
    "direction": "CALL",
    "signal_bar_ts": "2026-07-17 13:48:00",   # 09:48 ET en UTC
    "under_entry": "743.04",
    "replay_status": "ok",
}


def _signal(**overrides) -> Signal:
    meta = {
        "range_high": 742.66,
        "range_low": 740.80,
        "signal_bar_ts": pd.Timestamp(
            "2026-07-17 13:48:00", tz="UTC").isoformat(),
    }
    meta.update(overrides.pop("meta", {}))
    return Signal(
        direction=overrides.pop("direction", "CALL"),
        signal_ts=datetime(2026, 7, 17, 9, 49, tzinfo=NY),
        underlying=overrides.pop("underlying", 743.04),
        meta=meta,
    )


def _compare(signal) -> object:
    return compare_session(
        FakeStrategy(signal), GOLDEN_ROW, FakeProvider(), {})


# --- El caso bueno ------------------------------------------------------

def test_una_señal_identica_coincide():
    diff = _compare(_signal())
    assert diff.ok, diff.fields


def test_una_diferencia_de_un_centimo_se_tolera():
    """El rango sale de max/min sobre velas: un centimo es ruido de feed."""
    diff = _compare(_signal(meta={"range_high": 742.665}))
    assert diff.ok, diff.fields


# --- Divergencias que hay que cazar ------------------------------------

@pytest.mark.parametrize("signal_kwargs,expected_field", [
    ({"meta": {"range_high": 745.00}}, "range_high"),
    ({"meta": {"range_low": 735.00}}, "range_low"),
    ({"direction": "PUT"}, "direction"),
    ({"underlying": 750.00}, "under_entry"),
    ({"meta": {"signal_bar_ts": pd.Timestamp(
        "2026-07-17 13:52:00", tz="UTC").isoformat()}}, "signal_bar_ts"),
])
def test_caza_cada_tipo_de_divergencia(signal_kwargs, expected_field):
    diff = _compare(_signal(**signal_kwargs))
    assert not diff.ok
    assert expected_field in diff.fields


@pytest.mark.parametrize("missing", ["range_high", "range_low", "signal_bar_ts"])
def test_un_campo_ausente_es_divergencia_no_un_salto(missing):
    """El fallo mas caro: si la app deja de publicar un campo, la comparacion
    NO debe pasar en silencio."""
    signal = _signal()
    meta = dict(signal.meta)
    del meta[missing]
    diff = _compare(Signal(
        direction=signal.direction, signal_ts=signal.signal_ts,
        underlying=signal.underlying, meta=meta))

    assert not diff.ok, f"un {missing} ausente paso como coincidencia"
    assert missing in diff.fields
    assert "AUSENTE" in str(diff.fields[missing][1])


def test_sin_señal_es_divergencia():
    diff = _compare(None)
    assert not diff.ok
    assert "no detecta" in diff.note


def test_sin_velas_es_divergencia():
    diff = compare_session(
        FakeStrategy(_signal()), GOLDEN_ROW, FakeProvider(empty=True), {})
    assert not diff.ok
    assert "velas" in diff.note


def test_un_csv_sin_columnas_comparables_no_pasa_en_vacio():
    """Si el artefacto no trae ninguno de los campos que sabemos comparar,
    'coincide' seria una afirmacion sin respaldo."""
    diff = compare_session(
        FakeStrategy(_signal()),
        {"day": "2026-07-17", "otra_columna": "1.0"},
        FakeProvider(), {})
    assert not diff.ok
    assert "comparables" in diff.note

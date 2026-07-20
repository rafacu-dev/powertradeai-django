"""Ciclo completo con un proveedor falso: senal -> alerta pending -> cierre.

Sin red. El objetivo es que el contrato entre reglas, motor y modelos quede
fijado: si alguien rompe la causalidad o la convencion ask/bid, aqui salta.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from powerTradeAi_djangoApp.data.base import Quote
from powerTradeAi_djangoApp.engine.scanner import resolve_pending, scan_once
from powerTradeAi_djangoApp.models import Alert, Strategy
from powerTradeAi_djangoApp.strategies.base import NY, ScanContext
from powerTradeAi_djangoApp.strategies.orb15 import Orb15Base

SESSION = date(2026, 7, 15)


def _bars(closes: dict[str, float]) -> pd.DataFrame:
    """closes: {"09:30": 100.0, ...} en hora ET."""
    index, rows = [], []
    for hhmm, close in closes.items():
        hh, mm = (int(x) for x in hhmm.split(":"))
        index.append(pd.Timestamp(
            datetime(SESSION.year, SESSION.month, SESSION.day, hh, mm, tzinfo=NY)))
        rows.append({"open": close, "high": close, "low": close,
                     "close": close, "volume": 1000})
    return pd.DataFrame(rows, index=pd.DatetimeIndex(index).tz_convert("UTC"))


def _flat_range(level: float = 100.0) -> dict[str, float]:
    """Las 15 velas del rango, planas en ``level``."""
    return {f"09:{30 + i}": level for i in range(15)}


class FakeProvider:
    """Devuelve las velas que le des y una quote fija."""

    name = "fake"

    def __init__(self, bars: pd.DataFrame, quotes: dict | None = None):
        self._bars = bars
        self._quotes = quotes or {}
        self.default_quote = Quote(bid=1.00, ask=1.10)

    def bars_1m(self, symbol, session_date):
        return self._bars

    def option_quote(self, occ, at=None):
        return self._quotes.get("at_exit" if at else "at_entry", self.default_quote)

    def latest_price(self, symbol):
        return 100.0


# --- Deteccion ----------------------------------------------------------

def test_no_dispara_con_rango_incompleto():
    """Falta un minuto del rango: no hay senal, no se rellena el hueco."""
    incomplete = _flat_range()
    del incomplete["09:37"]
    bars = _bars({**incomplete, "09:45": 105.0})
    ctx = ScanContext(FakeProvider(bars), "SPY", SESSION,
                      datetime(2026, 7, 15, 9, 50, tzinfo=NY), bars)
    assert Orb15Base().evaluate(ctx) is None


def test_dispara_call_al_romper_por_arriba():
    bars = _bars({**_flat_range(), "09:45": 105.0})
    # 09:46: la vela 09:45 acaba de cerrar; la señal es fresca.
    ctx = ScanContext(FakeProvider(bars), "SPY", SESSION,
                      datetime(2026, 7, 15, 9, 46, tzinfo=NY), bars)
    signal = Orb15Base().evaluate(ctx)
    assert signal is not None
    assert signal.direction == "CALL"
    # La vela 09:45 solo se observa al cerrar, a las 09:46.
    assert signal.signal_ts.astimezone(NY).strftime("%H:%M") == "09:46"


def test_dispara_put_al_romper_por_abajo():
    bars = _bars({**_flat_range(), "09:45": 95.0})
    ctx = ScanContext(FakeProvider(bars), "SPY", SESSION,
                      datetime(2026, 7, 15, 9, 46, tzinfo=NY), bars)
    signal = Orb15Base().evaluate(ctx)
    assert signal is not None and signal.direction == "PUT"


def test_no_mira_la_vela_en_curso():
    """A las 09:45:30 la vela de 09:45 aun no ha cerrado: no puede disparar."""
    bars = _bars({**_flat_range(), "09:45": 105.0})
    ctx = ScanContext(FakeProvider(bars), "SPY", SESSION,
                      datetime(2026, 7, 15, 9, 45, 30, tzinfo=NY), bars)
    assert Orb15Base().evaluate(ctx) is None


def test_una_señal_orb_vieja_no_se_compra():
    """El productor original archiva la señal >90s como filtered_stale_signal.
    Un scanner reiniciado a las 10:00 no debe comprar el quiebre de 09:45."""
    bars = _bars({**_flat_range(), "09:45": 105.0})
    ctx = ScanContext(FakeProvider(bars), "SPY", SESSION,
                      datetime(2026, 7, 15, 10, 0, tzinfo=NY), bars)
    assert Orb15Base().evaluate(ctx) is None


def test_la_variante_0950_ignora_un_quiebre_de_las_0945():
    from powerTradeAi_djangoApp.strategies.orb15 import SpyOrb150950

    bars = _bars({**_flat_range(), "09:45": 105.0})
    ctx = ScanContext(FakeProvider(bars), "SPY", SESSION,
                      datetime(2026, 7, 15, 9, 46, tzinfo=NY), bars)
    assert SpyOrb150950().evaluate(ctx) is None
    assert Orb15Base().evaluate(ctx) is not None


# --- Ciclo completo contra la base de datos -----------------------------

@pytest.mark.django_db
def test_ciclo_completo_pending_y_cierre():
    strategy = Strategy.objects.create(
        strategy_id="SPY_ORB15_BASE", name="SPY ORB-15 apertura limpia",
        symbol="SPY", rule_version="orb15_base_causal_v3",
        params={}, contracts=2, commission=Decimal("1.30"),
    )
    bars = _bars({**_flat_range(), "09:45": 105.0})
    provider = FakeProvider(bars, {
        "at_entry": Quote(bid=1.00, ask=1.10),
        "at_exit": Quote(bid=1.60, ask=1.70),
    })

    # 1) Escaneo a las 09:47 (señal de 09:46, fresca): se abre la alerta.
    run = scan_once(datetime(2026, 7, 15, 9, 47, tzinfo=NY), provider)
    assert run.ok, run.error
    assert run.alerts_created == 1

    alert = Alert.objects.get()
    assert alert.status == Alert.Status.PENDING
    assert alert.direction == "CALL"
    assert alert.entry_premium == Decimal("1.1000")   # se paga el ASK
    assert alert.net_dollars is None                   # todavia no se sabe
    assert alert.contracts == 2

    # 2) Reescanear no duplica.
    scan_once(datetime(2026, 7, 15, 9, 48, tzinfo=NY), provider)
    assert Alert.objects.count() == 1

    # 3) Pasado el hold de 30 min, se cierra al BID.
    assert resolve_pending(
        datetime(2026, 7, 15, 10, 30, tzinfo=NY), provider) == 1

    alert.refresh_from_db()
    assert alert.status == Alert.Status.CLOSED
    assert alert.exit_premium == Decimal("1.6000")     # se cobra el BID
    assert alert.exit_reason == "time_exit"
    # (1.60 - 1.10) * 100 * 2 - 1.30 * 2 = 100.00 - 2.60 = 97.40
    assert alert.net_dollars == Decimal("97.40")
    # 97.40 / (1.10 * 100 * 2) = 44.27%
    assert alert.net_pct == Decimal("44.27")


@pytest.mark.django_db
def test_invalidacion_cierra_antes_que_el_reloj():
    strategy = Strategy.objects.create(
        strategy_id="SPY_ORB15_RANGE_INVALID", name="con invalidacion",
        symbol="SPY", rule_version="orb15_range_invalid_causal_v3", params={},
    )
    # Rompe a las 09:45 y vuelve dentro del rango a las 09:50.
    bars = _bars({**_flat_range(), "09:45": 105.0, "09:50": 100.0})
    provider = FakeProvider(bars, {
        "at_entry": Quote(bid=1.00, ask=1.10),
        "at_exit": Quote(bid=0.80, ask=0.90),
    })

    scan_once(datetime(2026, 7, 15, 9, 47, tzinfo=NY), provider)
    alert = Alert.objects.get()
    assert alert.status == Alert.Status.PENDING

    # 09:52, mucho antes del hold de 30 min: manda la invalidacion.
    assert resolve_pending(
        datetime(2026, 7, 15, 9, 52, tzinfo=NY), provider) == 1
    alert.refresh_from_db()
    assert alert.exit_reason == "range_invalidation"
    # (0.80 - 1.10) * 100 - 1.30 = -31.30
    assert alert.net_dollars == Decimal("-31.30")


@pytest.mark.django_db
def test_sin_quote_de_salida_no_se_inventa_un_resultado():
    Strategy.objects.create(
        strategy_id="SPY_ORB15_BASE", name="base", symbol="SPY",
        rule_version="orb15_base_causal_v3", params={},
    )
    bars = _bars({**_flat_range(), "09:45": 105.0})

    class NoExitQuote(FakeProvider):
        def option_quote(self, occ, at=None):
            return Quote(bid=1.00, ask=1.10) if at is None else None

    provider = NoExitQuote(bars)
    scan_once(datetime(2026, 7, 15, 9, 47, tzinfo=NY), provider)

    # Mismo dia, sin quote: se deja viva en vez de fabricar un cierre.
    assert resolve_pending(
        datetime(2026, 7, 15, 10, 30, tzinfo=NY), provider) == 0
    alert = Alert.objects.get()
    assert alert.status == Alert.Status.PENDING
    assert alert.net_dollars is None

    # Al dia siguiente ya no se puede resolver: EXPIRED, nunca un P&L inventado.
    assert resolve_pending(
        datetime(2026, 7, 16, 10, 0, tzinfo=NY), provider) == 1
    alert.refresh_from_db()
    assert alert.status == Alert.Status.EXPIRED
    assert alert.net_dollars is None

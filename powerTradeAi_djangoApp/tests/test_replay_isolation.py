"""La frontera entre alertas reales y reconstruidas.

Es la propiedad de correccion mas importante que introduce el replay: una
reconstruccion no sufrio latencia, no compitio por el fill y usa la quote del
instante teorico. Su P&L es un limite superior optimista. Si se cuela en un
agregado junto a operaciones reales, el numero resultante no significa nada —y
nadie se entera, porque sigue pareciendo un numero.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from powerTradeAi_djangoApp.models import Alert, ApiKey, Strategy

pytestmark = pytest.mark.django_db


def _strategy() -> Strategy:
    return Strategy.objects.create(
        strategy_id="SPY_ORB15_BASE", name="ORB base", symbol="SPY",
        rule_version="orb15_base_causal_v3", params={})


def _alert(strategy, *, source, day, net, direction="CALL") -> Alert:
    ts = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
    alert = Alert.objects.create(
        strategy=strategy, rule_version=strategy.rule_version, symbol="SPY",
        session_date=day, direction=direction, source=source,
        status=Alert.Status.CLOSED,
        signal_ts=ts, entry_ts=ts, exit_ts=ts,
        occ_symbol="SPY   260717C00743000", expiration=day,
        strike=Decimal("743"), entry_bid=Decimal("2.00"),
        entry_ask=Decimal("2.00"), entry_premium=Decimal("2.00"),
        exit_premium=Decimal("3.00"), exit_reason="time_exit", meta={},
    )
    # Se fija el neto a mano para que el test controle el signo.
    alert.net_dollars = Decimal(str(net))
    alert.net_pct = Decimal(str(net)) / Decimal("200") * 100
    alert.save(update_fields=["net_dollars", "net_pct"])
    return alert


@pytest.fixture
def client_and_key():
    from django.test import Client

    _, raw = ApiKey.generate("tests")
    return Client(), {"HTTP_AUTHORIZATION": f"Api-Key {raw}"}


# --- Modelo -------------------------------------------------------------

def test_live_y_replay_conviven_en_la_misma_sesion():
    """El unique constraint incluye ``source``: reconstruir un dia ya operado
    no debe chocar contra la alerta real ni pisarla."""
    strategy = _strategy()
    day = date(2026, 7, 17)
    live = _alert(strategy, source=Alert.Source.LIVE, day=day, net=-100)
    replay = _alert(strategy, source=Alert.Source.REPLAY, day=day, net=235)

    assert Alert.objects.count() == 2
    live.refresh_from_db()
    assert live.net_dollars == Decimal("-100")   # la real sigue intacta
    assert replay.source == Alert.Source.REPLAY


def test_una_regla_no_puede_duplicar_alerta_en_la_misma_fuente():
    from django.db import IntegrityError

    strategy = _strategy()
    day = date(2026, 7, 17)
    _alert(strategy, source=Alert.Source.LIVE, day=day, net=10)
    with pytest.raises(IntegrityError):
        _alert(strategy, source=Alert.Source.LIVE, day=day, net=20)


def test_por_defecto_una_alerta_nace_live():
    """Un descuido al crear una alerta debe caer del lado seguro."""
    strategy = _strategy()
    alert = Alert.objects.create(
        strategy=strategy, rule_version="v", symbol="SPY",
        session_date=date(2026, 7, 17), direction="CALL",
        signal_ts=datetime.now(timezone.utc), meta={})
    assert alert.source == Alert.Source.LIVE


# --- API ----------------------------------------------------------------

def test_el_listado_por_defecto_solo_devuelve_live(client_and_key):
    client, headers = client_and_key
    strategy = _strategy()
    _alert(strategy, source=Alert.Source.LIVE, day=date(2026, 7, 17), net=-100)
    _alert(strategy, source=Alert.Source.REPLAY, day=date(2026, 7, 17), net=235)

    rows = _rows(client.get("/api/alerts/", **headers))
    assert [r["source"] for r in rows] == ["live"]

    rows = _rows(client.get("/api/alerts/?source=replay", **headers))
    assert [r["source"] for r in rows] == ["replay"]

    rows = _rows(client.get("/api/alerts/?source=all", **headers))
    assert sorted(r["source"] for r in rows) == ["live", "replay"]


def test_el_agregado_no_mezcla_fuentes(client_and_key):
    """Una reconstruccion ganadora no puede maquillar una perdida real."""
    client, headers = client_and_key
    strategy = _strategy()
    _alert(strategy, source=Alert.Source.LIVE, day=date(2026, 7, 17), net=-100)
    _alert(strategy, source=Alert.Source.REPLAY, day=date(2026, 7, 17), net=235)

    live = _performance(client, headers, "")
    assert live["source"] == "live"
    assert live["alertas_cerradas"] == 1
    assert Decimal(live["neto_total"]) == Decimal("-100.00")

    replay = _performance(client, headers, "?source=replay")
    assert replay["source"] == "replay"
    assert replay["alertas_cerradas"] == 1
    assert Decimal(replay["neto_total"]) == Decimal("235.00")


def test_el_agregado_rechaza_source_all(client_and_key):
    """Mejor un 400 que una media sin significado."""
    client, headers = client_and_key
    _strategy()
    response = client.get(
        "/api/strategies/performance/?source=all", **headers)
    assert response.status_code == 400
    assert "mezclar" in str(response.json()).lower()


def test_el_endpoint_pending_tambien_respeta_la_fuente(client_and_key):
    client, headers = client_and_key
    strategy = _strategy()
    for source in (Alert.Source.LIVE, Alert.Source.REPLAY):
        Alert.objects.create(
            strategy=strategy, rule_version="v", symbol="SPY",
            session_date=date(2026, 7, 17), direction="CALL", source=source,
            status=Alert.Status.PENDING,
            signal_ts=datetime.now(timezone.utc), meta={})

    rows = client.get("/api/alerts/pending/", **headers).json()
    assert [r["source"] for r in rows] == ["live"]


# --- Utilidades ---------------------------------------------------------

def _rows(response):
    assert response.status_code == 200, response.content
    data = response.json()
    return data["results"] if isinstance(data, dict) else data


def _performance(client, headers, query):
    response = client.get(f"/api/strategies/performance/{query}", **headers)
    assert response.status_code == 200, response.content
    return [r for r in response.json()
            if r["strategy_id"] == "SPY_ORB15_BASE"][0]

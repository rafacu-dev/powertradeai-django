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
import json
from types import SimpleNamespace

import pytest
from django.http import HttpResponse

from powerTradeAi_djangoApp.models import Alert, ApiKey, ReplayRun, Strategy

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
        evaluation_version="investep_v2",
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


class _ReplayProvider:
    name = "test"


def _unsaved_replay(strategy, day, net=25) -> Alert:
    ts = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
    return Alert(
        strategy=strategy,
        rule_version=strategy.rule_version,
        symbol=strategy.symbol,
        session_date=day,
        direction=Alert.Direction.CALL,
        source=Alert.Source.REPLAY,
        evaluation_version="investep_v2",
        status=Alert.Status.CLOSED,
        signal_ts=ts,
        entry_ts=ts,
        exit_ts=ts,
        entry_premium=Decimal("1.00"),
        exit_premium=Decimal("1.25"),
        net_dollars=Decimal(str(net)),
        net_pct=Decimal(str(net)),
        meta={},
    )


def test_replay_sin_persistencia_no_escribe_nada(monkeypatch):
    from powerTradeAi_djangoApp.engine import replay

    _strategy()
    day = date(2026, 7, 17)
    monkeypatch.setattr(
        replay, "_replay_strategy",
        lambda row, session, provider: _unsaved_replay(row, session),
    )

    result = replay.replay_day(day, provider=_ReplayProvider(), persist=False)

    assert len(result.alerts) == 1
    assert Alert.objects.count() == 0
    assert ReplayRun.objects.count() == 0


def test_replay_overwrite_es_atomico_ante_fallo(monkeypatch):
    from powerTradeAi_djangoApp.engine import replay

    first = _strategy()
    second = Strategy.objects.create(
        strategy_id="ZZ_TEST_REPLAY", name="segunda", symbol="QQQ",
        rule_version="v1", params={}, enabled=True,
    )
    day = date(2026, 7, 17)
    previous = _alert(
        first, source=Alert.Source.REPLAY, day=day, net=77)

    def calculate(row, session, provider):
        if row.pk == second.pk:
            raise RuntimeError("feed incompleto")
        return _unsaved_replay(row, session, net=-50)

    monkeypatch.setattr(replay, "_replay_strategy", calculate)
    result = replay.replay_day(
        day, provider=_ReplayProvider(), overwrite=True, persist=True)

    previous.refresh_from_db()
    assert previous.net_dollars == Decimal("77")
    assert Alert.objects.filter(source=Alert.Source.REPLAY).count() == 1
    assert result.alerts == []
    assert result.errors[0][0] == second.strategy_id
    audit = ReplayRun.objects.get()
    assert audit.status == ReplayRun.Status.ERROR
    assert audit.alerts_created == 0


def test_replay_separa_timestamp_de_senal_y_ejecucion(monkeypatch):
    from types import SimpleNamespace

    import pandas as pd

    from powerTradeAi_djangoApp.engine import replay

    row = _strategy()
    day = date(2026, 7, 17)
    signal_ts = datetime(2026, 7, 17, 9, 31, tzinfo=timezone.utc)
    entry_ts = signal_ts + pd.Timedelta(seconds=12)
    exit_ts = signal_ts + pd.Timedelta(minutes=5)
    signal = SimpleNamespace(
        direction="CALL", signal_ts=signal_ts, underlying=100.0, meta={})
    entry_quote = SimpleNamespace(
        ts=entry_ts, bid=0.95, ask=1.00, is_live=True)
    exit_quote = SimpleNamespace(
        ts=exit_ts, bid=1.10, ask=1.12, is_live=True)

    class Provider:
        def bars_1m(self, symbol, session):
            return pd.DataFrame(
                {"close": [100.0]},
                index=pd.DatetimeIndex([signal_ts]),
            )

        def option_quote(self, occ, at=None):
            return exit_quote

    class Rule:
        def __init__(self, params):
            pass

        def select_contract(self, ctx, candidate, at=None):
            return "SPY   260717C00100000", day, 100.0, entry_quote

        def scheduled_exit(self, at):
            return at + pd.Timedelta(minutes=5)

    monkeypatch.setattr(replay, "get_strategy_class", lambda strategy_id: Rule)
    monkeypatch.setattr(
        replay, "detect_signal",
        lambda strategy, session, bars, provider, cache: (signal, signal_ts),
    )
    monkeypatch.setattr(
        replay, "_find_exit",
        lambda strategy, context, alert, session: (exit_ts, "time_exit"),
    )

    alert = replay._replay_strategy(row, day, Provider())

    assert alert.signal_ts == signal_ts
    assert alert.entry_ts == entry_ts


def test_dashboard_replay_es_solo_calculo(monkeypatch):
    from django.test import RequestFactory

    from powerTradeAi_djangoApp import dashboard
    from powerTradeAi_djangoApp.engine import replay

    captured = {}

    def calculate(day, **kwargs):
        captured.update(kwargs)
        return replay.ReplayResult(day=day)

    monkeypatch.setattr(replay, "replay_day", calculate)
    request = RequestFactory().post("/replay/", {"date": "2026-07-17"})
    request.user = SimpleNamespace(is_active=True, is_staff=True)

    response = dashboard.replay_action(request)

    assert response.status_code == 200
    assert captured == {"persist": False, "overwrite": False}


def test_dashboard_replay_puede_guardar_en_tabla(monkeypatch):
    from django.test import RequestFactory

    from powerTradeAi_djangoApp import dashboard
    from powerTradeAi_djangoApp.engine import replay

    captured = {}

    def calculate(day, **kwargs):
        captured.update(kwargs)
        return replay.ReplayResult(day=day)

    monkeypatch.setattr(replay, "replay_day", calculate)
    request = RequestFactory().post(
        "/replay/", {"date": "2026-07-17", "save": "1", "overwrite": "1"})
    request.user = SimpleNamespace(is_active=True, is_staff=True)

    response = dashboard.replay_action(request)

    assert response.status_code == 200
    assert captured == {"persist": True, "overwrite": True}
    payload = json.loads(response.content)
    assert payload["saved"] is True
    assert payload["overwritten"] is True


def test_dashboard_muestra_replay_legacy_sin_filtro_oculto(rf, monkeypatch):
    from powerTradeAi_djangoApp import dashboard

    strategy = _strategy()
    alert = _alert(
        strategy,
        source=Alert.Source.REPLAY,
        day=date(2026, 8, 26),
        net=125,
    )
    alert.evaluation_version = "legacy_v1"
    alert.save(update_fields=["evaluation_version"])

    request = rf.get(
        "/panel/",
        {"source": "replay", "desde": "2026-08-26", "hasta": "2026-08-26"},
    )
    request.user = SimpleNamespace(is_active=True, is_staff=True)
    captured = {}

    def capture_render(request, template, context):
        captured.update(context)
        return HttpResponse("ok")

    monkeypatch.setattr(dashboard, "render", capture_render)

    response = dashboard.dashboard(request)

    assert response.status_code == 200
    assert list(captured["alerts"]) == [alert]
    assert captured["filters"]["evaluation_version"] == "all"


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

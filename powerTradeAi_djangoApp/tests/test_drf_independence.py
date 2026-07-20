"""La app no debe depender de la configuracion global de DRF.

Se instala en proyectos que ya tienen su propio ``REST_FRAMEWORK`` con otra
autenticacion y otros permisos por defecto. Dos exigencias simetricas:

  1. Los endpoints de la app siguen pidiendo ApiKey aunque el proyecto
     anfitrion tenga ``AllowAny`` por defecto. Si esto falla, instalar la app
     expone las alertas a cualquiera.
  2. La app no necesita que el anfitrion anada nada a ``REST_FRAMEWORK``, para
     que nadie tenga que pegar un bloque global que alteraria SUS endpoints.

Estos tests simulan el settings de un anfitrion hostil: defaults opuestos a los
que la app querria.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from django.test import Client, override_settings

from powerTradeAi_djangoApp.models import Alert, ApiKey, Strategy

pytestmark = pytest.mark.django_db

# Lo que podria tener un proyecto cualquiera: sesion + acceso abierto.
HOSTILE_DRF = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.AllowAny",
    ],
}


@pytest.fixture
def alerta():
    strategy = Strategy.objects.create(
        strategy_id="SPY_ORB15_BASE", name="ORB", symbol="SPY",
        rule_version="v1", params={})
    Alert.objects.create(
        strategy=strategy, rule_version="v1", symbol="SPY",
        session_date=date(2026, 7, 17), direction="CALL",
        signal_ts=datetime.now(timezone.utc), meta={})
    return strategy


@override_settings(REST_FRAMEWORK=HOSTILE_DRF)
def test_sigue_exigiendo_apikey_con_allowany_global(alerta):
    """Lo critico: un anfitrion con AllowAny no abre las alertas."""
    assert Client().get("/api/alerts/").status_code == 401


@override_settings(REST_FRAMEWORK=HOSTILE_DRF)
def test_funciona_sin_que_el_anfitrion_configure_nada(alerta):
    """La app no necesita tocar el REST_FRAMEWORK del proyecto."""
    _, raw = ApiKey.generate("anfitrion")
    response = Client().get(
        "/api/alerts/", HTTP_AUTHORIZATION=f"Api-Key {raw}")
    assert response.status_code == 200
    data = response.json()
    rows = data["results"] if isinstance(data, dict) else data
    assert len(rows) == 1


@override_settings(REST_FRAMEWORK=HOSTILE_DRF)
def test_una_clave_revocada_sigue_rechazada(alerta):
    key, raw = ApiKey.generate("revocada")
    key.revoke()
    assert Client().get(
        "/api/alerts/", HTTP_AUTHORIZATION=f"Api-Key {raw}"
    ).status_code == 401


@override_settings(REST_FRAMEWORK={})
def test_funciona_con_rest_framework_vacio(alerta):
    """Sin ninguna configuracion global, los defaults de DRF son AllowAny +
    Session. La app debe seguir siendo la unica que decide sobre lo suyo."""
    assert Client().get("/api/alerts/").status_code == 401

    _, raw = ApiKey.generate("vacio")
    assert Client().get(
        "/api/alerts/", HTTP_AUTHORIZATION=f"Api-Key {raw}"
    ).status_code == 200


@override_settings(REST_FRAMEWORK={
    **HOSTILE_DRF,
    "DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.AnonRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {"anon": "1/day"},
})
def test_el_throttle_del_anfitrion_no_bloquea_al_worker(alerta):
    """Un throttle anonimo del proyecto no debe cortar las lecturas con clave:
    la peticion esta autenticada, no es anonima."""
    _, raw = ApiKey.generate("throttle")
    headers = {"HTTP_AUTHORIZATION": f"Api-Key {raw}"}
    for _ in range(3):
        assert Client().get("/api/alerts/", **headers).status_code == 200

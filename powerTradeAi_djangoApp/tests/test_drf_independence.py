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

from powerTradeAi_djangoApp.models import (
    AgentRun,
    Alert,
    ApiKey,
    ReplayRun,
    ScanRun,
    Strategy,
)

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


def test_transcript_exige_scope_y_redacta_secretos():
    run = AgentRun.objects.create(
        status=AgentRun.Status.DONE,
        goal="usa DEEPSEEK_API_KEY=sk-abcdefghijklmnop",
        summary="token=supersecreto",
        transcript=[{
            "role": "tool",
            "args": {"api_key": "secreto", "symbol": "TSLA"},
            "result": {
                "authorization": "Bearer secreto",
                "detail": "Authorization: Bearer sk-abcdefghijklmnop",
            },
        }],
    )
    _, read_raw = ApiKey.generate("lectura")
    read_headers = {"HTTP_AUTHORIZATION": f"Api-Key {read_raw}"}
    listing = Client().get("/api/agent-runs/", **read_headers)
    assert listing.status_code == 200
    payload = listing.json()
    listed = (payload["results"] if isinstance(payload, dict) else payload)[0]
    assert "sk-" not in listed["goal"]
    assert "supersecreto" not in listed["summary"]
    assert Client().get(
        f"/api/agent-runs/{run.id}/", **read_headers).status_code == 403

    _, transcript_raw = ApiKey.generate("auditoria", scopes=["transcript"])
    response = Client().get(
        f"/api/agent-runs/{run.id}/",
        HTTP_AUTHORIZATION=f"Api-Key {transcript_raw}",
    )
    assert response.status_code == 200
    step = response.json()["transcript"][0]
    assert step["args"]["api_key"] == "[REDACTED]"
    assert step["args"]["symbol"] == "TSLA"
    assert step["result"]["authorization"] == "[REDACTED]"
    assert "sk-" not in step["result"]["detail"]


def test_replay_exige_scope_separado():
    _, read_raw = ApiKey.generate("lectura")
    assert Client().post(
        "/api/replay/", data={}, content_type="application/json",
        HTTP_AUTHORIZATION=f"Api-Key {read_raw}",
    ).status_code == 403

    _, replay_raw = ApiKey.generate("replay", scopes=["replay"])
    response = Client().post(
        "/api/replay/", data={}, content_type="application/json",
        HTTP_AUTHORIZATION=f"Api-Key {replay_raw}",
    )
    assert response.status_code == 400

    empty_strategy = Client().post(
        "/api/replay/",
        data={
            "desde": "2026-07-06",
            "hasta": "2026-07-06",
            "strategy": [],
        },
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Api-Key {replay_raw}",
    )
    assert empty_strategy.status_code == 400


def test_alert_meta_tambien_redacta_secretos(alerta):
    Alert.objects.update(meta={
        "deepseek_api_key": "sk-abcdefghijklmnop",
        "detail": "token=supersecreto",
    })
    _, raw = ApiKey.generate("lectura")
    response = Client().get(
        "/api/alerts/", HTTP_AUTHORIZATION=f"Api-Key {raw}")
    payload = response.json()
    row = (payload["results"] if isinstance(payload, dict) else payload)[0]
    assert row["meta"]["deepseek_api_key"] == "[REDACTED]"
    assert "supersecreto" not in row["meta"]["detail"]


def test_auditorias_paginadas_permiten_recuperar_un_registro():
    scan = ScanRun.objects.create(ok=True)
    replay = ReplayRun.objects.create(session_date=date(2026, 7, 17))
    _, raw = ApiKey.generate("lectura")
    headers = {"HTTP_AUTHORIZATION": f"Api-Key {raw}"}

    assert Client().get(f"/api/scans/{scan.id}/", **headers).status_code == 200
    assert Client().get(
        f"/api/replay-runs/{replay.id}/", **headers).status_code == 200

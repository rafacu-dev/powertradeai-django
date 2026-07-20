"""El proceso web no debe cargar pandas.

La API de la app solo lee modelos: no evalua reglas ni toca datos de mercado.
Si arrancar Django importase las estrategias, cada worker de gunicorn del
proyecto anfitrion pagaria ~100 MB de pandas + numpy residentes sin usarlos —
caro en un plan de 512 MB.

Se comprueba en un subproceso limpio porque en la suite pandas ya esta
importado por otros tests: aqui haria falsos negativos.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap

SETTINGS = """
import django
from django.conf import settings
settings.configure(
    DEBUG=True, SECRET_KEY="x", USE_TZ=True,
    DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3",
                           "NAME": ":memory:"}},
    INSTALLED_APPS=["django.contrib.contenttypes", "django.contrib.auth",
                    "rest_framework", "powerTradeAi_djangoApp"],
    ROOT_URLCONF="powerTradeAi_djangoApp.tests.urls",
    DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
    POWERTRADEAI={},
)
django.setup()
"""


def _run(body: str) -> str:
    # El dedent va solo al cuerpo: SETTINGS ya esta a nivel cero.
    script = SETTINGS + textwrap.dedent(body)
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_arrancar_django_no_importa_pandas():
    """El caso que paga el anfitrion en cada worker de gunicorn."""
    out = _run("""
        import sys
        print("pandas" in sys.modules, "numpy" in sys.modules)
    """)
    assert out == "False False", (
        f"arrancar Django importo pandas/numpy ({out}); el proceso web "
        "estaria pagando ~100 MB que no usa")


def test_servir_la_api_tampoco_importa_pandas():
    """Ni siquiera atendiendo una peticion real a los endpoints."""
    out = _run("""
        import sys
        from django.test import Client
        from django.core.management import call_command
        call_command("migrate", verbosity=0, run_syncdb=True)
        status = Client().get("/api/alerts/").status_code
        print(status, "pandas" in sys.modules)
    """)
    assert out == "401 False", (
        f"servir la API importo pandas o no exigio auth ({out})")


def test_el_motor_si_carga_las_reglas_cuando_hace_falta():
    """La contrapartida: quien evalua reglas sigue teniendo el catalogo."""
    out = _run("""
        from powerTradeAi_djangoApp.strategies import all_strategies
        print(len(all_strategies()))
    """)
    assert out == "14", f"el catalogo deberia tener 14 reglas, tiene {out}"

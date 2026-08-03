"""Configura un Django minimo para poder testear la app fuera de un proyecto."""
from __future__ import annotations

import django
from django.conf import settings


def pytest_configure():
    if settings.configured:
        return
    settings.configure(
        DEBUG=True,
        USE_TZ=True,
        TIME_ZONE="UTC",
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": ":memory:",
            }
        },
        INSTALLED_APPS=[
            "django.contrib.contenttypes",
            "django.contrib.auth",
            "rest_framework",
            "powerTradeAi_djangoApp",
        ],
        ROOT_URLCONF="powerTradeAi_djangoApp.tests.urls",
        # Sin esto las plantillas del dashboard no se pueden renderizar en un
        # test, y hasta ahora no tenian ninguna cobertura: un {% url %} mal
        # escrito solo se descubria ya desplegado.
        TEMPLATES=[{
            "BACKEND": "django.template.backends.django.DjangoTemplates",
            "APP_DIRS": True,
            "OPTIONS": {"context_processors": []},
        }],
        REST_FRAMEWORK={
            "DEFAULT_AUTHENTICATION_CLASSES": [
                "powerTradeAi_djangoApp.auth.ApiKeyAuthentication",
            ],
            "DEFAULT_PERMISSION_CLASSES": [
                "rest_framework.permissions.IsAuthenticated",
            ],
        },
        DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
        POWERTRADEAI={"MARKET_DATA_PROVIDER": "thetadata"},
    )
    django.setup()

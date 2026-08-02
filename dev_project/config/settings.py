"""Proyecto Django minimo para correr PowerTradeAI en local.

No es para produccion: SQLite, DEBUG on y una SECRET_KEY de desarrollo. Sirve
para dos cosas — probar la app contra datos reales sin desplegar, y documentar
con codigo que funciona exactamente que hay que anadir a un proyecto anfitrion.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
# La app vive en el directorio padre del proyecto (la raiz del repo).
REPO_ROOT = BASE_DIR.parent

def _load_dotenv(path: Path) -> None:
    """Carga un .env sin dependencias externas. No pisa el entorno existente."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv(REPO_ROOT / ".env")


def _json_env(name: str, default):
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{name} debe contener JSON valido") from exc

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-no-usar-en-produccion")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = ["*"]

# --- Lo que hay que copiar a tu proyecto real --------------------------

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "powerTradeAi_djangoApp",
]

POWERTRADEAI = {
    # "hybrid" es la unica combinacion que cubre el ciclo completo con las
    # suscripciones actuales: Alpaca no tiene quotes historicas de opciones y
    # ThetaData FREE deniega los endpoints de acciones.
    "MARKET_DATA_PROVIDER": os.environ.get("MARKET_DATA_PROVIDER", "hybrid"),
    "HYBRID_STOCK_PROVIDER": os.environ.get("HYBRID_STOCK_PROVIDER", "alpaca"),
    "HYBRID_OPTION_PROVIDER": os.environ.get("HYBRID_OPTION_PROVIDER", "thetadata"),
    "THETADATA_API_KEY": os.environ.get("THETADATA_API_KEY"),
    "ALPACA_API_KEY": os.environ.get("ALPACA_API_KEY"),
    # El .env del repo usa ALPACA_SECRET_KEY; alpaca-py documenta
    # APCA_API_SECRET_KEY. Se aceptan los tres nombres.
    "ALPACA_API_SECRET": (
        os.environ.get("ALPACA_API_SECRET")
        or os.environ.get("ALPACA_SECRET_KEY")
        or os.environ.get("APCA_API_SECRET_KEY")
    ),
    "ALPACA_FEED": os.environ.get("ALPACA_FEED", "iex"),
    "AGENT_LLM": {
        "BASE_URL": os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        "API_KEY": os.environ.get("DEEPSEEK_API_KEY"),
        "MODEL": os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
        "TIMEOUT_SECONDS": int(os.environ.get("DEEPSEEK_TIMEOUT_SECONDS", "45")),
        "MAX_RETRIES": int(os.environ.get("DEEPSEEK_MAX_RETRIES", "1")),
    },
    # Universo operativo versionado. El catalogo conserva mas simbolos para
    # investigacion, pero seed_strategies solo activa estos tres.
    "INVESTEP_WATCHLIST": tuple(
        symbol.strip().upper()
        for symbol in os.environ.get(
            "INVESTEP_WATCHLIST", "TSLA,SPY,QQQ").split(",")
        if symbol.strip()
    ),
    "ACCOUNT_SIZE": float(os.environ.get(
        "ACCOUNT_SIZE", os.environ.get("PAPER_ACCOUNT", "10000"))),
    "RISK_PCT_PER_TRADE": float(os.environ.get("RISK_PCT_PER_TRADE", "2")),
    "MAX_CONTRACTS_PER_TRADE": int(
        os.environ.get("MAX_CONTRACTS_PER_TRADE", "5")),
    "MAX_DECISION_AGE_SECONDS": int(
        os.environ.get("MAX_DECISION_AGE_SECONDS", "180")),
    "MAX_OPTION_SPREAD_PCT": float(
        os.environ.get("MAX_OPTION_SPREAD_PCT", "5")),
    "MAX_OPTION_QUOTE_AGE_SECONDS": int(
        os.environ.get("MAX_OPTION_QUOTE_AGE_SECONDS", "30")),
    "MAX_OPTION_QUOTE_FUTURE_SKEW_SECONDS": int(
        os.environ.get("MAX_OPTION_QUOTE_FUTURE_SKEW_SECONDS", "2")),
    "MAX_HISTORICAL_OPTION_QUOTE_DELAY_SECONDS": int(
        os.environ.get("MAX_HISTORICAL_OPTION_QUOTE_DELAY_SECONDS", "90")),
    "REQUIRE_OPTION_QUOTE_TIMESTAMP": True,
    # Sin cobertura o modelo configurados, el validador devuelve PENDING_* y no
    # crea alertas. Esto es deliberado: el manual prohibe completar esos vacios.
    "EVENT_CALENDAR_COVERAGE_FROM": os.environ.get(
        "EVENT_CALENDAR_COVERAGE_FROM"),
    "EVENT_CALENDAR_COVERAGE_UNTIL": os.environ.get(
        "EVENT_CALENDAR_COVERAGE_UNTIL"),
    "EVENT_CALENDAR": _json_env("EVENT_CALENDAR_JSON", []),
    "SPOT_PREMIUM_MODELS": _json_env("SPOT_PREMIUM_MODELS_JSON", {}),
    "MIN_SPOT_PREMIUM_SAMPLES": int(
        os.environ.get("MIN_SPOT_PREMIUM_SAMPLES", "20")),
}

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "powerTradeAi_djangoApp.auth.ApiKeyAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_PAGINATION_CLASS":
        "rest_framework.pagination.LimitOffsetPagination",
    "PAGE_SIZE": 100,
}

# --- Boilerplate de Django ---------------------------------------------

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "powertradeai.sqlite3",
    }
}

AUTH_PASSWORD_VALIDATORS = []
LANGUAGE_CODE = "es-es"
# UTC en base de datos; las reglas convierten a ET donde hace falta.
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {
        "powerTradeAi_djangoApp": {
            "handlers": ["console"],
            "level": os.environ.get("PTAI_LOG_LEVEL", "INFO"),
        },
    },
}

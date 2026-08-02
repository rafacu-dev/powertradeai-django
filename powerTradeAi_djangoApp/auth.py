"""Autenticacion por ApiKey para DRF.

La clave viaja en ``Authorization: Api-Key <valor>``. Se compara por hash, asi
que una lectura de la base de datos no revela ninguna clave utilizable.
"""
from __future__ import annotations

from django.utils import timezone
from rest_framework import authentication, exceptions
from rest_framework.permissions import BasePermission
from rest_framework.throttling import SimpleRateThrottle

from .models import ApiKey

HEADER_KEYWORD = "Api-Key"


class ApiKeyUser:
    """Identidad minima para DRF. No es un usuario de Django: la app no
    necesita cuentas, solo saber que clave llamo."""

    is_authenticated = True

    def __init__(self, api_key: ApiKey):
        self.api_key = api_key

    def __str__(self) -> str:
        return f"apikey:{self.api_key.prefix}"


class ApiKeyAuthentication(authentication.BaseAuthentication):
    keyword = HEADER_KEYWORD

    def authenticate(self, request):
        try:
            header = authentication.get_authorization_header(request).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise exceptions.AuthenticationFailed(
                "Authorization invalido.") from exc
        if not header:
            return None
        parts = header.split()
        if len(parts) != 2 or parts[0].lower() != self.keyword.lower():
            # Otro esquema (Bearer, Basic): no es asunto nuestro.
            return None

        raw_key = parts[1]
        try:
            api_key = ApiKey.objects.get(key_hash=ApiKey.hash_key(raw_key))
        except ApiKey.DoesNotExist:
            raise exceptions.AuthenticationFailed("API key invalida.")

        if not api_key.is_active:
            raise exceptions.AuthenticationFailed("API key revocada.")

        # Escritura barata y sin condicion de carrera relevante: solo es
        # telemetria de uso, no participa en la decision de autenticar.
        ApiKey.objects.filter(pk=api_key.pk).update(last_used_at=timezone.now())
        return (ApiKeyUser(api_key), api_key)

    def authenticate_header(self, request) -> str:
        return self.keyword


class ApiKeyScopePermission(BasePermission):
    """Exige el scope declarado por la vista despues de autenticar la clave."""

    message = "La API key no tiene el scope requerido."

    def has_permission(self, request, view):
        key = getattr(request, "auth", None)
        if key is None:
            return False
        scopes = set(key.scopes or ["read"])
        required = getattr(view, "required_scope", "read")
        if isinstance(required, dict):
            required = required.get(getattr(view, "action", None), "read")
        return "*" in scopes or required in scopes


class ApiKeyRateThrottle(SimpleRateThrottle):
    scope = "powertradeai_api"
    rate = "120/min"

    def get_cache_key(self, request, view):
        key = getattr(request, "auth", None)
        if key is None:
            return None
        return self.cache_format % {"scope": self.scope, "ident": key.pk}


class ReplayRateThrottle(ApiKeyRateThrottle):
    scope = "powertradeai_replay"
    rate = "2/min"

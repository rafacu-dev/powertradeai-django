"""urlconf de los tests: monta la API en la raiz."""
from django.urls import include, path

urlpatterns = [path("api/", include("powerTradeAi_djangoApp.api.urls"))]

"""urlconf de los tests: monta la API en la raiz y el dashboard bajo /panel/.

El dashboard se monta para que las plantillas puedan resolver sus
``{% url 'powertradeai:...' %}``. De paso, un nombre de ruta mal escrito en una
plantilla falla aqui en vez de ya desplegado.
"""
from django.urls import include, path

urlpatterns = [
    path("api/", include("powerTradeAi_djangoApp.api.urls")),
    path("panel/", include("powerTradeAi_djangoApp.urls")),
]

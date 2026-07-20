from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/powertradeai/", include("powerTradeAi_djangoApp.api.urls")),
]

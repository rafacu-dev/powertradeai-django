"""URLs del dashboard standalone (no API).

En el proyecto anfitrion:

    path("powertradeai/", include("powerTradeAi_djangoApp.urls")),
"""
from django.urls import path

from .dashboard import dashboard, replay_action

app_name = "powertradeai"

urlpatterns = [
    path("", dashboard, name="dashboard"),
    path("replay/", replay_action, name="replay_action"),
]

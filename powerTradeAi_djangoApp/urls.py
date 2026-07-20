"""URLs del dashboard standalone (no API).

En el proyecto anfitrion:

    path("powertradeai/", include("powerTradeAi_djangoApp.urls")),
"""
from django.urls import path

from .dashboard import chart_data, chart_view, dashboard, replay_action

app_name = "powertradeai"

urlpatterns = [
    path("", dashboard, name="dashboard"),
    path("replay/", replay_action, name="replay_action"),
    path("chart/", chart_view, name="chart"),
    path("chart/data/", chart_data, name="chart_data"),
]

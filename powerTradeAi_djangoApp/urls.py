"""URLs del dashboard standalone (no API).

En el proyecto anfitrion:

    path("powertradeai/", include("powerTradeAi_djangoApp.urls")),
"""
from django.urls import path

from .dashboard import (
    agent_launch, agent_train_launch, agent_view, chart_chat, chart_data,
    chart_price, chart_view, convexidad_data, convexidad_view, dashboard,
    replay_action, replay_data, replay_view, scanner_data,
)

app_name = "powertradeai"

urlpatterns = [
    path("", dashboard, name="dashboard"),
    path("replay/", replay_view, name="replay"),
    path("replay/data/", replay_data, name="replay_data"),
    path("replay/run/", replay_action, name="replay_action"),
    path("chart/", chart_view, name="chart"),
    path("chart/data/", chart_data, name="chart_data"),
    path("chart/price/", chart_price, name="chart_price"),
    path("chart/chat/", chart_chat, name="chart_chat"),
    path("scanner/data/", scanner_data, name="scanner_data"),
    path("agent/", agent_view, name="agent"),
    path("agent/launch/", agent_launch, name="agent_launch"),
    path("agent/train/", agent_train_launch, name="agent_train_launch"),
    path("convexidad/", convexidad_view, name="convexidad"),
    path("convexidad/data/", convexidad_data, name="convexidad_data"),
]

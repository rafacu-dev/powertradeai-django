"""Rutas de la app. En el proyecto anfitrion:

    path("api/powertradeai/", include("powerTradeAi_djangoApp.api.urls")),
"""
from __future__ import annotations

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    AgentAnalysisViewSet,
    AgentNoteViewSet,
    AgentRunViewSet,
    AgentTriggerViewSet,
    AlertViewSet,
    ReplayView,
    ScanRunViewSet,
    StrategyViewSet,
)

app_name = "powertradeai-api"

router = DefaultRouter()
router.register("alerts", AlertViewSet, basename="alert")
router.register("strategies", StrategyViewSet, basename="strategy")
router.register("scans", ScanRunViewSet, basename="scan")
router.register("agent-runs", AgentRunViewSet, basename="agent-run")
router.register("agent-analyses", AgentAnalysisViewSet, basename="agent-analysis")
router.register("agent-notes", AgentNoteViewSet, basename="agent-note")
router.register("agent-triggers", AgentTriggerViewSet, basename="agent-trigger")

urlpatterns = [
    path("replay/", ReplayView.as_view(), name="replay"),
    path("", include(router.urls)),
]

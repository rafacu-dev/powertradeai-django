from django.template.loader import render_to_string
from django.urls import reverse


def test_replay_visual_tiene_rutas_separadas():
    assert reverse("powertradeai:replay") == "/panel/replay/"
    assert reverse("powertradeai:replay_data") == "/panel/replay/data/"
    assert reverse("powertradeai:replay_action") == "/panel/replay/run/"


def test_replay_visual_resuelve_endpoints_en_template():
    html = render_to_string("powertradeai/replay.html", {
        "symbols": ["SPY"],
        "strategies": [],
    })
    assert "/panel/replay/data/" in html
    assert "{{" not in html and "{%" not in html


def test_dashboard_enlaza_al_replay_visual():
    from pathlib import Path

    import powerTradeAi_djangoApp

    ruta = (Path(powerTradeAi_djangoApp.__file__).parent
            / "templates" / "powertradeai" / "dashboard.html")
    assert "powertradeai:replay" in ruta.read_text(encoding="utf-8")

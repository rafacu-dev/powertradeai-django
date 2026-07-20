from django.apps import AppConfig


class PowerTradeAiConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "powerTradeAi_djangoApp"
    label = "powertradeai"
    verbose_name = "PowerTradeAI"

    def ready(self):
        # Importa las reglas para que se auto-registren en el catalogo.
        from . import strategies  # noqa: F401

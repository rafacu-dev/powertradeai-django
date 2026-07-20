from django.apps import AppConfig


class PowerTradeAiConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "powerTradeAi_djangoApp"
    label = "powertradeai"
    verbose_name = "PowerTradeAI"

    # NO se importan las reglas aqui a proposito.
    #
    # Hacerlo cargaria pandas y numpy (~100 MB residentes) en TODOS los
    # procesos que arranquen Django, incluido el servidor web — que solo sirve
    # la API de lectura y nunca evalua una regla. En un plan de 512 MB eso es
    # memoria regalada.
    #
    # El catalogo se puebla solo cuando alguien lo necesita: importar
    # ``powerTradeAi_djangoApp.strategies`` registra las reglas, y eso lo hacen
    # el motor de escaneo y los comandos de gestion, no la API.

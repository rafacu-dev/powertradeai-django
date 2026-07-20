# powertradeai-django

App de Django instalable que escanea el mercado en horario RTH, detecta señales
de estrategias de opciones y registra cada alerta con su compra, su venta y su
resultado en monto y porcentaje.

Mientras una alerta no ha terminado, sus campos de resultado salen como
`"pending"` — nunca como `0` ni como `null` disfrazado de dato.

```bash
pip install "git+https://github.com/rafacu-dev/powertradeai-django.git"
```

Documentación:

- **[Guía de la app](powerTradeAi_djangoApp/README.md)** — instalación, API,
  convención de P&L, cómo añadir una regla, replay y verificación.
- **[Despliegue en Render](powerTradeAi_djangoApp/DEPLOY.md)** — instalar en un
  proyecto Django existente y levantar el worker.

## Estado

14 reglas portadas (ORB-15, BB midpoint, prev-close, agresividad W5).
29 tests. Proveedor híbrido Alpaca + ThetaData verificado contra mercado real.

**Ninguna regla está validada como rentable.** La app existe para generar
muestra forward, no para operar. Los veredictos de cada regla están en el
proyecto de research que las produjo.

## Desarrollo

`dev_project/` es un proyecto Django mínimo para trabajar en local:

```bash
cd dev_project
export THETADATA_API_KEY=...  ALPACA_API_KEY=...  ALPACA_SECRET_KEY=...
python manage.py migrate
python manage.py seed_strategies
python manage.py check_provider
python manage.py scan_once --dry-run
```

## Verificación de fidelidad

Los 29 tests de este repo cubren el ciclo completo, el aislamiento entre alertas
reales y reconstruidas, y el comparador contra artefactos.

Los **41 tests de paridad** —que comparan cada regla portada contra el motor de
research original y contra los golden CSV de 128 sesiones— viven en el proyecto
`LocalQuantAI`, junto a los motores y artefactos que verifican. Ahí se instala
esta app en modo editable:

```bash
pip install -e ../powertradeai-django
python -m pytest tests_powertradeai/
```

Si tocas una regla, ese es el sitio donde se comprueba que sigue reproduciendo
el backtest.

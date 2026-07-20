# Desplegar PowerTradeAI en un proyecto Django existente (Render)

Guía para instalar la app en **tu** proyecto Django y desplegarlo en Render con
el worker de escaneo. Configuración de arranque acordada: **las 14 reglas en
shadow, intervalo de 10 s**.

## 0. Requisitos

- Python **3.12+** en el proyecto anfitrión (lo exige la librería `thetadata`).
  En Render: variable `PYTHON_VERSION=3.12.7` o un `runtime.txt`.
- Postgres (o cualquier base soportada por Django; los `JSONField` de la app
  funcionan en Postgres y SQLite).

## 1. Llevar la app a tu proyecto

Añade a tu `requirements.txt`:

```
powertradeai-django @ git+https://github.com/rafacu-dev/powertradeai-django.git@main
```

O fijando una versión concreta, que es lo recomendable en producción para que un
push a `main` no cambie las reglas bajo los pies del worker:

```
powertradeai-django @ git+https://github.com/rafacu-dev/powertradeai-django.git@v1.0.0
```

**Si el repo es privado**, Render necesita acceso. Dos formas:

- Conectar la cuenta de GitHub en Render y darle acceso al repo (recomendado).
- O un token en la URL: `git+https://${GITHUB_TOKEN}@github.com/...`, con
  `GITHUB_TOKEN` como variable de entorno `sync: false`.

La app es autocontenida: modelos, migraciones, auth, motor, API y tests. No
arrastra nada del proyecto de research.

## 2. Dependencias del proyecto anfitrión

La instalación del paso 1 arrastra sola lo que necesita la app: `django`,
`djangorestframework`, `pandas`, `numpy`, `thetadata` y `alpaca-py`.

Solo tienes que añadir lo del despliegue en sí:

```
gunicorn
psycopg[binary]
```

**Aviso de conflicto:** si tu proyecto ya usa `alpaca-py`, comprueba la versión.
La app se apoya en que el paquete NO expone quotes históricas de opciones
(verificado en 0.43.4) y por eso las pide a ThetaData. Una versión muy distinta
podría cambiar los nombres de las clases de request que usa
`data/alpaca_provider.py`.

## 3. settings.py

```python
INSTALLED_APPS = [
    # ... lo tuyo ...
    "rest_framework",
    "powerTradeAi_djangoApp",
]

POWERTRADEAI = {
    # Configuración canónica: la misma división de feeds que validó las
    # reglas (Alpaca IEX subyacente + ThetaData opciones). No cambiar a un
    # proveedor único sin leer el README.
    "MARKET_DATA_PROVIDER": "hybrid",
    "HYBRID_STOCK_PROVIDER": "alpaca",
    "HYBRID_OPTION_PROVIDER": "thetadata",
    "THETADATA_API_KEY": os.environ["THETADATA_API_KEY"],
    "ALPACA_API_KEY": os.environ["ALPACA_API_KEY"],
    "ALPACA_API_SECRET": os.environ["ALPACA_SECRET_KEY"],
    "ALPACA_FEED": "iex",
}

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "powerTradeAi_djangoApp.auth.ApiKeyAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_PAGINATION_CLASS":
        "rest_framework.pagination.LimitOffsetPagination",
    "PAGE_SIZE": 100,
}
```

Si tu proyecto ya define `REST_FRAMEWORK`, integra las clases en lugar de
sustituir el bloque.

## 4. urls.py

```python
path("api/powertradeai/", include("powerTradeAi_djangoApp.api.urls")),
```

## 5. render.yaml (en el repo de tu proyecto)

```yaml
services:
  - type: web
    name: tu-proyecto-web
    runtime: python
    buildCommand: pip install -r requirements.txt && python manage.py migrate
    startCommand: gunicorn TU_PROYECTO.wsgi:application
    envVars:
      - key: PYTHON_VERSION
        value: 3.12.7
      - key: THETADATA_API_KEY
        sync: false          # se introduce en el dashboard, nunca en el repo
      - key: ALPACA_API_KEY
        sync: false
      - key: ALPACA_SECRET_KEY
        sync: false
      - key: DATABASE_URL
        fromDatabase:
          name: powertradeai-db
          property: connectionString

  - type: worker
    name: powertradeai-scanner
    runtime: python
    buildCommand: pip install -r requirements.txt
    startCommand: python manage.py scan_loop --interval 10
    envVars:
      # mismas variables que el web (PYTHON_VERSION y las tres claves)
      - key: DATABASE_URL
        fromDatabase:
          name: powertradeai-db
          property: connectionString

databases:
  - name: powertradeai-db
    plan: basic-256mb
```

Sustituye `TU_PROYECTO.wsgi` y ajusta a tu estructura. El worker es un proceso
vivo: fuera de horario RTH duerme solo, y atiende `SIGTERM` en los redeploys.

## 6. Puesta en marcha (una vez desplegado)

Desde el shell del servicio web en Render:

```bash
python manage.py seed_strategies      # crea las 14 reglas, todas activas
python manage.py create_api_key "produccion"   # copia la clave: no se repite
python manage.py check_provider       # 5/5 esperado
```

## 7. Checklist de la primera semana

- **Día 1:** `GET /api/powertradeai/scans/` durante horario de mercado — debe
  haber una pasada cada ~10 s con `ok: true`. Si no hay filas, el worker no
  corre; si hay filas con `ok: false`, el error viene en el campo `error`.
- **Cada día:** `GET /alerts/?status=pending` al cierre — no debe quedar nada
  vivo de días anteriores.
- **Fin de semana:** `GET /strategies/performance/` — todavía sin conclusiones,
  solo comprobar que registra.

## Avisos operativos

**Límite de peticiones de Alpaca.** `TSLA_W5_STABLE` pide 15 minutos de tape de
TSLA en cada pasada; a 10 s son ~6 descargas/minuto y es la regla más pesada
del conjunto. Si aparecen errores 429 en los logs del worker: sube a
`--interval 20`, o desactiva `TSLA_W5_STABLE` desde el admin (con más de 15-20 s
de intervalo esa regla igualmente descartaría casi todas sus señales por el
límite de edad de 15 s — desactivarla es más honesto que dejarla coja).

**Las alertas de este worker son shadow.** Registran señal, contrato y P&L
contrafactual con quotes reales; nadie envía órdenes a ningún broker. El
objetivo de los próximos 2-3 meses es muestra forward, no operar.

**No mezclar con replays.** Si reconstruyes sesiones pasadas con `replay_day`,
quedan como `source=replay` y no contaminan los agregados. El endpoint de
performance rechaza mezclarlas por diseño.

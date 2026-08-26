# Despliegue en Render

Esta guia actualiza un proyecto Django existente al contrato Investep v2. La
clave de DeepSeek permanece como variable secreta de Render; no se almacena en
la base ni se expone por API.

## 1. Version y dependencias

Fija una version del repositorio, no `main`:

```text
powertradeai-django @ git+https://github.com/rafacu-dev/powertradeai-django.git@v1.46.0
gunicorn
psycopg[binary]
```

La app requiere Python 3.12 o posterior. El conjunto probado se registra en
`requirements.lock`.

## 2. Django

```python
INSTALLED_APPS = [
    # aplicaciones del proyecto
    "rest_framework",
    "powerTradeAi_djangoApp",
]

urlpatterns = [
    # rutas del proyecto
    path("api/powertradeai/", include("powerTradeAi_djangoApp.api.urls")),
    path("powertradeai/", include("powerTradeAi_djangoApp.urls")),
]
```

La app declara autenticacion, permisos y throttling en sus propias vistas. No
es necesario reemplazar `REST_FRAMEWORK` global del proyecto anfitrion.

## 3. Variables de Render

Proveedores y LLM:

```text
MARKET_DATA_PROVIDER=hybrid
HYBRID_STOCK_PROVIDER=alpaca
HYBRID_OPTION_PROVIDER=thetadata
ALPACA_API_KEY=<secret>
ALPACA_SECRET_KEY=<secret>
ALPACA_FEED=iex
THETADATA_API_KEY=<secret>
DEEPSEEK_API_KEY=<secret>
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-chat
DEEPSEEK_TIMEOUT_SECONDS=45
DEEPSEEK_MAX_RETRIES=1
```

Universo y limites:

```text
INVESTEP_WATCHLIST=TSLA,SPY,QQQ
ACCOUNT_SIZE=10000
RISK_PCT_PER_TRADE=2
MAX_CONTRACTS_PER_TRADE=5
MAX_DECISION_AGE_SECONDS=180
MAX_OPTION_SPREAD_PCT=5
MAX_OPTION_QUOTE_AGE_SECONDS=30
MAX_OPTION_QUOTE_FUTURE_SKEW_SECONDS=2
MAX_HISTORICAL_OPTION_QUOTE_DELAY_SECONDS=90
MIN_SPOT_PREMIUM_SAMPLES=20
```

Gates de datos:

```text
EVENT_CALENDAR_COVERAGE_FROM=2026-08-01
EVENT_CALENDAR_COVERAGE_UNTIL=2026-08-31
EVENT_CALENDAR_JSON=[...]
SPOT_PREMIUM_MODELS_JSON={...}
```

El intervalo `COVERAGE_FROM..COVERAGE_UNTIL` debe estar completo.
`EVENT_CALENDAR_JSON` debe incluir earnings por ticker y eventos macro con
`symbol="*"`. `SPOT_PREMIUM_MODELS_JSON` debe contener valores calibrados,
muestra, fuente y version por ticker. Sin cobertura vigente o sin modelo con
muestra suficiente, la decision queda bloqueada. Ese comportamiento es
deliberado.

## 4. Servicios separados

Una llamada a DeepSeek puede tardar decenas de segundos. Por eso el scanner y
el agente no comparten proceso.

```yaml
services:
  - type: web
    name: powertradeai-web
    runtime: python
    buildCommand: pip install -r requirements.txt && python manage.py migrate
    startCommand: gunicorn TU_PROYECTO.wsgi:application
    envVars:
      - key: DATABASE_URL
        fromDatabase:
          name: powertradeai-db
          property: connectionString

  - type: worker
    name: powertradeai-scanner
    runtime: python
    buildCommand: pip install -r requirements.txt
    startCommand: python manage.py scan_loop --interval 30
    envVars:
      - key: DATABASE_URL
        fromDatabase:
          name: powertradeai-db
          property: connectionString

  - type: worker
    name: powertradeai-agent
    runtime: python
    buildCommand: pip install -r requirements.txt
    startCommand: python manage.py agent_loop --symbols TSLA,SPY,QQQ
    envVars:
      - key: DATABASE_URL
        fromDatabase:
          name: powertradeai-db
          property: connectionString

databases:
  - name: powertradeai-db
```

Asigna a los tres servicios las mismas variables de mercado y base de datos. La
clave de DeepSeek solo es necesaria en web si existe chat y en el worker del
agente; el scanner no la necesita.

No uses `scan_loop --agent`. El flag se conserva para advertir sobre comandos
antiguos, pero ya no ejecuta el agente dentro del scanner.

## 5. Migracion controlada

Ejecuta, en este orden:

```bash
python manage.py migrate
python manage.py seed_strategies
python manage.py check_provider
python manage.py check
```

La migracion v2 es de contencion:

- agrega `InvestepDecision`, `ReplayRun`, campos de procedencia, deduplicacion
  de señales academicas y el lock de presupuesto;
- da scope `read` a claves antiguas;
- deshabilita estrategias existentes;
- marca replays y posiciones del agente legadas incompletas como error.

Luego `seed_strategies` habilita **exclusivamente las reglas listadas en
`APTAS_PARA_PAPER`**, dentro del propio comando. Esa lista esta **vacia** desde
el 07-ago-2026 por decision del operador: el catalogo se conserva entero como
registro de investigacion, pero ninguna regla opera hasta que se la anada a
mano, una por una, cuando su evidencia lo justifique.

El criterio anterior era estructural — cualquier id con `_E01_` o `_E02_` se
activaba solo. Eso hacia que anadir una clase al catalogo la pusiera a operar
sin que nadie lo decidiera. La lista explicita invierte la carga: aparecer en el
catalogo no da permiso; darlo es un cambio de codigo visible en el historial.

`INVESTEP_WATCHLIST` ya no interviene en que se activa; sigue usandose para el
universo del agente.

**No se activa nada desde el admin como via normal.** El admin lo permite para
una prueba puntual, pero el siguiente `seed_strategies` lo revierte: la fuente
de verdad es la lista, no la base de datos. Para una prueba que deba sobrevivir
al seed, usa `--preserve-enabled`.

Antes de iniciar workers, confirma:

```bash
python manage.py shell -c "\
from powerTradeAi_djangoApp.models import Strategy; \
print(list(Strategy.objects.filter(enabled=True).values_list('strategy_id', flat=True)))"
```

Debe imprimir solo las reglas explicitamente autorizadas en
`APTAS_PARA_PAPER`. Para la promocion ORB del 26-ago-2026 son:

```text
SPY_ORB15_BASE_CALL_CLOSE80_TP125_STOP15
SPY_ORB15_0950_CALL_CLOSE80_TP125_STOP15_RANGE_INVALID
SPY_ORB15_0950_PUT_BODY70_TP100_STOP15
SPY_ORB5_VALIDATE_2ND_ENTER_3RD_VOL15_STOP15
```

Quedan fuera por solape temporal del grupo ORB15 09:50 CALL:
`SPY_ORB15_0950_CALL_CLOSE80_TP125_STOP15` y
`SPY_ORB15_0950_RANGE_INVALID_STOP15`. La regla compuesta es la representante:
misma familia de entrada, pero sale por el primer evento entre invalidacion de
rango, stop, take profit o tiempo.

Si la lista sale vacia, el scanner sigue corriendo y registrando `ScanRun` cada
pasada, pero `strategies_evaluated` sera 0 y no se creara ninguna alerta.

## 6. API keys con menor privilegio

```bash
python manage.py create_api_key "dashboard" --scope read
python manage.py create_api_key "replay" --scope read --scope replay
python manage.py create_api_key "auditoria" --scope read --scope transcript
```

- `read`: alertas, decisiones, estrategias y auditorias resumidas.
- `replay`: permite `POST /replay/`.
- `transcript`: permite detalle completo de una corrida del agente.
- `*`: solo para administracion excepcional.

El endpoint de replay tiene limite independiente de 2 solicitudes por minuto y
`save=false` por defecto. Los transcripts redactan claves conocidas incluso con
el scope correcto.

## 7. Comprobacion previa

```bash
python manage.py scan_once --dry-run
python manage.py create_api_key "smoke" --scope read
```

Verifica por API:

```bash
curl -H "Authorization: Api-Key $POWERTRADEAI_KEY" \
  https://TU_HOST/api/powertradeai/strategies/

curl -H "Authorization: Api-Key $POWERTRADEAI_KEY" \
  https://TU_HOST/api/powertradeai/scans/

curl -H "Authorization: Api-Key $POWERTRADEAI_KEY" \
  https://TU_HOST/api/powertradeai/investep-decisions/
```

Una ausencia de alertas no demuestra que el worker este activo. `ScanRun`
debe registrar pasadas y sus errores aislados por simbolo.

## 8. Monitoreo

Alertas operativas recomendadas:

- `ScanRun.ok=false`.
- falta de `ScanRun` durante horario de mercado.
- `AgentRun.status=running` por encima del limite esperado.
- crecimiento de blockers `PENDING_EVENT_CALENDAR`.
- crecimiento de blockers `PENDING_EMPIRICAL_MOVE_MODEL`.
- quotes `OPTION_QUOTE_STALE` o `OPTION_SPREAD_TOO_WIDE`.
- alertas pendientes despues del cierre.
- `ReplayRun.status=error`.

Los errores de un ticker no abortan el resto del scanner. Los fallos del agente
no detienen el resolver de sus posiciones, y ninguna llamada al LLM bloquea el
scanner determinista.

## 9. Actualizaciones

Antes de cambiar de tag:

1. Ejecuta la suite interna y la de paridad externa.
2. Revisa nuevas migraciones con `showmigrations` y `sqlmigrate`.
3. Despliega web y ejecuta migraciones.
4. Ejecuta `seed_strategies` para reaplicar la lista de reglas aptas.
5. Reinicia scanner y agente.
6. Confirma `manual_hash`, `prompt_version` y `rule_version` en decisiones
   nuevas.

Una decision creada con otra version del manual o prompt no puede consumirse.

# powerTradeAi_djangoApp

App de Django instalable que escanea el mercado en horario RTH, detecta las
estrategias del proyecto y guarda cada alerta con su compra, su venta y su
resultado en monto y porcentaje.

Mientras una alerta no ha terminado, los campos de resultado salen como
`"pending"` — nunca como `0` ni como `null` disfrazado de dato.

## Estado

| Pieza | Estado |
|---|---|
| Modelos, ApiKey, admin, API de lectura | completo |
| Motor de escaneo y resolución | completo |
| SPY ORB-15 (4 reglas) | portada, verificada contra el replay causal de 128 sesiones |
| TSLA prev-close (5 reglas) | portada, paridad verificada contra `paper/engines/prevclose.py` |
| BB midpoint (4 reglas) | portada, paridad verificada contra `paper/engines/bb_midpoint.py` |
| TSLA_W5_STABLE (agresividad) | portada, paridad verificada contra el detector original |
| 4 reglas `*_TIMESFM` | **pendientes** (requieren el modelo en Render) |

**14 de 18 reglas operativas.**

### La agresividad es distinta al resto

`TSLA_W5_STABLE` no trabaja sobre velas de un minuto: construye barras de **un
segundo** desde el tape de operaciones. Eso implica dos cosas prácticas:

- El worker debe correr con `--interval 10` o menos. Con 30 s, el límite de
  antigüedad de señal (15 s) descarta casi todo lo que detecte.
- Consume mucho más ancho de banda: cada pasada pide 15 minutos de tape.

El gate `confirm10` (el bid debe alcanzar `entry_ask × 1.05` en 10 segundos) se
reconstruye **a posteriori** con la serie histórica de quotes del contrato — un
poll de 30 s no puede observar ese máximo en vivo. Si el proveedor no sirve esa
serie, el gate queda indeterminado y la alerta **no se cierra**: abortar sin
haber observado el bid sería inventarse el motivo del cierre.

### Solapamiento entre reglas

`TSLA_PREVCLOSE_D1_G300400_P850` y `TSLA_PREVCLOSE_VOL1M` disparan sobre el
mismo movimiento, igual que `TSLA_FAILED_FADE_CALL_AWAY10` y `..._AWAY25`. Cada
una genera su propia alerta, así que **un agregado que sume todas las reglas
cuenta el mismo trade dos veces**. El motor original lo documenta explícitamente
y advierte que sus `n` no deben sumarse.

## Instalación

```bash
pip install django djangorestframework thetadata pandas
# alpaca-py solo si vas a usar ese proveedor
```

En `settings.py` del proyecto anfitrión:

```python
INSTALLED_APPS = [
    ...,
    "rest_framework",
    "powerTradeAi_djangoApp",
]

POWERTRADEAI = {
    "MARKET_DATA_PROVIDER": "hybrid",
    "HYBRID_STOCK_PROVIDER": "alpaca",
    "HYBRID_OPTION_PROVIDER": "thetadata",
    "THETADATA_API_KEY": os.environ["THETADATA_API_KEY"],
    "ALPACA_API_KEY": os.environ["ALPACA_API_KEY"],
    "ALPACA_API_SECRET": os.environ["ALPACA_SECRET_KEY"],
    "ALPACA_FEED": "iex",
}
```

### Por qué híbrido

**Porque es la configuración que validó las reglas.** El backtest causal
[`research/backtest_spy_alerts_thetadata.py`](../research/backtest_spy_alerts_thetadata.py)
construye el subyacente con `proxy/provider.py` (Alpaca, `FEED="iex"`) y las
quotes de opciones con `proxy/thetadata.py`. El híbrido reproduce esa misma
división, así que la app ve los mismos precios que el golden CSV de 128 sesiones.

Además es lo único que funciona con las suscripciones actuales (ThetaData de
pago para opciones, sin tier de acciones). Verificado el 19-jul-2026:

| | Alpaca | ThetaData FREE |
|---|---|---|
| Velas del subyacente | ✅ | ❌ requiere tier `value` |
| Precio actual | ✅ | ❌ requiere tier `value` |
| Tape de operaciones | ✅ | ❌ requiere tier `standard` |
| Quote de opción (snapshot) | ✅ | ✅ |
| **Quote de opción histórica** | ❌ **no existe el endpoint** | ✅ |

Sin quotes históricas de opciones no se pueden **resolver** las alertas, así que
Alpaca solo no basta. La suscripción de opciones de ThetaData cubre justo ese
hueco. Combinados: 5/5.

**No pases a `MARKET_DATA_PROVIDER="thetadata"` aunque contrates el tier de
acciones.** Los backtests se hicieron con Alpaca IEX en el subyacente; cambiar
ese feed dejaría de reproducirlos. El híbrido no es un apaño temporal: es la
configuración canónica.

En `urls.py`:

```python
path("api/powertradeai/", include("powerTradeAi_djangoApp.api.urls")),
```

Puesta en marcha:

```bash
python manage.py migrate
python manage.py seed_strategies
python manage.py create_api_key "dashboard"   # imprime la clave una sola vez
```

## Probar en local, sin desplegar

No hace falta Render para nada de esto. La librería `thetadata` se conecta a sus
servidores desde cualquier sitio; Render solo sirve para que el escaneo corra
solo cuando tu máquina está apagada.

En el repo hay un proyecto Django mínimo en [`dev_project/`](../dev_project/)
listo para usar:

```bash
cd dev_project
export THETADATA_API_KEY=...

python manage.py migrate
python manage.py seed_strategies
python manage.py check_provider          # ← empieza por aquí
python manage.py scan_once --dry-run     # qué dispararía, sin escribir nada
python manage.py runserver               # admin en /admin/

# Evaluar las reglas contra una sesión pasada real:
python manage.py scan_once --dry-run --at "2026-07-17 09:50"
```

`dev_project` carga automáticamente el `.env` de la raíz del repo, y acepta
`ALPACA_SECRET_KEY` (el nombre que ya usa este proyecto) además de
`ALPACA_API_SECRET`.

`--at` solo funciona con `--dry-run`: reevaluar el pasado y escribir alertas con
fecha de hoy corrompería el historial, así que el comando lo rechaza. En modo
replay la selección de contrato pide quotes del instante de la señal, no
snapshots en vivo — un contrato ya vencido no tiene snapshot.

Ejemplo real (sesión del 17-jul-2026):

```
SPY_ORB15_BASE   dispararia  CALL SPY 260717C00743000 ask 2.02 coste $202
```

rango 09:30-09:44 = 740.80–742.66, umbral CALL 742.8085; la vela de 09:47 cierra
en 742.71 (no dispara) y la de 09:48 en 743.04 (dispara), señal fechada a las
09:49 por la convención de cierre de vela.

`check_provider` es el diagnóstico que importa: hace las cinco llamadas que la
app necesita (velas 1m, historial, precio, quote de opción y tape) y valida que
lo devuelto tenga la forma esperada — columnas, zona horaria, orden y ausencia
de duplicados. Si los nombres de columna de ThetaData no coinciden con los alias
que espera la app, aquí sale con un mensaje claro en vez de reventar a media
sesión de mercado.

`dev_project/config/settings.py` es también la documentación ejecutable de qué
hay que copiar a tu proyecto real.

## Reconstruir una sesión pasada

```bash
python manage.py replay_day --date 2026-07-17
python manage.py replay_day --date 2026-07-17 --strategy SPY_ORB15_BASE
python manage.py replay_day --date 2026-07-17 --overwrite
```

Recorre el día minuto a minuto como lo habría hecho el worker, deja que cada
regla decida con la información disponible en ese instante, y resuelve la salida
con quotes históricas reales del contrato. Una sesión completa tarda ~30 s.

### Las reconstrucciones NO son resultados

Se guardan con `source="replay"` y quedan separadas de las reales en todo el
sistema. Un replay:

- no sufrió latencia de red ni competencia por el fill;
- toma la quote del instante teórico, no la que se habría pagado;
- no modela el rechazo del broker ni la falta de liquidez en ese strike.

Su P&L es un **límite superior optimista**, no un resultado.

Por eso el aislamiento es estructural, no una convención:

| | comportamiento |
|---|---|
| `GET /alerts/` | solo `live` por defecto |
| `GET /alerts/?source=replay` | solo reconstruidas |
| `GET /alerts/?source=all` | ambas, cada una etiquetada |
| `GET /strategies/performance/` | solo `live` por defecto |
| `GET /strategies/performance/?source=all` | **400** — mezclar fuentes en una media da un número sin significado |

El `unique constraint` incluye `source`, así que reconstruir un día ya operado
en vivo no choca contra la alerta real ni la pisa. Y una alerta creada sin
especificar fuente nace `live`: un descuido cae del lado seguro.

Fijado en `tests/test_replay_isolation.py`.

### Rango de sesiones

```bash
python manage.py replay_range --desde 2026-07-13 --hasta 2026-07-17 \
    --strategy SPY_ORB15_BASE
```

Encadena días hábiles. Una sesión que falle no aborta el rango.

## Verificar fidelidad al backtest

```bash
python manage.py compare_golden --strategy SPY_ORB15_BASE \
    --csv research/runs/2026-07-15_spy_orb15_causal_120sessions_trades.csv
```

Reconstruye cada sesión **desde velas crudas** y compara contra el artefacto:
rango de apertura, dirección, vela que disparó y subyacente de entrada. 128
sesiones en ~15 s.

Esto cierra el hueco que dejaban los golden tests: ellos verifican la aritmética
de P&L y la selección de strike, pero dan la señal por buena — el CSV trae
`range_high` y `signal_bar_ts` ya resueltos, no las velas que los produjeron.

**Resultado actual: 128/128** para `SPY_ORB15_BASE` y `SPY_ORB15_RANGE_INVALID`.

No compara P&L a propósito. Un P&L reconstruido es un límite superior optimista
y compararlo solo produciría ruido; lo que se verifica es si la app **ve** las
mismas señales.

### El comparador está verificado por mutación

Un verificador que pasa en vacío es peor que no tenerlo. Se comprobó rompiendo
la regla a propósito:

| mutación | resultado |
|---|---|
| `RANGE_BARS` 15 → 14 | 14/20 (6 divergen) |
| `BUF` 0.0002 → 0 | 21/40 (19 divergen) |
| `range_high` renombrado en `meta` | 0/5 — «AUSENTE en la app» |

La tercera es la importante: un campo que desaparece se marca como divergencia,
no se salta en silencio. Fijado en `tests/test_golden_comparator.py`.

## Uso

```bash
python manage.py scan_loop --interval 30   # worker: respeta RTH, duerme fuera
python manage.py scan_once                 # una pasada, escribiendo alertas
python manage.py scan_once --dry-run       # una pasada sin escribir nada
```

Si activas `TSLA_W5_STABLE`, baja el intervalo a `--interval 10` o menos: esa
regla descarta señales de más de 15 segundos.

### Endpoints

Todos requieren `Authorization: Api-Key <clave>`.

| Endpoint | Qué devuelve |
|---|---|
| `GET /alerts/` | alertas; filtros `status`, `strategy`, `symbol`, `direction`, `desde`, `hasta` |
| `GET /alerts/pending/` | solo lo que sigue vivo |
| `GET /strategies/` | catálogo de reglas |
| `GET /strategies/performance/` | agregado por regla (**solo alertas cerradas**) |
| `GET /scans/` | salud del worker |

```bash
curl -H "Authorization: Api-Key ptai_..." \
     "https://tu-app.onrender.com/api/powertradeai/alerts/?status=pending"
```

Forma de una alerta:

```json
{
  "strategy_id": "SPY_ORB15_0950",
  "session_date": "2026-07-15",
  "direction": "CALL",
  "status": "pending",
  "strike": "686.00",
  "contracts": 2,
  "compra": {"prima": "1.5000", "bid": "1.4900", "ask": "1.5000", "coste_total": "300.00"},
  "venta":  {"prima": "pending", "motivo": "pending", "cierre_previsto": "..."},
  "resultado": {"monto": "pending", "porciento": "pending", "estado": "pending"}
}
```

## Convención de P&L

Idéntica al replay causal que validó las reglas:

```
neto  = (prima_venta - prima_compra) * 100 * contratos - comisión * contratos
pct   = neto / (prima_compra * 100 * contratos) * 100
```

Se **compra al ask** y se **vende al bid**. Verificado contra las 128 sesiones de
`research/runs/2026-07-15_spy_orb15_causal_120sessions_trades.csv`.

## Despliegue en Render

**Guía completa para un proyecto Django existente: [DEPLOY.md](DEPLOY.md).**
Lo de abajo es el esquema general.

`render.yaml` en la raíz del proyecto anfitrión:

```yaml
services:
  - type: web
    name: powertradeai-api
    env: python
    buildCommand: pip install -r requirements.txt && python manage.py migrate
    startCommand: gunicorn tuproyecto.wsgi:application

  - type: worker
    name: powertradeai-scanner
    env: python
    buildCommand: pip install -r requirements.txt
    startCommand: python manage.py scan_loop --interval 30

databases:
  - name: powertradeai-db
```

El worker es un proceso vivo, no un cron: mantiene las velas de la sesión en
memoria y atiende `SIGTERM` para salir limpio en cada redeploy.

**ThetaData funciona en Render** vía la librería Python v3, que se conecta
directo a sus servidores por HTTPS/gRPC — no necesita el Theta Terminal local.
Requiere Python 3.12+.

## Añadir una regla

```python
from powerTradeAi_djangoApp.strategies.base import BaseStrategy, Signal, register

@register
class MiRegla(BaseStrategy):
    strategy_id = "SYMBOL_MI_REGLA"
    name = "Descripción"
    symbol = "TSLA"
    rule_version = "mi_regla_v1"
    default_params = {"hold_minutes": 30}

    def evaluate(self, ctx) -> Signal | None:
        bars = ctx.causal_bars(1)   # SIEMPRE: solo velas ya cerradas
        ...
```

Impórtala en `strategies/__init__.py` y corre `seed_strategies`.

### Lo único que no se negocia

`ctx.causal_bars(n)` devuelve solo las velas cuyo cierre ya es observable. Este
proyecto ya produjo un veredicto falso por entrar un minuto antes de tiempo. Una
regla que mire `ctx.bars` directamente puede estar leyendo el futuro.

## Tests

```bash
python -m pytest powerTradeAi_djangoApp/tests/ -v
```

- `test_orb15_golden.py` — contra el artefacto causal de 128 sesiones.
- `test_engine.py` — ciclo completo con proveedor falso, sin red.
- `test_prevclose_parity.py` — mismos datos al motor original y al port.
- `test_bb_parity.py` — indicadores valor a valor + regla completa.
- `test_aggression_parity.py` — barras de 1s, detector y gates de W5.

### Qué cubren y qué no

Cubren: la aritmética de P&L, la selección de strike, la convención de que la
señal se observa al cierre de la vela, la causalidad, la idempotencia del
scanner, que sin quote de salida **no se fabrica un resultado**, y que las
fórmulas duplicadas en `strategies/indicators.py` siguen siendo idénticas a las
de `core/`.

No cubren: la detección a partir de velas reales descargadas del proveedor. Eso
exige bajar el histórico y es un test de integración aún por escribir.

Los tests de paridad **exigen que el motor original dispare**: si un escenario
deja de producir señal, el test falla en vez de pasar en vacío. Esa guardia
existe porque la primera versión del test de BB pasaba sin comparar nada.

## Hallazgo al portar la regla

`paper/orb15_paper.py:704` calcula el strike base de los PUT como
`int(spot) + 1`. Cuando el SPY cotiza en un entero exacto eso elige un strike de
más: en la sesión 2026-01-20 (spot 681.00) el replay causal eligió 681 y esa
fórmula habría elegido 682. Con `floor`/`ceil` el artefacto reproduce 128/128.

Esta app usa `floor`/`ceil`. **El productor local sigue sin corregir.**

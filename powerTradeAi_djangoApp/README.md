# PowerTradeAI Django App

## Proposito

Esta app aplica el conocimiento recopilado de Investep Academy sin delegar al
LLM las reglas que deben ser mecanicas. DeepSeek puede consultar datos, proponer
una estrategia y explicar una tesis; Django decide si la propuesta cumple el
contrato operativo.

El flujo obligatorio es:

```text
consultar_manual
  -> validate_investep_setup
     -> InvestepDecision(valid | wait | blocked)
        -> create_alert(decision_id) solo cuando status=valid
           -> resolver por target/stop de prima
              -> P&L neto y auditoria
```

No se puede llamar `create_alert` con ticker, direccion o nombre de estrategia.
Esos campos salen de la decision validada. `InvestepDecision` y `Alert` tienen
relacion uno-a-uno para impedir duplicados y sobrescrituras por reintentos.
Una restriccion adicional impide consumir la misma señal academica otra vez en
otra corrida del mismo modo.

## Fidelidad de estrategia

### E01/E02: dos ramas distintas

`OPENING_GAP`

- Temporalidad: 15 minutos.
- Reloj: intervalo `[09:31, 09:32)` ET.
- Dato observado: vela 09:30-09:45 en formacion, reconstruida solo con minutos
  de 1m cuyo cierre ya ocurrio.
- Contexto: tramo previo contrario o lateral.
- Confirmacion: gap rompe linea de tendencia y punto medio de Bollinger, las
  bandas empiezan a abrir y el precio avanza hacia la banda correspondiente.
- Nunca usa el OHLC final de las 09:45 antes de tiempo.

`INTRADAY_BREAK`

- Temporalidad: 15 minutos.
- Reloj: desde 09:45 hasta 15:45 ET.
- Dato observado: vela completa de 15m.
- Contexto: tendencia previa contraria o lateral.
- Confirmacion: el cuerpo cierra al otro lado de la linea y del punto medio,
  con apertura direccional de Bollinger.
- Una mecha que cruza y cierra de vuelta no confirma.

La frecuencia intradia no se atribuye a la rama de gap. Son estrategias con
identificadores separados, por ejemplo `TSLA_E01_APERTURA` y
`TSLA_E01_INTRADIA`.

### Gestion E01/E02

- E01 siempre produce `CALL`; E02 siempre produce `PUT`.
- Target permitido: 10%-15% sobre la prima.
- Stop: 20% sobre la prima.
- No se permite reforzar una posicion asociada a Plan 10.
- MA40, pisos y techos se usan antes de entrar como terreno disponible, no como
  una salida estructural obligatoria de E01/E02.

### Estrategias restantes

El manual incluye E03-E10, pero el servidor devuelve
`NO_DETERMINISTIC_VALIDATOR`. Esto impide que la descripcion verbal del agente
se convierta en una implementacion accidental. E11/E12 estan marcadas como no
operables.

## Gates no negociables

Una decision solo llega a `valid` cuando pasan todas estas capas:

1. Estrategia y rama reconocidas.
2. Simbolo dentro de `INVESTEP_WATCHLIST`.
3. Consulta del manual en la misma corrida.
4. Setup mecanico confirmado con datos causales.
5. Calendario cubierto y sin evento bloqueante.
6. Terreno suficiente hasta la primera barrera.
7. Modelo empirico spot-prima con muestra minima.
8. Target y stop dentro de Plan 10.
9. Quote de opcion valida, no cruzada, con spread y timestamp aceptables.
10. Presupuesto y limite de contratos disponibles.

Un dato ausente produce `WAIT` o `BLOCKED`; nunca se convierte en permiso.

### Calendario

`EVENT_CALENDAR_COVERAGE_FROM` y `EVENT_CALENDAR_COVERAGE_UNTIL` declaran el
intervalo completo del calendario. Si cualquiera falta o la sesion queda fuera,
el blocker es
`PENDING_EVENT_CALENDAR`.

`EVENT_CALENDAR_JSON` acepta una lista como:

```json
[
  {
    "type": "earnings",
    "symbol": "TSLA",
    "date": "2026-08-05",
    "confirmed": true,
    "source": "proveedor-calendario"
  },
  {
    "type": "macro",
    "symbol": "*",
    "date": "2026-08-07",
    "source": "proveedor-calendario"
  }
]
```

La regla bloquea earnings entre 0 y 3 dias y eventos macro el mismo dia.

### Terreno y modelo spot-prima

`assess_terrain` construye barreras con MA20/40/100/200 y pivotes confirmados de
15m, 1h y diario. La primera barrera en la direccion de la operacion se compara
contra el movimiento del subyacente necesario para alcanzar el target de prima.

Ese movimiento no se inventa. Debe configurarse con evidencia por simbolo:

```json
{
  "TSLA": {
    "required_move_abs_usd": 0.0,
    "sample_size": 0,
    "target_premium_pct": 15,
    "source": "identificador-del-estudio",
    "version": "version-del-modelo"
  }
}
```

Los ceros son solo la forma del esquema, no valores utilizables. Con muestra
menor que `MIN_SPOT_PREMIUM_SAMPLES` o sin movimiento calibrado, el resultado es
`PENDING_EMPIRICAL_MOVE_MODEL`. El target del modelo debe coincidir con el
target propuesto; no se reutiliza una calibracion de 15% para una salida de 10%.

## Configuracion

```python
POWERTRADEAI = {
    "MARKET_DATA_PROVIDER": "hybrid",
    "HYBRID_STOCK_PROVIDER": "alpaca",
    "HYBRID_OPTION_PROVIDER": "thetadata",
    "THETADATA_API_KEY": os.environ["THETADATA_API_KEY"],
    "ALPACA_API_KEY": os.environ["ALPACA_API_KEY"],
    "ALPACA_API_SECRET": os.environ["ALPACA_SECRET_KEY"],
    "ALPACA_FEED": "iex",
    "AGENT_LLM": {
        "BASE_URL": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        "API_KEY": os.environ["DEEPSEEK_API_KEY"],
        "MODEL": os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
        "TIMEOUT_SECONDS": 45,
        "MAX_RETRIES": 1,
    },
    "INVESTEP_WATCHLIST": ("TSLA", "SPY", "QQQ"),
    "ACCOUNT_SIZE": 10_000,
    "RISK_PCT_PER_TRADE": 2,
    "MAX_CONTRACTS_PER_TRADE": 5,
    "MAX_DECISION_AGE_SECONDS": 180,
    "MAX_OPTION_SPREAD_PCT": 5,
    "MAX_OPTION_QUOTE_AGE_SECONDS": 30,
    "MAX_OPTION_QUOTE_FUTURE_SKEW_SECONDS": 2,
    "MAX_HISTORICAL_OPTION_QUOTE_DELAY_SECONDS": 90,
    "REQUIRE_OPTION_QUOTE_TIMESTAMP": True,
    "EVENT_CALENDAR_COVERAGE_FROM": os.getenv(
        "EVENT_CALENDAR_COVERAGE_FROM"),
    "EVENT_CALENDAR_COVERAGE_UNTIL": os.getenv(
        "EVENT_CALENDAR_COVERAGE_UNTIL"),
    "EVENT_CALENDAR": json.loads(os.getenv("EVENT_CALENDAR_JSON", "[]")),
    "SPOT_PREMIUM_MODELS": json.loads(
        os.getenv("SPOT_PREMIUM_MODELS_JSON", "{}")),
    "MIN_SPOT_PREMIUM_SAMPLES": 20,
}
```

Los porcentajes de cuenta, spread, frescura y muestra son controles de
implementacion configurables. No se presentan como reglas de la academia.

## Skills relevantes

- `consultar_manual`: recupera la seccion y registra la consulta en la corrida.
- `validate_investep_setup`: crea la decision estructurada y aplica todos los
  gates.
- `get_estado_volatilidad`: secuencia de Bollinger con dato crudo, percentil y
  soporte explicito para la barra en formacion de apertura.
- `get_trendlines`: auxiliar de lineas con calibracion de software; no valida
  por si sola una estrategia.
- `get_event_risk`: estado reproducible del calendario.
- `get_available_terrain`: barreras y recorrido disponible.
- `calculate_option_price_range`: compara contratos cercanos y devuelve dos
  limites de costo por contrato con filas auditables.
- `create_alert`: consume una decision valida, revalida quote y calcula cantidad
  en el servidor.

El chat del grafico es de analisis. No recibe skills para crear, ajustar o
cerrar posiciones.

## Persistencia y metricas

Campos de trazabilidad principales:

- `manual_hash`, `prompt_version`, `rule_version`.
- `academy_strategy`, `strategy_branch`, `evaluation_version`.
- evidencia, validaciones y blockers completos.
- bid/ask, spread, timestamp y edad de quote.
- target, stop, costo y contratos decididos por servidor.

El P&L se calcula asi:

```text
net_dollars = (exit_bid - entry_ask) * 100 * contracts
              - round_trip_commission * contracts
net_pct = net_dollars / (entry_ask * 100 * contracts) * 100
```

Una operacion ganadora es `net_dollars > 0`, no un movimiento bruto positivo.
Los dashboards y endpoints de performance filtran `investep_v2` por defecto.
En reconstruccion, ThetaData puede entregar la primera NBBO posterior a la
decision: hasta 90 segundos se registra como latencia de ejecucion y el reloj de
la posicion empieza en ese timestamp, no en el cierre de la señal.

## Replay

El endpoint HTTP usa `save=false` por defecto. En ese modo no crea `Alert` ni
`ReplayRun`. Cuando se pide persistencia, primero calcula toda la sesion y luego
guarda en una sola transaccion; si una estrategia falla, no reemplaza resultados
anteriores ni deja una sesion parcial.

```bash
python manage.py replay_day --date 2026-07-17
python manage.py replay_range --desde 2026-07-13 --hasta 2026-07-17
```

Las fuentes `live`, `replay`, `agent` y `agent_train` no se agregan juntas. El
endpoint de performance rechaza `source=all`.

## API

Todas las rutas usan `Authorization: Api-Key <clave>` y throttling por clave.
Metas, tesis, notas, errores, resumenes y transcripts se redactan antes de salir
por API.

| Ruta | Scope | Contenido |
|---|---|---|
| `GET /alerts/` | `read` | alertas y resultado neto |
| `GET /strategies/` | `read` | catalogo y estado enabled |
| `GET /investep-decisions/` | `read` | decisiones, gates y blockers |
| `GET /replay-runs/` | `read` | auditoria de reconstrucciones |
| `GET /agent-runs/` | `read` | resumen de corridas |
| `GET /agent-runs/<id>/` | `transcript` | transcript con secretos redactados |
| `POST /replay/` | `replay` | calculo o persistencia explicita |

```bash
python manage.py create_api_key "dashboard" --scope read
python manage.py create_api_key "replay" --scope read --scope replay
python manage.py create_api_key "auditoria" --scope read --scope transcript
```

## Procesos y comandos

```bash
python manage.py migrate
python manage.py seed_strategies
python manage.py check_provider
python manage.py scan_once --dry-run
python manage.py scan_loop --interval 30
python manage.py agent_loop --symbols TSLA,SPY,QQQ
```

`seed_strategies` aplica contencion cada vez: habilita solo E01/E02, ambas
ramas, para la watchlist configurada. `--preserve-enabled` existe para una
intervencion consciente; `--disable-new` deja todo apagado.

El scanner y el agente deben correr en procesos distintos. `scan_loop --agent`
no ejecuta DeepSeek y emite una advertencia.

## Pruebas

```bash
python3 dev_project/manage.py check
python3 dev_project/manage.py makemigrations --check --dry-run
python3 -m pytest -q
```

La suite cubre causalidad de barras, las dos ramas E01/E02, rechazo por mecha,
calendario, quote fresca, rango de contratos, decisiones por corrida,
idempotencia, presupuesto serializado, replay atomico, fuentes, scopes y
redaccion de secretos.

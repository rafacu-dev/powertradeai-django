# powertradeai-django

Aplicacion Django para ejecutar y auditar la metodologia documentada de
Investep Academy con datos de Alpaca y ThetaData y un agente conectado a
DeepSeek.

La arquitectura no permite que el LLM convierta una narracion en una entrada.
DeepSeek propone una estrategia; el servidor vuelve a calcular el setup, aplica
los gates y persiste una `InvestepDecision`. Solo una decision `valid` puede ser
consumida una vez por `create_alert`.

## Estado operativo

- E01/E02 `OPENING_GAP`: evaluacion 09:31 ET con la vela 15m en formacion y
  exclusivamente el minuto 09:30-09:31 ya cerrado.
- E01/E02 `INTRADAY_BREAK`: evaluacion durante la sesion con velas 15m cerradas.
- E03-E10: presentes en el manual, pero bloqueadas con
  `NO_DETERMINISTIC_VALIDATOR` hasta implementar y verificar su mecanica.
- E11/E12: no operables segun el corpus.
- Las reglas heredadas siguen disponibles para investigacion, pero
  `seed_strategies` las deja deshabilitadas.
- Watchlist por defecto: `TSLA,SPY,QQQ`.

No existe evidencia suficiente para afirmar rentabilidad. Los agregados nuevos
usan `evaluation_version=investep_v2`, P&L neto y fuentes separadas.

## Inicio rapido

```bash
pip install -e .
python3 dev_project/manage.py migrate
python3 dev_project/manage.py seed_strategies
python3 dev_project/manage.py check
python3 -m pytest -q
```

Documentacion:

- [Contrato y operacion](powerTradeAi_djangoApp/README.md)
- [Despliegue en Render](powerTradeAi_djangoApp/DEPLOY.md)
- [Manual incluido para el agente](powerTradeAi_djangoApp/agent/manual/investep.md)

## Procesos

El scanner determinista y DeepSeek corren en procesos distintos para que una
llamada lenta al LLM no retrase stops ni cierres.

```bash
python manage.py scan_loop --interval 30
python manage.py agent_loop --symbols TSLA,SPY,QQQ
```

`scan_loop --agent` ya no ejecuta el agente; muestra una advertencia para que un
despliegue antiguo no falle en silencio.

## API keys

```bash
python manage.py create_api_key "dashboard" --scope read
python manage.py create_api_key "replay" --scope read --scope replay
python manage.py create_api_key "auditoria" --scope read --scope transcript
```

Scopes: `read`, `replay`, `transcript` y `*`. Las claves se almacenan por hash y
el valor en claro solo aparece al crearlas.

## Verificacion

La suite interna cubre causalidad, E01/E02, gates, decision idempotente, limites
de riesgo, replay atomico, separacion de fuentes y permisos de API. La suite de
paridad externa vive en `../LocalQuantAI/tests_powertradeai/`.

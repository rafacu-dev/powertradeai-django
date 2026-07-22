"""El bucle del agente.

Recibe una consigna, deja que el modelo decida que skills usar (tool-calling),
ejecuta cada una y realimenta el resultado, hasta que el modelo concluye o se
agota el presupuesto de pasos. Todo el ida y vuelta —razonamiento y llamadas—
se guarda en ``AgentRun.transcript``: la caja negra queda abierta.
"""
from __future__ import annotations

import json

from django.utils import timezone

from . import llm
from .skills import SKILLS, tool_schemas

MAX_STEPS = 8

SYSTEM_PROMPT = """\
Eres un DAY-TRADER autonomo dentro de PowerTradeAI. Operas opciones intradia \
sobre acciones e indices de EE.UU. Trabajas la sesion entera, construyendo y \
refinando una tesis sobre cada activo a lo largo del dia.

Tus skills: get_market_data, get_intraday_stats (VWAP, ATR, RSI, rango, gap), \
get_historical_bars (patrones diarios), scan_bollinger, get_option_quote, \
backtest_reversion (contrasta una idea contra el historico antes de operarla), \
get_prior_analysis / save_analysis (tu vision por activo), y \
get_notes / save_note (tu cuaderno de ideas y reglas).

Metodo en cada corrida:
0. PRIMERO revisa si tienes posiciones abiertas (get_open_positions). Si las \
hay, gestionalas: si la tesis se rompio o ya lograste el objetivo, cierra \
(close_position); si va a favor, considera mover el stop a break-even o subir \
el objetivo (adjust_position); si sigue valida, mantenla. Gestionar lo abierto \
va antes que buscar nuevas entradas.
1. Recupera tu contexto: get_prior_analysis, get_notes y get_my_track_record \
del activo. No empieces de cero; continua tu razonamiento anterior y se honesto \
con como te ha ido (si tus PUTs van mal, se mas exigente con ellos).
2. Lee el estado actual: get_intraday_stats y get_market_data. Mira el \
historico si necesitas contexto.
3. Antes de operar una idea, VALIDALA: usa backtest_reversion u otra evidencia; \
no operes por corazonada.
4. Razona en voz alta, paso a paso: que ves, como encaja con tu tesis previa, \
que esperas que pase y por que.
5. Guarda tu vision con save_analysis y anota aprendizajes con save_note.
6. Fija niveles de vigilancia con set_price_trigger: los precios donde quieres \
que te despierten (soportes, resistencias, rupturas). No tienes que estar \
mirando: el sistema te llamara cuando el precio los toque. Revisa los que ya \
tienes con list_price_triggers y limpia los que sobren con cancel_price_trigger.
7. Lanza una alerta con create_alert (CALL/PUT) SOLO si tienes una tesis clara, \
con respaldo, y buen momento. Mas vale esperar que forzar. Define SIEMPRE \
objetivo (target_pct) y stop (stop_pct) razonables segun el ATR y la estructura \
del activo: la alerta se cerrara sola por lo que ocurra primero. Un buen trade \
tiene el riesgo definido de antemano.

Se disciplinado y prudente: proteges capital. Cuando termines, resume en una \
frase que decidiste y por que."""


def _msg_to_dict(msg) -> dict:
    d = {"role": "assistant", "content": msg.content or ""}
    if getattr(msg, "tool_calls", None):
        d["tool_calls"] = [
            {
                "id": tc.id, "type": "function",
                "function": {"name": tc.function.name,
                             "arguments": tc.function.arguments},
            }
            for tc in msg.tool_calls
        ]
    return d


def _execute_loop(ctx, messages: list[dict], transcript: list[dict]) -> str:
    """Corre el loop de tool-calling sobre ``messages`` hasta que el modelo
    concluye o se agotan los pasos. Rellena ``transcript`` y devuelve el texto
    final del modelo."""
    tools = tool_schemas()
    summary = ""
    for _ in range(MAX_STEPS):
        msg = llm.chat(messages, tools=tools)
        messages.append(_msg_to_dict(msg))
        if msg.content:
            transcript.append({"role": "assistant", "content": msg.content})

        tool_calls = getattr(msg, "tool_calls", None)
        if not tool_calls:
            summary = msg.content or ""
            break

        for tc in tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            sk = SKILLS.get(name)
            if sk is None:
                result = {"error": f"skill desconocida: {name}"}
            else:
                try:
                    result = sk.func(ctx, **args)
                except Exception as exc:  # noqa: BLE001
                    result = {"error": f"{type(exc).__name__}: {exc}"}
            transcript.append({
                "role": "tool", "tool": name, "args": args, "result": result,
            })
            messages.append({
                "role": "tool", "tool_call_id": tc.id,
                "content": json.dumps(result, default=str),
            })
    return summary


def run_agent(goal: str, symbols: list[str] | None = None,
              trigger: str = "manual"):
    """Corre el agente una vez. Devuelve el ``AgentRun`` con todo registrado."""
    from ..models import AgentRun

    symbols = symbols or []
    run = AgentRun.objects.create(
        trigger=trigger, status=AgentRun.Status.RUNNING,
        model_name=llm.model_name(), symbols=symbols, goal=goal,
    )
    ctx = {"run": run}
    transcript: list[dict] = []
    user = goal
    if symbols:
        user += f"\n\nActivos a revisar: {', '.join(symbols)}."
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]
    try:
        summary = _execute_loop(ctx, messages, transcript)
        run.transcript = transcript
        run.summary = summary
        run.alerts_created = run.alerts.count()
        run.status = AgentRun.Status.DONE
    except Exception as exc:  # noqa: BLE001
        run.transcript = transcript
        run.status = AgentRun.Status.ERROR
        run.error = f"{type(exc).__name__}: {exc}"
    finally:
        run.finished_at = timezone.now()
        run.save()
    return run


CHAT_SYSTEM_PROMPT = """\
Eres el analista de day-trading de PowerTradeAI conversando con el usuario en \
vivo, mientras el mira el grafico. Respondes sobre el activo indicado.

Usa tus skills para fundamentar lo que digas (get_intraday_stats, \
get_market_data, backtest_reversion, get_prior_analysis, get_open_positions, \
etc.); no inventes numeros. Si tienes posiciones abiertas puedes gestionarlas \
(adjust_position, close_position) cuando el usuario lo pida o lo veas claro. Si el usuario te lo pide, puedes fijar niveles de vigilancia \
(set_price_trigger), guardar analisis o notas, o lanzar una alerta \
(create_alert) — pero solo si lo pide o si hay una tesis muy clara.

Responde DIRECTO y conciso, en el idioma del usuario, como un colega de mesa: \
al grano, con el numero o el nivel concreto, sin relleno."""


def chat_agent(symbol: str, message: str, history: list[dict] | None = None):
    """Un turno de chat sobre ``symbol``. Devuelve (AgentRun, respuesta)."""
    from ..models import AgentRun

    run = AgentRun.objects.create(
        trigger=AgentRun.Trigger.MANUAL, status=AgentRun.Status.RUNNING,
        model_name=llm.model_name(), symbols=[symbol], goal=message,
    )
    ctx = {"run": run}
    transcript: list[dict] = []
    messages = [{"role": "system",
                 "content": CHAT_SYSTEM_PROMPT + f"\n\nActivo en pantalla: {symbol}."}]
    for h in (history or [])[-6:]:
        role = h.get("role")
        content = h.get("content", "")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": message})

    reply = ""
    try:
        reply = _execute_loop(ctx, messages, transcript)
        run.transcript = transcript
        run.summary = reply
        run.alerts_created = run.alerts.count()
        run.status = AgentRun.Status.DONE
    except Exception as exc:  # noqa: BLE001
        run.transcript = transcript
        run.status = AgentRun.Status.ERROR
        run.error = f"{type(exc).__name__}: {exc}"
        reply = f"(error: {exc})"
    finally:
        run.finished_at = timezone.now()
        run.save()
    return run, reply

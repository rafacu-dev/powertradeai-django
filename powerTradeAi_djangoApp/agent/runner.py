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
MAX_TOOL_CALLS_PER_STEP = 8
MAX_TOOL_CALLS_TOTAL = 24

SYSTEM_PROMPT = """\
Eres el agente de analisis de PowerTradeAI. Trabajas exclusivamente con el \
manual de Investep Academy y con datos obtenidos por skills. No inventas \
umbrales, setups, eventos, quotes ni condiciones.

Orden obligatorio en cada corrida:
1. Revisa posiciones abiertas y gestiona primero las que ya existen. Plan 10 no \
permite refuerzo ni ajustes posteriores. Solo cierra antes si la tesis se invalida.
2. Consulta contexto y datos con get_daily_briefing, get_market_data, \
get_estado_volatilidad y get_trendlines. Las medias academicas son \
MA20/MA40/MA100/MA200.
3. Nombra una sola estrategia y una rama concreta. Llama consultar_manual con su \
codigo; recordar el texto no sustituye esa llamada.
4. Llama validate_investep_setup. El servidor recalcula el setup y devuelve \
VALID, WAIT o BLOCKED. No discutas ni reinterpretas un bloqueo.
5. Solo si el resultado es VALID puedes pasar su decision_id a create_alert. El \
servidor elige direccion, Plan 10 y cantidad; no intentes reemplazarlos.
6. Si faltan calendario, terreno, modelo spot-prima, quote o validador mecanico, \
la respuesta correcta es no operar y reportar el blocker exacto.
7. Guarda analisis y notas como observaciones, nunca como nuevas reglas. Un \
backtest generico no valida una estrategia academica.

Separa siempre REGLA_ACADEMIA, IMPLEMENTACION, EVIDENCIA y \
PENDIENTE_CALIBRACION. Resume la decision y la condicion que podria cambiarla."""


def _system_prompt() -> str:
    """El prompt base mas la restriccion del manual Investep.

    Se compone en tiempo de ejecucion para que el catalogo de estrategias sea la
    unica fuente: si manana se documenta una nueva o se retira otra, el prompt
    cambia solo. El manual completo NO va aqui (son ~12.000 tokens): el agente
    lo consulta por partes con ``consultar_manual``.
    """
    from .investep import bloque_prompt
    return SYSTEM_PROMPT + "\n\n" + bloque_prompt()


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
    # El chat de grafico es de analisis: no posee herramientas que alteren el
    # estado de posiciones. El refuerzo tampoco pertenece a Plan 10 y no se
    # ofrece al modelo en ninguna modalidad.
    denied = {"reinforce_position", "backtest_reversion", "adjust_position"}
    if ctx.get("channel") == "chat":
        denied.update({"create_alert", "adjust_position", "close_position"})
    tools = [
        item for item in tools
        if item["function"]["name"] not in denied
    ]
    summary = ""
    executed_calls = 0
    for _ in range(MAX_STEPS):
        msg = llm.chat(messages, tools=tools)
        messages.append(_msg_to_dict(msg))
        if msg.content:
            transcript.append({"role": "assistant", "content": msg.content})

        tool_calls = getattr(msg, "tool_calls", None)
        if not tool_calls:
            summary = msg.content or ""
            break

        for index, tc in enumerate(tool_calls):
            name = tc.function.name
            over_limit = (
                index >= MAX_TOOL_CALLS_PER_STEP
                or executed_calls >= MAX_TOOL_CALLS_TOTAL
            )
            if name in denied:
                args = {}
                result = {"error": f"skill no permitida en canal {ctx.get('channel')}"}
            elif over_limit:
                args = {}
                result = {
                    "error": "limite de llamadas a skills alcanzado",
                    "max_per_step": MAX_TOOL_CALLS_PER_STEP,
                    "max_total": MAX_TOOL_CALLS_TOTAL,
                }
            else:
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
                executed_calls += 1
            transcript.append({
                "role": "tool", "tool": name, "args": args, "result": result,
            })
            messages.append({
                "role": "tool", "tool_call_id": tc.id,
                "content": json.dumps(result, default=str),
            })
    return summary


def run_agent(goal: str, symbols: list[str] | None = None,
              trigger: str = "manual", as_of=None):
    """Corre el agente una vez. Devuelve el ``AgentRun`` con todo registrado.

    ``as_of`` (opcional): reloj causal para entrenamiento en tiempo pasado.
    Las skills solo veran datos hasta ese instante."""
    from ..models import AgentRun

    if not symbols:
        from .decision import configured_watchlist
        symbols = list(configured_watchlist())
    run = AgentRun.objects.create(
        trigger=trigger, status=AgentRun.Status.RUNNING,
        model_name=llm.model_name(), symbols=symbols, goal=goal,
    )
    ctx = {"run": run, "as_of": as_of, "channel": "agent"}
    transcript: list[dict] = []
    user = goal
    if symbols:
        user += f"\n\nActivos a revisar: {', '.join(symbols)}."
    messages = [
        {"role": "system", "content": _system_prompt()},
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
Estas conversando con el usuario mientras observa un grafico. Esta modalidad es \
solo de analisis: puedes consultar datos, validar un setup y guardar notas, pero \
no crear, ajustar ni cerrar posiciones. Responde directo, en el idioma del \
usuario, citando datos concretos y blockers del validador."""


def chat_agent(symbol: str, message: str, history: list[dict] | None = None):
    """Un turno de chat sobre ``symbol``. Devuelve (AgentRun, respuesta)."""
    from ..models import AgentRun

    run = AgentRun.objects.create(
        trigger=AgentRun.Trigger.MANUAL, status=AgentRun.Status.RUNNING,
        model_name=llm.model_name(), symbols=[symbol], goal=message,
    )
    ctx = {"run": run, "channel": "chat"}
    transcript: list[dict] = []
    messages = [{
        "role": "system",
        "content": (
            _system_prompt() + "\n\n" + CHAT_SYSTEM_PROMPT
            + f"\n\nActivo en pantalla: {symbol}."
        ),
    }]
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

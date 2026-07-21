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
Eres un analista de trading dentro de PowerTradeAI. Operas opciones intradia \
sobre acciones e indices de EE.UU.

Tu trabajo cada corrida:
1. Revisa los activos que se te indiquen con las skills de mercado y el scanner.
2. Lee tu analisis previo (get_prior_analysis) para dar continuidad; no empieces \
de cero si ya tenias una vision.
3. Razona en voz alta, paso a paso, que ves y por que.
4. Deja constancia de tu vision de cada activo con save_analysis (aunque no \
operes).
5. Solo si tienes una tesis clara y accionable, lanza una alerta con \
create_alert (CALL o PUT), explicando la tesis.

Se prudente: mas vale observar y guardar analisis que forzar una alerta debil. \
Cuando termines, resume en una frase que hiciste y por que."""


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
    tools = tool_schemas()

    try:
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

"""Cliente LLM compatible con la API de OpenAI.

Por defecto apunta a DeepSeek (el mejor calidad/precio de los proveedores
chinos), pero ``base_url``, ``api_key`` y ``model`` se configuran por settings,
asi que sirve igual para Kimi (Moonshot), GLM (Zhipu) o Qwen: todos exponen una
API compatible con OpenAI. Una sola integracion los cubre a todos.

    POWERTRADEAI = {
        "AGENT_LLM": {
            "BASE_URL": "https://api.deepseek.com",
            "API_KEY": os.getenv("DEEPSEEK_API_KEY"),
            "MODEL": "deepseek-chat",
        },
    }
"""
from __future__ import annotations

from django.conf import settings

DEFAULTS = {
    "BASE_URL": "https://api.deepseek.com",
    "MODEL": "deepseek-chat",
    "API_KEY": None,
    "TEMPERATURE": 0.3,
    "MAX_TOKENS": 2048,
}


class LLMError(RuntimeError):
    pass


def _config() -> dict:
    cfg = dict(DEFAULTS)
    cfg.update(getattr(settings, "POWERTRADEAI", {}).get("AGENT_LLM", {}))
    return cfg


def model_name() -> str:
    return _config()["MODEL"]


def _client(cfg: dict):
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover
        raise LLMError(
            "Falta el paquete 'openai'. Instalalo: pip install openai") from exc
    if not cfg.get("API_KEY"):
        raise LLMError(
            "No hay API key del LLM. Configura POWERTRADEAI['AGENT_LLM']"
            "['API_KEY'] (p.ej. desde DEEPSEEK_API_KEY).")
    return OpenAI(api_key=cfg["API_KEY"], base_url=cfg["BASE_URL"])


def chat(messages: list[dict], tools: list[dict] | None = None):
    """Una vuelta de chat con tool-calling. Devuelve el ``message`` del modelo
    (con ``.content`` y/o ``.tool_calls``)."""
    cfg = _config()
    client = _client(cfg)
    kwargs = dict(
        model=cfg["MODEL"],
        messages=messages,
        temperature=cfg["TEMPERATURE"],
        max_tokens=cfg["MAX_TOKENS"],
    )
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    try:
        resp = client.chat.completions.create(**kwargs)
    except Exception as exc:  # pragma: no cover - depende de la red
        raise LLMError(f"Fallo la llamada al LLM: {exc}") from exc
    return resp.choices[0].message

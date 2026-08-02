"""Modo Investep: el agente opera SOLO las estrategias documentadas del manual.

Diseño en dos piezas, porque el manual son ~12.000 tokens y meterlo entero en
cada llamada seria caro y ademas los detalles se diluyen:

  1. El PROMPT lleva la restriccion dura y el indice de las 12 estrategias. Es
     lo que el modelo debe tener siempre presente.
  2. La skill ``consultar_manual`` sirve la seccion concreta cuando la necesita.

Y una tercera pieza que hace real la restriccion: ``create_alert`` solo acepta
el ``decision_id`` emitido por el validador determinista. Recordar o narrar una
estrategia no permite saltarse ese contrato.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

RUTA = Path(__file__).parent / "manual" / "investep.md"

# Catalogo cerrado. La fuente documenta doce; E11/E12 estan CORREGIDAS en la
# auditoria (el Worden no tiene direccion, formula ni umbral publicados) y su
# proxy quedo desactivado, asi que no son operables por un bot.
ESTRATEGIAS = {
    "E01": "Cambio de tendencia al alza, Bollinger 15m (CALL)",
    "E02": "Cambio de tendencia a la baja, Bollinger 15m (PUT)",
    "E03": "Cambio de tendencia al alza, hora (CALL)",
    "E04": "Cambio de tendencia a la baja, hora (PUT)",
    "E05": "Rebote diario alcista (CALL)",
    "E06": "Rebote diario bajista (PUT)",
    "E07": "Ruptura de lateral al alza (CALL)",
    "E08": "Ruptura de lateral a la baja (PUT)",
    "E09": "Apertura fuera arriba sin volatilidad (PUT)",
    "E10": "Apertura fuera abajo sin volatilidad (CALL)",
}
AUTOMATIZADAS = frozenset({"E01", "E02"})
NO_OPERABLES = {
    "E11": "Efecto iman alcista — Worden sin direccion/formula/umbral publicados",
    "E12": "Efecto iman bajista — misma correccion que E11",
}


@lru_cache(maxsize=1)
def texto() -> str:
    return RUTA.read_text(encoding="utf-8") if RUTA.exists() else ""


@lru_cache(maxsize=1)
def secciones() -> dict[str, str]:
    """{titulo: contenido} a partir de los encabezados markdown."""
    t = texto()
    if not t:
        return {}
    out, actual, buf = {}, None, []
    for linea in t.splitlines():
        if re.match(r"^#{1,3} ", linea):
            if actual:
                out[actual] = "\n".join(buf).strip()
            actual, buf = linea.lstrip("# ").strip(), []
        elif actual:
            buf.append(linea)
    if actual:
        out[actual] = "\n".join(buf).strip()
    return out


def buscar(consulta: str, max_chars: int = 6000) -> list[dict]:
    """Secciones cuyo titulo o cuerpo mencionan la consulta."""
    q = consulta.lower().strip()
    if not q:
        return []
    exactas, parciales = [], []
    for titulo, cuerpo in secciones().items():
        if q in titulo.lower():
            exactas.append({"seccion": titulo, "texto": cuerpo[:max_chars]})
        elif q in cuerpo.lower():
            parciales.append({"seccion": titulo, "texto": cuerpo[:max_chars]})
    return (exactas + parciales)[:5]


def es_operable(codigo: str) -> tuple[bool, str]:
    c = (codigo or "").strip().upper()
    if c in AUTOMATIZADAS:
        return True, ESTRATEGIAS[c]
    if c in ESTRATEGIAS:
        # Operable en fase de INVESTIGACION: sin validador mecanico el servidor
        # no confirma el setup, pero bloquearlas significaria no aprender nunca
        # si sirven. La decision queda marcada JUICIO_AGENTE y se analiza aparte.
        return True, (f"{ESTRATEGIAS[c]} — sin validador determinista: "
                      "la decision quedara marcada como JUICIO_AGENTE")
    if c in NO_OPERABLES:
        return False, f"{c} NO es operable por el bot: {NO_OPERABLES[c]}"
    return False, (f"'{codigo}' no es una estrategia del manual. "
                   f"Validas: {', '.join(sorted(ESTRATEGIAS))}.")


def bloque_prompt() -> str:
    """Restriccion e indice para el system prompt. Corto a proposito."""
    lineas = [f"  {k} — {v}" for k, v in sorted(ESTRATEGIAS.items())]
    no_op = [f"  {k} — NO OPERABLE ({v})" for k, v in sorted(NO_OPERABLES.items())]
    return (
        "MODO INVESTEP. Operas EXCLUSIVAMENTE las estrategias del manual de la "
        "academia. No inventas setups, no operas corazonadas y no combinas "
        "reglas de estrategias distintas.\n\n"
        "Estrategias documentadas:\n" + "\n".join(lineas) + "\n"
        + "\n".join(no_op) + "\n\n"
        "Dos niveles de validacion, y debes saber en cual estas:\n"
        "  DETERMINISTA (E01/E02, ramas OPENING_GAP e INTRADAY_BREAK): el "
        "servidor RECALCULA el setup y solo emite decision_id si se cumple.\n"
        "  JUICIO_AGENTE (E03-E10): el servidor NO puede verificar el setup. "
        "Son operables porque estamos en investigacion y hay que aprender si "
        "sirven, pero la unica evidencia sera lo que TU declares haber visto. "
        "Por eso ahi se exige una tesis mucho mas detallada: condicion por "
        "condicion, con los niveles concretos que ves. Estas alertas se "
        "analizan por separado y no se mezclan con las deterministas.\n\n"
        "Antes de operar DEBES llamar a consultar_manual con la estrategia que "
        "crees ver, y comprobar UNA POR UNA sus condiciones y sus "
        "invalidaciones. Si falta cualquier requisito, NO operas: la respuesta "
        "correcta la mayoria de los dias es no hacer nada.\n\n"
        "Luego DEBES llamar validate_investep_setup con estrategia, rama y una "
        "tesis concreta. Solo su ``decision_id`` con estado VALID puede pasar "
        "a create_alert; WAIT y BLOCKED no se reinterpretan.\n\n"
        "Gestion publicada: objetivo 10%-15% sobre la PRIMA y stop -20% "
        "(Plan 10). Las barreras (MA40, techo, piso) sirven para comprobar que "
        "hay TERRENO antes de entrar, no como objetivo de salida.\n\n"
        "Vacios reconocidos del material: no hay umbral numerico de 'alta "
        "volatilidad' ni 'tendencia clara'. El terreno solo se automatiza con "
        "un modelo empirico configurado y versionado. Ante cualquier vacio NO "
        "inventes un numero: conserva el bloqueo y reportalo."
    )

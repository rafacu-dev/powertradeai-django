"""Comparacion de la deteccion contra un artefacto de backtest.

Cierra el unico hueco de verificacion que quedaba abierto. Los golden tests
existentes comprueban la aritmetica de P&L y la seleccion de strike, pero dan la
senal por buena: el CSV trae ``range_high``, ``signal_bar_ts`` y ``entry_ask``
ya resueltos, no las velas que los produjeron.

Aqui se reconstruye la senal desde velas crudas y se compara campo a campo con
el artefacto. Si coincide en las 128 sesiones, la regla esta portada fielmente;
si no, el diff dice exactamente donde diverge.

Deliberadamente NO compara P&L. Un P&L reconstruido es un limite superior
optimista y compararlo solo produciria ruido: lo que se verifica aqui es si la
app VE las mismas senales, no si gana lo mismo.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd

from ..strategies import get_strategy_class
from .replay import detect_signal
from .session import NY

# Tolerancias. El rango sale de max/min sobre velas, asi que un centimo de
# diferencia es ruido de feed, no una regla distinta.
PRICE_TOL = 0.01


@dataclass
class SessionDiff:
    day: str
    ok: bool
    fields: dict = field(default_factory=dict)   # campo -> (esperado, obtenido)
    note: str = ""

    @property
    def diverged(self) -> list[str]:
        return sorted(self.fields)


@dataclass
class GoldenReport:
    strategy_id: str
    artifact: str
    diffs: list[SessionDiff] = field(default_factory=list)

    @property
    def matched(self) -> list[SessionDiff]:
        return [d for d in self.diffs if d.ok]

    @property
    def mismatched(self) -> list[SessionDiff]:
        return [d for d in self.diffs if not d.ok]

    @property
    def field_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for diff in self.mismatched:
            for name in diff.diverged:
                counts[name] = counts.get(name, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def load_artifact(path: str | Path) -> list[dict]:
    """Filas utilizables del CSV causal."""
    with Path(path).open(newline="", encoding="utf-8") as fh:
        return [row for row in csv.DictReader(fh)
                if row.get("replay_status", "ok") == "ok"]


def _num(value):
    if value in (None, "", "nan"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def compare_session(strategy, row: dict, provider,
                    history_cache: dict) -> SessionDiff:
    """Reconstruye una sesion y la compara con su fila del artefacto."""
    day_text = row["day"]
    day = datetime.strptime(day_text, "%Y-%m-%d").date()

    try:
        bars = provider.bars_1m(strategy.symbol, day)
    except Exception as exc:
        return SessionDiff(day_text, False, note=f"sin velas: {exc}")
    if bars.empty:
        return SessionDiff(day_text, False, note="el proveedor no devolvio velas")

    signal, _ = detect_signal(strategy, day, bars, provider, history_cache)
    if signal is None:
        return SessionDiff(
            day_text, False,
            note="el artefacto tiene senal y la app no detecta ninguna")

    fields: dict = {}

    # Si el artefacto trae un campo y la app no lo produce, eso es una
    # DIVERGENCIA, no un campo a saltar. Sin esto, un ``meta`` que dejara de
    # publicar ``range_high`` haria que la comparacion pasara sin comparar
    # nada — el fallo mas caro posible en una herramienta de verificacion.
    def expect(name: str, want, got, equal) -> None:
        if want is None:
            return                      # el artefacto no lo trae: nada que exigir
        if got is None:
            fields[name] = (want, "AUSENTE en la app")
            return
        if not equal(want, got):
            fields[name] = (want, got)

    def close_enough(want, got):
        return abs(float(want) - float(got)) <= PRICE_TOL

    # --- Rango de apertura ---
    for key in ("range_high", "range_low"):
        expect(key, _num(row.get(key)), signal.meta.get(key), close_enough)

    # --- Direccion ---
    expect("direction", row.get("direction") or None, signal.direction,
           lambda a, b: a == b)

    # --- Vela que disparo ---
    expected_bar = row.get("signal_bar_ts") or None
    got_bar = signal.meta.get("signal_bar_ts")
    if expected_bar is not None and got_bar is None:
        fields["signal_bar_ts"] = (expected_bar, "AUSENTE en la app")
    elif expected_bar is not None:
        # El artefacto guarda UTC naive; la app, ISO con zona.
        want = pd.Timestamp(expected_bar).tz_localize("UTC")
        have = pd.Timestamp(got_bar).tz_convert("UTC")
        if want != have:
            fields["signal_bar_ts"] = (
                want.tz_convert(NY).strftime("%H:%M"),
                have.tz_convert(NY).strftime("%H:%M"))

    # --- Subyacente en la entrada ---
    expect("under_entry", _num(row.get("under_entry")),
           signal.underlying, close_enough)

    if not fields and not _compared_anything(row):
        return SessionDiff(
            day_text, False,
            note="el artefacto no trae ninguno de los campos comparables")

    return SessionDiff(day_text, not fields, fields)


COMPARED_KEYS = ("range_high", "range_low", "direction",
                 "signal_bar_ts", "under_entry")


def _compared_anything(row: dict) -> bool:
    """Guardia contra un CSV con otras columnas: sin campos comunes, la
    comparacion pasaria siempre y no verificaria nada."""
    return any(row.get(key) not in (None, "") for key in COMPARED_KEYS)


def compare_artifact(strategy_id: str, artifact: str | Path, provider,
                     limit: int | None = None,
                     on_progress=None) -> GoldenReport:
    """Compara la deteccion de una regla contra todas las sesiones del CSV."""
    strategy = get_strategy_class(strategy_id)()
    rows = load_artifact(artifact)
    if limit:
        rows = rows[:limit]

    report = GoldenReport(strategy_id=strategy_id, artifact=str(artifact))
    history_cache: dict = {}

    for index, row in enumerate(rows, start=1):
        diff = compare_session(strategy, row, provider, history_cache)
        report.diffs.append(diff)
        if on_progress:
            on_progress(index, len(rows), diff)
    return report

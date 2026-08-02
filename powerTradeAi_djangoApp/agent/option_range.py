"""Calculo puro del rango de precio de contratos explicado por la academia."""
from __future__ import annotations

import numpy as np
import pandas as pd


def calculate_option_price_range(
    contracts: pd.DataFrame,
    *,
    spot: float,
    direction: str,
    minimum_contract_cost: float = 20.0,
    maximum_contract_cost: float | None = None,
    contract_count: int = 8,
    rounding: float = 5.0,
) -> dict:
    """Selecciona los dos contratos con mayor excursion historica de prima.

    ``ask``, ``low`` y ``high`` son primas por accion. Las fronteras devueltas
    son costo por contrato, es decir, prima multiplicada por 100.
    """
    required = {"occ_symbol", "expiration", "strike", "ask", "low", "high"}
    missing = required.difference(contracts.columns)
    if missing:
        raise ValueError(f"faltan columnas: {', '.join(sorted(missing))}")
    if direction not in {"CALL", "PUT"}:
        raise ValueError("direction debe ser CALL o PUT")
    if contract_count < 2:
        raise ValueError("contract_count debe ser al menos 2")

    table = contracts.copy()
    for column in ("strike", "ask", "low", "high"):
        table[column] = pd.to_numeric(table[column], errors="coerce")
    table = table.dropna(subset=["strike", "ask", "low", "high"])
    table = table[np.isfinite(table[["strike", "ask", "low", "high"]]).all(axis=1)]
    table = table[
        (table["ask"] > 0)
        & (table["low"] > 0)
        & (table["high"] >= table["low"])
    ].copy()
    table["contract_cost"] = table["ask"] * 100
    table = table[table["contract_cost"] >= float(minimum_contract_cost)]
    if maximum_contract_cost is not None:
        table = table[table["contract_cost"] <= float(maximum_contract_cost)]
    if len(table) < 2:
        raise ValueError("menos de dos contratos superan los filtros")

    table["distance_atm"] = (table["strike"] - float(spot)).abs()
    table["is_otm"] = (
        table["strike"].ge(float(spot))
        if direction == "CALL"
        else table["strike"].le(float(spot))
    )
    table = table.sort_values(
        ["distance_atm", "is_otm"],
        ascending=[True, False],
        kind="stable",
    ).head(int(contract_count))
    table["historical_excursion_pct"] = (
        (table["high"] - table["low"]) / table["low"] * 100
    )
    table = table.sort_values(
        "historical_excursion_pct", ascending=False, kind="stable"
    ).reset_index(drop=True)
    table["selected"] = table.index < 2
    limits = sorted(table.loc[table["selected"], "contract_cost"].tolist())

    def rounded(value: float) -> float:
        if rounding <= 0:
            return float(value)
        return float(round(value / rounding) * rounding)

    columns = [
        "occ_symbol", "expiration", "strike", "ask", "low", "high",
        "contract_cost", "historical_excursion_pct", "selected",
    ]
    for optional in ("bid", "spread_pct", "quote_timestamp"):
        if optional in table:
            columns.append(optional)
    rows = table[columns].copy()
    if "expiration" in rows:
        rows["expiration"] = rows["expiration"].astype(str)
    return {
        "status": "ok",
        "range_per_contract": {
            "minimum": rounded(limits[0]),
            "maximum": rounded(limits[1]),
            "currency": "USD",
        },
        "contracts": rows.to_dict(orient="records"),
        "formula_provenance": "REGLA_ACADEMIA",
        "selection_provenance": "IMPLEMENTACION",
    }

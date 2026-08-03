"""SPX y el resto de indices en la pantalla de convexidad.

Tres cosas se rompian al anadirlos, y cada una es silenciosa:

  1. ``latest_price`` pega al endpoint de ACCIONES. Un indice no cotiza ahi, asi
     que el simbolo desaparecia de la tabla sin error visible.
  2. El root de opciones de los vencimientos cercanos es SPXW, no SPX. Con el
     root equivocado la cadena vuelve vacia.
  3. La banda de strikes se calculaba en dolares con paso fijo: con SPX a 7.500
     y paso 5 cubria +-0.96%, y los contratos que ganan estan al 1-3%. El
     ranking salia, pero de un universo que excluia a los ganadores.
"""
from datetime import date
from types import SimpleNamespace

import pytest

from powerTradeAi_djangoApp.agent import convexidad as cx
from powerTradeAi_djangoApp.agent import convexidad_scan as cs

HOY = date(2026, 7, 31)
EXP = date(2026, 8, 3)

# SPX, cierre del 31-jul-2026, vencimiento 03-ago. Bid/ask reales (Alpaca).
# Spot verdadero por paridad ese dia: 7486.76 (mediana de 12 pares ATM,
# dispersion 0.45 puntos).
SPX_SPOT_REAL = 7486.76
SPX_GOLDEN = [
    (7400, "C", 88.51, 98.95), (7420, "C", 70.89, 76.52),
    (7440, "C", 56.21, 57.74), (7460, "C", 38.11, 44.78),
    (7470, "C", 32.80, 34.33), (7480, "C", 27.18, 28.33),
    (7490, "C", 21.83, 22.00), (7500, "C", 16.03, 16.33),
    (7510, "C", 11.21, 11.49), (7520, "C", 7.71, 7.82),
    (7530, "C", 4.52, 4.78), (7540, "C", 2.70, 2.87),
    (7550, "C", 1.33, 1.53), (7560, "C", 0.63, 0.78),
    (7570, "C", 0.37, 0.47), (7580, "C", 0.18, 0.27),
    (7400, "P", 4.07, 4.28), (7420, "P", 5.88, 5.96),
    (7440, "P", 8.38, 8.91), (7460, "P", 12.28, 12.63),
    (7470, "P", 14.54, 15.20), (7480, "P", 17.98, 18.39),
    (7490, "P", 21.78, 22.46), (7500, "P", 26.63, 26.69),
    (7510, "P", 31.76, 32.92), (7520, "P", 37.61, 38.99),
    (7540, "P", 52.42, 55.47), (7550, "P", 61.90, 62.96),
    (7320, "P", 1.02, 1.03), (7340, "P", 1.37, 1.52),
    (7360, "P", 1.87, 2.03), (7380, "P", 2.88, 3.05),
]


def _filas(datos):
    return [{"strike": k, "right": r, "bid": b, "ask": a} for k, r, b, a in datos]


# --- spot por paridad -----------------------------------------------------

def test_la_paridad_recupera_el_spot_real_de_spx():
    """Contra el valor calculado aparte: 7486.76."""
    s = cx.spot_por_paridad(_filas(SPX_GOLDEN), EXP, HOY, fraccion_sesion=1.0)
    assert s is not None
    assert abs(s - SPX_SPOT_REAL) < 3.0, f"paridad dio {s}, real {SPX_SPOT_REAL}"


def test_la_paridad_es_mejor_que_un_ratio_fijo_sobre_el_proxy():
    """SPY no es SPX/10: acumula dividendos y el cociente deriva. Ese error no
    se ve en pantalla, pero desplaza todos los strikes."""
    spy_ese_dia = 668.42
    por_ratio_10 = spy_ese_dia * 10
    paridad = cx.spot_por_paridad(_filas(SPX_GOLDEN), EXP, HOY)
    assert abs(paridad - SPX_SPOT_REAL) < abs(por_ratio_10 - SPX_SPOT_REAL)


def test_sin_pares_suficientes_no_inventa_un_spot():
    solo_calls = [f for f in _filas(SPX_GOLDEN) if f["right"] == "C"]
    assert cx.spot_por_paridad(solo_calls, EXP, HOY) is None


def test_la_paridad_no_se_sesga_cuando_todos_los_pares_empatan():
    """Caso degenerado: si C == P en todos los strikes, |C-P| empata y el corte
    no puede decidirse por posicion — el desempate acababa siendo el propio
    spot estimado y el resultado se iba sistematicamente a los strikes bajos.
    Aparecio con un proveedor de prueba y habria sesgado tambien cualquier
    cadena poco liquida con muchos mids simetricos."""
    centro = 7500.0
    filas = []
    for i in range(-10, 11):
        k = centro + i * 100
        filas += [{"strike": k, "right": "C", "bid": 10.0, "ask": 10.5},
                  {"strike": k, "right": "P", "bid": 10.0, "ask": 10.5}]
    s = cx.spot_por_paridad(filas, EXP, HOY)
    assert abs(s - centro) < 5.0, f"sesgado a {s}, deberia centrarse en {centro}"


def test_la_paridad_aguanta_un_par_mal_cotizado():
    """Se toma la mediana justamente para esto."""
    sucio = _filas(SPX_GOLDEN) + [
        {"strike": 7490.0, "right": "C", "bid": 900.0, "ask": 910.0},
        {"strike": 7490.0, "right": "P", "bid": 0.01, "ask": 0.02},
    ]
    s = cx.spot_por_paridad(sucio, EXP, HOY)
    assert abs(s - SPX_SPOT_REAL) < 5.0


# --- banda de strikes -----------------------------------------------------

@pytest.mark.parametrize("spot,paso", [(309.55, 2.5), (668.42, 5.0),
                                       (7486.76, 5.0), (25000.0, 5.0)])
def test_la_banda_cubre_siempre_el_porcentaje_pedido(spot, paso):
    """Antes esto fallaba en SPX: la banda se estrechaba al 0.96% sin avisar."""
    ks = cx.strikes_a_explorar(spot, paso)
    assert min(ks) <= spot * 0.965, f"banda inferior insuficiente en {spot}"
    assert max(ks) >= spot * 1.035, f"banda superior insuficiente en {spot}"


def test_la_banda_no_dispara_el_numero_de_cotizaciones():
    """Cada strike son dos llamadas al proveedor."""
    for spot, paso in ((309.55, 2.5), (7486.76, 5.0), (25000.0, 5.0)):
        assert len(cx.strikes_a_explorar(spot, paso)) <= cx.MAX_STRIKES_LADO * 2 + 1


def test_los_strikes_de_spx_caen_en_multiplos_validos():
    ks = cx.strikes_a_explorar(7486.76, 5.0)
    assert all(abs(k / 5.0 - round(k / 5.0)) < 1e-6 for k in ks)


# --- ranking de SPX sobre la cadena real ----------------------------------

def test_spx_da_call_y_put_y_la_call_es_la_barata():
    """El skew de indice: la put paga mas del doble de IV que la call, asi que
    exige mucho mas movimiento. Es la asimetria que NO aparece en acciones."""
    par = cx.mejor_par(SPX_SPOT_REAL, _filas(SPX_GOLDEN), EXP, HOY)
    assert par["call"] and par["put"]
    assert par["put"]["iv_pct"] > par["call"]["iv_pct"]
    assert abs(par["put"]["movimiento_pct"]) > abs(par["call"]["movimiento_pct"])


def test_spx_exige_mucho_menos_movimiento_que_una_accion():
    par = cx.mejor_par(SPX_SPOT_REAL, _filas(SPX_GOLDEN), EXP, HOY)
    assert abs(par["call"]["movimiento_pct"]) < 0.5


# --- configuracion del escaneo -------------------------------------------

def test_spx_esta_en_el_universo_y_declarado_como_indice():
    assert "SPX" in cs.UNIVERSO
    assert cs.INDICES["SPX"]["root"] == "SPXW", (
        "el root SPX son las mensuales AM-settled; los vencimientos cercanos "
        "son SPXW y con el root equivocado la cadena vuelve vacia")


def test_el_escaneo_de_spx_no_pide_el_precio_del_indice(monkeypatch):
    """``latest_price('SPX')`` pega al endpoint de acciones y fallaria."""
    pedidos = []

    class Proveedor:
        def latest_price(self, symbol):
            pedidos.append(symbol)
            if symbol == "SPX":
                raise RuntimeError("SPX no cotiza como accion")
            return 668.42

        def option_quote(self, occ, at=None):
            from powerTradeAi_djangoApp.data import parse_occ
            simbolo, _, direccion, strike = parse_occ(occ)
            assert simbolo == "SPXW", f"root equivocado: {simbolo}"
            dist = abs(strike - SPX_SPOT_REAL) / SPX_SPOT_REAL
            prima = max(0.30, 60.0 * (1 - dist * 22))
            return SimpleNamespace(bid=round(prima, 2), ask=round(prima * 1.05, 2))

    from powerTradeAi_djangoApp.engine.session import now_ny
    r = cs.escanear_simbolo(Proveedor(), "SPX", now_ny())
    assert "SPX" not in pedidos, "no debe pedirse el precio al contado del indice"
    assert r is not None and r["symbol"] == "SPX"
    assert r["spot_por_paridad"] is True
    assert abs(r["spot"] - SPX_SPOT_REAL) < 60


def test_una_accion_sigue_usando_su_precio_al_contado():
    """El camino de los indices no debe cambiar el de las acciones."""
    pedidos = []

    class Proveedor:
        def latest_price(self, symbol):
            pedidos.append(symbol)
            return 100.0

        def option_quote(self, occ, at=None):
            from powerTradeAi_djangoApp.data import parse_occ
            _, _, _, strike = parse_occ(occ)
            prima = max(0.06, 3.0 * (1 - abs(strike - 100.0) / 100.0 * 8))
            return SimpleNamespace(bid=round(prima, 2), ask=round(prima * 1.06, 2))

    from powerTradeAi_djangoApp.engine.session import now_ny
    r = cs.escanear_simbolo(Proveedor(), "TSLA", now_ny())
    assert pedidos == ["TSLA"]
    assert r["spot_por_paridad"] is False

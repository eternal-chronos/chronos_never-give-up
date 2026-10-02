"""Los indicadores en Python puro: la SMMA y las velas Heikin Ashi del setup 1, y el resto.

Además de los casos a mano, cada indicador se compara contra su versión
vectorizada con pandas —la que había antes de que el dominio pasara a Python
puro—, que aquí queda como oráculo: el cambio de implementación no cambia ningún
valor.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import cast

import numpy as np
import pandas as pd
import pytest

from chronos.domain.strategies.indicators import (
    Atr,
    Bollinger,
    Ema,
    HeikinAshi,
    Rma,
    RollingMax,
    RollingMin,
    RollingStd,
    Rsi,
    Sma,
    Smma,
    atr,
    bollinger,
    ema,
    heikin_ashi,
    rma,
    rolling_max,
    rolling_min,
    rolling_std,
    rsi,
    sma,
    smma,
    true_range,
)


def _nan(valores: list[float]) -> list[bool]:
    return [math.isnan(valor) for valor in valores]


# --- SMMA ----------------------------------------------------------------------------


def test_arranca_en_la_media_simple_y_suaviza_con_alfa_uno_entre_n() -> None:
    resultado = smma([1.0, 2.0, 3.0, 4.0, 5.0, 6.0], 3)

    assert _nan(resultado[:2]) == [True, True]
    assert resultado[2] == pytest.approx(2.0)  # (1 + 2 + 3) / 3
    assert resultado[3] == pytest.approx((2.0 * 2 + 4.0) / 3)
    assert resultado[4] == pytest.approx((resultado[3] * 2 + 5.0) / 3)
    assert resultado[5] == pytest.approx((resultado[4] * 2 + 6.0) / 3)


def test_con_periodo_uno_es_el_propio_valor() -> None:
    valores = [3.0, 1.0, 4.0, 1.0, 5.0]
    assert smma(valores, 1) == pytest.approx(valores)


def test_vacio_devuelve_vacio() -> None:
    assert smma([], 5) == []


def test_una_barra_no_da_valor() -> None:
    resultado = smma([1.5], 5)
    assert len(resultado) == 1
    assert math.isnan(resultado[0])


def test_ventana_mayor_que_los_datos_no_da_ningun_valor() -> None:
    assert all(_nan(smma([0.0, 1.0, 2.0, 3.0], 5)))


def test_justo_el_periodo_da_un_solo_valor_que_es_la_media() -> None:
    resultado = smma([1.0, 2.0, 3.0, 4.0, 5.0], 5)
    assert all(_nan(resultado[:4]))
    assert resultado[4] == pytest.approx(3.0)


def test_vela_a_vela_da_lo_mismo_que_la_serie_y_none_al_calentar() -> None:
    valores = [2000.0, 2001.5, 1999.0, 2003.0, 2002.0, 2004.5, 2001.0]
    media = Smma(3)
    vela_a_vela = [media.update(valor) for valor in valores]
    assert vela_a_vela[:2] == [None, None]
    assert vela_a_vela[2:] == smma(valores, 3)[2:]


def test_no_modifica_la_entrada() -> None:
    valores = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    copia = list(valores)
    smma(valores, 3)
    assert valores == copia


def test_periodo_invalido() -> None:
    with pytest.raises(ValueError, match="periodo"):
        smma([1.0, 2.0], 0)


@pytest.mark.parametrize(
    "clase", [Sma, Ema, Rma, Smma, Atr, Rsi, RollingMax, RollingMin, RollingStd, Bollinger]
)
def test_ningun_indicador_acepta_un_periodo_menor_que_uno(clase: Callable[[int], object]) -> None:
    with pytest.raises(ValueError, match="periodo"):
        clase(0)


# --- Heikin Ashi -------------------------------------------------------------------


def _velas() -> tuple[list[float], list[float], list[float], list[float]]:
    apertura = [10.0, 11.0, 12.5, 12.0]
    maximo = [12.0, 13.0, 13.0, 12.5]
    minimo = [9.0, 10.5, 11.0, 10.0]
    cierre = [11.0, 12.5, 12.0, 10.5]
    return apertura, maximo, minimo, cierre


def test_heikin_ashi_a_mano() -> None:
    apertura, maximo, minimo, cierre = _velas()
    ha_open, ha_high, ha_low, ha_close = heikin_ashi(apertura, maximo, minimo, cierre)

    cierres = [(o + h + low + c) / 4 for o, h, low, c in zip(*_velas(), strict=True)]
    assert ha_close == pytest.approx(cierres)
    assert ha_open[0] == pytest.approx((10.0 + 11.0) / 2)
    for i in range(1, 4):
        assert ha_open[i] == pytest.approx((ha_open[i - 1] + ha_close[i - 1]) / 2)
        assert ha_high[i] == pytest.approx(max(maximo[i], ha_open[i], ha_close[i]))
        assert ha_low[i] == pytest.approx(min(minimo[i], ha_open[i], ha_close[i]))


def test_heikin_ashi_vacio_y_una_vela() -> None:
    assert heikin_ashi([], [], [], []) == ([], [], [], [])

    ha_open, ha_high, ha_low, ha_close = heikin_ashi([10.0], [12.0], [9.0], [11.0])
    assert (ha_open[0], ha_close[0]) == pytest.approx((10.5, 10.5))
    assert (ha_high[0], ha_low[0]) == pytest.approx((12.0, 9.0))


def test_heikin_ashi_vela_a_vela() -> None:
    velas = HeikinAshi()
    serie = heikin_ashi(*_velas())
    for i, (o, h, low, c) in enumerate(zip(*_velas(), strict=True)):
        vela = velas.update(o, h, low, c)
        assert (vela.open, vela.high, vela.low, vela.close) == tuple(s[i] for s in serie)


def test_heikin_ashi_no_mira_el_futuro() -> None:
    apertura, maximo, minimo, cierre = _ohlc(200, semilla=11)
    completo = heikin_ashi(apertura, maximo, minimo, cierre)
    for corte in (1, 2, 50, 199):
        truncado = heikin_ashi(apertura[:corte], maximo[:corte], minimo[:corte], cierre[:corte])
        for parcial, entero in zip(truncado, completo, strict=True):
            assert parcial == entero[:corte]


# --- Paridad con la versión vectorizada ------------------------------------------------


def _ohlc(n: int, semilla: int) -> tuple[list[float], list[float], list[float], list[float]]:
    generador = np.random.default_rng(semilla)
    cierre = 2000.0 + np.cumsum(generador.normal(0.0, 1.5, n))
    apertura = np.concatenate(([2000.0], cierre[:-1]))
    maximo = np.maximum(apertura, cierre) + generador.uniform(0.0, 1.0, n)
    minimo = np.minimum(apertura, cierre) - generador.uniform(0.0, 1.0, n)
    return apertura.tolist(), maximo.tolist(), minimo.tolist(), cierre.tolist()


def _ref_rma(valores: np.ndarray, periodo: int) -> np.ndarray:
    serie = pd.Series(valores).ewm(alpha=1.0 / periodo, adjust=False).mean()
    return serie.to_numpy(dtype=float, copy=True)


def _ref_sma(valores: np.ndarray, periodo: int) -> np.ndarray:
    return pd.Series(valores).rolling(periodo).mean().to_numpy(dtype=float, copy=True)


def _ref_ema(valores: np.ndarray, periodo: int) -> np.ndarray:
    serie = pd.Series(valores).ewm(span=periodo, adjust=False).mean()
    resultado = serie.to_numpy(dtype=float, copy=True)
    resultado[: periodo - 1] = np.nan
    return resultado


def _ref_smma(valores: np.ndarray, periodo: int) -> np.ndarray:
    if len(valores) < periodo:
        return np.full(len(valores), np.nan)
    sembrado = valores.copy()
    sembrado[periodo - 1] = valores[:periodo].mean()
    sembrado[: periodo - 1] = np.nan
    return _ref_rma(sembrado, periodo)


def _ref_true_range(maximo: np.ndarray, minimo: np.ndarray, cierre: np.ndarray) -> np.ndarray:
    anterior = np.concatenate(([cierre[0]], cierre[:-1]))
    return cast(
        np.ndarray,
        np.maximum.reduce([maximo - minimo, np.abs(maximo - anterior), np.abs(minimo - anterior)]),
    )


def _ref_atr(maximo: np.ndarray, minimo: np.ndarray, cierre: np.ndarray, periodo: int) -> np.ndarray:
    resultado = _ref_rma(_ref_true_range(maximo, minimo, cierre), periodo)
    resultado[: periodo - 1] = np.nan
    return resultado


def _ref_rsi(cierre: np.ndarray, periodo: int) -> np.ndarray:
    delta = np.diff(cierre, prepend=cierre[0])
    ganancias = _ref_rma(np.where(delta > 0, delta, 0.0), periodo)
    perdidas = _ref_rma(np.where(delta < 0, -delta, 0.0), periodo)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = np.where(perdidas > 0, ganancias / perdidas, np.inf)
    resultado = 100.0 - 100.0 / (1.0 + rs)
    resultado[:periodo] = np.nan
    return resultado


def _ref_rolling(valores: np.ndarray, periodo: int, como: str) -> np.ndarray:
    ventana = pd.Series(valores).rolling(periodo)
    serie = ventana.std(ddof=0) if como == "std" else getattr(ventana, como)()
    return serie.to_numpy(dtype=float, copy=True)


def _igual(nuevo: list[float], referencia: np.ndarray) -> None:
    np.testing.assert_allclose(np.asarray(nuevo), referencia, rtol=1e-9, atol=1e-9, equal_nan=True)


@pytest.mark.parametrize("periodo", [1, 2, 5, 14, 50])
def test_las_medias_dan_lo_mismo_que_la_version_vectorizada(periodo: int) -> None:
    *_, cierre = _ohlc(400, semilla=periodo)
    valores = np.asarray(cierre)
    _igual(sma(cierre, periodo), _ref_sma(valores, periodo))
    _igual(ema(cierre, periodo), _ref_ema(valores, periodo))
    _igual(rma(cierre, periodo), _ref_rma(valores, periodo))
    _igual(smma(cierre, periodo), _ref_smma(valores, periodo))


@pytest.mark.parametrize("periodo", [1, 7, 14, 30])
def test_volatilidad_y_osciladores_dan_lo_mismo_que_la_version_vectorizada(periodo: int) -> None:
    _, maximo, minimo, cierre = _ohlc(400, semilla=100 + periodo)
    h, low, c = np.asarray(maximo), np.asarray(minimo), np.asarray(cierre)
    _igual(true_range(maximo, minimo, cierre), _ref_true_range(h, low, c))
    _igual(atr(maximo, minimo, cierre, periodo), _ref_atr(h, low, c, periodo))
    _igual(rsi(cierre, periodo), _ref_rsi(c, periodo))
    _igual(rolling_max(cierre, periodo), _ref_rolling(c, periodo, "max"))
    _igual(rolling_min(cierre, periodo), _ref_rolling(c, periodo, "min"))
    _igual(rolling_std(cierre, periodo), _ref_rolling(c, periodo, "std"))
    media = _ref_sma(c, periodo)
    ancho = _ref_rolling(c, periodo, "std") * 2.0
    for nuevo, referencia in zip(
        bollinger(cierre, periodo, 2.0), (media + ancho, media, media - ancho), strict=True
    ):
        _igual(nuevo, referencia)


def test_heikin_ashi_da_lo_mismo_que_la_version_vectorizada() -> None:
    apertura, maximo, minimo, cierre = _ohlc(400, semilla=5)
    o, h, low, c = (np.asarray(serie) for serie in (apertura, maximo, minimo, cierre))
    ha_close = (o + h + low + c) / 4.0
    anterior = np.concatenate(([(o[0] + c[0]) / 2.0], ha_close[:-1]))
    ha_open = pd.Series(anterior).ewm(alpha=0.5, adjust=False).mean().to_numpy(dtype=float)
    referencia = (
        ha_open,
        np.maximum.reduce([h, ha_open, ha_close]),
        np.minimum.reduce([low, ha_open, ha_close]),
        ha_close,
    )
    for nuevo, esperado in zip(heikin_ashi(apertura, maximo, minimo, cierre), referencia, strict=True):
        _igual(nuevo, esperado)


def test_rsi_sin_perdidas_es_cien() -> None:
    assert rsi([1.0, 2.0, 3.0, 4.0, 5.0], 2)[2:] == [100.0, 100.0, 100.0]


# --- Casos límite comunes ------------------------------------------------------------------

_SERIES: dict[str, Callable[[list[float]], list[float]]] = {
    "sma": lambda v: sma(v, 5),
    "ema": lambda v: ema(v, 5),
    "rma": lambda v: rma(v, 5),
    "smma": lambda v: smma(v, 5),
    "atr": lambda v: atr(v, v, v, 5),
    "rsi": lambda v: rsi(v, 5),
    "rolling_max": lambda v: rolling_max(v, 5),
    "rolling_min": lambda v: rolling_min(v, 5),
    "rolling_std": lambda v: rolling_std(v, 5),
    "bollinger": lambda v: bollinger(v, 5)[1],
}


@pytest.mark.parametrize("nombre", sorted(_SERIES))
def test_vacio_una_vela_y_ventana_mayor_que_los_datos(nombre: str) -> None:
    calcula = _SERIES[nombre]
    assert calcula([]) == []
    assert len(calcula([2000.0])) == 1
    corta = calcula([2000.0, 2001.0, 2002.0])
    assert len(corta) == 3
    if nombre != "rma":  # la de Wilder da valor desde la primera vela
        assert all(_nan(corta))


@pytest.mark.parametrize("nombre", sorted(_SERIES))
def test_ningun_indicador_mira_el_futuro(nombre: str) -> None:
    """El valor en `t` con los datos truncados en `t` es el mismo que con todo el histórico."""
    calcula = _SERIES[nombre]
    *_, cierre = _ohlc(120, semilla=7)
    completo = calcula(cierre)
    for corte in (1, 4, 5, 6, 50, 119):
        np.testing.assert_array_equal(calcula(cierre[:corte]), completo[:corte])

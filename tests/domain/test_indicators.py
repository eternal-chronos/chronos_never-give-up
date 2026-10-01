"""La SMMA —media móvil suavizada— y las velas Heikin Ashi sobre las que corre el setup 1."""

from __future__ import annotations

import numpy as np
import pytest

from chronos.domain.strategies.indicators import heikin_ashi, smma


def test_arranca_en_la_media_simple_y_suaviza_con_alfa_uno_entre_n() -> None:
    resultado = smma(np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0]), 3)

    assert np.isnan(resultado[:2]).all()
    assert resultado[2] == pytest.approx(2.0)  # (1 + 2 + 3) / 3
    assert resultado[3] == pytest.approx((2.0 * 2 + 4.0) / 3)
    assert resultado[4] == pytest.approx((resultado[3] * 2 + 5.0) / 3)
    assert resultado[5] == pytest.approx((resultado[4] * 2 + 6.0) / 3)


def test_con_periodo_uno_es_el_propio_valor() -> None:
    valores = np.array([3.0, 1.0, 4.0, 1.0, 5.0])
    assert smma(valores, 1) == pytest.approx(valores)


def test_vacio_devuelve_vacio() -> None:
    assert smma(np.array([]), 5).shape == (0,)


def test_una_barra_no_da_valor() -> None:
    resultado = smma(np.array([1.5]), 5)
    assert resultado.shape == (1,)
    assert np.isnan(resultado).all()


def test_ventana_mayor_que_los_datos_no_da_ningun_valor() -> None:
    assert np.isnan(smma(np.arange(4, dtype=float), 5)).all()


def test_justo_el_periodo_da_un_solo_valor_que_es_la_media() -> None:
    resultado = smma(np.array([1.0, 2.0, 3.0, 4.0, 5.0]), 5)
    assert np.isnan(resultado[:4]).all()
    assert resultado[4] == pytest.approx(3.0)


def test_no_mira_el_futuro() -> None:
    """El valor en `t` con los datos truncados en `t` es el mismo que con todo el histórico."""
    valores = np.random.default_rng(7).normal(2000.0, 15.0, 300)
    completo = smma(valores, 5)
    for corte in (1, 4, 5, 6, 50, 299):
        truncado = smma(valores[:corte], 5)
        np.testing.assert_allclose(truncado, completo[:corte], equal_nan=True)


def test_no_modifica_la_entrada_y_devuelve_un_array_escribible() -> None:
    valores = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
    copia = valores.copy()
    resultado = smma(valores, 3)

    np.testing.assert_array_equal(valores, copia)
    resultado[0] = 0.0  # no es una vista de solo lectura


def test_periodo_invalido() -> None:
    with pytest.raises(ValueError, match="periodo"):
        smma(np.array([1.0, 2.0]), 0)


# --- Heikin Ashi -------------------------------------------------------------------


def _velas() -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    apertura = np.array([10.0, 11.0, 12.5, 12.0])
    maximo = np.array([12.0, 13.0, 13.0, 12.5])
    minimo = np.array([9.0, 10.5, 11.0, 10.0])
    cierre = np.array([11.0, 12.5, 12.0, 10.5])
    return apertura, maximo, minimo, cierre


def test_heikin_ashi_a_mano() -> None:
    apertura, maximo, minimo, cierre = _velas()
    ha_open, ha_high, ha_low, ha_close = heikin_ashi(apertura, maximo, minimo, cierre)

    cierres = (apertura + maximo + minimo + cierre) / 4
    assert ha_close == pytest.approx(cierres)
    assert ha_open[0] == pytest.approx((10.0 + 11.0) / 2)
    for i in range(1, 4):
        assert ha_open[i] == pytest.approx((ha_open[i - 1] + ha_close[i - 1]) / 2)
    assert ha_high == pytest.approx(np.maximum.reduce([maximo, ha_open, ha_close]))
    assert ha_low == pytest.approx(np.minimum.reduce([minimo, ha_open, ha_close]))


def test_heikin_ashi_vacio_y_una_vela() -> None:
    assert all(serie.shape == (0,) for serie in heikin_ashi(*(np.array([]),) * 4))

    ha_open, ha_high, ha_low, ha_close = heikin_ashi(
        np.array([10.0]), np.array([12.0]), np.array([9.0]), np.array([11.0])
    )
    assert (ha_open[0], ha_close[0]) == pytest.approx((10.5, 10.5))
    assert (ha_high[0], ha_low[0]) == pytest.approx((12.0, 9.0))


def test_heikin_ashi_no_mira_el_futuro() -> None:
    generador = np.random.default_rng(11)
    cierre = 2000.0 + np.cumsum(generador.normal(0.0, 1.0, 200))
    apertura = np.concatenate(([2000.0], cierre[:-1]))
    maximo = np.maximum(apertura, cierre) + generador.uniform(0.0, 1.0, 200)
    minimo = np.minimum(apertura, cierre) - generador.uniform(0.0, 1.0, 200)
    completo = heikin_ashi(apertura, maximo, minimo, cierre)
    for corte in (1, 2, 50, 199):
        truncado = heikin_ashi(apertura[:corte], maximo[:corte], minimo[:corte], cierre[:corte])
        for parcial, entero in zip(truncado, completo, strict=True):
            np.testing.assert_allclose(parcial, entero[:corte])

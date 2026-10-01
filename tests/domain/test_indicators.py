"""La SMMA: media móvil suavizada, la primera capa calculada del explorador."""

from __future__ import annotations

import numpy as np
import pytest

from chronos.domain.strategies.indicators import smma


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

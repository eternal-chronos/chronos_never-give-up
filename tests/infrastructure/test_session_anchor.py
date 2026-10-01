"""Corte diario anclado a la sesión de una plaza, con horario de verano real.

El gráfico no corta a una hora fija de UTC sino cuando abre Nueva York, así que
el corte se mueve solo dos veces al año. Eso es lo que se fija aquí: 17:00 de
Nueva York son las 22:00 UTC en invierno y las 21:00 en verano; las 18:00 son
las 23:00 y las 22:00. La rejilla en uso es la de las 17:00, que es la de
cTrader/Pepperstone —donde se ejecuta— y no la de TradingView.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from chronos.application.chart.config import (
    DAILY,
    H1,
    H4,
    M5,
    M15,
    AggregationConfig,
)
from chronos.domain.errors import DomainError
from chronos.infrastructure.market.aggregation import (
    aggregate,
    minutes_covered,
    session_buckets,
)

#: Cambios de hora de Estados Unidos en 2025: segundo domingo de marzo y primero
#: de noviembre. Son los dos días en que la sesión no dura 24 horas.
SPRING_FORWARD = "2025-03-09"
FALL_BACK = "2025-11-02"


def m1(start: str, end: str) -> pd.DataFrame:
    """Histórico M1 continuo, sin fines de semana: aquí sólo se mide la rejilla."""
    index = pd.date_range(start, end, freq="1min", tz="UTC", inclusive="left")
    price = 2000.0 + np.arange(len(index)) * 0.01
    return pd.DataFrame(
        {
            "open": price,
            "high": price + 0.5,
            "low": price - 0.5,
            "close": price + 0.1,
            "volume": 1.0,
        },
        index=index,
    )


def labels_of(frame: pd.DataFrame) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(frame.index)


# --- El corte se mueve con el horario de verano -----------------------------


@pytest.mark.parametrize(
    ("session", "winter_hour", "summer_hour"),
    [("NY_17:00", 22, 21), ("NY_18:00", 23, 22)],
)
def test_el_corte_cae_donde_lo_pone_el_horario_de_nueva_york(
    session: str, winter_hour: int, summer_hour: int
) -> None:
    config = AggregationConfig(d_session_start=session)

    invierno = aggregate(m1("2025-01-10", "2025-01-20"), DAILY, config).frame
    verano = aggregate(m1("2025-07-10", "2025-07-20"), DAILY, config).frame

    assert set(labels_of(invierno).hour) == {winter_hour}
    assert set(labels_of(verano).hour) == {summer_hour}
    # Y no es que se haya elegido una hora fija: son distintas entre estaciones.
    assert winter_hour != summer_hour


@pytest.mark.parametrize("session", ["NY_17:00", "NY_18:00"])
def test_el_dia_dura_24_horas_salvo_en_los_dos_cambios_de_hora(session: str) -> None:
    config = AggregationConfig(d_session_start=session)
    index = pd.DatetimeIndex(
        aggregate(m1("2025-03-01", "2025-11-10"), DAILY, config).frame.index
    )

    lengths = index.to_series().diff().dropna()
    counts = lengths.value_counts()

    assert counts[pd.Timedelta(hours=24)] == len(lengths) - 2
    assert counts[pd.Timedelta(hours=23)] == 1  # marzo: la sesión se acorta
    assert counts[pd.Timedelta(hours=25)] == 1  # noviembre: se alarga


@pytest.mark.parametrize("session", ["NY_17:00", "NY_18:00"])
def test_la_sesion_de_25_horas_es_una_sola_vela(session: str) -> None:
    """La hora que se repite en noviembre no puede abrir una vela diaria propia."""
    config = AggregationConfig(d_session_start=session)
    frame = aggregate(m1("2025-10-30", "2025-11-05"), DAILY, config).frame

    minutes = minutes_covered(m1("2025-10-30", "2025-11-05"), DAILY, config, labels_of(frame))
    largest = int(minutes.max())

    assert largest == 25 * 60
    assert (minutes == 25 * 60).sum() == 1


def test_cada_barra_cae_en_la_sesion_que_la_contiene() -> None:
    """Etiqueta <= barra < siguiente apertura, también los días del cambio."""
    config = AggregationConfig(d_session_start="NY_18:00")
    frame = m1("2025-10-30", "2025-11-05")
    index = pd.DatetimeIndex(frame.index)

    starts, ends = session_buckets(index, DAILY, config.session_anchor)

    assert (starts <= index).all()
    assert (index < ends).all()


# --- H4 con el mismo origen de sesión ---------------------------------------


def test_h4_arranca_con_la_sesion_y_avanza_de_cuatro_en_cuatro() -> None:
    config = AggregationConfig(d_session_start="NY_18:00")
    frame = m1("2025-01-10", "2025-01-20")
    diario = aggregate(frame, DAILY, config).frame
    cuatro = aggregate(frame, H4, config).frame

    # Toda apertura de sesión es también apertura de una vela H4.
    assert set(diario.index) <= set(cuatro.index)
    # Y dentro de la sesión las velas van cada cuatro horas exactas.
    steps = pd.DatetimeIndex(cuatro.index).to_series().diff().dropna().unique()
    assert list(steps) == [pd.Timedelta(hours=4)]
    assert len(cuatro) == len(diario) * 6


#: Rejilla H4 de **cTrader / Pepperstone**, la plataforma con la que se opera en
#: vivo. Comprobada vela a vela contra el M1 de Dukascopy: la vela `02:00` del
#: 16-01-2023 en UTC-4 es 06:00-10:00 UTC, o sea las 01:00 de Nueva York.
CTRADER_H4_GRID = {17, 21, 1, 5, 9, 13}

#: La de TradingView, que corta una hora después porque arma las velas con la
#: sesión del símbolo y no con la hora del servidor del broker. Se deja escrita
#: para que se vea que la diferencia es esa y no otra.
TRADINGVIEW_H4_GRID = {18, 22, 2, 6, 10, 14}


@pytest.mark.parametrize(
    ("session", "matches"), [("NY_17:00", True), ("NY_18:00", False)]
)
def test_solo_el_ancla_de_las_17_reproduce_la_rejilla_de_ctrader(
    session: str, matches: bool
) -> None:
    """Las velas H4 de cTrader abren a las 17, 21, 01, 05, 09 y 13 de Nueva York.

    Se comprueba en invierno y en verano a la vez: una rejilla fija en UTC puede
    acertar media year y fallar la otra, y eso es justo lo que hay que descartar.
    """
    frame = pd.concat(
        [m1("2025-01-10", "2025-01-20"), m1("2025-07-10", "2025-07-20")]
    )
    cuatro = aggregate(frame, H4, AggregationConfig(d_session_start=session)).frame

    hours = set(labels_of(cuatro).tz_convert("America/New_York").hour)

    assert (hours == CTRADER_H4_GRID) is matches
    assert (hours == TRADINGVIEW_H4_GRID) is not matches


@pytest.mark.parametrize("offset", [0, 1, 2, 3])
def test_ninguna_rejilla_fija_en_utc_reproduce_la_de_ctrader(offset: int) -> None:
    """Un offset fijo se desplaza una hora al cambiar la hora: nunca son seis horas."""
    frame = pd.concat(
        [m1("2025-01-10", "2025-01-20"), m1("2025-07-10", "2025-07-20")]
    )
    cuatro = aggregate(
        frame, H4, AggregationConfig(h4_offset_hours=offset, d_session_start="00:00")
    ).frame

    hours = set(labels_of(cuatro).tz_convert("America/New_York").hour)

    assert hours != CTRADER_H4_GRID
    assert len(hours) == 12  # seis en invierno y otras seis en verano


def test_h4_ignora_el_offset_fijo_cuando_hay_ancla_de_sesion() -> None:
    """Con ancla, H4 arranca con la sesión: `h4_offset_hours` deja de pintar."""
    frame = m1("2025-01-10", "2025-01-20")
    sin_offset = aggregate(frame, H4, AggregationConfig(d_session_start="NY_18:00")).frame
    con_offset = aggregate(
        frame, H4, AggregationConfig(d_session_start="NY_18:00", h4_offset_hours=3)
    ).frame

    pd.testing.assert_frame_equal(sin_offset, con_offset)


def test_el_ultimo_trozo_de_la_sesion_larga_no_llega_a_cuatro_horas() -> None:
    """25 horas no son seis velas de cuatro: la última cubre una."""
    config = AggregationConfig(d_session_start="NY_18:00")
    frame = m1("2025-10-30", "2025-11-05")
    cuatro = aggregate(frame, H4, config).frame

    minutes = minutes_covered(frame, H4, config, labels_of(cuatro))

    assert int(minutes.max()) == 240
    assert int(minutes.min()) == 60
    assert (minutes == 60).sum() == 1


# --- El ancla no toca lo que no le corresponde ------------------------------


@pytest.mark.parametrize("timeframe", [H1, M15, M5])
def test_las_temporalidades_menores_no_se_anclan_a_la_sesion(timeframe: str) -> None:
    """M5, M15 y H1 tienen la misma rejilla en todas las plataformas."""
    frame = m1("2025-01-10", "2025-01-14")
    fija = aggregate(frame, timeframe, AggregationConfig()).frame
    anclada = aggregate(frame, timeframe, AggregationConfig(d_session_start="NY_18:00")).frame

    pd.testing.assert_frame_equal(fija, anclada)


def test_los_minutos_contados_cuadran_con_las_velas() -> None:
    config = AggregationConfig(d_session_start="NY_17:00")
    frame = m1("2025-01-10", "2025-01-20")
    diario = aggregate(frame, DAILY, config).frame

    minutes = minutes_covered(frame, DAILY, config, labels_of(diario))

    assert minutes.sum() == len(frame) - _tail(frame, diario)


def _tail(frame: pd.DataFrame, aggregated: pd.DataFrame) -> int:
    """Minutos que quedaron fuera por caer en la vela incompleta descartada."""
    last = pd.DatetimeIndex(aggregated.index)[-1] + pd.Timedelta(hours=24)
    return int((pd.DatetimeIndex(frame.index) >= last).sum())


# --- Configuración ----------------------------------------------------------


def test_una_plaza_desconocida_es_un_error_de_configuracion() -> None:
    with pytest.raises(DomainError, match="Plaza desconocida"):
        AggregationConfig(d_session_start="XX_18:00")


def test_el_ancla_se_describe_con_su_zona_y_no_como_utc() -> None:
    fija = AggregationConfig(d_session_start="22:00")
    anclada = AggregationConfig(d_session_start="NY_18:00")

    assert fija.session_anchor is None
    assert fija.describe_daily_start() == "22:00 UTC"
    assert anclada.session_anchor is not None
    assert anclada.session_anchor.timezone == "America/New_York"
    assert "America/New_York" in anclada.describe_daily_start()


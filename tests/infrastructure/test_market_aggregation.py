"""Agregación M1 → M5 / M15 / H1 / H4 / Diario y carga de bid y ask.

Las velas dependen íntegramente del offset, así que lo primero que
hay que fijar con un test es que el offset cambia las velas de verdad y que no
hay nada incrustado en el código.
"""

from __future__ import annotations

import pandas as pd
import pytest

from chronos.application.chart.config import (
    DAILY,
    H1,
    H4,
    M5,
    M15,
    AggregationConfig,
    HistoryConfig,
)
from chronos.domain.errors import DomainError
from chronos.infrastructure.market.aggregation import aggregate, aggregate_all
from chronos.infrastructure.market.loader import load_history
from tests.conftest import make_m1_history

#: Rejilla fija en UTC. La configuración del proyecto ancla el día —y con él H4—
#: a la sesión de Nueva York, así que los tests que miden el desplazamiento de la
#: rejilla UTC tienen que pedirla: con el ancla puesta, `h4_offset_hours` se
#: ignora a propósito.
REJILLA_UTC = "00:00"


@pytest.fixture
def history() -> pd.DataFrame:
    return make_m1_history(weeks=2)


# --- Offsets ----------------------------------------------------------------


def test_el_offset_h4_desplaza_el_inicio_de_las_velas(history: pd.DataFrame) -> None:
    sin_offset = aggregate(history, H4, AggregationConfig(h4_offset_hours=0, d_session_start=REJILLA_UTC)).frame
    con_offset = aggregate(history, H4, AggregationConfig(h4_offset_hours=1, d_session_start=REJILLA_UTC)).frame

    assert set(pd.DatetimeIndex(sin_offset.index).hour) <= {0, 4, 8, 12, 16, 20}
    assert set(pd.DatetimeIndex(con_offset.index).hour) <= {1, 5, 9, 13, 17, 21}


def test_el_offset_h4_cambia_los_precios_no_solo_las_etiquetas(
    history: pd.DataFrame,
) -> None:
    sin_offset = aggregate(history, H4, AggregationConfig(h4_offset_hours=0, d_session_start=REJILLA_UTC)).frame
    con_offset = aggregate(history, H4, AggregationConfig(h4_offset_hours=2, d_session_start=REJILLA_UTC)).frame
    comunes = sin_offset.index.intersection(con_offset.index)
    # Ninguna vela coincide: las dos rejillas cortan el histórico en sitios
    # distintos, así que ni siquiera comparten marcas de tiempo.
    assert comunes.empty
    assert not sin_offset["close"].reset_index(drop=True).equals(
        con_offset["close"].reset_index(drop=True)
    )


def test_el_offset_h4_es_ciclico_cada_cuatro_horas(history: pd.DataFrame) -> None:
    """Un offset de 5 h produce exactamente las mismas velas que uno de 1 h."""
    una = aggregate(history, H4, AggregationConfig(h4_offset_hours=1, d_session_start=REJILLA_UTC)).frame
    cinco = aggregate(history, H4, AggregationConfig(h4_offset_hours=5, d_session_start=REJILLA_UTC)).frame
    pd.testing.assert_frame_equal(una, cinco)


def test_el_inicio_de_sesion_desplaza_la_vela_diaria(history: pd.DataFrame) -> None:
    medianoche = aggregate(history, DAILY, AggregationConfig(d_session_start="00:00")).frame
    sesion = aggregate(history, DAILY, AggregationConfig(d_session_start="22:00")).frame

    assert set(pd.DatetimeIndex(medianoche.index).hour) == {0}
    assert set(pd.DatetimeIndex(sesion.index).hour) == {22}
    assert medianoche.index.intersection(sesion.index).empty


def test_hora_de_sesion_mal_escrita_es_un_error() -> None:
    with pytest.raises(DomainError, match="HH:MM"):
        AggregationConfig(d_session_start="22h")


# --- Semántica de la agregación ---------------------------------------------


def test_open_high_low_close_de_una_vela_agregada() -> None:
    index = pd.date_range("2024-03-04 00:00", periods=240, freq="1min", tz="UTC")
    frame = pd.DataFrame(
        {
            "open": 2000.0,
            "high": 2001.0,
            "low": 1999.0,
            "close": 2000.5,
            "volume": 1.0,
        },
        index=index,
    )
    frame.iloc[0, frame.columns.get_loc("open")] = 1995.0  # open de la primera M1
    frame.iloc[100, frame.columns.get_loc("high")] = 2010.0  # máximo de los high
    frame.iloc[150, frame.columns.get_loc("low")] = 1980.0  # mínimo de los low
    frame.iloc[-1, frame.columns.get_loc("close")] = 2005.0  # close de la última M1

    vela = aggregate(frame, H4, AggregationConfig(d_session_start=REJILLA_UTC)).frame
    assert len(vela) == 1
    assert vela.iloc[0]["open"] == pytest.approx(1995.0)
    assert vela.iloc[0]["high"] == pytest.approx(2010.0)
    assert vela.iloc[0]["low"] == pytest.approx(1980.0)
    assert vela.iloc[0]["close"] == pytest.approx(2005.0)


def test_la_barra_incompleta_del_final_se_descarta() -> None:
    """Media vela H4 al final del histórico no es una vela H4."""
    index = pd.date_range("2024-03-04 00:00", periods=300, freq="1min", tz="UTC")
    frame = pd.DataFrame(
        {"open": 2000.0, "high": 2001.0, "low": 1999.0, "close": 2000.0, "volume": 1.0},
        index=index,
    )
    resultado = aggregate(frame, H4, AggregationConfig(d_session_start=REJILLA_UTC))
    assert len(resultado.frame) == 1  # 00:00-04:00 completa; 04:00-08:00 no
    assert resultado.dropped_incomplete == pd.Timestamp("2024-03-04 04:00", tz="UTC")


def test_una_vela_exactamente_completa_no_se_descarta() -> None:
    index = pd.date_range("2024-03-04 00:00", periods=240, freq="1min", tz="UTC")
    frame = pd.DataFrame(
        {"open": 2000.0, "high": 2001.0, "low": 1999.0, "close": 2000.0, "volume": 1.0},
        index=index,
    )
    resultado = aggregate(frame, H4, AggregationConfig(d_session_start=REJILLA_UTC))
    assert len(resultado.frame) == 1
    assert resultado.dropped_incomplete is None


def test_h4_desde_m1_y_desde_h1_son_la_misma_vela(history: pd.DataFrame) -> None:
    """Justifica poder bajar H1 en vez de M1: treinta veces menos peticiones.

    Los límites de una vela H4 caen siempre en horas en punto, así que agregar
    cuatro velas H1 y agregar doscientos cuarenta minutos tienen que dar
    exactamente lo mismo. Lo mismo vale para la vela diaria mientras el inicio
    de sesión sea una hora entera.
    """
    hourly = history.resample("1h", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna(subset=["open"])

    for timeframe, config in (
        (H4, AggregationConfig(d_session_start=REJILLA_UTC)),
        (H4, AggregationConfig(h4_offset_hours=2, d_session_start=REJILLA_UTC)),
        (DAILY, AggregationConfig(d_session_start="22:00")),
    ):
        desde_m1 = aggregate(history, timeframe, config).frame
        desde_h1 = aggregate(hourly, timeframe, config).frame
        comunes = desde_m1.index.intersection(desde_h1.index)
        assert len(comunes) > 5
        pd.testing.assert_frame_equal(
            desde_m1.loc[comunes, ["open", "high", "low", "close"]],
            desde_h1.loc[comunes, ["open", "high", "low", "close"]],
        )


def test_la_barra_final_no_se_descarta_por_asumir_paso_de_un_minuto() -> None:
    """Con entrada H1, la última barra cubre una hora, no un minuto."""
    index = pd.date_range("2024-03-04 00:00", periods=4, freq="1h", tz="UTC")
    hourly = pd.DataFrame(
        {"open": 2000.0, "high": 2001.0, "low": 1999.0, "close": 2000.0, "volume": 1.0},
        index=index,
    )
    resultado = aggregate(hourly, H4, AggregationConfig(d_session_start=REJILLA_UTC))
    assert resultado.dropped_incomplete is None
    assert len(resultado.frame) == 1


def test_las_cinco_temporalidades_del_explorador(history: pd.DataFrame) -> None:
    series = aggregate_all(
        history, AggregationConfig(d_session_start=REJILLA_UTC), (M5, M15, H1, H4, DAILY)
    )
    assert list(series) == [DAILY, H4, H1, M15, M5]  # de mayor a menor
    tamaños = [len(series[timeframe].frame) for timeframe in (DAILY, H4, H1, M15, M5)]
    assert tamaños == sorted(tamaños)


def test_m5_m15_y_h1_no_llevan_desplazamiento(history: pd.DataFrame) -> None:
    """Su rejilla no es ambigua: cinco minutos, cuartos de hora y horas en punto."""
    config = AggregationConfig(h4_offset_hours=3, d_session_start="22:00")
    m5 = aggregate(history, M5, config).frame
    m15 = aggregate(history, M15, config).frame
    h1 = aggregate(history, H1, config).frame

    assert set(pd.DatetimeIndex(h1.index).minute) == {0}
    assert set(pd.DatetimeIndex(m15.index).minute) <= {0, 15, 30, 45}
    assert set(pd.DatetimeIndex(m5.index).minute) <= set(range(0, 60, 5))


def test_m5_es_la_vela_de_cinco_minutos_del_m1(history: pd.DataFrame) -> None:
    """Cada vela M5 es exactamente el resumen de sus cinco minutos: abre con el
    primero, cierra con el último y cubre el máximo y el mínimo de los cinco."""
    m5 = aggregate(history, M5, AggregationConfig(d_session_start=REJILLA_UTC)).frame
    label = pd.Timestamp(m5.index[10])
    minutos = history.loc[label : label + pd.Timedelta(minutes=4)]

    assert 1 <= len(minutos) <= 5
    assert m5.loc[label, "open"] == minutos["open"].iloc[0]
    assert m5.loc[label, "close"] == minutos["close"].iloc[-1]
    assert m5.loc[label, "high"] == minutos["high"].max()
    assert m5.loc[label, "low"] == minutos["low"].min()


def test_temporalidad_no_soportada() -> None:
    frame = make_m1_history(weeks=1)
    with pytest.raises(DomainError, match="no soportada"):
        aggregate(frame, "M3", AggregationConfig(d_session_start=REJILLA_UTC))


def test_no_se_puede_bajar_de_temporalidad(history: pd.DataFrame) -> None:
    """Pedir M15 a un histórico H1 —o M5 a uno M15— daría velas falsas sin avisar."""
    config = AggregationConfig(d_session_start=REJILLA_UTC)
    hourly = aggregate(history, H1, config).frame
    with pytest.raises(DomainError, match="hace falta un histórico más fino"):
        aggregate(hourly, M15, config)
    quarter = aggregate(history, M15, config).frame
    with pytest.raises(DomainError, match="hace falta un histórico más fino"):
        aggregate(quarter, M5, config)


# --- Carga de bid y ask -----------------------------------------------------


@pytest.fixture
def sides(tmp_path: object) -> tuple[str, str]:
    bid = make_m1_history(weeks=1)
    ask = bid.copy()
    for column in ("open", "high", "low", "close"):
        ask[column] = ask[column] + 0.30  # horquilla constante de 30 céntimos
    bid_path = f"{tmp_path}/bid.parquet"
    ask_path = f"{tmp_path}/ask.parquet"
    bid.to_parquet(bid_path)
    ask.to_parquet(ask_path)
    return bid_path, ask_path


def test_el_lado_elegido_es_el_que_se_carga(sides: tuple[str, str]) -> None:
    bid_path, ask_path = sides
    config = HistoryConfig(bid_path=bid_path, ask_path=ask_path)

    bid = load_history(config, "bid")
    ask = load_history(config, "ask")
    mid = load_history(config, "mid")

    assert ask.frame["close"].iloc[0] == pytest.approx(bid.frame["close"].iloc[0] + 0.30)
    assert mid.frame["close"].iloc[0] == pytest.approx(bid.frame["close"].iloc[0] + 0.15)
    assert bid.has_ask and mid.side == "mid"


def test_pedir_ask_sin_fichero_de_ask_es_un_error(sides: tuple[str, str]) -> None:
    bid_path, _ = sides
    config = HistoryConfig(bid_path=bid_path)
    with pytest.raises(DomainError, match="no trae lado ask"):
        load_history(config, "ask")


def test_fichero_unico_con_columnas_bid_y_ask(tmp_path: object) -> None:
    base = make_m1_history(weeks=1)
    combined = pd.DataFrame(index=base.index)
    for column in ("open", "high", "low", "close"):
        combined[f"bid_{column}"] = base[column]
        combined[f"ask_{column}"] = base[column] + 0.40
    path = f"{tmp_path}/xauusd.parquet"
    combined.to_parquet(path)

    config = HistoryConfig(path=path)
    bid = load_history(config, "bid")
    ask = load_history(config, "ask")
    assert bid.has_ask
    assert ask.frame["close"].iloc[0] == pytest.approx(bid.frame["close"].iloc[0] + 0.40)
    assert "bid_* / ask_*" in bid.provenance


def test_ohlc_unico_se_declara_como_asumido(tmp_path: object) -> None:
    """Si el fichero no distingue lados, el informe tiene que decirlo."""
    path = f"{tmp_path}/plano.parquet"
    make_m1_history(weeks=1).to_parquet(path)
    history = load_history(HistoryConfig(path=path), "bid")
    assert history.has_ask is False
    assert "se asume" in history.provenance


def test_sin_ruta_declarada_no_hay_carga() -> None:
    with pytest.raises(DomainError, match="`path` o `bid_path`"):
        load_history(HistoryConfig(), "bid")

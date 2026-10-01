"""Verificación empírica de zona horaria.

Es la comprobación que evita el fallo más caro del proyecto: un offset horario
equivocado no lanza ningún error, sólo desplaza las velas H4 y diarias y produce
un gráfico distinto al que el propietario ve en su pantalla.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from chronos.application.chart.config import TimezoneAuditConfig
from chronos.application.chart.timezone_audit import audit_timezone
from tests.conftest import make_m1_history


def test_historico_en_utc_pasa_la_verificacion() -> None:
    audit = audit_timezone(make_m1_history(), TimezoneAuditConfig())
    assert audit.ok
    assert audit.problems == ()


def test_el_hueco_semanal_cae_viernes_domingo() -> None:
    audit = audit_timezone(make_m1_history(), TimezoneAuditConfig())
    assert audit.gaps_found > 0
    assert audit.gaps_starting_friday == audit.gaps_found
    assert audit.gaps_ending_sunday == audit.gaps_found
    for gap in audit.sample_gaps:
        assert gap.last_before.weekday() == 4
        assert gap.first_after.weekday() == 6
        assert gap.hours > 12


def test_el_pico_de_volatilidad_esta_en_las_13_30_utc() -> None:
    audit = audit_timezone(make_m1_history(), TimezoneAuditConfig())
    assert audit.peak_minute_utc == "13:30"
    assert audit.peak_offset_minutes == 0
    assert len(audit.minute_profile) == 1440


def test_un_historico_desplazado_falla_por_las_dos_vias() -> None:
    """Cinco horas de desfase mueven el hueco y el pico a la vez."""
    audit = audit_timezone(make_m1_history(shift_hours=5), TimezoneAuditConfig())
    assert not audit.ok
    assert len(audit.problems) == 3  # inicios, finales y pico
    assert audit.peak_minute_utc == "18:30"
    assert audit.peak_offset_minutes == 300
    assert audit.gaps_starting_friday == 0


def test_un_desfase_pequeno_dentro_de_tolerancia_no_falla_por_el_pico() -> None:
    audit = audit_timezone(
        make_m1_history(shift_hours=0.25), TimezoneAuditConfig(tolerance_minutes=30)
    )
    assert audit.peak_minute_utc == "13:45"
    assert all("pico de volatilidad" not in problem for problem in audit.problems)


def test_la_distancia_al_pico_es_circular() -> None:
    """23:50 y 00:05 distan 15 minutos, no 1.425."""
    audit = audit_timezone(
        make_m1_history(shift_hours=10.5),
        TimezoneAuditConfig(expected_peak_utc="00:00", tolerance_minutes=5),
    )
    assert audit.peak_minute_utc == "00:00"
    assert audit.peak_offset_minutes == 0


def test_con_barras_horarias_la_tolerancia_se_amplia() -> None:
    """Un histórico H1 no puede poner el pico en el minuto 30: no sería justo exigirlo."""
    hourly = (
        make_m1_history()
        .resample("1h", label="left", closed="left")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna(subset=["open"])
    )
    audit = audit_timezone(hourly, TimezoneAuditConfig(tolerance_minutes=10))

    assert audit.resolution_minutes == 60
    assert audit.effective_tolerance_minutes == 30  # medio paso
    assert audit.peak_minute_utc == "13:00"
    assert audit.ok


def test_con_barras_de_un_minuto_la_tolerancia_es_la_configurada() -> None:
    audit = audit_timezone(make_m1_history(), TimezoneAuditConfig(tolerance_minutes=10))
    assert audit.resolution_minutes == 1
    assert audit.effective_tolerance_minutes == 10


def test_historico_sin_fines_de_semana_se_denuncia() -> None:
    index = pd.date_range("2024-03-04", periods=2_000, freq="1min", tz="UTC")
    frame = pd.DataFrame(
        {"open": 2000.0, "high": 2001.0, "low": 1999.0, "close": 2000.0, "volume": 1.0},
        index=index,
    )
    audit = audit_timezone(frame, TimezoneAuditConfig())
    assert not audit.ok
    assert any("no parece contener fines de semana" in problem for problem in audit.problems)


def test_historico_naif_no_se_puede_auditar() -> None:
    index = pd.date_range("2024-03-04", periods=100, freq="1min")
    frame = pd.DataFrame(
        {"open": 2000.0, "high": 2001.0, "low": 1999.0, "close": 2000.0}, index=index
    )
    with pytest.raises(ValueError, match="tz-aware"):
        audit_timezone(frame, TimezoneAuditConfig())


# --- A.1, A.2 y A.3 -----------------------------------------------------------


def _con_horario_de_verano(weeks: int = 40) -> pd.DataFrame:
    """Histórico cuyo pico sigue a Nueva York: 13:30 UTC en invierno, 12:30 en verano.

    Es lo que hace el oro de verdad, y lo que A.3 tiene que reconocer.
    """
    frame = make_m1_history(weeks=weeks, start="2024-01-07 22:00")
    index = pd.DatetimeIndex(frame.index)
    local = index.tz_convert("America/New_York")
    en_verano = np.asarray(
        (local.tz_localize(None) - index.tz_localize(None)) == pd.Timedelta(hours=-4)
    )

    minuto = (index.hour * 60 + index.minute).to_numpy()
    esperado = np.where(en_verano, 12 * 60 + 30, 13 * 60 + 30)
    distancia = np.minimum(np.abs(minuto - esperado), 1440 - np.abs(minuto - esperado))
    # Campana más marcada que la del fixture base: así el minuto exacto gana al
    # ruido del paseo aleatorio y el test comprueba el minuto, no la vecindad.
    campana = 0.05 * (1.0 + 40.0 * np.exp(-((distancia / 15.0) ** 2)))

    ajustado = frame.copy()
    cuerpo_alto = np.maximum(frame["open"].to_numpy(), frame["close"].to_numpy())
    cuerpo_bajo = np.minimum(frame["open"].to_numpy(), frame["close"].to_numpy())
    ajustado["high"] = cuerpo_alto + campana
    ajustado["low"] = cuerpo_bajo - campana
    return ajustado


def test_se_enseña_un_fin_de_semana_por_año() -> None:
    audit = audit_timezone(make_m1_history(weeks=40), TimezoneAuditConfig())
    assert audit.yearly_gaps
    assert len(audit.yearly_gaps) <= 10
    for gap in audit.yearly_gaps:
        assert gap.last_before.weekday() == 4
        assert gap.first_after.weekday() == 6


def test_el_perfil_por_hora_tiene_veinticuatro_valores() -> None:
    audit = audit_timezone(make_m1_history(), TimezoneAuditConfig())
    assert len(audit.hour_profile) == 24
    assert audit.peak_hour_utc == "13:00"


def test_el_pico_de_verano_se_admite_igual_que_el_de_invierno() -> None:
    """El agregado del año cae en el régimen con más meses: no es un fallo."""
    audit = audit_timezone(_con_horario_de_verano(), TimezoneAuditConfig())
    assert audit.ok
    assert audit.peak_minute_utc == "12:30"
    assert audit.peak_offset_minutes == 0


def test_el_desglose_por_regimen_encuentra_el_desplazamiento() -> None:
    audit = audit_timezone(_con_horario_de_verano(), TimezoneAuditConfig())
    entero = audit.whole_history_seasons

    assert entero is not None
    assert entero.daylight is not None and entero.standard is not None
    assert entero.daylight.peak_minute_utc == "12:30"
    assert entero.standard.peak_minute_utc == "13:30"
    assert audit.dst_shift_minutes == 60
    assert audit.dst_shift_ok is True


def test_un_pico_que_no_se_mueve_se_denuncia_sin_bloquear() -> None:
    """El fixture base pone el pico a las 13:30 todo el año: eso no es UTC real."""
    audit = audit_timezone(make_m1_history(weeks=40), TimezoneAuditConfig())

    assert audit.ok, "A.3 es diagnóstico: no puede detener nada por sí sola"
    assert audit.dst_shift_ok is False
    assert audit.dst_shift_minutes == 0


def test_el_puesto_del_minuto_esperado_distingue_un_segundo_de_un_ausente() -> None:
    audit = audit_timezone(_con_horario_de_verano(), TimezoneAuditConfig())
    verano = audit.whole_history_seasons.daylight
    assert verano.expected_rank == 1
    assert verano.expected_mean_range == pytest.approx(verano.peak_mean_range)


def test_sin_los_dos_regimenes_no_hay_desplazamiento_que_medir() -> None:
    """Ocho semanas de enero: todo invierno, nada que comparar."""
    audit = audit_timezone(make_m1_history(weeks=8), TimezoneAuditConfig())
    assert audit.dst_shift_ok is None
    assert audit.dst_shift_minutes is None


def test_los_años_se_desglosan_por_separado() -> None:
    audit = audit_timezone(_con_horario_de_verano(weeks=60), TimezoneAuditConfig())
    años = [season.year for season in audit.seasons]
    assert años == sorted(set(años))
    assert all(season.whole is not None for season in audit.seasons)

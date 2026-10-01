"""La configuración del explorador y su CLI: `chronos chart verify-tz | info | explorer`.

Lo que se fija aquí es lo que no puede romperse en silencio: que el YAML del
proyecto carga con la rejilla de cTrader, que una errata es un error con nombre
y que con la zona horaria mal no se dibuja nada salvo que se pida a sabiendas.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
import yaml
from typer.testing import CliRunner

from chronos.application.chart.config import DAILY, H1, H4, M5, M15
from chronos.domain.errors import DomainError
from chronos.infrastructure.config.loader import ConfigError, load_explorer_config
from chronos.interface.cli import app
from tests.conftest import make_m1_history

ROOT = Path(__file__).resolve().parents[2]

runner = CliRunner()


# --- Configuración ----------------------------------------------------------


def test_el_yaml_del_proyecto_carga_con_la_rejilla_de_ctrader() -> None:
    config = load_explorer_config(ROOT / "config" / "explorer.yaml")

    assert config.symbol == "XAUUSD"
    assert config.price_side == "bid"
    assert config.ordered_timeframes == (DAILY, H4, H1, M15, M5)
    assert config.aggregation.d_session_start == "NY_17:00"
    assert config.reporting.output_dir == "now/explorador"
    assert config.reporting.session_timezone == "Etc/GMT+4"
    assert config.marks.rects == ("Zona 1", "Zona 2", "Zona 3")
    assert config.timezone_audit.enabled


def test_una_clave_desconocida_no_pasa_en_silencio(tmp_path: Path) -> None:
    ruta = tmp_path / "malo.yaml"
    ruta.write_text(yaml.safe_dump({"aggregation": {"d_sesion_start": "NY_17:00"}}), encoding="utf-8")
    with pytest.raises(ConfigError, match="d_sesion_start"):
        load_explorer_config(ruta)


def test_un_lado_inexistente_es_un_error_de_configuracion(tmp_path: Path) -> None:
    ruta = tmp_path / "malo.yaml"
    ruta.write_text(yaml.safe_dump({"price_side": "last"}), encoding="utf-8")
    with pytest.raises(ConfigError, match="price_side"):
        load_explorer_config(ruta)


def test_una_temporalidad_que_no_se_sabe_construir_se_rechaza(tmp_path: Path) -> None:
    ruta = tmp_path / "malo.yaml"
    ruta.write_text(yaml.safe_dump({"timeframes": ["D", "M3"]}), encoding="utf-8")
    with pytest.raises(DomainError, match="no soportada"):
        load_explorer_config(ruta)


# --- CLI --------------------------------------------------------------------


def _write_run(root: Path, history: pd.DataFrame) -> Path:
    data_path = root / "bid.parquet"
    history.to_parquet(data_path)
    config = {
        "symbol": "XAUUSD",
        "price_side": "bid",
        "data": {"bid_path": str(data_path)},
        "reporting": {"output_dir": str(root / "out")},
    }
    path = root / "explorer.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Histórico correcto en UTC y su configuración apuntando a él."""
    _write_run(tmp_path, make_m1_history(weeks=6))
    return tmp_path


def test_verify_tz_aprueba_un_historico_en_utc(workspace: Path) -> None:
    result = runner.invoke(
        app, ["chart", "verify-tz", "--config", str(workspace / "explorer.yaml")]
    )
    assert result.exit_code == 0
    assert "OK" in result.stdout


def test_verify_tz_falla_con_un_historico_desplazado(tmp_path: Path) -> None:
    config = _write_run(tmp_path, make_m1_history(weeks=6, shift_hours=5))
    result = runner.invoke(app, ["chart", "verify-tz", "--config", str(config)])
    assert result.exit_code == 1
    assert "FALLO" in result.stdout


def test_info_dice_que_hay_cargado(workspace: Path) -> None:
    result = runner.invoke(app, ["chart", "info", "--config", str(workspace / "explorer.yaml")])
    assert result.exit_code == 0
    for nombre in ("Diario", "H4", "H1", "M15", "M5"):
        assert nombre in result.stdout


def test_explorer_escribe_el_html(workspace: Path) -> None:
    result = runner.invoke(
        app, ["chart", "explorer", "--config", str(workspace / "explorer.yaml")]
    )
    assert result.exit_code == 0, result.stdout
    html = (workspace / "out" / "explorador.html").read_text(encoding="utf-8")
    assert '<script id="explorer-data"' in html
    assert "sin estrategia" in html
    # El punto de composición le pasa las SMMA calculadas: sin esto, el HTML
    # saldría sin ellas aunque el explorador sepa dibujarlas.
    assert '"smma":{"period":5,' in html


def test_explorer_no_dibuja_si_la_zona_horaria_no_cuadra(tmp_path: Path) -> None:
    config = _write_run(tmp_path, make_m1_history(weeks=6, shift_hours=5))
    result = runner.invoke(app, ["chart", "explorer", "--config", str(config)])

    assert result.exit_code == 1
    assert "NO SE DIBUJA" in result.stdout
    assert not (tmp_path / "out").exists(), "no debe escribir nada si la verificación falla"


def test_explorer_continua_con_el_flag_explicito_y_avisa(tmp_path: Path) -> None:
    config = _write_run(tmp_path, make_m1_history(weeks=6, shift_hours=5))
    result = runner.invoke(
        app, ["chart", "explorer", "--config", str(config), "--skip-tz-audit"]
    )

    assert result.exit_code == 0
    assert "AVISO" in result.stdout
    assert "NO son auditables" in result.stdout
    assert (tmp_path / "out" / "explorador.html").is_file()


def test_explorer_avisa_si_falta_el_historico(tmp_path: Path) -> None:
    config = tmp_path / "explorer.yaml"
    config.write_text(
        yaml.safe_dump({"data": {"bid_path": str(tmp_path / "no-existe.parquet")}}),
        encoding="utf-8",
    )
    result = runner.invoke(app, ["chart", "explorer", "--config", str(config)])
    assert result.exit_code == 1
    assert "No se encontró el histórico" in result.stdout

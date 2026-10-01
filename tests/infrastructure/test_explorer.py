"""El explorador de velas: lo que se dibuja y lo que se marca a mano.

Es la herramienta de auditoría visual del proyecto y todavía no hay estrategia
que dibujar, así que lo que se comprueba aquí es doble:

  · que el CHASIS funciona —las temporalidades, la ventana, el volumen, el
    replay, el zoom y las herramientas de mano—, y
  · que NO dibuja nada calculado. Mientras no exista una regla, cualquier traza
    que no sean las velas o el volumen del histórico es un error, y el test lo
    dice con nombre.

El JavaScript se ejecuta con node contra un DOM simulado: no sustituye a mirar
el fichero en un navegador, pero detecta lo que más se rompe —identificadores
que no existen, campos mal nombrados en el payload y excepciones dentro del
ciclo de render— y comprueba que los controles cambian la figura de verdad.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

from chronos.application.chart.config import (
    DAILY,
    H1,
    H4,
    M5,
    M15,
    ExplorerConfig,
    ExplorerReportingConfig,
    HistoryConfig,
    MarksConfig,
)
from chronos.domain.errors import DomainError
from chronos.infrastructure.market.aggregation import aggregate, aggregate_all
from chronos.infrastructure.market.chart_run import ChartRun, build_chart_run
from chronos.infrastructure.market.loader import SidedHistory
from chronos.infrastructure.reporting.explorer import (
    ASSETS,
    BEARISH,
    BULLISH,
    HAND_COLORS,
    bar_counts,
    build_payload,
    payload_size,
    render_explorer,
)
from chronos.infrastructure.reporting.timezones import session_label
from tests.conftest import make_m1_history

EPOCH = pd.Timestamp("1970-01-01", tz="UTC")


def _config(**overrides: object) -> ExplorerConfig:
    base: dict[str, object] = {"data": HistoryConfig(path="no-se-lee.parquet")}
    base.update(overrides)
    return ExplorerConfig(**base)  # type: ignore[arg-type]


def _run(config: ExplorerConfig | None = None, weeks: int = 16) -> ChartRun:
    config = config or _config()
    frame = make_m1_history(weeks=weeks)
    history = SidedHistory(
        frame=frame,
        side=config.price_side,
        provenance="fixture sintética",
        has_bid=True,
        has_ask=False,
    )
    series = aggregate_all(frame, config.aggregation, config.ordered_timeframes)
    return ChartRun(config=config, history=history, series=series)


@pytest.fixture(scope="module")
def run() -> ChartRun:
    return _run()


# --- Payload ------------------------------------------------------------------


def test_cada_grafico_tiene_sus_velas(run: ChartRun) -> None:
    counts = bar_counts(build_payload(run))
    assert set(counts) == {DAILY, H4, H1, M15, M5}
    assert counts[M5] > counts[M15] > counts[H1] > counts[H4] > counts[DAILY]


def test_los_graficos_se_ofrecen_de_mayor_a_menor(run: ChartRun) -> None:
    assert build_payload(run)["charts"] == [DAILY, H4, H1, M15, M5]


def test_cada_temporalidad_declara_cuanto_dura_su_vela(run: ChartRun) -> None:
    """Sin la duración no se puede saber cuándo cerró una vela, y sin eso el
    replay no sabe qué puede dibujar."""
    spans = build_payload(run)["spans"]
    assert spans == {DAILY: 1440, H4: 240, H1: 60, M15: 15, M5: 5}


def test_el_payload_no_lleva_ni_una_capa_calculada(run: ChartRun) -> None:
    """Mientras no haya estrategia, lo único que viaja son velas y su volumen.

    Es la promesa del proyecto: este explorador es un chasis. Si un día aparece
    una clave que no sea de las de abajo, será porque alguien ha metido una capa
    calculada y este test tiene que enterarse —y actualizarse a propósito—.
    """
    payload = build_payload(run)
    assert set(payload) == {
        "meta", "colors", "charts", "labels", "spans", "bars", "marks", "skipped"
    }
    for bars in payload["bars"].values():
        assert set(bars) == {"truncated", "total", "t", "o", "h", "l", "c", "v"}


def test_cada_vela_lleva_su_volumen(run: ChartRun) -> None:
    payload = build_payload(run)
    for chart, frame in run.frames.items():
        assert payload["bars"][chart]["v"] == pytest.approx(frame["volume"].tolist())


def test_el_payload_no_lleva_texto_montado(run: ChartRun) -> None:
    """Las etiquetas se componen en el navegador: con M5 de ocho años, mandar
    el texto ya hecho serían cientos de megabytes."""
    payload = build_payload(run)
    total_velas = sum(bar_counts(payload).values())
    # Guardarraíl de tamaño, no un presupuesto ajustado: con el texto montado en
    # Python esto pasaba de 200 bytes por vela.
    assert payload_size(payload) < 100 * total_velas


def test_el_recorte_conserva_las_velas_mas_recientes(run: ChartRun) -> None:
    completo = build_payload(run)
    corto = replace(
        run, config=replace(run.config, reporting=ExplorerReportingConfig(max_explorer_bars=50))
    )
    recortado = build_payload(corto)
    assert recortado["bars"][M15]["truncated"] is True
    assert recortado["bars"][M15]["total"] == completo["bars"][M15]["total"]
    assert recortado["bars"][M15]["t"] == completo["bars"][M15]["t"][-50:]


@pytest.mark.parametrize(
    ("timezone", "escrito"),
    [("Etc/GMT+4", "UTC-4"), ("Etc/GMT-3", "UTC+3"), ("America/New_York", "America/New_York")],
)
def test_la_zona_de_desfase_fijo_se_escribe_con_su_signo(
    run: ChartRun, timezone: str, escrito: str
) -> None:
    """`Etc/GMT+4` ES el UTC-4: el nombre IANA lleva el signo al revés y al lado
    de una hora se leería justo como lo contrario. Las plazas van tal cual."""
    otra = replace(
        run, config=replace(run.config, reporting=ExplorerReportingConfig(session_timezone=timezone))
    )
    meta = build_payload(otra)["meta"]
    assert meta["sessionTimezone"] == timezone
    assert meta["sessionTimezoneLabel"] == escrito


def test_los_nombres_de_las_marcas_salen_de_la_configuracion() -> None:
    """Renombrarlas no puede exigir tocar el JavaScript."""
    marks = MarksConfig(rects=("Soporte", "Resistencia"))
    payload = build_payload(_run(_config(marks=marks), weeks=4))
    assert payload["marks"] == {"rects": ["Soporte", "Resistencia"]}
    assert payload["colors"]["rects"] == {
        "Soporte": HAND_COLORS[0],
        "Resistencia": HAND_COLORS[1],
    }


def test_no_se_admiten_mas_marcas_que_colores_de_mano() -> None:
    """Un cuarto nombre tendría que repetir un color y dejaría de distinguirse."""
    with pytest.raises(DomainError, match="tres nombres como mucho"):
        MarksConfig(rects=("a", "b", "c", "d"))


def test_los_colores_de_la_mano_no_son_los_de_las_velas() -> None:
    """El día que haya capas calculadas, lo de la mano tiene que seguir separándose."""
    assert BULLISH not in HAND_COLORS
    assert BEARISH not in HAND_COLORS
    assert len(set(HAND_COLORS)) == len(HAND_COLORS)


def test_un_grafico_que_el_historico_no_da_para_construir_se_omite_y_se_dice(
    tmp_path: Path,
) -> None:
    """Un histórico H1 sirve para el diario, H4 y H1 pero no para M15 ni M5:
    se dibujan los que hay y la pestaña que falta se explica."""
    hourly = aggregate(make_m1_history(weeks=4), H1, _config().aggregation).frame
    path = tmp_path / "h1.parquet"
    hourly.to_parquet(path)

    run = build_chart_run(_config(data=HistoryConfig(bid_path=str(path))))
    payload = build_payload(run)

    assert payload["charts"] == [DAILY, H4, H1]
    assert [item.split(":")[0] for item in payload["skipped"]] == [M15, M5]
    assert "sin construir: M15, M5" in render_explorer(run)


def test_sin_ninguna_temporalidad_la_corrida_no_existe(run: ChartRun) -> None:
    with pytest.raises(DomainError, match="no da para ninguna"):
        ChartRun(config=run.config, history=run.history, series={})


# --- HTML ---------------------------------------------------------------------


def test_no_quedan_marcadores_sin_sustituir(run: ChartRun) -> None:
    template = (ASSETS / "explorer.html").read_text(encoding="utf-8")
    marcadores = set(re.findall(r"__[A-Z][A-Z_]*__", template))
    assert marcadores, "la plantilla debería tener marcadores"

    html = render_explorer(run)
    assert not {marcador for marcador in marcadores if marcador in html}


def test_todos_los_controles_que_busca_el_javascript_estan_en_la_plantilla() -> None:
    """El DOM simulado de los tests declara los elementos a mano, así que un
    identificador mal escrito en la plantilla se le escaparía: aquí se compara
    contra el HTML de verdad."""
    script = (ASSETS / "explorer.js").read_text(encoding="utf-8")
    template = (ASSETS / "explorer.html").read_text(encoding="utf-8")
    buscados = set(re.findall(r'getElementById\("([^"]+)"\)', script))
    declarados = set(re.findall(r'id="([^"]+)"', template))

    assert buscados, "el explorador tiene que buscar sus controles"
    assert not buscados - declarados


def test_el_html_es_autocontenido(run: ChartRun) -> None:
    html = render_explorer(run)
    assert "Plotly" in html
    assert "<script src=" not in html
    assert "<link " not in html


def test_la_cabecera_dice_que_no_hay_estrategia(run: ChartRun) -> None:
    html = render_explorer(run)
    assert "XAUUSD · explorador de velas · lado bid" in html
    assert "sin estrategia" in html


def test_el_json_embebido_no_puede_cerrar_la_etiqueta_script(run: ChartRun) -> None:
    html = render_explorer(run)
    bloque = html.split('id="explorer-data"')[1].split("</script>")[0]
    assert "</" not in bloque


# --- El JavaScript, contra un DOM simulado ------------------------------------


def _draw(run: ChartRun, tmp_path: Path) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node no está disponible: no se puede ejecutar el JavaScript")

    payload_path = tmp_path / "payload.json"
    payload_path.write_text(json.dumps(build_payload(run), default=str), encoding="utf-8")
    stub = Path(__file__).parent / "explorer_dom_stub.js"
    output = subprocess.run(
        [node, str(stub), str(ASSETS / "explorer.js"), str(payload_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(output.stdout)


@pytest.fixture(scope="module")
def drawn(run: ChartRun, tmp_path_factory: pytest.TempPathFactory) -> dict:
    return _draw(run, tmp_path_factory.mktemp("explorer"))


@pytest.fixture(scope="module")
def payload(run: ChartRun) -> dict:
    return build_payload(run)


def _step(resultado: dict, label: str) -> dict:
    return next(step for step in resultado["steps"] if step["label"] == label)


def _trace_names(step: dict) -> list[str]:
    return [trace["name"] for trace in step["plot"]["traces"]]


def _minute(stamp: str) -> int:
    return int((pd.Timestamp(stamp, tz="UTC") - EPOCH) // pd.Timedelta(minutes=1))


def test_el_explorador_se_dibuja_sin_errores(drawn: dict) -> None:
    assert not drawn["unknownElements"], (
        f"el explorador busca elementos que la plantilla no define: {drawn['unknownElements']}"
    )
    assert drawn["chartTabs"] == ["Diario", "H4", "H1", "M15", "M5"]
    assert drawn["presetLabels"][0] == "Todo"

    todo = _step(drawn, "todo")["plot"]
    assert todo["target"] == "chart"
    assert todo["yTickFormat"] == ".4f"
    assert todo["xAnchor"] == "y"


def test_no_se_dibuja_ni_una_capa_calculada(drawn: dict) -> None:
    """La promesa del proyecto, comprobada en cada paso del recorrido.

    Al añadir la primera capa calculada hay que actualizar este test a
    propósito —que acepte esa capa por su nombre—, no borrarlo.
    """
    for step in drawn["steps"]:
        assert step["plot"]["calculated"] == [], (
            f"en «{step['label']}» hay trazas que no son velas ni volumen: "
            f"{step['plot']['calculated']}"
        )


def test_lo_unico_que_hay_en_shapes_lo_ha_puesto_una_mano(drawn: dict) -> None:
    for step in drawn["steps"]:
        assert step["plot"]["shapes"] == 0, (
            f"en «{step['label']}» hay formas que no ha dibujado el propietario"
        )


def test_el_estado_dice_que_no_hay_estrategia(drawn: dict) -> None:
    """Un gráfico pelado sin decirlo se lee como que ahí no pasó nada."""
    notas = _step(drawn, "todo")["notes"]
    assert "SIN ESTRATEGIA" in notas
    assert "lo pone tu mano" in notas


def test_los_botones_dicen_su_atajo(drawn: dict) -> None:
    titulos = dict(zip(drawn["chartTabs"], drawn["chartTitles"], strict=True))
    assert titulos["H4"] == "Velas de H4. Atajo de teclado: 4"
    assert titulos["M5"] == "Velas de M5. Atajo de teclado: 5"


# --- Temporalidades, ventana y vista -------------------------------------------


def test_cada_temporalidad_dibuja_sus_velas(drawn: dict) -> None:
    velas = {
        timeframe: _step(drawn, f"grafico-{timeframe}")["plot"]["bars"]
        for timeframe in (DAILY, H4, H1, M15, M5)
    }
    assert velas[M5] > velas[M15] > velas[H1] > velas[H4] > velas[DAILY]


def test_el_conmutador_de_velas_a_lineas(drawn: dict) -> None:
    assert _step(drawn, "velas")["plot"]["traces"][0]["type"] == "candlestick"
    lineas = _step(drawn, "lineas")["plot"]
    assert lineas["traces"][0]["type"] == "scatter"
    assert lineas["traces"][0]["name"].startswith("Cierres")


def test_el_detalle_muestra_utc_y_la_zona_de_la_sesion(drawn: dict, run: ChartRun) -> None:
    detalle = _step(drawn, "todo")["plot"]["hover"]
    assert "UTC" in detalle
    assert session_label(run.config.reporting.session_timezone) in detalle
    assert re.search(r"O \d+\.\d{4} · H \d+\.\d{4}", detalle)


def test_el_preset_recorta_la_ventana(drawn: dict) -> None:
    todo = _step(drawn, "todo")
    corto = _step(drawn, "preset-corto")

    assert corto["plot"]["bars"] < todo["plot"]["bars"]
    assert corto["from"] > todo["from"]
    assert corto["to"] == todo["to"]


def test_los_pasos_recorren_el_historico_sin_solaparse(drawn: dict) -> None:
    actual = _step(drawn, "preset-corto")
    anterior = _step(drawn, "ventana-anterior")
    siguiente = _step(drawn, "ventana-siguiente")

    assert anterior["from"] < actual["from"]
    assert anterior["to"] < actual["to"]
    assert (siguiente["from"], siguiente["to"]) == (actual["from"], actual["to"])
    assert anterior["plot"]["lastBar"] <= actual["plot"]["firstBar"]


def test_la_ventana_no_se_sale_del_historico(drawn: dict, payload: dict) -> None:
    for step in drawn["steps"]:
        tiempos = payload["bars"][step["chart"]]["t"]
        primera = (EPOCH + pd.Timedelta(minutes=tiempos[0])).strftime("%Y-%m-%d")
        ultima = (EPOCH + pd.Timedelta(minutes=tiempos[-1])).strftime("%Y-%m-%d")
        assert step["from"] >= primera
        assert step["to"] <= ultima


def test_las_flechas_del_teclado_mueven_la_ventana(drawn: dict) -> None:
    partida = _step(drawn, "ventana-siguiente")
    izquierda = _step(drawn, "teclado-izquierda")
    derecha = _step(drawn, "teclado-derecha")

    assert izquierda["from"] < partida["from"]
    assert (derecha["from"], derecha["to"]) == (partida["from"], partida["to"])


def test_las_flechas_no_roban_el_teclado_a_los_campos(drawn: dict) -> None:
    """Con el foco en un campo, las flechas mueven el cursor, no el gráfico."""
    derecha = _step(drawn, "teclado-derecha")
    en_campo = _step(drawn, "teclado-en-un-campo")
    assert (en_campo["from"], en_campo["to"]) == (derecha["from"], derecha["to"])


def test_las_teclas_d_4_1_m_5_cambian_la_temporalidad(drawn: dict) -> None:
    """Una tecla por gráfico: d = Diario, 4 = H4, 1 = H1, m = M15, 5 = M5."""
    assert _step(drawn, "teclado-tf-h4")["chart"] == H4
    assert _step(drawn, "teclado-tf-m15")["chart"] == M15
    assert _step(drawn, "teclado-tf-m5")["chart"] == M5
    assert _step(drawn, "teclado-tf-h1")["chart"] == H1
    assert _step(drawn, "teclado-tf-diario")["chart"] == DAILY


def test_las_teclas_de_temporalidad_no_roban_el_teclado_a_los_campos(drawn: dict) -> None:
    """Con el foco en un campo, «d» escribe una letra: el gráfico no se mueve."""
    assert _step(drawn, "teclado-tf-en-un-campo")["chart"] == H1


# --- El volumen -------------------------------------------------------------------


def test_el_volumen_va_al_pie_del_precio_en_su_eje(drawn: dict, payload: dict) -> None:
    paso = _step(drawn, "volumen-por-defecto")
    volumen = [trace for trace in paso["plot"]["traces"] if trace["name"] == "Volumen H1"]
    assert len(volumen) == 1 and volumen[0]["type"] == "bar"
    assert volumen[0]["yaxis"] == "y3"
    # Sólo las últimas velas, y hasta la última a la vista.
    assert payload["meta"]["volumeBars"] == 40
    assert volumen[0]["points"] == min(40, paso["plot"]["bars"])
    assert volumen[0]["lastX"] == paso["plot"]["lastBar"]
    eje = paso["plot"]["volumeAxis"]
    assert eje["overlaying"] == "y" and eje["visible"] is False
    # Las barras se quedan en la franja de abajo: el eje llega a 5 veces la más alta.
    assert eje["range"][0] == 0
    assert "VOLUMEN" in paso["notes"]
    assert paso["volumeBox"] is True


def test_el_volumen_se_apaga(drawn: dict) -> None:
    apagado = _step(drawn, "volumen-apagado")
    assert not any(nombre.startswith("Volumen") for nombre in _trace_names(apagado))
    assert apagado["plot"]["volumeAxis"] is None
    assert "volumen apagado" in apagado["notes"]
    assert apagado["volumeBox"] is False


# --- Auditoría ciega ----------------------------------------------------------------


def test_la_auditoria_ciega_deja_solo_las_velas(drawn: dict) -> None:
    ciega = _step(drawn, "ciega")

    assert len(ciega["plot"]["traces"]) == 1, "con la venda no puede quedar nada más"
    assert ciega["plot"]["traces"][0]["type"] == "candlestick"
    assert ciega["plot"]["volumeAxis"] is None
    assert "AUDITORÍA CIEGA" in ciega["notes"]
    assert "semilla 4242" in ciega["notes"]


def test_revelar_devuelve_lo_que_la_venda_tapaba(drawn: dict) -> None:
    ciega = _step(drawn, "ciega")
    revelada = _step(drawn, "revelada")

    assert len(revelada["plot"]["traces"]) > len(ciega["plot"]["traces"])
    assert (revelada["from"], revelada["to"]) == (ciega["from"], ciega["to"])
    assert "semilla 4242" in revelada["notes"]


def test_la_misma_semilla_reabre_la_misma_ventana(drawn: dict) -> None:
    primera = _step(drawn, "ciega")
    repetida = _step(drawn, "ciega-misma-semilla")
    assert (repetida["from"], repetida["to"]) == (primera["from"], primera["to"])


def test_salir_de_la_ciega_restaura_el_rango(drawn: dict) -> None:
    antes = _step(drawn, "antes-de-la-ciega")
    fuera = _step(drawn, "fuera-de-la-ciega")

    assert (fuera["from"], fuera["to"]) == (antes["from"], antes["to"])
    assert "AUDITORÍA CIEGA" not in fuera["notes"]


# --- Replay ---------------------------------------------------------------------


def _replay_steps(resultado: dict) -> list[dict]:
    return [
        step
        for step in resultado["steps"]
        if step["label"].startswith("replay-") and step["label"] != "replay-fuera"
    ]


def _clock(payload: dict, step: dict) -> int:
    """Minuto en que cerró la última vela dibujada: el presente de ese paso."""
    return _minute(step["plot"]["lastBar"]) + payload["spans"][step["chart"]]


def _fine_clock(step: dict) -> int:
    """El reloj que declaran las notas: hasta qué minuto se ha visto el mercado.

    No es el cierre de la última vela dibujada cuando hay una a medio armar, y es
    lo único que tiene que coincidir entre temporalidades.
    """
    marca = re.search(r"reloj (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) UTC", step["notes"])
    assert marca is not None, f"el replay no declara su reloj: {step['notes']}"
    return _minute(marca.group(1))


def test_el_replay_arranca_en_la_fecha_elegida(drawn: dict) -> None:
    antes = _step(drawn, "antes-del-replay")
    inicio = _step(drawn, "replay-inicio")

    assert "REPLAY" in inicio["notes"]
    assert inicio["plot"]["lastBar"] < antes["plot"]["lastBar"]
    # La ventana arranca justo antes del día pedido: el primer paso descubre su
    # primera vela en vez de enseñarla ya hecha.
    assert inicio["plot"]["lastBar"][:10] <= inicio["replayDate"]


def test_el_replay_no_dibuja_nada_que_no_se_supiera(drawn: dict, payload: dict) -> None:
    """Fuera de las velas y de la vela en formación —el volumen incluido—, nada
    puede caer más allá de la última vela cerrada."""
    pasos = _replay_steps(drawn)
    assert pasos, "el recorrido tiene que pasar por el replay"

    for paso in pasos:
        dibujado = paso["plot"]["maxEngineX"]
        if dibujado is None:
            continue
        assert _minute(dibujado) <= _clock(payload, paso), paso["label"]


def test_la_vela_se_arma_con_la_temporalidad_inferior(drawn: dict, payload: dict) -> None:
    inicio = _step(drawn, "replay-inicio")
    pasos = [_step(drawn, f"replay-paso-{numero}") for numero in range(1, 5)]
    intermedios = payload["spans"][H4] // payload["spans"][H1] - 1

    assert "Vela en formación" not in _trace_names(inicio)
    for numero, paso in enumerate(pasos[:intermedios], start=1):
        assert "Vela en formación" in _trace_names(paso)
        assert f"{numero} de {intermedios + 1} velas de H1" in paso["notes"]
        # Mientras se arma, la vela no ha cerrado.
        assert paso["plot"]["lastBar"] == inicio["plot"]["lastBar"]

    cierre = pasos[intermedios]
    assert cierre["plot"]["lastBar"] > inicio["plot"]["lastBar"]
    assert "Vela en formación" not in _trace_names(cierre)


def test_sin_vela_en_formacion_cada_paso_es_una_vela_entera(drawn: dict) -> None:
    apagada = _step(drawn, "replay-sin-formacion")
    siguiente = _step(drawn, "replay-vela-entera")

    assert "Vela en formación" not in _trace_names(apagada)
    assert siguiente["plot"]["lastBar"] > apagada["plot"]["lastBar"]


def test_el_paso_atras_deshace_el_ultimo(drawn: dict) -> None:
    antes = _step(drawn, "replay-paso-5")
    atras = _step(drawn, "replay-atras")
    teclado = _step(drawn, "replay-teclado")

    assert atras["notes"] == antes["notes"]
    # Y la flecha derecha vuelve a avanzar, como el botón.
    assert teclado["notes"] == _step(drawn, "replay-paso-6")["notes"]


def test_la_reproduccion_se_enciende_y_se_apaga(drawn: dict) -> None:
    assert _step(drawn, "replay-reproduciendo")["replayPlay"] == "⏸"
    assert _step(drawn, "replay-pausado")["replayPlay"] == "▶"


def test_cambiar_de_temporalidad_no_mueve_el_reloj(drawn: dict, payload: dict) -> None:
    """El mismo instante visto en otra temporalidad: la última vela cerrada de la
    nueva, ni una más."""
    origen = _step(drawn, "replay-sin-formacion")
    destino = _step(drawn, "replay-otra-temporalidad")

    assert destino["chart"] == DAILY
    reloj = _clock(payload, origen)
    cierre = _clock(payload, destino)
    assert cierre <= reloj
    # El histórico tiene hueco de fin de semana, así que la vela diaria de
    # después puede empezar mucho más tarde que el cierre de ésta. Lo que se
    # comprueba es que no haya ninguna posterior que ya hubiera cerrado.
    span = payload["spans"][DAILY]
    posteriores = [t for t in payload["bars"][DAILY]["t"] if cierre < t + span <= reloj]
    assert not posteriores, "hay una vela diaria posterior que ya había cerrado"


def test_durante_el_replay_los_controles_de_periodo_se_apagan(drawn: dict) -> None:
    """Con el cursor mandando, un selector de fechas vivo mentiría."""
    assert all(paso["replayLocked"] for paso in _replay_steps(drawn))
    assert not _step(drawn, "antes-del-replay")["replayLocked"]
    assert not _step(drawn, "replay-fuera")["replayLocked"]


def test_el_replay_deja_aire_a_la_derecha(drawn: dict) -> None:
    """Sin margen la última vela quedaría pegada al borde y el eje daría un salto
    en cada paso."""
    inicio = _step(drawn, "replay-inicio")
    rango = inicio["plot"]["xRange"]

    assert rango is not None
    assert rango[0] <= inicio["plot"]["firstBar"]
    assert rango[1] > inicio["plot"]["lastBar"]


def test_salir_del_replay_devuelve_el_periodo_de_partida(drawn: dict) -> None:
    antes = _step(drawn, "antes-del-replay")
    fuera = _step(drawn, "replay-fuera")

    assert (fuera["from"], fuera["to"]) == (antes["from"], antes["to"])
    assert fuera["plot"]["bars"] == antes["plot"]["bars"]
    assert fuera["plot"]["xRange"] is None
    assert "REPLAY" not in fuera["notes"]


def test_lo_avanzado_en_una_temporalidad_se_ve_en_las_demas(drawn: dict, payload: dict) -> None:
    """El reloj es uno solo. Lo que llevas corrido dentro de la vela de H4 tiene
    que aparecer en el diario como su vela a medio armar; si no, saltar de
    temporalidad devolvería el gráfico al último cierre y parecería un reinicio."""
    h4_antes = _step(drawn, "reloj-h4-avanzado")
    diario = _step(drawn, "reloj-en-diario")

    assert diario["chart"] == DAILY
    assert "Vela en formación" in _trace_names(diario), "el diario volvió al cierre de ayer"
    formadas = re.search(r"vela en formación con (\d+) de \d+ velas de H4", diario["notes"])
    assert formadas is not None and int(formadas.group(1)) >= 1

    assert _fine_clock(diario) == _fine_clock(h4_antes)
    assert _clock(payload, diario) <= _fine_clock(diario)


def test_el_reloj_no_se_degrada_al_pasar_por_el_diario(drawn: dict) -> None:
    """El diario no tiene resolución para la hora y cuarto que llevas corrida en
    H1, pero el reloj no la olvida: volver a H1 devuelve el mismo minuto."""
    en_h1 = _step(drawn, "reloj-fino-h1")
    diario = _step(drawn, "reloj-fino-en-diario")
    vuelta = _step(drawn, "reloj-fino-de-vuelta")

    assert _fine_clock(en_h1) == _fine_clock(diario) == _fine_clock(vuelta)
    assert vuelta["notes"] == en_h1["notes"], "volver a H1 no devolvió el mismo paso"
    assert "Vela en formación" in _trace_names(diario)


def test_volver_a_la_temporalidad_de_partida_no_retrocede(drawn: dict) -> None:
    antes = _step(drawn, "reloj-h4-avanzado")
    vuelta = _step(drawn, "reloj-de-vuelta-en-h4")

    assert vuelta["chart"] == H4
    assert vuelta["plot"]["lastBar"] == antes["plot"]["lastBar"]


# --- El encuadre manual ---------------------------------------------------------
#
# Auditar de cerca exige acercar el zoom y quedarse ahí: si cada paso del replay
# devolviera el gráfico a su escala, no se podría mirar una vela concreta
# mientras se avanza.


def _width(step: dict) -> pd.Timedelta:
    x = step["plot"]["xRange"]
    return pd.Timestamp(x[1]) - pd.Timestamp(x[0])


def test_el_zoom_manual_sobrevive_a_los_pasos_del_replay(drawn: dict) -> None:
    pasos = [
        _step(drawn, f"replay-zoom-{etiqueta}")
        for etiqueta in ("ancho-1", "ancho-2", "estrecho-1", "estrecho-2")
    ]

    for paso in pasos:
        assert paso["plot"]["yRange"] == [1.05, 1.35], "el eje de precios se rehízo"
        assert "encuadre manual" in paso["notes"]
        assert not paso["zoomFree"], "«Ajustar» tiene que quedar disponible"
    assert _width(pasos[0]) == _width(pasos[1])
    assert _width(pasos[2]) == _width(pasos[3])
    assert _width(pasos[2]) < _width(pasos[0])


def test_el_encuadre_manual_sigue_al_presente(drawn: dict, payload: dict) -> None:
    """Conservar el zoom no puede dejar la vela nueva fuera de la pantalla: la
    ventana se desplaza justo un paso, sin cambiar de escala."""
    uno = _step(drawn, "replay-zoom-estrecho-1")
    dos = _step(drawn, "replay-zoom-estrecho-2")

    vela = pd.Timedelta(minutes=payload["spans"][H4])
    corrimiento = pd.Timestamp(dos["plot"]["xRange"][0]) - pd.Timestamp(uno["plot"]["xRange"][0])
    assert corrimiento == vela
    for paso in (uno, dos):
        ultima = pd.Timestamp(paso["plot"]["lastBar"])
        assert pd.Timestamp(paso["plot"]["xRange"][0]) <= ultima
        assert ultima <= pd.Timestamp(paso["plot"]["xRange"][1])


def test_el_paso_atras_no_mueve_el_encuadre_manual(drawn: dict) -> None:
    atras = _step(drawn, "replay-zoom-atras")
    ultimo = _step(drawn, "replay-zoom-estrecho-2")

    assert atras["plot"]["xRange"] == ultimo["plot"]["xRange"]
    assert atras["plot"]["lastBar"] < ultimo["plot"]["lastBar"]


def test_el_encuadre_manual_pide_las_velas_que_tapa(drawn: dict) -> None:
    """Alejar el zoom más allá de las velas a la vista no puede dejar media
    pantalla vacía: el recorte se amplía hasta cubrir lo que se ve."""
    sin_zoom = _step(drawn, "replay-zoom-sin-zoom")
    ancho = _step(drawn, "replay-zoom-ancho-1")

    assert ancho["plot"]["bars"] > sin_zoom["plot"]["bars"]
    assert pd.Timestamp(ancho["plot"]["firstBar"]) <= pd.Timestamp(ancho["plot"]["xRange"][0])


def test_ajustar_devuelve_el_encuadre_automatico(drawn: dict) -> None:
    suelto = _step(drawn, "replay-zoom-suelto")

    assert suelto["zoomFree"]
    assert suelto["plot"]["yRange"] is None
    assert "encuadre manual" not in suelto["notes"]
    assert pd.Timestamp(suelto["plot"]["xRange"][1]) > pd.Timestamp(suelto["plot"]["lastBar"])


def test_fuera_del_replay_el_encuadre_manual_tambien_manda(drawn: dict) -> None:
    """Redibujar no devuelve el gráfico a su sitio; pedir otro tramo de historia
    sí, porque ahí el encuadre anterior ya no significa nada."""
    capa = _step(drawn, "zoom-fuera-del-replay")
    preset = _step(drawn, "zoom-suelto-por-el-preset")

    assert capa["plot"]["xRange"] is not None
    assert capa["plot"]["yRange"] == [1.05, 1.35]
    assert not capa["zoomFree"]
    assert preset["plot"]["xRange"] is None
    assert preset["plot"]["yRange"] is None
    assert preset["zoomFree"]


def test_arrastrar_sobre_los_ejes_escala_el_grafico(drawn: dict) -> None:
    antes = _step(drawn, "ejes-antes-de-escalar")["plot"]
    precios = _step(drawn, "eje-precios-arrastrado")["lastRelayout"]
    fechas = _step(drawn, "eje-fechas-arrastrado")["lastRelayout"]

    base_y = [float(v) for v in antes["yRange"]]
    nuevo_y = [float(v) for v in precios["yaxis.range"]]
    assert precios["yaxis.autorange"] is False
    # Hacia abajo se ve MÁS rango y el centro se queda donde estaba: el gesto
    # escala, no desplaza.
    assert nuevo_y[1] - nuevo_y[0] > base_y[1] - base_y[0]
    assert sum(nuevo_y) / 2 == pytest.approx(sum(base_y) / 2, abs=1e-6)

    base_x = [_minute(v) for v in antes["xRange"]]
    nuevo_x = [_minute(v) for v in fechas["xaxis.range"]]
    # Hacia la izquierda entran más velas, y la última no se mueve.
    assert nuevo_x[1] - nuevo_x[0] > base_x[1] - base_x[0]
    assert nuevo_x[1] == base_x[1]


def test_la_escala_tomada_en_los_ejes_sobrevive_al_redibujo(drawn: dict) -> None:
    fechas = _step(drawn, "eje-fechas-arrastrado")["lastRelayout"]
    precios = _step(drawn, "eje-precios-arrastrado")["lastRelayout"]
    despues = _step(drawn, "ejes-tras-redibujar")["plot"]

    assert [_minute(v) for v in despues["xRange"]] == [
        _minute(v) for v in fechas["xaxis.range"]
    ]
    assert [float(v) for v in despues["yRange"]] == pytest.approx(
        [float(v) for v in precios["yaxis.range"]]
    )


# --- La caja simulada ------------------------------------------------------------
#
# Dos botones que arman, un clic que planta la caja y arrastres que la mueven.
# El recorrido pulsa en el centro del encuadre que él mismo ha fijado
# (1,05 → 1,35), así que las cifras se pueden comprobar a mano.


def _caja(step: dict) -> dict[str, dict]:
    return {forma["name"]: forma for forma in step["plot"]["sim"]}


def _niveles(step: dict) -> dict[str, float]:
    caja = _caja(step)
    return {
        "entrada": caja["sim-entrada"]["y0"],
        "objetivo": caja["sim-objetivo"]["y1"],
        "stop": caja["sim-riesgo"]["y1"],
    }


def _pips(distancia: float) -> int:
    return round(abs(distancia) / 1e-4)


def test_el_boton_arma_y_lo_dice_antes_de_plantar_nada(drawn: dict) -> None:
    armado = _step(drawn, "sim-armado")

    assert armado["simArmed"] == "long"
    assert not armado["plot"]["sim"], "armar no puede dibujar todavía ninguna caja"
    assert "SIMULADOR ARMADO (largo)" in armado["notes"]
    assert armado["simCursor"] == "crosshair"


def test_escape_desarma_sin_plantar(drawn: dict) -> None:
    desarmado = _step(drawn, "sim-desarmado")

    assert desarmado["simArmed"] is None
    assert not desarmado["plot"]["sim"]
    assert "SIMULADOR" not in desarmado["notes"]


def test_la_caja_se_planta_en_el_precio_del_clic(drawn: dict) -> None:
    largo = _step(drawn, "sim-largo")
    niveles = _niveles(largo)

    assert niveles["entrada"] == pytest.approx(1.2000, abs=1e-4)
    assert niveles["stop"] < niveles["entrada"] < niveles["objetivo"]
    assert _caja(largo)["sim-entrada"]["y1"] == niveles["entrada"], "la entrada es una línea"


def test_la_caja_dice_los_pips_de_cada_lado_y_el_ratio(drawn: dict) -> None:
    caja = _caja(_step(drawn, "sim-largo"))

    # Cada caja dice además lo que se juega con el capital puesto —50 $ al 2 %
    # son 1,00 $ de riesgo—, que es la razón de dibujarla.
    assert caja["sim-objetivo"]["label"] == "objetivo 300 pips · +2,00 $"
    assert caja["sim-riesgo"]["label"] == "riesgo 150 pips · -1,00 $"
    assert caja["sim-entrada"]["label"] == "LARGO · R:R 1:2,0"


def test_las_notas_dicen_que_la_caja_no_es_una_operacion(drawn: dict) -> None:
    notas = _step(drawn, "sim-largo")["notes"]

    assert "simulación LARGO" in notas
    assert "entrada 1.2000" in notas
    assert "stop 1.1850 (150 pips)" in notas
    assert "objetivo 1.2300 (300 pips)" in notas
    assert "R:R 1:2,0" in notas
    assert "ES DIBUJO A MANO" in notas
    assert "no hay orden" in notas


def test_arrastrar_el_stop_no_mueve_la_entrada_y_respeta_el_ratio(drawn: dict) -> None:
    """Con un R:R fijo, el stop es lo que se decide y el objetivo su
    consecuencia: se va con él y el ratio no se mueve."""
    paso = _step(drawn, "sim-stop-arrastrado")
    antes = _niveles(_step(drawn, "sim-largo"))
    despues = _niveles(paso)

    assert despues["entrada"] == antes["entrada"]
    assert despues["stop"] < antes["stop"], "el arrastre iba hacia abajo: más riesgo"
    assert despues["objetivo"] > antes["objetivo"], "el objetivo sigue al stop"
    # El dinero en juego no lo mueve el stop: el riesgo es del capital y lo que
    # cambia con los pips es el tamaño de la posición, no lo que se arriesga.
    riesgo = _pips(despues["entrada"] - despues["stop"])
    assert _caja(paso)["sim-riesgo"]["label"] == f"riesgo {riesgo} pips · -1,00 $"
    assert _caja(paso)["sim-entrada"]["label"] == "LARGO · R:R 1:2,0"
    assert paso["simRatio"] == "2"


def test_arrastrar_la_entrada_mueve_la_caja_entera(drawn: dict) -> None:
    antes = _niveles(_step(drawn, "sim-stop-arrastrado"))
    despues = _niveles(_step(drawn, "sim-entrada-arrastrada"))

    salto = despues["entrada"] - antes["entrada"]
    assert salto > 0, "el arrastre iba hacia arriba"
    assert despues["stop"] - antes["stop"] == pytest.approx(salto, abs=1e-4)
    assert despues["objetivo"] - antes["objetivo"] == pytest.approx(salto, abs=1e-4)


def test_en_corto_el_objetivo_va_por_debajo_de_la_entrada(drawn: dict) -> None:
    corto = _step(drawn, "sim-corto")
    niveles = _niveles(corto)

    assert niveles["objetivo"] < niveles["entrada"] < niveles["stop"]
    assert _caja(corto)["sim-entrada"]["label"].startswith("CORTO")
    assert "simulación CORTO" in corto["notes"]


def test_quitar_borra_la_caja_y_lo_que_decia_de_ella(drawn: dict) -> None:
    quitado = _step(drawn, "sim-quitado")

    assert not quitado["plot"]["sim"]
    assert quitado["simClearDisabled"], "sin caja no hay nada que quitar"
    assert "simulación" not in quitado["notes"]


def test_el_ratio_recoloca_el_objetivo_sin_tocar_el_stop(drawn: dict) -> None:
    antes = _niveles(_step(drawn, "sim-entrada-arrastrada"))
    paso = _step(drawn, "sim-ratio-1-3")
    despues = _niveles(paso)

    assert paso["simRatio"] == "3"
    assert (despues["stop"], despues["entrada"]) == (antes["stop"], antes["entrada"])
    riesgo = despues["entrada"] - despues["stop"]
    assert despues["objetivo"] - despues["entrada"] == pytest.approx(3 * riesgo, abs=1e-4)
    assert _caja(paso)["sim-entrada"]["label"] == "LARGO · R:R 1:3,0"


def test_con_el_ratio_puesto_el_stop_arrastra_al_objetivo(drawn: dict) -> None:
    antes = _niveles(_step(drawn, "sim-ratio-1-3"))
    paso = _step(drawn, "sim-stop-con-candado")
    despues = _niveles(paso)

    assert despues["stop"] > antes["stop"], "el arrastre iba hacia arriba: menos riesgo"
    assert despues["objetivo"] < antes["objetivo"], "el objetivo se acerca con él"
    riesgo = despues["entrada"] - despues["stop"]
    assert despues["objetivo"] - despues["entrada"] == pytest.approx(3 * riesgo, abs=1e-4)
    assert paso["simRatio"] == "3"


def test_arrastrar_el_objetivo_suelta_el_candado(drawn: dict) -> None:
    """Manda lo que se ve: si el objetivo se mueve a mano, ningún botón puede
    seguir diciendo que el ratio es suyo."""
    antes = _niveles(_step(drawn, "sim-stop-con-candado"))
    paso = _step(drawn, "sim-objetivo-a-mano")
    despues = _niveles(paso)

    assert paso["simRatio"] is None
    assert (despues["stop"], despues["entrada"]) == (antes["stop"], antes["entrada"])
    assert despues["objetivo"] < antes["objetivo"]
    assert "a mano" in paso["notes"]


def test_fijar_un_ratio_sin_caja_no_rompe_nada(drawn: dict) -> None:
    paso = _step(drawn, "ratio-sin-caja")

    assert paso["simRatio"] == "2"
    assert not paso["plot"]["sim"]


def test_la_caja_nueva_se_planta_con_el_ratio_puesto(drawn: dict) -> None:
    corto = _step(drawn, "sim-corto")
    niveles = _niveles(corto)

    assert corto["simRatio"] == "4"
    riesgo = niveles["stop"] - niveles["entrada"]
    assert niveles["entrada"] - niveles["objetivo"] == pytest.approx(4 * riesgo, abs=1e-4)
    assert _caja(corto)["sim-entrada"]["label"] == "CORTO · R:R 1:4,0"


# --- La cuenta simulada -------------------------------------------------------
#
# Un capital, un riesgo por operación y tres botones que apuntan la caja que hay
# dibujada. El recorrido trabaja siempre sobre la misma caja —1:2 exacto—, así
# que las cifras se pueden comprobar a mano: 50 $ al 2 % son 1,00 $ de riesgo y
# 2,00 $ de objetivo. Cada operación se queda con los dólares que se jugó.


def _cuenta(step: dict) -> dict:
    return step["account"]


def test_la_cuenta_sale_con_su_capital_y_sin_operaciones(drawn: dict) -> None:
    cuenta = _cuenta(_step(drawn, "cuenta-sin-nada"))

    assert cuenta["initial"] == "50"
    assert cuenta["mode"] == "percent"
    assert cuenta["risk"] == "2"
    assert cuenta["summary"] == "50,00 $ · riesgo 1,00 $ · 0 operaciones"
    assert cuenta["undoDisabled"] and cuenta["resetDisabled"] and cuenta["copyDisabled"]


def test_sin_caja_dibujada_no_hay_nada_que_apuntar(drawn: dict) -> None:
    assert all(_cuenta(_step(drawn, "cuenta-sin-nada"))["resultsDisabled"])
    assert not any(_cuenta(_step(drawn, "cuenta-con-caja"))["resultsDisabled"])
    assert "CUENTA SIMULADA" not in _step(drawn, "cuenta-sin-nada")["notes"]


def test_la_caja_dice_lo_que_se_juega_con_el_capital_puesto(drawn: dict) -> None:
    paso = _step(drawn, "cuenta-con-caja")

    assert _caja(paso)["sim-objetivo"]["label"].endswith("· +2,00 $")
    assert _caja(paso)["sim-riesgo"]["label"].endswith("· -1,00 $")
    assert "se juega 1,00 $ para ganar 2,00 $" in paso["notes"]


def test_una_ganada_suma_el_riesgo_por_el_ratio(drawn: dict) -> None:
    paso = _step(drawn, "cuenta-ganada")

    assert _cuenta(paso)["summary"] == "52,00 $ · riesgo 1,00 $ · 1 operación · +2,0 R"
    assert "capital 52,00 $ (partía de 50,00 $) · +2,00 $ (+4,0 %)" in paso["notes"]
    assert "acierto 100,0 %" in paso["notes"]
    # La caja se cobra y se va: dejarla puesta invita a apuntarla dos veces.
    assert not paso["plot"]["sim"]
    assert all(_cuenta(paso)["resultsDisabled"])


def test_el_riesgo_es_del_capital_de_partida_y_no_compone(drawn: dict) -> None:
    paso = _step(drawn, "cuenta-perdida")

    assert _cuenta(paso)["summary"] == "51,00 $ · riesgo 1,00 $ · 2 operaciones · +1,0 R"
    assert "riesgo 2,0 % de 50,00 $ puestos = 1,00 $ en la siguiente operación" in paso["notes"]
    assert "caída máxima 1,00 $ (1,9 %)" in paso["notes"]


def test_el_break_even_no_mueve_el_saldo_pero_cuenta_como_operacion(drawn: dict) -> None:
    paso = _step(drawn, "cuenta-break-even")

    assert _cuenta(paso)["summary"].startswith("51,00 $ · riesgo 1,00 $ · 3 operaciones")
    assert "acierto 50,0 % (el break-even no cuenta)" in paso["notes"]


def test_deshacer_devuelve_el_saldo_y_tambien_la_caja(drawn: dict) -> None:
    paso = _step(drawn, "cuenta-deshecha")

    assert _cuenta(paso)["summary"].startswith("51,00 $ · riesgo 1,00 $ · 2 operaciones")
    assert _niveles(paso) == _niveles(_step(drawn, "cuenta-con-caja"))


def test_copiar_se_lleva_la_configuracion_el_historial_y_las_estadisticas(drawn: dict) -> None:
    paso = _step(drawn, "cuenta-copiada")
    texto = paso["copiado"]

    assert "partía de 50,00 $ · riesgo 2,0 % de 50,00 $ puestos = 1,00 $" in texto
    lineas = [linea for linea in texto.splitlines() if linea.startswith(("1 ", "2 "))]
    assert len(lineas) == 2
    assert "LARGO" in lineas[0] and "GANADA" in lineas[0] and "52,00 $" in lineas[0]
    assert "PERDIDA" in lineas[1] and "51,00 $" in lineas[1]
    assert "el motor no ve estas operaciones" in texto
    assert "historial copiado al portapapeles (2 operaciones)" in paso["notes"]


def test_cambiar_el_capital_no_toca_lo_apuntado_solo_la_siguiente_apuesta(drawn: dict) -> None:
    paso = _step(drawn, "cuenta-capital-100")

    assert _cuenta(paso)["initial"] == "100"
    assert _cuenta(paso)["summary"] == "51,00 $ · riesgo 2,00 $ · 2 operaciones · +1,0 R"


def test_el_riesgo_en_dolares_fijos_no_compone(drawn: dict) -> None:
    paso = _step(drawn, "cuenta-riesgo-fijo")

    assert _cuenta(paso)["mode"] == "cash"
    assert _cuenta(paso)["summary"] == "51,00 $ · riesgo 5,00 $ · 2 operaciones · +1,0 R"


def test_la_apuesta_nueva_solo_cobra_desde_ese_momento(drawn: dict) -> None:
    paso = _step(drawn, "cuenta-riesgo-nuevo")

    assert _cuenta(paso)["summary"] == "61,00 $ · riesgo 5,00 $ · 3 operaciones · +3,0 R"
    filas = [
        linea for linea in paso["copiado"].splitlines()
        if linea.startswith(("1 ", "2 ", "3 "))
    ]
    assert len(filas) == 3
    assert "5,00 $" in filas[2] and "+10,00 $" in filas[2] and "61,00 $" in filas[2]


def test_un_riesgo_imposible_no_se_acepta(drawn: dict) -> None:
    paso = _step(drawn, "cuenta-riesgo-invalido")

    assert _cuenta(paso)["risk"] == "5"
    assert _cuenta(paso)["summary"].startswith("61,00 $ · riesgo 5,00 $")


def test_reiniciar_borra_las_operaciones(drawn: dict) -> None:
    paso = _step(drawn, "cuenta-reiniciada")

    assert _cuenta(paso)["summary"] == "100,00 $ · riesgo 5,00 $ · 0 operaciones"
    assert _cuenta(paso)["undoDisabled"] and _cuenta(paso)["resetDisabled"]


def test_las_notas_dicen_que_la_cuenta_no_es_dinero(drawn: dict) -> None:
    notas = _step(drawn, "cuenta-ganada")["notes"]

    assert "NO ES DINERO" in notas
    assert "el motor no ve nada de esto" in notas


# --- Los recuadros a mano --------------------------------------------------------


def _recuadros(step: dict) -> list[dict]:
    return step["plot"]["rect"]


def _alto(recuadro: dict) -> float:
    return recuadro["y1"] - recuadro["y0"]


def test_hay_un_boton_por_nombre_de_la_configuracion(drawn: dict) -> None:
    assert drawn["rectLabels"] == ["Zona 1", "Zona 2", "Zona 3"]


def test_el_boton_del_recuadro_arma_y_lo_dice_antes_de_plantar_nada(drawn: dict) -> None:
    armado = _step(drawn, "rect-armado")

    assert armado["rectArmed"] == "Zona 1"
    assert not _recuadros(armado), "armar no puede dibujar todavía ningún recuadro"
    assert "RECUADRO DE Zona 1 ARMADO" in armado["notes"]
    assert armado["simCursor"] == "crosshair"


def test_escape_desarma_el_recuadro_sin_plantarlo(drawn: dict) -> None:
    desarmado = _step(drawn, "rect-desarmado")

    assert desarmado["rectArmed"] is None
    assert not _recuadros(desarmado)
    assert "ARMADO" not in desarmado["notes"]


def test_el_recuadro_se_planta_centrado_en_el_precio_del_clic(drawn: dict) -> None:
    recuadros = _recuadros(_step(drawn, "rect-plantado"))

    assert len(recuadros) == 1
    recuadro = recuadros[0]
    assert (recuadro["y0"] + recuadro["y1"]) / 2 == pytest.approx(1.2000, abs=1e-3)
    assert recuadro["x1"] > recuadro["x0"]
    assert recuadro["label"] == "Zona 1 1 (a mano)"
    assert recuadro["dash"] == "dot", "los recuadros a mano van punteados"
    assert recuadro["color"] == HAND_COLORS[0]


def test_arrastrar_el_techo_no_mueve_el_suelo(drawn: dict) -> None:
    antes = _recuadros(_step(drawn, "rect-plantado"))[0]
    despues = _recuadros(_step(drawn, "rect-techo-arrastrado"))[0]

    assert despues["y1"] > antes["y1"], "el arrastre iba hacia arriba"
    assert despues["y0"] == antes["y0"], "el suelo se queda donde estaba"
    assert (despues["x0"], despues["x1"]) == (antes["x0"], antes["x1"])


def test_arrastrar_por_dentro_mueve_el_recuadro_entero(drawn: dict) -> None:
    antes = _recuadros(_step(drawn, "rect-techo-arrastrado"))[0]
    despues = _recuadros(_step(drawn, "rect-movido"))[0]

    assert despues["y1"] < antes["y1"] and despues["y0"] < antes["y0"], "bajó entero"
    assert _alto(despues) == pytest.approx(_alto(antes), abs=1e-3)
    assert despues["x0"] > antes["x0"] and despues["x1"] > antes["x1"]


def test_cada_nombre_lleva_su_color(drawn: dict) -> None:
    segundo = _step(drawn, "rect-segundo")
    recuadros = _recuadros(segundo)

    assert [recuadro["label"] for recuadro in recuadros] == [
        "Zona 1 1 (a mano)",
        "Zona 2 1 (a mano)",
    ]
    assert [recuadro["color"] for recuadro in recuadros] == list(HAND_COLORS[:2])
    assert "recuadros marcados a mano: 2 (1 de Zona 1 · 1 de Zona 2)" in segundo["notes"]


def test_los_recuadros_se_numeran_por_nombre(drawn: dict) -> None:
    tercero = _step(drawn, "rect-tercero")

    assert [recuadro["label"] for recuadro in _recuadros(tercero)] == [
        "Zona 1 1 (a mano)",
        "Zona 2 1 (a mano)",
        "Zona 1 2 (a mano)",
    ]


def test_quitar_el_ultimo_deja_los_demas(drawn: dict) -> None:
    deshecho = _step(drawn, "rect-deshecho")

    assert len(_recuadros(deshecho)) == 2
    assert not deshecho["rectUndoDisabled"]


def test_quitar_todos_borra_los_recuadros_y_lo_que_decian(drawn: dict) -> None:
    limpio = _step(drawn, "rect-limpio")

    assert not _recuadros(limpio)
    assert limpio["rectUndoDisabled"] and limpio["rectClearDisabled"]
    assert "recuadros marcados a mano" not in limpio["notes"]


def test_las_notas_dicen_que_el_recuadro_lo_ha_puesto_una_mano(drawn: dict) -> None:
    notas = _step(drawn, "rect-plantado")["notes"]

    assert "recuadros marcados a mano: 1 (1 de Zona 1)" in notas
    assert "NO los ha detectado ningún cálculo" in notas


def test_sin_recuadros_los_botones_de_quitar_estan_apagados(drawn: dict) -> None:
    vacio = _step(drawn, "rect-sin-nada")

    assert vacio["rectUndoDisabled"] and vacio["rectClearDisabled"]
    assert vacio["rectArmed"] is None

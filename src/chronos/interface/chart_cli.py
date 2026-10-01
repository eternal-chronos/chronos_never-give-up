"""CLI del explorador de velas.

Punto de composición: aquí se juntan configuración, carga de bid/ask,
verificación de zona horaria, agregación M1 → M5/M15/H1/H4/D y el HTML.

No hay ningún comando de estrategia porque todavía no hay estrategia. Cuando la
haya, sus comandos van en su propio módulo y éste sigue haciendo lo que hace:
enseñar las velas.

También es donde se calculan las velas Heikin Ashi y la única capa del
explorador —las SMMA del setup 1, sobre cada tipo de vela— y se le pasan
hechas: el dibujo no puede importar indicadores.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import pandas as pd
import typer
from rich.console import Console
from rich.table import Table

from chronos.application.chart.config import TIMEFRAME_LABELS, ExplorerConfig
from chronos.application.chart.timezone_audit import TimezoneAudit, audit_timezone
from chronos.domain.errors import DomainError
from chronos.domain.strategies.indicators import heikin_ashi, smma
from chronos.infrastructure.config.loader import ConfigError, load_explorer_config
from chronos.infrastructure.market.chart_run import ChartRun, build_chart_run
from chronos.infrastructure.market.loader import SidedHistory, load_history
from chronos.infrastructure.reporting.explorer import (
    HeikinAshiLayer,
    SmmaLayer,
    bar_counts,
    build_payload,
    payload_size,
    render_explorer,
)

chart_app = typer.Typer(
    help="Explorador de velas: dibuja el histórico y déjalo listo para marcar.",
    no_args_is_help=True,
)
console = Console()

DEFAULT_CONFIG = Path("config/explorer.yaml")

#: Nombre del fichero que se escribe dentro de `reporting.output_dir`.
EXPLORER_FILE = "explorador.html"

#: Setup 1, fase 1: longitud de las dos SMMA, la de los máximos y la de los
#: mínimos. Cuando el setup sea una estrategia, pasa a sus parámetros.
SMMA_PERIOD = 5


def smma_layer(frames: Mapping[str, pd.DataFrame], period: int = SMMA_PERIOD) -> SmmaLayer:
    """Las SMMA de máximos y de mínimos de cada temporalidad, sobre todas sus velas."""
    return SmmaLayer(
        period=period,
        high={tf: smma(frame["high"].to_numpy(dtype=float), period) for tf, frame in frames.items()},
        low={tf: smma(frame["low"].to_numpy(dtype=float), period) for tf, frame in frames.items()},
    )


def heikin_ashi_frames(frames: Mapping[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """Las velas Heikin Ashi de cada temporalidad, calculadas sobre sus velas normales."""
    result: dict[str, pd.DataFrame] = {}
    for timeframe, frame in frames.items():
        ha_open, ha_high, ha_low, ha_close = heikin_ashi(
            *(frame[column].to_numpy(dtype=float) for column in ("open", "high", "low", "close"))
        )
        result[timeframe] = pd.DataFrame(
            {"open": ha_open, "high": ha_high, "low": ha_low, "close": ha_close},
            index=frame.index,
        )
    return result


def explorer_layers(run: ChartRun) -> tuple[SmmaLayer, HeikinAshiLayer]:
    """Lo calculado que dibuja el explorador: las SMMA de las velas normales, y las
    velas Heikin Ashi con sus propias SMMA."""
    candles = heikin_ashi_frames(run.frames)
    return smma_layer(run.frames), HeikinAshiLayer(frames=candles, smma=smma_layer(candles))


@chart_app.command("verify-tz")
def verify_timezone(
    config: Annotated[Path, typer.Option("--config", "-c")] = DEFAULT_CONFIG,
) -> None:
    """Verifica empíricamente la zona horaria del histórico.

    Obligatoria **antes de mirar nada**. Un offset horario equivocado no produce
    ningún error visible: produce velas H4 y diarias desplazadas, y por tanto un
    gráfico distinto al que se ve en la plataforma.

    Dos hechos que no dependen de ninguna convención del bróker: el hueco
    semanal debe caer sábado y domingo UTC, y el rango medio por minuto debe
    tener un pico marcado hacia las 13:30 UTC.
    """
    with _handled():
        run_config = load_explorer_config(config)
        history = load_history(run_config.data, run_config.price_side)
        audit = audit_timezone(history.frame, run_config.timezone_audit)
        _print_audit(audit, history)
        if not audit.ok:
            raise typer.Exit(code=1)


@chart_app.command("info")
def info(
    config: Annotated[Path, typer.Option("--config", "-c")] = DEFAULT_CONFIG,
) -> None:
    """Qué hay cargado: temporalidades, velas y tramo de cada una."""
    with _handled():
        run = build_chart_run(load_explorer_config(config))
        _print_summary(run)


@chart_app.command("explorer")
def explorer(
    config: Annotated[Path, typer.Option("--config", "-c")] = DEFAULT_CONFIG,
    out: Annotated[
        Path | None,
        typer.Option("--out", "-o", help="Fichero de salida; por defecto, el de la configuración"),
    ] = None,
    skip_tz_audit: Annotated[
        bool,
        typer.Option(
            "--skip-tz-audit",
            help="Dibuja aunque la verificación de zona horaria falle. El HTML no será auditable.",
        ),
    ] = False,
) -> None:
    """Escribe el explorador: un HTML autocontenido con todas las temporalidades dentro."""
    with _handled():
        run_config = load_explorer_config(config)
        run = build_chart_run(run_config)
        _print_summary(run)
        _audit_or_stop(run, run_config, skip_tz_audit)

        destination = out or (Path(run_config.reporting.output_dir) / EXPLORER_FILE)
        destination.parent.mkdir(parents=True, exist_ok=True)
        smma_standard, candles_ha = explorer_layers(run)
        html = render_explorer(run, smma=smma_standard, heikin_ashi=candles_ha)
        destination.write_text(html, encoding="utf-8")

        payload = build_payload(run, smma_standard, candles_ha)
        velas = sum(bar_counts(payload).values())
        console.print(
            f"\n[green]OK[/green] explorador → {destination}\n"
            f"[dim]{len(html) / 1_048_576:.1f} MB de fichero · "
            f"{payload_size(payload) / 1_048_576:.1f} MB de datos embebidos · "
            f"{velas:,} velas[/dim]"
        )
        console.print(
            "[dim]Ábrelo con doble clic. Funciona sin conexión y sin servidor.[/dim]"
        )


# --- Impresión ---------------------------------------------------------------


def _print_summary(run: ChartRun) -> None:
    table = Table(box=None, pad_edge=False)
    table.add_column("Gráfico", style="dim")
    table.add_column("Velas", justify="right")
    table.add_column("Desde → hasta")
    for timeframe, frame in run.frames.items():
        index = frame.index
        table.add_row(
            TIMEFRAME_LABELS.get(timeframe, timeframe),
            f"{len(frame):,}",
            f"{index[0]} → {index[-1]}",
        )
    console.print(
        f"\n[bold]{run.config.symbol}[/bold] · lado {run.history.side} · "
        f"{run.history.provenance}"
    )
    console.print(table)
    console.print(
        f"[dim]Día desde {run.config.aggregation.describe_daily_start()}[/dim]"
    )
    for note in run.skipped:
        console.print(f"[yellow]Aviso:[/yellow] no se dibuja {note}")


def _audit_or_stop(run: ChartRun, config: ExplorerConfig, skip: bool) -> None:
    if not config.timezone_audit.enabled:
        console.print(
            "[yellow]Verificación de zona horaria desactivada por configuración. "
            "Un offset equivocado no da error: da velas desplazadas.[/yellow]"
        )
        return

    audit = audit_timezone(run.history.frame, config.timezone_audit)
    _print_audit(audit, run.history)
    if audit.ok:
        return

    if not skip:
        console.print(
            "\n[bold red]NO SE DIBUJA.[/bold red] La verificación de zona horaria ha "
            "fallado.\nUn offset horario equivocado no produce un error visible: produce "
            "velas H4 y diarias\ndesplazadas, y por tanto un gráfico distinto al de tu "
            "plataforma. Corrige el histórico\nantes de seguir, o repite con "
            "--skip-tz-audit si sabes lo que estás mirando."
        )
        raise typer.Exit(code=1)

    console.print(
        "\n[bold yellow]AVISO:[/bold yellow] se continúa con --skip-tz-audit pese al fallo. "
        "Las velas de este explorador NO son auditables contra la plataforma."
    )


def _print_audit(audit: TimezoneAudit, history: SidedHistory) -> None:
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column(style="dim")
    table.add_column(justify="right")
    table.add_row("Origen", history.provenance)
    table.add_row("Lado del precio", history.side)
    table.add_row(
        f"Barras del histórico ({audit.resolution_minutes} min)", f"{audit.bars:,}"
    )
    table.add_row("Rango", f"{audit.first_bar} → {audit.last_bar}")
    table.add_row("Paradas > 12 h", f"{audit.gaps_found:,}")
    table.add_row(
        "Empiezan viernes / terminan domingo",
        f"{audit.gaps_starting_friday:,} / {audit.gaps_ending_sunday:,}",
    )
    table.add_row(
        "Pico de volatilidad",
        f"{audit.peak_minute_utc} UTC (esperado {audit.expected_peak_utc}, "
        f"desviación {audit.peak_offset_minutes} min)",
    )
    console.print()
    console.print(table)
    if audit.sample_gaps:
        gap = audit.sample_gaps[0]
        console.print(
            f"[dim]Ejemplo de parada: último {gap.last_before} → primero {gap.first_after}[/dim]"
        )
    verdict = "[green]OK[/green]" if audit.ok else "[bold red]FALLO[/bold red]"
    console.print(f"Verificación de zona horaria: {verdict}")
    for problem in audit.problems:
        console.print(f"  [red]·[/red] {problem}")


@contextmanager
def _handled() -> Iterator[None]:
    """Traduce los errores del dominio y de configuración en salida limpia."""
    try:
        yield
    except (ConfigError, DomainError) as error:
        console.print(f"[bold red]Error:[/bold red] {error}")
        raise typer.Exit(code=1) from error

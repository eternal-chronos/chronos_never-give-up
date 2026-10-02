"""CLI del cBot de cTrader: empaqueta una estrategia para la nube de cTrader."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from chronos.domain.errors import DomainError
from chronos.infrastructure.ctrader.bundle import CORE_MODULE, default_cbot_name, write_cbot

ctrader_app = typer.Typer(
    help="cBot de cTrader: la misma estrategia del backtest, empaquetada para la nube de cTrader.",
    no_args_is_help=True,
)
console = Console()


@ctrader_app.command("build")
def build(
    strategy: Annotated[str, typer.Option("--strategy", "-s")] = "ema_cross",
    name: Annotated[str | None, typer.Option("--name", "-n", help="Nombre del cBot")] = None,
    output: Annotated[Path, typer.Option("--output", "-o")] = Path("dist/ctrader"),
) -> None:
    """Genera los ficheros del cBot de Python para cTrader."""
    cbot = name or default_cbot_name(strategy)
    try:
        files = write_cbot(output, strategy, cbot)
    except DomainError as error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=1) from error

    for path in files:
        console.print(f"[green]escrito[/green] {path}")
    console.print(
        "\nEn cTrader (Windows 5.4+ o Mac 5.7+):\n"
        f"  1. New cBot → lenguaje Python → nombre [bold]{cbot}[/bold].\n"
        f"  2. Copia los tres ficheros sobre los de su carpeta: {CORE_MODULE}.py, "
        f"{cbot}_main.py y {cbot}.cs.\n"
        "  3. Build. Pruébalo primero en una instancia de la nube con cuenta demo y mira\n"
        "     el log: la primera línea dice la versión de Python y cuántas velas calentó.\n"
        "  4. La temporalidad del gráfico es la de la estrategia (M5)."
    )

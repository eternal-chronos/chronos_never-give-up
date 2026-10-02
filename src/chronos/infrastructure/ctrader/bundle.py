"""Empaqueta una estrategia como cBot de Python para cTrader.

cTrader carga los `.py` de un cBot como módulos PLANOS, por su nombre de fichero
(`robot_wrapper.py` → `import robot_wrapper`): no hay paquetes, así que
`from chronos.domain.signal import ...` no se puede resolver allí dentro. Y en su
nube no se instala nada: sólo hay biblioteca estándar.

Por eso el cBot lleva el núcleo en un único `chronos_core.py`: `domain/` entero y
el `runner`, en orden de dependencias y sin los imports internos. Al generarlo se
comprueba que no queda nada fuera de la biblioteca estándar, que dos módulos no
definen el mismo nombre y que la sintaxis es de Python 3.11.

Por cBot se escriben tres ficheros, que se copian sobre el proyecto que crea
cTrader al hacer «New cBot» en Python con el mismo nombre:

    chronos_core.py       el núcleo (generado: no se edita a mano)
    <Nombre>_main.py      la clase que instancia cTrader; delega en el runner
    <Nombre>.cs           parámetros del cBot y atributos del Robot (UTC)
"""

from __future__ import annotations

import ast
import inspect
import json
import re
import sys
import typing
from dataclasses import dataclass
from pathlib import Path

import chronos
from chronos.domain.errors import StrategyError
from chronos.domain.strategies.registry import strategy_class
from chronos.domain.strategy import Strategy

#: Nombre del módulo del núcleo dentro del cBot.
CORE_MODULE = "chronos_core"

#: Python mínimo del núcleo: `enum.StrEnum` y `datetime.UTC` son de la 3.11.
MIN_PYTHON = (3, 11)

_PACKAGE = Path(chronos.__file__).resolve().parent
_RUNNER = _PACKAGE / "infrastructure" / "ctrader" / "runner.py"

#: Parámetros del runner en el `.cs`. Los de la estrategia no pueden llamarse igual.
_RUNNER_PARAMETERS = ("Label", "FixedLots", "RiskPerTradePercent", "MaxLots", "MaxPositions")

#: Miembros de `Robot` en cTrader que un parámetro taparía.
_ROBOT_MEMBERS = frozenset(
    {
        "Account", "Application", "Bars", "Chart", "History", "Indicators", "IsBacktesting",
        "MarketData", "Notifications", "PendingOrders", "Positions", "RunningMode", "Server",
        "Symbol", "SymbolName", "Symbols", "Time", "TimeFrame", "Timer",
    }
)

_CSHARP_TYPES: dict[type, str] = {bool: "bool", int: "int", float: "double", str: "string"}


class BundleError(StrategyError):
    """El núcleo o el cBot no se pueden generar tal como están."""


# --- El núcleo -------------------------------------------------------------------


def core_sources() -> list[Path]:
    """Ficheros que entran en el núcleo, en orden de dependencias."""
    sources = [
        path for path in sorted((_PACKAGE / "domain").rglob("*.py")) if path.name != "__init__.py"
    ]
    sources.append(_RUNNER)
    by_module = {_module_name(path): path for path in sources}
    pending = {
        module: {dep for dep in _internal_imports(path) if dep != module}
        for module, path in by_module.items()
    }
    for module, deps in pending.items():
        unknown = deps - set(by_module)
        if unknown:
            raise BundleError(
                f"{module} importa {', '.join(sorted(unknown))}, que no viaja al cBot"
            )

    ordered: list[Path] = []
    while pending:
        ready = sorted(module for module, deps in pending.items() if not deps)
        if not ready:
            raise BundleError(f"Imports circulares entre {', '.join(sorted(pending))}")
        for module in ready:
            ordered.append(by_module[module])
            del pending[module]
        for deps in pending.values():
            deps.difference_update(ready)
    return ordered


def build_core() -> str:
    """El texto de `chronos_core.py`."""
    parts = [
        f'"""{CORE_MODULE}: el núcleo de chronos para el cBot de cTrader.\n\n'
        "GENERADO con `chronos ctrader build`: no se edita a mano. Es `domain/` y\n"
        "`infrastructure/ctrader/runner.py` en un solo módulo, porque cTrader no carga\n"
        'paquetes. Sólo biblioteca estándar.\n"""\n',
        "from __future__ import annotations\n",
        "import sys\n",
        f"if sys.version_info < {MIN_PYTHON!r}:\n"
        "    raise RuntimeError(\n"
        f"        \"{CORE_MODULE} necesita Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} o superior; \"\n"
        '        "este cTrader trae " + sys.version.split()[0]\n'
        "    )\n",
    ]
    owners: dict[str, str] = {}
    for path in core_sources():
        relative = path.relative_to(_PACKAGE.parent).as_posix()
        source = path.read_text(encoding="utf-8")
        _claim_names(ast.parse(source), relative, owners)
        parts.append(f"\n# {'=' * 76}\n# {relative}\n# {'=' * 76}\n")
        parts.append(_strip_internal_imports(source))

    core = "\n".join(parts)
    _check_core(core)
    return core


def _module_name(path: Path) -> str:
    return ".".join(path.relative_to(_PACKAGE.parent).with_suffix("").parts)


def _internal_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("chronos"):
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names if alias.name.startswith("chronos"))
    return found


def _strip_internal_imports(source: str) -> str:
    """Quita `from __future__` y los imports de chronos: en el núcleo todo es un módulo."""
    tree = ast.parse(source)
    drop: set[int] = set()
    for node in tree.body:
        internal = (
            isinstance(node, ast.ImportFrom)
            and node.module is not None
            and (node.module == "__future__" or node.module.startswith("chronos"))
        ) or (
            isinstance(node, ast.Import)
            and any(alias.name.startswith("chronos") for alias in node.names)
        )
        if internal:
            end = node.end_lineno or node.lineno
            drop.update(range(node.lineno, end + 1))
    lines = source.splitlines()
    return "\n".join(line for number, line in enumerate(lines, start=1) if number not in drop) + "\n"


def _claim_names(tree: ast.Module, relative: str, owners: dict[str, str]) -> None:
    """Dos módulos no pueden definir el mismo nombre: en el núcleo se pisarían.

    Importar lo mismo de la biblioteca estándar en varios módulos sí vale.
    """
    for node in tree.body:
        claims: list[tuple[str, str]] = []
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            claims.append((node.name, f"definido en {relative}"))
        elif isinstance(node, ast.Assign):
            claims.extend(
                (target.id, f"definido en {relative}")
                for target in node.targets
                if isinstance(target, ast.Name)
            )
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            claims.append((node.target.id, f"definido en {relative}"))
        elif isinstance(node, ast.Import):
            claims.extend(
                ((alias.asname or alias.name).split(".")[0], f"import {alias.name}")
                for alias in node.names
                if not alias.name.startswith("chronos")
            )
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module != "__future__"
            and not node.module.startswith("chronos")
        ):
            claims.extend(
                (alias.asname or alias.name, f"from {node.module} import {alias.name}")
                for alias in node.names
            )
        for name, origin in claims:
            previous = owners.setdefault(name, origin)
            if previous != origin:
                raise BundleError(f"'{name}' choca en el núcleo: {previous} y {origin}")


def _check_core(core: str) -> None:
    """Sintaxis de Python 3.11 y nada fuera de la biblioteca estándar."""
    try:
        tree = ast.parse(core, feature_version=MIN_PYTHON)
    except SyntaxError as error:
        raise BundleError(f"El núcleo no es Python {MIN_PYTHON}: {error}") from error
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [node.module]
        for name in names:
            root = name.split(".")[0]
            if root != "__future__" and root not in sys.stdlib_module_names:
                raise BundleError(f"El núcleo importa '{name}': en la nube de cTrader no existe")


# --- El cBot ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CbotParameter:
    """Un parámetro de la estrategia expuesto en cTrader como `[Parameter]`."""

    name: str  # el de Python: fast_period
    property: str  # el de C#: FastPeriod
    python_type: type
    default: bool | int | float | str


def strategy_parameters(cls: type[Strategy]) -> list[CbotParameter]:
    """Los parámetros del constructor de la estrategia, con su tipo y su valor por defecto."""
    hints = typing.get_type_hints(cls.__init__)
    parameters: list[CbotParameter] = []
    for parameter in inspect.signature(cls.__init__).parameters.values():
        if parameter.name == "self" or parameter.kind in (
            inspect.Parameter.VAR_KEYWORD,
            inspect.Parameter.VAR_POSITIONAL,
        ):
            continue
        python_type = hints.get(parameter.name)
        if python_type not in _CSHARP_TYPES:
            raise BundleError(
                f"El parámetro '{parameter.name}' es {python_type}: en cTrader sólo hay "
                "bool, int, float y str"
            )
        if parameter.default is inspect.Parameter.empty:
            raise BundleError(f"El parámetro '{parameter.name}' necesita un valor por defecto")
        name = _pascal(parameter.name)
        if name in _RUNNER_PARAMETERS or name in _ROBOT_MEMBERS:
            raise BundleError(
                f"El parámetro '{parameter.name}' se llamaría {name} en cTrader, que ya existe"
            )
        parameters.append(CbotParameter(parameter.name, name, python_type, parameter.default))
    return parameters


def default_cbot_name(strategy_name: str) -> str:
    return "Chronos" + _pascal(strategy_name)


def render_main(cbot_name: str, cls: type[Strategy], parameters: list[CbotParameter]) -> str:
    kwargs = "\n".join(
        f"            {p.name}={p.python_type.__name__}(api.{p.property})," for p in parameters
    )
    return f'''"""cBot {cbot_name}: estrategia `{cls.name}` de chronos.

GENERADO con `chronos ctrader build`: no se edita a mano. La lógica está en
{CORE_MODULE}.py; esto sólo lee los parámetros del cBot y delega en el runner.
"""

import clr

clr.AddReference("cAlgo.API")

from cAlgo.API import *

from {CORE_MODULE} import CTraderRunner, RunnerSettings, {cls.__name__}


class {cbot_name}():
    def on_start(self):
        strategy = {cls.__name__}(
{kwargs}
        )
        settings = RunnerSettings(
            label=str(api.Label),
            fixed_lots=float(api.FixedLots),
            risk_per_trade=float(api.RiskPerTradePercent) / 100.0,
            max_lots=float(api.MaxLots),
            max_positions=int(api.MaxPositions),
        )
        self.runner = CTraderRunner(api, TradeType, strategy, settings)
        self.runner.start()

    def on_bar_closed(self):
        self.runner.on_bar_closed()

    def on_stop(self):
        self.runner.stop()
'''


def render_cs(cbot_name: str, cls: type[Strategy], parameters: list[CbotParameter]) -> str:
    strategy_block = "\n\n".join(
        f'    [Parameter("{p.name}", Group = "{cls.name}", DefaultValue = {_csharp(p.default)})]\n'
        f"    public {_CSHARP_TYPES[p.python_type]} {p.property} {{ get; set; }}"
        for p in parameters
    )
    return f"""// GENERADO con `chronos ctrader build`: no se edita a mano.
// Parámetros del cBot {cbot_name} (estrategia `{cls.name}` de chronos). La lógica
// está en {CORE_MODULE}.py. TimeZone UTC: el runner lee las velas en UTC.
using System;
using cAlgo.API;

namespace cAlgo.Robots;

[Robot(AccessRights = AccessRights.None, TimeZone = TimeZones.UTC)]
public partial class {cbot_name} : Robot
{{
    [Parameter("Label", Group = "Chronos", DefaultValue = "chronos-{cls.name}")]
    public string Label {{ get; set; }}

    [Parameter("Fixed lots", Group = "Sizing", DefaultValue = 0.01, MinValue = 0.01, Step = 0.01)]
    public double FixedLots {{ get; set; }}

    [Parameter("Risk per trade (%)", Group = "Sizing", DefaultValue = 0, MinValue = 0, MaxValue = 10, Step = 0.1)]
    public double RiskPerTradePercent {{ get; set; }}

    [Parameter("Max lots", Group = "Sizing", DefaultValue = 1, MinValue = 0.01, Step = 0.01)]
    public double MaxLots {{ get; set; }}

    [Parameter("Max positions", Group = "Sizing", DefaultValue = 1, MinValue = 1)]
    public int MaxPositions {{ get; set; }}

{strategy_block}
}}
"""


def write_cbot(output_dir: Path, strategy_name: str, cbot_name: str | None = None) -> list[Path]:
    """Escribe los tres ficheros del cBot en `output_dir/<Nombre>/`."""
    cls = strategy_class(strategy_name)
    name = cbot_name or default_cbot_name(strategy_name)
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", name):
        raise BundleError(
            f"'{name}' no vale como nombre de cBot: sólo letras y números, sin espacios, "
            "porque es también el nombre de la clase en C# y en Python"
        )
    parameters = strategy_parameters(cls)
    folder = output_dir / name
    folder.mkdir(parents=True, exist_ok=True)
    files = {
        folder / f"{CORE_MODULE}.py": build_core(),
        folder / f"{name}_main.py": render_main(name, cls, parameters),
        folder / f"{name}.cs": render_cs(name, cls, parameters),
    }
    for path, text in files.items():
        path.write_text(text, encoding="utf-8")
    return list(files)


def _pascal(snake: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in snake.split("_") if part)


def _csharp(value: bool | int | float | str) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value)
    return repr(value)

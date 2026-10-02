"""El cBot de cTrader: runner, empaquetado y el cBot generado corriendo solo.

El runner se prueba con un doble del `Robot` de cTrader (`ctrader_fakes`). Lo que
no se puede probar aquí es cTrader mismo —su puente de Python, su versión de
Python, sus órdenes reales—: eso se prueba en una instancia de demo en la nube.
"""

from __future__ import annotations

import ast
import json
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from typer.testing import CliRunner

from chronos.application.backtest.config import BacktestConfig, RiskConfig
from chronos.application.backtest.engine import BacktestEngine
from chronos.domain.context import BarContext
from chronos.domain.enums import ExitReason, Side
from chronos.domain.errors import DomainError, StrategyError
from chronos.domain.instrument import InstrumentSpec
from chronos.domain.position import Position as DomainPosition
from chronos.domain.signal import EntrySignal, ExitSignal, ModifyStops, StrategyAction
from chronos.domain.strategies.ema_cross import EmaCrossStrategy
from chronos.domain.strategy import Strategy
from chronos.domain.trade import Trade
from chronos.infrastructure.broker.simulated import build_simulated_broker
from chronos.infrastructure.ctrader.bundle import (
    CORE_MODULE,
    BundleError,
    _claim_names,
    build_core,
    render_cs,
    render_main,
    strategy_parameters,
    write_cbot,
)
from chronos.infrastructure.ctrader.runner import CTraderRunner, RunnerSettings
from chronos.interface.cli import app
from tests.conftest import make_frame
from tests.infrastructure import ctrader_fakes
from tests.infrastructure.ctrader_fakes import (
    NetDateTime,
    Position,
    Robot,
    TradeType,
    make_bars,
)

START = datetime(2024, 3, 4, 8, 0, tzinfo=UTC)
LABEL = "chronos-test"


class Scripted(Strategy):
    """Pide lo que se le diga en las barras indicadas y apunta lo que ve."""

    name = "scripted"

    def __init__(
        self, plan: dict[int, Sequence[StrategyAction]] | None = None, warmup: int = 0
    ) -> None:
        super().__init__()
        self.plan = plan or {}
        self.warmup = warmup
        self.seen: list[int] = []
        self.positions: list[tuple[DomainPosition, ...]] = []
        self.times: list[datetime] = []

    @property
    def warmup_bars(self) -> int:
        return self.warmup

    def on_bar(self, ctx: BarContext) -> Sequence[StrategyAction]:
        self.seen.append(ctx.index)
        self.positions.append(ctx.positions)
        self.times.append(ctx.now)
        return self.plan.get(ctx.index, ())


def _settings(**overrides: object) -> RunnerSettings:
    values: dict[str, object] = {"label": LABEL, "fixed_lots": 0.01}
    values.update(overrides)
    return RunnerSettings(**values)  # type: ignore[arg-type]


def _robot(history: int, **kwargs: object) -> Robot:
    """`history` velas cerradas y una en formación."""
    closes = [2000.0 + i for i in range(history + 1)]
    return Robot(bars=make_bars(closes, START), **kwargs)  # type: ignore[arg-type]


def _close(robot: Robot, runner: CTraderRunner, bars: int = 1, close: float = 2000.0) -> None:
    """Cierra `bars` velas seguidas y avisa al runner de cada una, como cTrader."""
    for _ in range(bars):
        robot.close_next(close)
        runner.on_bar_closed()


# --- Arranque ---------------------------------------------------------------------


def test_arranca_calentando_con_el_historico_y_sin_operar() -> None:
    robot = _robot(history=10)
    always = EntrySignal(side=Side.BUY, stop_distance=5.0)
    strategy = Scripted(plan=dict.fromkeys(range(20), (always,)))
    CTraderRunner(robot, TradeType, strategy, _settings()).start()

    assert strategy.seen == list(range(10))  # la vela en formación no se ve
    assert robot.orders == []  # el histórico es pasado: no se opera sobre él
    assert any("10 velas de histórico" in line for line in robot.printed)
    assert strategy.times[0] == START  # la fecha de .NET llega como datetime UTC


def test_las_fechas_valen_tambien_si_llegan_como_datetime() -> None:
    robot = Robot(bars=make_bars([2000.0, 2001.0, 2002.0], START, net_dates=False))
    strategy = Scripted()
    CTraderRunner(robot, TradeType, strategy, _settings()).start()
    assert strategy.times == [START, START.replace(minute=5)]


def test_no_arranca_si_la_estrategia_necesita_on_trade_closed() -> None:
    class Cooldown(Scripted):
        def on_trade_closed(self, trade: Trade) -> None:
            pass

    with pytest.raises(StrategyError, match="on_trade_closed"):
        CTraderRunner(_robot(history=3), TradeType, Cooldown(), _settings())


def test_ajustes_invalidos() -> None:
    with pytest.raises(DomainError):
        _settings(label=" ")
    with pytest.raises(DomainError):
        _settings(risk_per_trade=1.0)
    with pytest.raises(DomainError):
        _settings(max_positions=0)


# --- Velas en vivo ---------------------------------------------------------------------


def test_al_cerrar_la_vela_manda_la_orden_con_sl_y_tp_en_pips() -> None:
    robot = _robot(history=5)
    signal = EntrySignal(side=Side.BUY, stop_distance=5.0, take_profit_distance=10.0, tag="t")
    strategy = Scripted(plan={5: (signal,)})
    runner = CTraderRunner(robot, TradeType, strategy, _settings())
    runner.start()
    _close(robot, runner)

    assert strategy.seen[-1] == 5
    assert len(robot.orders) == 1
    order = robot.orders[0]
    assert order["trade_type"] == TradeType.Buy
    assert order["symbol"] == "XAUUSD"
    assert order["volume"] == 1.0  # 0.01 lotes de 100 onzas
    assert order["label"] == LABEL
    assert order["stop_loss_pips"] == pytest.approx(500.0)  # 5 USD / 0.01
    assert order["take_profit_pips"] == pytest.approx(1000.0)
    assert order["comment"] == "t"


def test_una_vela_ya_vista_no_se_vuelve_a_procesar() -> None:
    robot = _robot(history=3)
    strategy = Scripted()
    runner = CTraderRunner(robot, TradeType, strategy, _settings())
    runner.start()
    _close(robot, runner)
    runner.on_bar_closed()  # mismo evento repetido, sin vela nueva
    assert strategy.seen == [0, 1, 2, 3]


def test_el_calentamiento_espera_aunque_el_historico_no_llegue() -> None:
    robot = _robot(history=2)
    entry = (EntrySignal(side=Side.BUY, stop_distance=5.0),)
    strategy = Scripted(plan=dict.fromkeys(range(10), entry), warmup=4)
    runner = CTraderRunner(robot, TradeType, strategy, _settings(max_positions=10))
    runner.start()
    _close(robot, runner, bars=3)  # velas 2, 3 y 4

    assert strategy.seen == [0, 1, 2, 3, 4]
    assert [order["bar"] for order in robot.orders] == [4]  # 2 y 3 aún calientan


def test_una_orden_rechazada_se_registra_y_no_rompe() -> None:
    robot = _robot(history=2, reject_orders=True)
    strategy = Scripted(plan={2: (EntrySignal(side=Side.SELL, stop_distance=5.0),)})
    runner = CTraderRunner(robot, TradeType, strategy, _settings())
    runner.start()
    _close(robot, runner)
    assert not robot.Positions
    assert any("rechazada" in line and "NoMoney" in line for line in robot.printed)


# --- Posiciones: las del bróker ------------------------------------------------------------


def _position(id_: int, label: str = LABEL, symbol: str = "XAUUSD", **kwargs: object) -> Position:
    values: dict[str, object] = {
        "Id": id_,
        "Label": label,
        "SymbolName": symbol,
        "TradeType": TradeType.Buy,
        "VolumeInUnits": 20.0,
        "EntryPrice": 2001.0,
        "EntryTime": NetDateTime(datetime(2024, 3, 4, 8, 6)),
        "StopLoss": 1995.0,
        "TakeProfit": 2010.0,
        "Comment": "ema_cross",
    }
    values.update(kwargs)
    return Position(**values)  # type: ignore[arg-type]


def test_la_estrategia_ve_las_posiciones_del_broker_con_su_etiqueta() -> None:
    robot = _robot(history=3)
    robot.Positions.extend(
        [_position(1), _position(2, label="manual"), _position(3, symbol="EURUSD")]
    )
    strategy = Scripted()
    runner = CTraderRunner(robot, TradeType, strategy, _settings())
    runner.start()
    assert strategy.positions[-1] == ()  # el calentamiento no ve posiciones de ahora
    _close(robot, runner)

    (seen,) = strategy.positions[-1]
    assert (seen.id, seen.side, seen.volume) == (1, Side.BUY, pytest.approx(0.2))
    assert (seen.entry_price, seen.stop_loss, seen.take_profit) == (2001.0, 1995.0, 2010.0)
    assert seen.tag == "ema_cross"
    assert seen.entry_index == 1  # abierta durante la vela de las 08:05


def test_un_tp_editado_a_mano_del_lado_de_la_perdida_no_rompe_el_cbot() -> None:
    robot = _robot(history=2)
    robot.Positions.append(_position(1, TakeProfit=1998.0))  # largo con TP bajo la entrada
    strategy = Scripted()
    runner = CTraderRunner(robot, TradeType, strategy, _settings())
    runner.start()
    _close(robot, runner)

    (seen,) = strategy.positions[-1]
    assert seen.take_profit is None
    assert any("TP del lado de la pérdida" in line for line in robot.printed)


def test_cerrar_solo_toca_las_posiciones_del_cbot() -> None:
    robot = _robot(history=2)
    robot.Positions.extend([_position(1), _position(2, label="manual")])
    strategy = Scripted(plan={2: (ExitSignal(reason=ExitReason.SIGNAL),)})
    runner = CTraderRunner(robot, TradeType, strategy, _settings())
    runner.start()
    _close(robot, runner)

    assert robot.closed == [1]
    assert [p.Id for p in robot.Positions] == [2]


def test_no_abre_mas_posiciones_que_las_permitidas() -> None:
    robot = _robot(history=2)
    robot.Positions.append(_position(1))
    strategy = Scripted(plan={2: (EntrySignal(side=Side.BUY, stop_distance=5.0),)})
    runner = CTraderRunner(robot, TradeType, strategy, _settings(max_positions=1))
    runner.start()
    _close(robot, runner)
    assert robot.orders == []


def test_modificar_stops_conserva_lo_que_no_se_toca_y_respeta_las_invariantes() -> None:
    robot = _robot(history=2)
    robot.Positions.append(_position(1))
    plan: dict[int, Sequence[StrategyAction]] = {
        2: (ModifyStops(stop_loss=1999.004),),  # sube el stop; el TP no se toca
        3: (ModifyStops(take_profit=1990.0),),  # TP de un largo bajo la entrada: no
    }
    runner = CTraderRunner(robot, TradeType, Scripted(plan=plan), _settings())
    runner.start()
    _close(robot, runner, bars=2)

    assert robot.modified == [(1, 1999.0, 2010.0)]  # redondeado al tick
    assert any("modificación de la posición 1 descartada" in line for line in robot.printed)


# --- Tamaño ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("settings", "signal", "units"),
    [
        # Lote fijo: 0.05 lotes = 5 onzas.
        ({"fixed_lots": 0.05}, EntrySignal(side=Side.BUY, stop_distance=5.0), 5.0),
        # Lotes en la señal: mandan sobre el lote fijo.
        ({}, EntrySignal(side=Side.BUY, volume=0.3, stop_distance=5.0), 30.0),
        # 1 % de 10 000 USD con el stop a 5 USD (500 pips de 0.01 USD por onza): 20 onzas.
        ({"risk_per_trade": 0.01}, EntrySignal(side=Side.BUY, stop_distance=5.0), 20.0),
        # El riesgo de la señal manda sobre el de los ajustes.
        (
            {"risk_per_trade": 0.01},
            EntrySignal(side=Side.BUY, risk_fraction=0.005, stop_distance=5.0),
            10.0,
        ),
        # Con tope de 0.1 lotes, 10 onzas aunque el riesgo pida 20.
        ({"risk_per_trade": 0.01, "max_lots": 0.1}, EntrySignal(side=Side.BUY, stop_distance=5.0), 10.0),
        # Redondeo hacia abajo al paso: 1 % con el stop a 7 USD son 14.28 onzas → 14.
        ({"risk_per_trade": 0.01}, EntrySignal(side=Side.BUY, stop_distance=7.0), 14.0),
    ],
)
def test_tamano_con_los_datos_del_broker(
    settings: dict[str, object], signal: EntrySignal, units: float
) -> None:
    robot = _robot(history=2)
    runner = CTraderRunner(robot, TradeType, Scripted(plan={2: (signal,)}), _settings(**settings))
    runner.start()
    _close(robot, runner)
    assert [order["volume"] for order in robot.orders] == [pytest.approx(units)]


def test_arriesgar_un_porcentaje_sin_stop_no_opera() -> None:
    robot = _robot(history=2)
    signal = EntrySignal(side=Side.BUY, risk_fraction=0.01)
    runner = CTraderRunner(robot, TradeType, Scripted(plan={2: (signal,)}), _settings())
    runner.start()
    _close(robot, runner)
    assert robot.orders == []


# --- Misma estrategia, mismas decisiones -----------------------------------------------------------


def test_ema_cross_decide_lo_mismo_en_el_cbot_que_en_el_backtest(
    no_cost_spec: InstrumentSpec, config: BacktestConfig
) -> None:
    """La estrategia de referencia sobre la misma serie: mismas entradas, misma vela.

    Stops muy lejanos para que sólo cierren las señales: el doble de cTrader no
    simula stops.
    """
    closes = (2000.0 + np.cumsum(np.random.default_rng(5).normal(0.0, 1.5, 400))).tolist()
    params = {"fast_period": 5, "slow_period": 13, "atr_period": 7, "sl_atr_mult": 200.0, "tp_atr_mult": 200.0}

    bars = make_bars(closes, START)
    frame = make_frame([(b.Open, b.High, b.Low, b.Close) for b in bars])
    risk = RiskConfig(sizing="fixed_lot", fixed_lot=0.01, max_daily_loss=None, max_drawdown_stop=None)
    config = replace(config, risk=risk)
    engine = BacktestEngine(no_cost_spec, config, build_simulated_broker(no_cost_spec, config))
    backtest = engine.run(frame, EmaCrossStrategy(**params))
    backtest_entries = [(t.entry_index - 1, t.side.value) for t in backtest.trades]

    robot = Robot(bars=[bars[0]])  # sin histórico: la vela 0 está en formación
    runner = CTraderRunner(robot, TradeType, EmaCrossStrategy(**params), _settings())
    runner.start()
    for bar in bars:
        robot.close_bar(bar)
        runner.on_bar_closed()
    cbot_entries = [
        (order["bar"], "BUY" if order["trade_type"] == TradeType.Buy else "SELL")
        for order in robot.orders
    ]

    assert len(backtest_entries) > 5
    # La última entrada del backtest puede ser la de la última vela, que el
    # motor ya no ejecuta: se compara lo que ambos ejecutaron.
    assert cbot_entries[: len(backtest_entries)] == backtest_entries


# --- Empaquetado -----------------------------------------------------------------------------------


def test_el_nucleo_es_python_311_de_biblioteca_estandar_y_sin_imports_internos() -> None:
    core = build_core()
    tree = ast.parse(core, feature_version=(3, 11))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith("chronos"), node.module
            assert node.module.split(".")[0] in sys.stdlib_module_names | {"__future__"}
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] in sys.stdlib_module_names, alias.name
    for name in ("CTraderRunner", "RunnerSettings", "EmaCrossStrategy", "Smma", "HeikinAshi"):
        assert f"class {name}" in core


def test_un_nombre_repetido_entre_modulos_no_se_empaqueta() -> None:
    owners: dict[str, str] = {}
    _claim_names(ast.parse("def helper():\n    pass\n"), "a.py", owners)
    with pytest.raises(BundleError, match="helper"):
        _claim_names(ast.parse("helper = 1\n"), "b.py", owners)
    # Importar lo mismo de la biblioteca estándar en varios módulos sí vale...
    _claim_names(ast.parse("from datetime import time\n"), "c.py", owners)
    _claim_names(ast.parse("from datetime import time\n"), "d.py", owners)
    # ...pero no dos cosas distintas con el mismo nombre.
    with pytest.raises(BundleError, match="time"):
        _claim_names(ast.parse("import time\n"), "e.py", owners)


def test_los_parametros_de_la_estrategia_llegan_al_cs_y_al_main() -> None:
    parameters = strategy_parameters(EmaCrossStrategy)
    cs = render_cs("ChronosEmaCross", EmaCrossStrategy, parameters)
    main = render_main("ChronosEmaCross", EmaCrossStrategy, parameters)

    assert "[Robot(AccessRights = AccessRights.None, TimeZone = TimeZones.UTC)]" in cs
    assert "public partial class ChronosEmaCross : Robot" in cs
    assert '[Parameter("fast_period", Group = "ema_cross", DefaultValue = 20)]' in cs
    assert "public int FastPeriod { get; set; }" in cs
    assert '[Parameter("sl_atr_mult", Group = "ema_cross", DefaultValue = 2.0)]' in cs
    assert "public double SlAtrMult { get; set; }" in cs
    assert "DefaultValue = true)]\n    public bool AllowShorts { get; set; }" in cs

    assert "class ChronosEmaCross():" in main
    assert "fast_period=int(api.FastPeriod)," in main
    assert "allow_shorts=bool(api.AllowShorts)," in main
    assert f"from {CORE_MODULE} import CTraderRunner, RunnerSettings, EmaCrossStrategy" in main


def test_un_parametro_que_tapa_un_miembro_del_robot_no_se_empaqueta() -> None:
    class Clash(Strategy):
        name = "clash"

        def __init__(self, symbol: str = "XAUUSD") -> None:
            super().__init__(symbol=symbol)

        def on_bar(self, ctx: BarContext) -> Sequence[StrategyAction]:
            return ()

    with pytest.raises(BundleError, match="Symbol"):
        strategy_parameters(Clash)


def test_el_comando_build_escribe_los_tres_ficheros(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["ctrader", "build", "--output", str(tmp_path)])
    assert result.exit_code == 0, result.output
    folder = tmp_path / "ChronosEmaCross"
    assert sorted(path.name for path in folder.iterdir()) == [
        "ChronosEmaCross.cs",
        "ChronosEmaCross_main.py",
        "chronos_core.py",
    ]


def test_el_comando_build_con_una_estrategia_desconocida_falla(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app, ["ctrader", "build", "--strategy", "no_existe", "--output", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "desconocida" in result.output


def test_el_nombre_del_cbot_no_admite_espacios(tmp_path: Path) -> None:
    with pytest.raises(BundleError, match="nombre de cBot"):
        write_cbot(tmp_path, "ema_cross", "Chronos Ema")


_DRIVER = '''
"""Hace de cTrader: el mismo importador en memoria de PythonHooks.cs, `api` en
builtins como EngineHelper.cs, y exec del _main.py como Engine.cs."""
import builtins
import importlib.abc
import importlib.util
import json
import sys
from pathlib import Path

here = Path(__file__).resolve().parent
sys.path.insert(0, str(here))

for forbidden in ("chronos", "numpy", "pandas"):
    try:
        __import__(forbidden)
    except ImportError:
        pass
    else:
        raise SystemExit(forbidden + " no debería poder importarse")


class InMemoryModuleLoader(importlib.abc.Loader):
    def __init__(self, module_code_map):
        self.module_code_map = module_code_map

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        exec(self.module_code_map.get(module.__name__), module.__dict__)


class InMemoryModuleFinder(importlib.abc.MetaPathFinder):
    def __init__(self, module_code_map):
        self.module_code_map = module_code_map

    def find_spec(self, fullname, path, target=None):
        if fullname in self.module_code_map:
            return importlib.util.spec_from_loader(fullname, InMemoryModuleLoader(self.module_code_map))
        return None


cbot = here / "cbot" / "ChronosEmaCross"
sys.meta_path.insert(0, InMemoryModuleFinder({"chronos_core": (cbot / "chronos_core.py").read_text()}))

from datetime import datetime, timezone
from ctrader_fakes import Robot, make_bars

closes = [2010.0 - i for i in range(10)] + [2001.0 + 2 * i for i in range(15)]
bars = make_bars(closes, datetime(2024, 3, 4, 8, 0, tzinfo=timezone.utc))
robot = Robot(bars=bars[:9]).with_parameters(
    Label="chronos-ema_cross", FixedLots=0.01, RiskPerTradePercent=0.0, MaxLots=1.0,
    MaxPositions=1, FastPeriod=3, SlowPeriod=6, AtrPeriod=3, SlAtrMult=2.0,
    TpAtrMult=3.0, AllowShorts=True,
)
builtins.api = robot

scope = {}
exec((cbot / "ChronosEmaCross_main.py").read_text(), scope)
bot = scope["ChronosEmaCross"]()
bot.on_start()
for bar in bars[8:]:
    robot.close_bar(bar)
    bot.on_bar_closed()
bot.on_stop()
print(json.dumps({"orders": robot.orders, "printed": robot.printed}))
'''


def test_el_cbot_generado_corre_solo_sin_chronos_ni_numpy(tmp_path: Path) -> None:
    """Los ficheros que se copian a cTrader, ejecutados como los ejecuta cTrader,
    en un Python sin site-packages: ni chronos, ni numpy, ni pandas."""
    write_cbot(tmp_path / "cbot", "ema_cross")
    shutil.copy(Path(ctrader_fakes.__file__), tmp_path / "ctrader_fakes.py")
    (tmp_path / "clr.py").write_text("def AddReference(name):\n    pass\n", encoding="utf-8")
    (tmp_path / "cAlgo").mkdir()
    (tmp_path / "cAlgo" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "cAlgo" / "API.py").write_text(
        "from ctrader_fakes import TradeType\n", encoding="utf-8"
    )
    driver = tmp_path / "driver.py"
    driver.write_text(_DRIVER, encoding="utf-8")

    done = subprocess.run(
        [sys.executable, "-I", "-S", str(driver)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    output = json.loads(done.stdout)
    assert output["printed"][0].startswith("chronos ema_cross | Python ")
    assert [order["trade_type"] for order in output["orders"]] == ["Buy"]
    order = output["orders"][0]
    assert order["label"] == "chronos-ema_cross"
    assert order["volume"] == 1.0
    assert order["stop_loss_pips"] > 0 and order["take_profit_pips"] > order["stop_loss_pips"]

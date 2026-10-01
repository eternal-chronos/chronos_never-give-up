/* Arranca explorer.js contra un DOM mínimo y simula los controles.
 *
 * No sustituye a mirar el explorador en un navegador, pero sí detecta lo que
 * más suele romperse: identificadores que no existen, campos mal nombrados en
 * el payload y excepciones dentro del ciclo de render. Además comprueba que los
 * controles —velas/líneas, las temporalidades, la navegación por fechas, el
 * replay, el zoom y las herramientas de mano— cambian la figura de verdad.
 *
 * Uso: node explorer_dom_stub.js <explorer.js> <payload.json>
 */
const fs = require('fs');

const [, , scriptPath, payloadPath] = process.argv;

const plotCalls = [];
const missing = [];
const elements = {};

function makeElement(id) {
  return {
    id: id,
    dataset: {},
    checked: true,
    value: '',
    min: '',
    max: '',
    title: '',
    className: '',
    textContent: '',
    children: [],
    attributes: {},
    listeners: {},
    style: {},
    set innerHTML(value) { if (value === '') { this.children = []; } },
    get innerHTML() { return ''; },
    addEventListener(type, handler) {
      (this.listeners[type] = this.listeners[type] || []).push(handler);
    },
    // Los eventos de Plotly se enganchan al div con `on`, no con addEventListener.
    on(type, handler) { this.addEventListener(type, handler); },
    // El gesto de escalar sobre los ejes necesita saber dónde está el gráfico.
    getBoundingClientRect() { return { left: 0, top: 0, width: 1200, height: 720 }; },
    setAttribute(name, value) { this.attributes[name] = value; },
    getAttribute(name) { return this.attributes[name] ?? null; },
    appendChild(child) { this.children.push(child); return child; },
    fire(type, event) {
      (this.listeners[type] || []).forEach(function (handler) { handler(event || {}); });
    },
  };
}

function declare(id) {
  elements[id] = makeElement(id);
  return elements[id];
}

// Elementos que la plantilla HTML declara de verdad.
['tf-buttons', 'view-buttons', 'preset-buttons', 'chart', 'zoom-reset',
 'prev', 'next', 'from', 'to',
 'volume-layer', 'layer-volume',
 'blind-seed', 'blind-start', 'blind-reveal', 'blind-exit',
 'sim-group', 'sim-buttons', 'sim-ratio', 'sim-clear',
 'rect-group', 'rect-buttons', 'rect-undo', 'rect-clear',
 'account-group', 'account-initial', 'account-mode', 'account-risk',
 'account-buttons', 'account-undo', 'account-reset', 'account-copy', 'account-summary',
 'replay-group', 'replay-date', 'replay-start', 'replay-back', 'replay-step',
 'replay-play', 'replay-exit', 'replay-forming', 'replay-speed', 'replay-window',
 'notes', 'explorer-data'].forEach(declare);

elements['explorer-data'].textContent = fs.readFileSync(payloadPath, 'utf8');

const viewButtons = ['candles', 'line'].map(function (view) {
  const button = makeElement('view-' + view);
  button.dataset.view = view;
  return button;
});

const documentListeners = {};

// El portapapeles. `writeText` devuelve algo con `catch`, como la promesa de
// verdad, para que el explorador pueda encadenarlo.
const copiado = [];
// `navigator` es un global de node y no se deja reasignar: hay que redefinirlo.
Object.defineProperty(global, 'navigator', {
  configurable: true,
  writable: true,
  value: {
    clipboard: {
      writeText(text) { copiado.push(text); return { catch() { return null; } }; },
    },
  },
});

global.document = {
  activeElement: null,
  getElementById(id) {
    if (!elements[id]) { missing.push(id); declare(id); }
    return elements[id];
  },
  createElement() { return makeElement('created'); },
  createTextNode(text) { return { text: text }; },
  addEventListener(type, handler) {
    (documentListeners[type] = documentListeners[type] || []).push(handler);
  },
  querySelectorAll(selector) {
    if (selector === '#tf-buttons button') { return elements['tf-buttons'].children; }
    if (selector === '#preset-buttons button') { return elements['preset-buttons'].children; }
    if (selector === '#sim-buttons button') { return elements['sim-buttons'].children; }
    if (selector === '#sim-ratio button') { return elements['sim-ratio'].children; }
    if (selector === '#rect-buttons button') { return elements['rect-buttons'].children; }
    if (selector === '#account-buttons button') { return elements['account-buttons'].children; }
    if (selector === '#view-buttons button') { return viewButtons; }
    missing.push(selector);
    return [];
  },
};

function fireDocument(type, event) {
  (documentListeners[type] || []).forEach(function (handler) { handler(event); });
}

function pressKey(key, focusedTag) {
  global.document.activeElement = focusedTag ? { tagName: focusedTag } : null;
  (documentListeners['keydown'] || []).forEach(function (handler) {
    handler({ key: key, preventDefault() {} });
  });
  global.document.activeElement = null;
}

/* Lo que se dibuja con los datos, frente a las velas y a la vela en formación.
 * En el replay ningún punto de estas capas puede caer más allá del reloj. */
function engineLayer(name) {
  return !/^(Velas |Cierres |Vela en formación)/.test(name || '');
}

/* Lo que es precio o el volumen del histórico. Todo lo demás es una capa
 * calculada, y mientras no haya estrategia no puede haber ninguna. */
function priceLayer(name) {
  return /^(Velas |Cierres |Vela en formación|Volumen )/.test(name || '');
}

function simShape(shape) {
  return String(shape.name || '').indexOf('sim-') === 0;
}

// Los recuadros los planta el propietario a mano, igual que la caja simulada:
// tampoco son una capa calculada.
function rectShape(shape) {
  return String(shape.name || '').indexOf('rect-') === 0;
}

function handDrawn(shape) {
  return simShape(shape) || rectShape(shape);
}

function furthest(traces, layout) {
  const points = [];
  traces.forEach(function (trace) {
    if (!engineLayer(trace.name)) { return; }
    (trace.x || []).forEach(function (value) { if (value) { points.push(value); } });
  });
  // Lo que dibuja el propietario a mano no puede contar como que el replay se
  // ha adelantado al reloj.
  (layout.shapes || []).forEach(function (shape) {
    if (!handDrawn(shape)) { points.push(shape.x1); }
  });
  return points.length ? points.sort()[points.length - 1] : null;
}

const relayoutCalls = [];

global.Plotly = {
  // Lo que el gesto de escalar sobre los ejes le pide a Plotly. Se reemite como
  // `plotly_relayout`, que es lo que hace Plotly de verdad: así el recorrido
  // comprueba también que el encuadre queda guardado.
  relayout(target, update) {
    relayoutCalls.push(update);
    elements['chart'].fire('plotly_relayout', update);
  },
  react(target, traces, layout) {
    plotCalls.push({
      maxEngineX: furthest(traces, layout),
      target: target,
      traces: traces.map(function (trace) {
        return {
          name: trace.name,
          type: trace.type,
          points: (trace.x && trace.x.length) || 0,
          lastX: (trace.x && trace.x.length && trace.x[trace.x.length - 1]) || null,
          yaxis: trace.yaxis || 'y',
        };
      }),
      calculated: traces
        .filter(function (trace) { return !priceLayer(trace.name); })
        .map(function (trace) { return trace.name; }),
      bars: (traces[0] && traces[0].x && traces[0].x.length) || 0,
      firstBar: traces[0] && traces[0].x && traces[0].x[0],
      lastBar: traces[0] && traces[0].x && traces[0].x[traces[0].x.length - 1],
      hover: traces[0] && traces[0].text && traces[0].text[0],
      shapes: (layout.shapes || []).filter(function (shape) {
        return !handDrawn(shape);
      }).length,
      // La caja de la entrada simulada, con lo que dice cada rectángulo.
      sim: (layout.shapes || []).filter(simShape).map(function (shape) {
        return {
          name: shape.name, type: shape.type,
          x0: shape.x0, x1: shape.x1, y0: shape.y0, y1: shape.y1,
          label: (shape.label && shape.label.text) || null,
        };
      }),
      // Los recuadros marcados a mano, con el color y el trazo que los
      // distinguen de cualquier capa calculada.
      rect: (layout.shapes || []).filter(rectShape).map(function (shape) {
        return {
          name: shape.name, type: shape.type,
          x0: shape.x0, x1: shape.x1, y0: shape.y0, y1: shape.y1,
          color: (shape.line && shape.line.color) || null,
          dash: (shape.line && shape.line.dash) || null,
          fillcolor: shape.fillcolor || null,
          label: (shape.label && shape.label.text) || null,
        };
      }),
      yTickFormat: layout.yaxis && layout.yaxis.tickformat,
      xAnchor: (layout.xaxis && layout.xaxis.anchor) || null,
      // El volumen va en su propio eje, superpuesto al del precio y sin verse.
      volumeAxis: layout.yaxis3
        ? {
          overlaying: layout.yaxis3.overlaying,
          visible: layout.yaxis3.visible,
          range: layout.yaxis3.range,
        }
        : null,
      xRange: (layout.xaxis && layout.xaxis.range) || null,
      yRange: (layout.yaxis && layout.yaxis.range) || null,
    });
  },
};

function pressed(container, key) {
  return (elements[container].children.filter(function (button) {
    return button.getAttribute('aria-pressed') === 'true';
  })[0] || {}).dataset?.[key] || null;
}

function snapshot(label) {
  return {
    label: label,
    chart: pressed('tf-buttons', 'tf'),
    plot: plotCalls[plotCalls.length - 1],
    notes: elements['notes'].textContent,
    from: elements['from'].value,
    to: elements['to'].value,
    replayDate: elements['replay-date'].value,
    zoomFree: elements['zoom-reset'].disabled === true,
    replayPlay: elements['replay-play'].textContent,
    lastRelayout: relayoutCalls[relayoutCalls.length - 1] || null,
    replayLocked: elements['from'].disabled === true && elements['next'].disabled === true,
    volumeBox: elements['layer-volume'].checked === true,
    simArmed: pressed('sim-buttons', 'side'),
    simClearDisabled: elements['sim-clear'].disabled === true,
    simRatio: pressed('sim-ratio', 'ratio'),
    simCursor: elements['chart'].style.cursor || '',
    // Los recuadros a mano: qué botón espera el clic y si hay algo que quitar.
    rectArmed: pressed('rect-buttons', 'kind'),
    rectUndoDisabled: elements['rect-undo'].disabled === true,
    rectClearDisabled: elements['rect-clear'].disabled === true,
    // La cuenta simulada: lo que dice la barra y lo que deja hacer.
    account: {
      summary: elements['account-summary'].textContent,
      initial: elements['account-initial'].value,
      mode: elements['account-mode'].value,
      risk: elements['account-risk'].value,
      resultsDisabled: elements['account-buttons'].children.map(function (button) {
        return button.disabled === true;
      }),
      undoDisabled: elements['account-undo'].disabled === true,
      resetDisabled: elements['account-reset'].disabled === true,
      copyDisabled: elements['account-copy'].disabled === true,
    },
    copiado: copiado.length ? copiado[copiado.length - 1] : null,
  };
}

global.window = global;
eval(fs.readFileSync(scriptPath, 'utf8'));

const payload = JSON.parse(elements['explorer-data'].textContent);
const steps = [];
const tabs = elements['tf-buttons'].children;
const presets = elements['preset-buttons'].children;

function tab(timeframe) {
  return tabs.filter(function (item) { return item.dataset.tf === timeframe; })[0] || tabs[0];
}

/* Fuerza un redibujo sin cambiar nada: la casilla del volumen se vuelve a
 * marcar como ya estaba. */
function redraw() {
  elements['layer-volume'].fire('change', { target: { checked: true } });
}

steps.push(snapshot('de-salida'));

// Un recorrido por cada gráfico, mirando el preset más corto para que la
// ventana quepa en cualquier histórico de prueba.
presets[presets.length - 1].fire('click');
tabs.forEach(function (item) {
  item.fire('click');
  steps.push(snapshot('grafico-' + item.dataset.tf));
});

// De vuelta al primero: periodo completo, navegación y vista.
tabs[0].fire('click');
presets[0].fire('click');
steps.push(snapshot('todo'));
presets[presets.length - 1].fire('click');
steps.push(snapshot('preset-corto'));
elements['prev'].fire('click');
steps.push(snapshot('ventana-anterior'));
elements['next'].fire('click');
steps.push(snapshot('ventana-siguiente'));

// Las flechas del teclado mueven la ventana igual que los botones, salvo cuando
// el foco está en un campo de texto.
pressKey('ArrowLeft');
steps.push(snapshot('teclado-izquierda'));
pressKey('ArrowRight');
steps.push(snapshot('teclado-derecha'));
pressKey('ArrowLeft', 'INPUT');
steps.push(snapshot('teclado-en-un-campo'));

// Atajos de temporalidad: d/4/1/m/5 saltan de gráfico, salvo con el foco en un
// campo de texto.
pressKey('4');
steps.push(snapshot('teclado-tf-h4'));
pressKey('m');
steps.push(snapshot('teclado-tf-m15'));
pressKey('5');
steps.push(snapshot('teclado-tf-m5'));
pressKey('1');
steps.push(snapshot('teclado-tf-h1'));
pressKey('d', 'INPUT');
steps.push(snapshot('teclado-tf-en-un-campo'));
pressKey('d');
steps.push(snapshot('teclado-tf-diario'));

viewButtons[1].fire('click');
steps.push(snapshot('lineas'));
viewButtons[0].fire('click');
steps.push(snapshot('velas'));

// El volumen al pie del precio: se apaga y se enciende sobre las mismas velas.
tab('H1').fire('click');
steps.push(snapshot('volumen-por-defecto'));
elements['layer-volume'].fire('change', { target: { checked: false } });
steps.push(snapshot('volumen-apagado'));
elements['layer-volume'].fire('change', { target: { checked: true } });
tabs[0].fire('click');
presets[presets.length - 1].fire('click');
steps.push(snapshot('antes-de-la-ciega'));

// Auditoría ciega: sortear con semilla, revelar, repetir y salir.
elements['blind-seed'].value = '4242';
elements['blind-seed'].fire('change', {});
elements['blind-start'].fire('click');
steps.push(snapshot('ciega'));
elements['blind-reveal'].fire('click');
steps.push(snapshot('revelada'));
elements['blind-seed'].value = '4242';
elements['blind-seed'].fire('change', {});
elements['blind-start'].fire('click');
steps.push(snapshot('ciega-misma-semilla'));
elements['blind-exit'].fire('click');
steps.push(snapshot('fuera-de-la-ciega'));

// Replay desde una fecha: paso a paso, con la vela en formación armada con la
// temporalidad inferior y sin dibujar nada que no se supiera aún.
const h4 = tab('H4');
h4.fire('click');
const serie = payload.bars[h4.dataset.tf].t;
const arranque = new Date(serie[Math.floor(serie.length / 2)] * 60000)
  .toISOString().slice(0, 10);
elements['replay-date'].value = arranque;
steps.push(snapshot('antes-del-replay'));
elements['replay-start'].fire('click');
steps.push(snapshot('replay-inicio'));
for (let paso = 1; paso <= 6; paso += 1) {
  elements['replay-step'].fire('click');
  steps.push(snapshot('replay-paso-' + paso));
}
elements['replay-back'].fire('click');
steps.push(snapshot('replay-atras'));
pressKey('ArrowRight');
steps.push(snapshot('replay-teclado'));

// La reproducción automática se enciende y se apaga sin dejar temporizadores
// colgando: si los dejara, este proceso no terminaría.
elements['replay-play'].fire('click');
steps.push(snapshot('replay-reproduciendo'));
elements['replay-play'].fire('click');
steps.push(snapshot('replay-pausado'));

elements['replay-forming'].fire('change', { target: { checked: false } });
steps.push(snapshot('replay-sin-formacion'));
elements['replay-step'].fire('click');
steps.push(snapshot('replay-vela-entera'));
elements['replay-forming'].fire('change', { target: { checked: true } });

// Cambiar de temporalidad no mueve el reloj del replay.
tabs[0].fire('click');
steps.push(snapshot('replay-otra-temporalidad'));
h4.fire('click');
elements['replay-exit'].fire('click');
steps.push(snapshot('replay-fuera'));

// El reloj es uno solo para todas las temporalidades: lo que avanzas en H4 se
// tiene que ver en el diario como su vela a medio armar, y volver a H4 no puede
// devolverte al principio.
elements['replay-date'].value = arranque;
elements['replay-start'].fire('click');
elements['replay-forming'].fire('change', { target: { checked: true } });
for (let paso = 0; paso < 10; paso += 1) { elements['replay-step'].fire('click'); }
steps.push(snapshot('reloj-h4-avanzado'));
tabs[0].fire('click');
steps.push(snapshot('reloj-en-diario'));
h4.fire('click');
steps.push(snapshot('reloj-de-vuelta-en-h4'));
elements['replay-exit'].fire('click');

// El reloj no se degrada al pasar por una temporalidad de grano grueso: ir de H1
// al diario y volver tiene que devolver el mismo minuto, no el último cierre.
const h1 = tab('H1');
h1.fire('click');
elements['replay-date'].value = arranque;
elements['replay-start'].fire('click');
elements['replay-forming'].fire('change', { target: { checked: true } });
for (let paso = 0; paso < 9; paso += 1) { elements['replay-step'].fire('click'); }
steps.push(snapshot('reloj-fino-h1'));
tabs[0].fire('click');
steps.push(snapshot('reloj-fino-en-diario'));
h1.fire('click');
steps.push(snapshot('reloj-fino-de-vuelta'));
elements['replay-exit'].fire('click');
h4.fire('click');

// El encuadre hecho a mano tiene que sobrevivir a los pasos: el zoom no se rehace
// en cada dibujo y la ventana sólo se desplaza para seguir al presente.
function minuteOf(text) {
  return Math.round(Date.parse(String(text).replace(' ', 'T') + 'Z') / 60000);
}

function zoomTo(x, y) {
  elements['chart'].fire('plotly_relayout', {
    'xaxis.range[0]': new Date(x[0] * 60000).toISOString().replace('T', ' ').slice(0, 19),
    'xaxis.range[1]': new Date(x[1] * 60000).toISOString().replace('T', ' ').slice(0, 19),
    'yaxis.range[0]': y[0],
    'yaxis.range[1]': y[1],
  });
}

const spanChart = payload.spans[h4.dataset.tf];
elements['replay-date'].value = arranque;
elements['replay-start'].fire('click');
elements['replay-forming'].fire('change', { target: { checked: false } });
// Ventana corta a propósito: así el zoom ancho pide más historia de la que el
// replay recorta por su cuenta.
elements['replay-window'].fire('change', { target: { value: '40' } });
steps.push(snapshot('replay-zoom-sin-zoom'));

const presente = minuteOf(plotCalls[plotCalls.length - 1].lastBar) + spanChart;
const precios = [1.05, 1.35];
zoomTo([presente - 200 * spanChart, presente], precios);
elements['replay-step'].fire('click');
steps.push(snapshot('replay-zoom-ancho-1'));
elements['replay-step'].fire('click');
steps.push(snapshot('replay-zoom-ancho-2'));

// Encuadre estrecho pegado al presente: cada paso lo empuja hacia delante sin
// cambiar la anchura.
const ahora = minuteOf(plotCalls[plotCalls.length - 1].lastBar) + spanChart;
zoomTo([ahora - 5 * spanChart, ahora + spanChart], precios);
elements['replay-step'].fire('click');
steps.push(snapshot('replay-zoom-estrecho-1'));
elements['replay-step'].fire('click');
steps.push(snapshot('replay-zoom-estrecho-2'));
elements['replay-back'].fire('click');
steps.push(snapshot('replay-zoom-atras'));

elements['zoom-reset'].fire('click');
steps.push(snapshot('replay-zoom-suelto'));
elements['replay-exit'].fire('click');

// Escalar arrastrando sobre los ejes, como en cualquier gráfico de trading. La
// banda de la izquierda son los precios; la de abajo, las fechas.
function arrastrarEje(desde, hasta) {
  elements['chart'].fire('mousedown', {
    clientX: desde[0], clientY: desde[1],
    preventDefault() {}, stopPropagation() {},
  });
  fireDocument('mousemove', {
    clientX: hasta[0], clientY: hasta[1], preventDefault() {},
  });
  fireDocument('mouseup', {});
}

// Con un encuadre ya tomado a mano el rango de partida es conocido, así que lo
// que cambia el gesto se puede medir.
zoomTo([presente - 100 * spanChart, presente], precios);
// Un redibujo para que ese encuadre quede en la figura: `zoomTo` sólo lo
// guarda, porque el gráfico ya está pintado como el usuario acaba de dejarlo.
redraw();
steps.push(snapshot('ejes-antes-de-escalar'));
// Sobre los precios y hacia abajo: se ve más rango, las velas se hacen pequeñas.
arrastrarEje([30, 300], [30, 450]);
steps.push(snapshot('eje-precios-arrastrado'));
// Sobre las fechas y hacia la izquierda: entran más velas por el mismo sitio.
arrastrarEje([600, 700], [450, 700]);
steps.push(snapshot('eje-fechas-arrastrado'));
// Y el encuadre así tomado sobrevive al siguiente dibujo, como el de la rueda.
redraw();
steps.push(snapshot('ejes-tras-redibujar'));
// Un arrastre en mitad del gráfico no es este gesto y no toca la escala.
const escalados = relayoutCalls.length;
arrastrarEje([600, 300], [500, 400]);
steps.push(snapshot('centro-no-escala'));
if (relayoutCalls.length !== escalados) {
  throw new Error('arrastrar en el centro del gráfico no puede escalar los ejes');
}
elements['zoom-reset'].fire('click');

// Fuera del replay el encuadre manual también manda: redibujar no puede
// devolver el gráfico a su sitio.
zoomTo([presente - 50 * spanChart, presente], precios);
redraw();
steps.push(snapshot('zoom-fuera-del-replay'));
// Y el preset lo suelta: pedir otro tramo de historia es pedir otro sitio.
presets[presets.length - 1].fire('click');
steps.push(snapshot('zoom-suelto-por-el-preset'));

// El simulador de entradas: dos botones que arman, un clic que planta la caja y
// arrastres que la mueven. Con el encuadre tomado a mano se sabe qué precio hay
// debajo de cada píxel, así que el recorrido puede comprobar dónde cae la caja y
// cuánto se mueve. Es dibujo del propietario.
tabs[0].fire('click');
presets[0].fire('click');
const spanDiario = payload.spans[tabs[0].dataset.tf];
const simX = [
  minuteOf(plotCalls[plotCalls.length - 1].lastBar) - 200 * spanDiario,
  minuteOf(plotCalls[plotCalls.length - 1].lastBar),
];
const simY = precios;
zoomTo(simX, simY);

// El mismo cálculo que hace el explorador: MARGIN sobre el div de 1200 x 720, con
// el precio ocupando todo el alto del área de dibujo.
function pixelOf(minute, price) {
  const width = 1200 - 66 - 18;
  const height = 720 - 16 - 44;
  return {
    x: 66 + (minute - simX[0]) / (simX[1] - simX[0]) * width,
    y: 16 + (simY[1] - price) / (simY[1] - simY[0]) * height,
  };
}

const simButtons = elements['sim-buttons'].children;

function armar(side) {
  simButtons.filter(function (button) { return button.dataset.side === side; })
    .forEach(function (button) { button.fire('click'); });
}

function clicGrafico(point) {
  elements['chart'].fire('click', {
    clientX: point.x, clientY: point.y,
    preventDefault() {}, stopPropagation() {},
  });
}

function arrastrarCaja(desde, hasta) {
  elements['chart'].fire('mousedown', {
    clientX: desde.x, clientY: desde.y,
    preventDefault() {}, stopPropagation() {},
  });
  fireDocument('mousemove', {
    clientX: hasta.x, clientY: hasta.y, preventDefault() {},
  });
  fireDocument('mouseup', {});
}

function cajaSimulada() {
  const shapes = plotCalls[plotCalls.length - 1].sim || [];
  const busca = function (name) {
    return shapes.filter(function (shape) { return shape.name === name; })[0];
  };
  const linea = busca('sim-entrada');
  if (!linea) { return null; }
  return {
    entry: linea.y0,
    target: busca('sim-objetivo').y1,
    stop: busca('sim-riesgo').y1,
    from: minuteOf(linea.x0),
    to: minuteOf(linea.x1),
  };
}

function asaDeLaCaja(price) {
  const caja = cajaSimulada();
  return pixelOf(Math.round((caja.from + caja.to) / 2), caja[price]);
}

const entradaSimulada = (simY[0] + simY[1]) / 2;
const minutoSimulado = simX[0] + Math.round((simX[1] - simX[0]) * 0.4);

armar('long');
steps.push(snapshot('sim-armado'));
// Escape suelta el botón sin plantar nada.
pressKey('Escape');
steps.push(snapshot('sim-desarmado'));

armar('long');
clicGrafico(pixelOf(minutoSimulado, entradaSimulada));
steps.push(snapshot('sim-largo'));

// El borde de fuera de la caja roja mueve el stop.
const asaStop = asaDeLaCaja('stop');
arrastrarCaja(asaStop, { x: asaStop.x, y: asaStop.y + 40 });
steps.push(snapshot('sim-stop-arrastrado'));

// La línea de la entrada mueve el conjunto entero: las distancias no cambian.
const asaEntrada = asaDeLaCaja('entry');
arrastrarCaja(asaEntrada, { x: asaEntrada.x, y: asaEntrada.y - 25 });
steps.push(snapshot('sim-entrada-arrastrada'));

// El R:R fijo: 1:3 recoloca el objetivo sin tocar el stop, y con el candado
// puesto mover el stop arrastra el objetivo con él.
const ratios = elements['sim-ratio'].children;

function fijarRatio(value) {
  ratios.filter(function (button) { return button.dataset.ratio === value; })
    .forEach(function (button) { button.fire('click'); });
}

fijarRatio('3');
steps.push(snapshot('sim-ratio-1-3'));

const asaStopConCandado = asaDeLaCaja('stop');
arrastrarCaja(asaStopConCandado, { x: asaStopConCandado.x, y: asaStopConCandado.y - 20 });
steps.push(snapshot('sim-stop-con-candado'));

// Arrastrar el objetivo suelta el candado: manda lo que se ve.
const asaObjetivo = asaDeLaCaja('target');
arrastrarCaja(asaObjetivo, { x: asaObjetivo.x, y: asaObjetivo.y + 30 });
steps.push(snapshot('sim-objetivo-a-mano'));

// En corto el objetivo va por debajo de la entrada y el riesgo por encima.
fijarRatio('4');
armar('short');
clicGrafico(pixelOf(minutoSimulado, entradaSimulada));
steps.push(snapshot('sim-corto'));

elements['sim-clear'].fire('click');
steps.push(snapshot('sim-quitado'));

// La cuenta simulada: el capital, el riesgo y los tres botones que apuntan la
// caja dibujada. El encuadre y el ratio son los mismos que usa el bloque de
// arriba, así que el R:R de cada caja es 1:2 exacto y las cifras se pueden
// comprobar a mano: 50 $ al 2 % son 1,00 $ de riesgo y 2,00 $ de objetivo.
const resultados = elements['account-buttons'].children;

function apuntar(result) {
  resultados.filter(function (button) { return button.dataset.result === result; })
    .forEach(function (button) { button.fire('click'); });
}

function plantarCaja(side) {
  armar(side);
  clicGrafico(pixelOf(minutoSimulado, entradaSimulada));
}

// Fijar un R:R sin caja plantada deja el ratio puesto para la siguiente y no
// puede romper nada: es el gesto natural antes de dibujar.
fijarRatio('2');
steps.push(snapshot('ratio-sin-caja'));
steps.push(snapshot('cuenta-sin-nada'));
plantarCaja('long');
steps.push(snapshot('cuenta-con-caja'));
apuntar('win');
steps.push(snapshot('cuenta-ganada'));
plantarCaja('long');
apuntar('loss');
steps.push(snapshot('cuenta-perdida'));
plantarCaja('long');
apuntar('be');
steps.push(snapshot('cuenta-break-even'));
elements['account-undo'].fire('click');
steps.push(snapshot('cuenta-deshecha'));
elements['account-copy'].fire('click');
steps.push(snapshot('cuenta-copiada'));

// Cambiar el capital no toca lo apuntado: el saldo se queda y sólo cambia la
// apuesta de la siguiente (el 2 % de 100 $ son 2,00 $).
elements['account-initial'].fire('change', { target: { value: '100' } });
steps.push(snapshot('cuenta-capital-100'));

// Y el riesgo en dólares fijos, que no compone.
elements['account-mode'].fire('change', { target: { value: 'cash' } });
elements['account-risk'].fire('change', { target: { value: '5' } });
steps.push(snapshot('cuenta-riesgo-fijo'));

// La siguiente ganada se cobra con los 5 $ nuevos; las dos anteriores siguen
// valiendo 1,00 $ cada una en el historial.
plantarCaja('long');
apuntar('win');
elements['account-copy'].fire('click');
steps.push(snapshot('cuenta-riesgo-nuevo'));

// Un riesgo imposible no se acepta y el control repone el que está puesto.
elements['account-risk'].fire('change', { target: { value: '0' } });
steps.push(snapshot('cuenta-riesgo-invalido'));

elements['account-reset'].fire('click');
steps.push(snapshot('cuenta-reiniciada'));
elements['sim-clear'].fire('click');

// Los recuadros a mano: un botón por nombre de la configuración que arma, un
// clic que planta el suyo y arrastres que lo mueven. Se marcan varios de cada
// nombre y se quitan de uno en uno o de golpe.
const nombres = payload.marks.rects;

function armarRect(kind) {
  elements['rect-buttons'].children.filter(function (button) {
    return button.dataset.kind === kind;
  }).forEach(function (button) { button.fire('click'); });
}

function recuadros() {
  return (plotCalls[plotCalls.length - 1].rect || []).map(function (shape) {
    return {
      high: shape.y1,
      low: shape.y0,
      from: minuteOf(shape.x0),
      to: minuteOf(shape.x1),
    };
  });
}

function asaDelRecuadro(index, borde) {
  const rect = recuadros()[index];
  const minuto = Math.round((rect.from + rect.to) / 2);
  if (borde === 'high') { return pixelOf(minuto, rect.high); }
  if (borde === 'low') { return pixelOf(minuto, rect.low); }
  return pixelOf(minuto, (rect.high + rect.low) / 2);
}

steps.push(snapshot('rect-sin-nada'));
armarRect(nombres[0]);
steps.push(snapshot('rect-armado'));
// Escape suelta el botón sin plantar nada, igual que en el simulador.
pressKey('Escape');
steps.push(snapshot('rect-desarmado'));

armarRect(nombres[0]);
clicGrafico(pixelOf(minutoSimulado, entradaSimulada));
steps.push(snapshot('rect-plantado'));

// El borde de arriba mueve el techo y nada más.
const asaTecho = asaDelRecuadro(0, 'high');
arrastrarCaja(asaTecho, { x: asaTecho.x, y: asaTecho.y - 20 });
steps.push(snapshot('rect-techo-arrastrado'));

// Por dentro se mueve entero: el alto y el ancho no cambian.
const asaDentro = asaDelRecuadro(0, 'body');
arrastrarCaja(asaDentro, { x: asaDentro.x + 40, y: asaDentro.y + 30 });
steps.push(snapshot('rect-movido'));

// Se marcan varios y de nombres distintos: cada uno con su color.
const medioSimulado = (simY[0] + entradaSimulada) / 2;
armarRect(nombres[1]);
clicGrafico(pixelOf(minutoSimulado, medioSimulado));
steps.push(snapshot('rect-segundo'));

// Y se numeran POR NOMBRE: el segundo del primer nombre es su «2» aunque entre
// medias se haya plantado otro.
armarRect(nombres[0]);
clicGrafico(pixelOf(minutoSimulado, (simY[0] + medioSimulado) / 2));
steps.push(snapshot('rect-tercero'));

elements['rect-undo'].fire('click');
steps.push(snapshot('rect-deshecho'));
elements['rect-clear'].fire('click');
steps.push(snapshot('rect-limpio'));

console.log(JSON.stringify({
  unknownElements: missing,
  chartTabs: tabs.map(function (item) { return item.textContent; }),
  chartTitles: tabs.map(function (item) { return item.title; }),
  presetLabels: presets.map(function (button) { return button.textContent; }),
  rectLabels: elements['rect-buttons'].children.map(function (button) {
    return button.textContent;
  }),
  totalPlots: plotCalls.length,
  steps: steps,
}));

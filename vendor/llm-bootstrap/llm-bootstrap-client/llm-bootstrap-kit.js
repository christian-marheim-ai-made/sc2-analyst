/* llm-bootstrap-kit.js - the widgets of llm-bootstrap, for a page of your own.
   Built by build.py from parts/ (MANIFEST): never edited by hand.

   A page names its bootstrap server once, in a script before this one
   (window.llmServer = 'http://127.0.0.1:7072'; named nowhere, the page's own
   address is asked), includes this file as a script, and then places what it
   wants shown:

     llmKit.place('bench', document.getElementById('bench'));
     llmKit.container('bottom', { name: 'progress', slots: { middle: ['queue'] } });

   The server must allow the page's address (server.py --allow-origin). The
   calls are described in kit.js, the last part. */
(function () {
'use strict';
/* the kit's look: the css parts, put in the page by kit.js */
const KIT_CSS = "/* The kit's one look: every widget, container, tray, banner and panel. The\n   kit puts it in the page when the first thing is placed, and it reaches\n   only what the kit builds (`.llmk` and what is inside it): an\n   application's page keeps its own look. Colours are names, set once here.\n\n   A CONTAINER IS THREE SLOTS along one axis: a row (docked to the top or the\n   bottom edge, or in an element) or a column (docked to the left or the\n   right). Longer than it needs, the room goes BETWEEN start, middle and end.\n   Every widget sits in a box of the width and height it declared: its\n   content fits or is clipped, and a container never moves because a message\n   got longer. The room around and between the boxes is the slot's and the\n   container's, painted from the numbers the shedding adds up (container.js,\n   BOX): a box has no edge of its own. */\n\n.llmk {\n  --llmk-bg: #15171c;\n  --llmk-panel: #1d2027;\n  --llmk-line: #2c313b;\n  --llmk-text: #e6e6e6;\n  --llmk-dim: #8a8f98;\n  --llmk-accent: #6fa8ff;\n  --llmk-ok: #6fcf8f;\n  --llmk-warn: #f0b35a;\n  --llmk-bad: #ef6b6b;\n  --llmk-button: #2a2f39;\n  --llmk-banner: #213150;\n  --llmk-unknown: #555b66;\n  --llmk-shadow: #000a;\n  font: 14px/1.4 system-ui, sans-serif;\n  color: var(--llmk-text);\n  color-scheme: dark;\n}\n\n.llmk, .llmk * { box-sizing: border-box; }\n\n/* ------------------------------------------------------------ containers */\n\n.llmk-container { display: flex; flex-direction: column; background: var(--llmk-panel); }\n.llmk-container:not(.dock) { position: relative; border: 1px solid var(--llmk-line); border-radius: 6px; }\n.llmk-container.dock { position: fixed; z-index: 1000; }\n.llmk-container.dock-top { top: 0; left: 0; right: 0; border-bottom: 1px solid var(--llmk-line); }\n.llmk-container.dock-bottom { bottom: 0; left: 0; right: 0; border-top: 1px solid var(--llmk-line); }\n.llmk-container.dock-left { left: 0; border-right: 1px solid var(--llmk-line); }\n.llmk-container.dock-right { right: 0; border-left: 1px solid var(--llmk-line); }\n/* a column docked left or right stands between the rows docked top and\n   bottom, by the sizes they say (container.js, dockAt): two docks that meet\n   at a corner never cover each other */\n.llmk-container.dock-left, .llmk-container.dock-right { top: var(--llmk-dock-top, 0px); bottom: var(--llmk-dock-bottom, 0px); }\n.llmk-container.axis-column:not(.dock) { height: 100%; }\n\n/* the slots: their gaps and padding are painted from BOX (container.js), the\n   numbers the shedding adds up - so the sum and the paint cannot disagree */\n.llmk-slots { display: flex; justify-content: space-between; align-items: center; min-width: 0; }\n.axis-column > .llmk-slots { flex-direction: column; flex: 1; min-height: 0; }\n\n.llmk .slot { display: flex; align-items: center; min-width: 0; }\n.axis-column .slot { flex-direction: column; }\n\n.llmk .item { flex: none; overflow: hidden; display: flex; align-items: center; position: relative; }\n.llmk .item > .inner { flex: 1; min-width: 0; max-width: 100%; height: 100%; display: flex; align-items: center; }\n.llmk .item > .inner > * { min-width: 0; max-width: 100%; }\n.llmk .item .away {\n  position: absolute; top: -0.2em; right: -0.2em; padding: 0 0.25em; line-height: 1.2;\n  font-size: 0.8em; color: var(--llmk-bad);\n}\n.llmk-slots.customizing .item { outline: 1px dashed var(--llmk-accent); outline-offset: 2px; cursor: grab; }\n\n/* the tray is its button's box, 3 em by a line - its words kept on that\n   line and clipped to it (measured: left to wrap, they pushed the tray's\n   baseline down, and the tray stood 42.3 px tall against its 22.4) */\n.llmk .tray { position: relative; flex: none; display: flex; }\n.llmk .tray > button { height: 1.6em; padding: 0; white-space: nowrap; overflow: hidden; }\n.llmk .tray .menu {\n  position: absolute; right: 0; top: 100%; z-index: 6; list-style: none; margin: 0; min-width: 14em;\n  display: flex; flex-direction: column; gap: 2px; padding: 4px;\n  background: var(--llmk-panel); border: 1px solid var(--llmk-line); border-radius: 6px;\n}\n.dock-bottom .tray .menu { top: auto; bottom: 100%; }\n.dock-left .tray .menu { top: auto; bottom: 0; right: auto; left: 100%; }\n.dock-right .tray .menu { top: auto; bottom: 0; right: 100%; }\n.llmk .tray .menu li { display: flex; align-items: center; gap: 0.4em; padding: 0.1em 0.3em; }\n\n.llmk .banner:empty { display: none; }\n.llmk .banner { padding: 0.3em 0.6em; background: var(--llmk-banner); color: var(--llmk-text); }\n.axis-column > .banner { max-width: 18em; }\n.llmk .banner .shed { white-space: nowrap; }\n.llmk .banner .shed button { padding: 0 0.3em; }\n\n/* a panel opens beside what it belongs to, over the page: it moves nothing */\n.llmk .panel:empty { display: none; }\n.llmk .panel {\n  position: absolute; top: 100%; left: 0.6em; z-index: 10;\n  width: min(34em, calc(100vw - 1.2em)); max-height: 60vh; overflow: auto;\n  background: var(--llmk-panel); border: 1px solid var(--llmk-line); border-radius: 6px;\n  padding: 0.7em 0.9em; box-shadow: 0 8px 28px var(--llmk-shadow);\n}\n.dock-bottom > .panel { top: auto; bottom: 100%; }\n.dock-left > .panel { top: 0.6em; left: 100%; }\n.dock-right > .panel { top: 0.6em; left: auto; right: 100%; }\n.llmk-one > .panel { left: 0; }\n.llmk .panel h3 { margin: 0 0 0.5em; font-size: 1em; }\n\n/* one widget alone, in an element of the page: its box, and its panel below */\n.llmk-one { position: relative; display: inline-flex; vertical-align: middle; }\n\n/* an instrument alone, filling an element of the page */\n.llmk-tool { padding: 0.8em; background: var(--llmk-bg); }\n\n/* the instruments behind one door: the strip, and the one shown below it */\n.llmk-tools { display: grid; grid-template-rows: auto 1fr; height: 100%; min-height: 0; background: var(--llmk-bg); }\n\n/* --------------------------------------------------------------- controls */\n\n.llmk .clip { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }\n\n.llmk .strip { display: flex; gap: 0.2em; padding: 0.3em 0.6em 0; border-bottom: 1px solid var(--llmk-line); }\n.llmk .strip:empty { display: none; }\n\n.llmk .tab {\n  background: none; border: 1px solid transparent; border-bottom: none;\n  color: var(--llmk-dim); padding: 0.3em 0.8em; border-radius: 4px 4px 0 0; cursor: pointer;\n}\n.llmk .tab.active { color: var(--llmk-text); border-color: var(--llmk-line); background: var(--llmk-bg); }\n\n.llmk .pane { overflow: auto; padding: 0.8em; min-height: 0; }\n\n.llmk button {\n  font: inherit; color: var(--llmk-text); background: var(--llmk-button); border: 1px solid var(--llmk-line);\n  border-radius: 4px; padding: 0.15em 0.7em; cursor: pointer;\n}\n.llmk button:hover { border-color: var(--llmk-accent); }\n.llmk button:disabled { color: var(--llmk-dim); cursor: default; border-color: var(--llmk-line); }\n.llmk button.on { border-color: var(--llmk-accent); color: var(--llmk-accent); }\n\n.llmk .row { display: flex; gap: 0.4em; margin: 0.5em 0; }\n.llmk .row input[type=text] { flex: 1; min-width: 0; }\n.llmk input, .llmk select {\n  font: inherit; color: var(--llmk-text); background: var(--llmk-bg);\n  border: 1px solid var(--llmk-line); border-radius: 4px; padding: 0.15em 0.4em;\n}\n.llmk .said:empty { display: none; }\n.llmk .said { color: var(--llmk-warn); margin: 0.3em 0; }\n.llmk .recent { display: flex; flex-direction: column; align-items: flex-start; gap: 0.15em; }\n.llmk button.link { background: none; border: none; color: var(--llmk-accent); padding: 0; max-width: 100%; text-align: left; }\n.llmk .dim { color: var(--llmk-dim); }\n.llmk code { font-family: ui-monospace, Consolas, monospace; font-size: 0.95em; }\n\n/* the log's light, in its box */\n.llmk .light { color: var(--llmk-dim); padding: 0.15em 0.5em; }\n.llmk .light.bad { color: var(--llmk-bad); border-color: var(--llmk-bad); }\n\n/* the log */\n.llmk .controls { display: flex; flex-wrap: wrap; align-items: center; gap: 0.5em; margin-bottom: 0.4em; }\n.llmk .check { display: inline-flex; align-items: center; gap: 0.2em; color: var(--llmk-dim); }\n.llmk ol.log { list-style: none; margin: 0.4em 0 0; padding: 0; font-family: ui-monospace, Consolas, monospace; font-size: 0.9em; }\n.llmk ol.log .line { padding: 0.15em 0.3em; border-bottom: 1px solid var(--llmk-line); cursor: pointer; }\n.llmk ol.log .time { color: var(--llmk-dim); }\n.llmk ol.log .level { display: inline-block; min-width: 3.2em; }\n.llmk ol.log .level.error { color: var(--llmk-bad); }\n.llmk ol.log .level.warn { color: var(--llmk-warn); }\n.llmk ol.log .level.info { color: var(--llmk-dim); }\n.llmk ol.log .area { color: var(--llmk-accent); }\n.llmk ol.log .count { color: var(--llmk-warn); }\n.llmk ol.log .detail { white-space: pre-wrap; margin: 0.3em 0 0.2em 1em; color: var(--llmk-dim); }\n\n/* the model's progress, in its box: the dot, the words, the blocks, the fill */\n.llmk button.graphic {\n  position: relative; overflow: hidden;\n  display: flex; align-items: center; gap: 0.45em; width: 100%; height: 100%;\n  padding: 0 0.5em; text-align: left;\n}\n.llmk .graphic .fill {\n  position: absolute; left: 0; bottom: 0; height: 2px;\n  background: var(--llmk-accent); pointer-events: none;\n}\n.llmk .graphic .words { flex: 1; }\n.llmk .dot { flex: none; width: 0.7em; height: 0.7em; border-radius: 50%; background: var(--llmk-dim); }\n.llmk .dot.idle { background: var(--llmk-ok); }\n.llmk .dot.busy { background: var(--llmk-warn); }\n.llmk .dot.check { background: var(--llmk-accent); }\n.llmk .dot.bad { background: var(--llmk-bad); }\n.llmk .dot.unknown { background: var(--llmk-unknown); }\n.llmk .blocks { flex: none; display: flex; align-items: center; gap: 2px; }\n.llmk .blocks i { display: block; width: 0.45em; height: 0.8em; background: var(--llmk-accent); border-radius: 1px; }\n.llmk .blocks .more { color: var(--llmk-dim); font-size: 0.85em; margin-left: 0.2em; }\n\n/* the Tools door: at the end of the strip, a menu of the instruments */\n.llmk .strip .tools { margin-left: auto; position: relative; }\n.llmk .strip .menu {\n  position: absolute; right: 0; top: 100%; z-index: 5; min-width: 10em;\n  display: flex; flex-direction: column; gap: 2px; padding: 4px;\n  background: var(--llmk-panel); border: 1px solid var(--llmk-line); border-radius: 6px;\n}\n.llmk .strip .menu button { text-align: left; }\n\n/* Bench: the controls, the prompt, Run, and the session's runs */\n.llmk .bench .controls { display: flex; flex-wrap: wrap; gap: 0.4em 1em; margin-bottom: 0.5em; }\n.llmk .bench label { display: inline-flex; align-items: center; gap: 0.35em; }\n.llmk .bench textarea {\n  width: 100%; box-sizing: border-box; font: inherit; color: var(--llmk-text); background: var(--llmk-bg);\n  border: 1px solid var(--llmk-line); border-radius: 4px; padding: 0.4em;\n}\n.llmk .bench .go { margin: 0.4em 0; color: var(--llmk-dim); }\n/* what the chosen model does not take is put away: the samplers and the\n   prompt, or the audio and the fields */\n.llmk [hidden] { display: none !important; }\n.llmk .bench .group { display: contents; }\n.llmk .bench .hears { display: grid; gap: 0.4em; }\n.llmk .bench .hears label { align-items: flex-start; }\n.llmk .bench .hears input { flex: 1; min-width: 0; }\n/* Check: a transcription's audio and its length on one line, its fields on\n   the next (R-B2) */\n.llmk .check-tool .hears { display: grid; gap: 0.4em; }\n.llmk .check-tool .hears .line { display: flex; align-items: center; gap: 0.5em; }\n.llmk .check-tool .hears label { display: flex; align-items: flex-start; gap: 0.35em; }\n.llmk .check-tool .hears input { flex: 1; min-width: 0; }\n/* a transcript, whole */\n.llmk .heard { margin-top: 0.3em; }\n.llmk .heard ol { margin: 0.3em 0; padding-left: 1.4em; }\n.llmk .heard .when { color: var(--llmk-dim); font-variant-numeric: tabular-nums; }\n.llmk .heard .words { display: flex; flex-wrap: wrap; gap: 0.2em 0.6em; font-size: 0.9em; }\n.llmk .heard .word { color: var(--llmk-text); }\n.llmk .heard details { margin: 0.3em 0; }\n.llmk .bad { color: var(--llmk-bad); }\n.llmk ol.runs { list-style: none; padding: 0; margin: 0.6em 0 0; display: grid; gap: 0.6em; }\n.llmk ol.runs .run { border: 1px solid var(--llmk-line); border-radius: 6px; padding: 0.5em 0.7em; background: var(--llmk-panel); }\n.llmk ol.runs .head { display: flex; flex-wrap: wrap; align-items: center; gap: 0.5em; }\n.llmk ol.runs .doing { color: var(--llmk-warn); }\n.llmk ol.runs pre { white-space: pre-wrap; margin: 0.3em 0; font: inherit; }\n.llmk ol.runs .answer { border-top: 1px solid var(--llmk-line); margin-top: 0.4em; padding-top: 0.3em; }\n.llmk ol.runs .bad { color: var(--llmk-bad); }\n\n/* Models: a card a model */\n.llmk .models { display: grid; gap: 0.6em; margin-top: 0.6em; }\n.llmk .model { border: 1px solid var(--llmk-line); border-radius: 6px; padding: 0.5em 0.7em; background: var(--llmk-panel); }\n.llmk .model h4 { margin: 0 0 0.3em; font-size: 1em; }\n.llmk .model .bad { color: var(--llmk-bad); }\n.llmk .model .problems { margin: 0.3em 0; padding-left: 1.2em; color: var(--llmk-warn); }\n.llmk .model .measure { margin-top: 0.4em; }\n/* a model's statistics: numbers in columns, so they read at a glance */\n.llmk .model table.stats { border-collapse: collapse; margin: 0.3em 0; }\n.llmk .model table.stats th,\n.llmk .model table.stats td { padding: 0.05em 0.8em 0.05em 0; text-align: left; font-weight: normal; white-space: nowrap; }\n.llmk .model table.stats tr:first-child th { color: var(--llmk-dim); font-size: 0.85em; }\n.llmk .model table.stats td { font-variant-numeric: tabular-nums; text-align: right; }\n.llmk .model table.stats td.dim { text-align: left; }\n.llmk .badge {\n  margin-left: 0.5em; padding: 0 0.4em; border-radius: 3px; font-size: 0.85em;\n  color: var(--llmk-bg); background: var(--llmk-ok);\n}\n\n/* the queue's panel */\n.llmk ol.jobs { list-style: none; padding: 0; margin: 0.3em 0; }\n.llmk ol.jobs li { display: flex; align-items: center; gap: 0.4em; padding: 0.2em 0; border-bottom: 1px solid var(--llmk-line); }\n.llmk ol.jobs .where { width: 2.6em; color: var(--llmk-dim); }\n.llmk ol.jobs .name { flex: 1; }\n.llmk ol.jobs .doing { color: var(--llmk-warn); }\n";
/* ---- @contract/ids.js ---- */
(function () {
/* The ids every part of llm-bootstrap mints and reads. No dependencies.

   The same rules as ids.py, which describes them, producing the same answers
   character for character - test_contract.py runs both over one table.

   In a browser:  llmIds.mintActor('cli')
   In node:       const ids = require('./ids.js')
   For the agreement test:
                  node ids.js --batch <cases.json>                            */
'use strict';

const ALPHABET = 'abcdefghijkmnpqrstuvwxyz23456789';
const ROLES = ['srv', 'cli', 'crd', 'load'];
const L = '[a-km-np-z2-9]';
const PATTERNS = {
  actor: `(?:srv|cli|crd|load)-${L}{8}`,
  correlation: `c-[0-9]{8}-[0-9]{6}-${L}{8}`,
  job: 'j-[0-9]{4,}',
  handle: `(srv-${L}{8}):(j-[0-9]{4,})#([0-9]{2,})`,
  ticket: `t-([0-9]{4,})-${L}{4}`,
  pipeline: `pipe-${L}{8}`,
};
const KINDS = Object.keys(PATTERNS);
const SHOWN = {
  actor: 'an actor id',
  correlation: 'a correlation id',
  job: 'a job id',
  handle: 'a handle',
  ticket: 'a ticket id',
  pipeline: 'a pipeline id',
};
const RE = Object.fromEntries(KINDS.map(k => [k, new RegExp(`^(?:${PATTERNS[k]})$`)]));

function defaultDraw() {
  const c = globalThis.crypto;
  if (c && c.getRandomValues) {
    // rejection sampling is unnecessary: 32 divides 256 evenly
    return c.getRandomValues(new Uint8Array(1))[0] % ALPHABET.length;
  }
  return Math.floor(Math.random() * ALPHABET.length);
}

function letters(n, draw) {
  draw = draw || defaultDraw;
  let out = '';
  for (let i = 0; i < n; i++) out += ALPHABET[draw() % ALPHABET.length];
  return out;
}

function matches(kind, value) {
  if (!Object.prototype.hasOwnProperty.call(RE, kind)) throw new Error(`no id kind '${kind}'`);
  return typeof value === 'string' && RE[kind].test(value);
}

function kindOf(value) {
  return KINDS.find(k => matches(k, value)) || null;
}

function mintActor(role, draw) {
  if (!ROLES.includes(role)) throw new Error(`no actor role '${role}': one of ${ROLES.join(', ')}`);
  return `${role}-${letters(8, draw)}`;
}

const pad = (n, w) => String(n).padStart(w, '0');

function mintCorrelation(now, draw) {
  const d = new Date(Math.floor((now === undefined || now === null ? Date.now() / 1000 : now)) * 1000);
  return `c-${d.getFullYear()}${pad(d.getMonth() + 1, 2)}${pad(d.getDate(), 2)}-`
       + `${pad(d.getHours(), 2)}${pad(d.getMinutes(), 2)}${pad(d.getSeconds(), 2)}-`
       + letters(8, draw);
}

function mintTicket(seq, draw) {
  return `t-${pad(Math.trunc(seq), 4)}-${letters(4, draw)}`;
}

function mintPipeline(draw) {
  return `pipe-${letters(8, draw)}`;
}

function handle(server, job, sample) {
  const h = `${server}:${job}#${pad(Math.trunc(sample), 2)}`;
  if (!matches('handle', h))
    throw new Error(`not a handle: '${h}' - the server must be a srv- actor id and the job a job id`);
  return h;
}

function parseHandle(value) {
  const m = typeof value === 'string' ? RE.handle.exec(value) : null;
  return m ? { server: m[1], job: m[2], sample: Number(m[3]) } : null;
}

function ticketSeq(value) {
  const m = typeof value === 'string' ? RE.ticket.exec(value) : null;
  return m ? Number(m[1]) : null;
}

const api = { ALPHABET, ROLES, KINDS, SHOWN, matches, kindOf, mintActor, mintCorrelation,
              mintTicket, mintPipeline, handle, parseHandle, ticketSeq };
if (typeof module !== 'undefined') module.exports = api;
else globalThis.llmIds = api;

if (typeof require !== 'undefined' && typeof module !== 'undefined'
    && require.main === module) {
  const i = process.argv.indexOf('--batch');
  const doc = JSON.parse(require('fs').readFileSync(process.argv[i + 1], 'utf8'));
  // each case: {op, args, draws?} - draws script the random letters, so a
  // mint can be compared across the two languages
  const out = doc.cases.map(c => {
    const queue = (c.draws || []).slice();
    const draw = () => queue.shift();
    const fn = { matches, kindOf, parseHandle, ticketSeq, handle,
                 mintActor: (r) => mintActor(r, draw),
                 mintCorrelation: (n) => mintCorrelation(n, draw),
                 mintTicket: (s) => mintTicket(s, draw),
                 mintPipeline: () => mintPipeline(draw) }[c.op];
    try { return { ok: fn(...c.args) }; } catch (e) { return { error: true }; }
  });
  process.stdout.write(JSON.stringify(out));
}

}).call(globalThis);
/* ---- @contract/validate.js ---- */
(function () {
/* Check JSON values against the contract's shapes. No dependencies.

   The same algorithm as validate.py, producing the same problems character
   for character - test_contract.py runs both over every case and compares,
   because two validators that disagree are two contracts. The shape language
   is described once, in validate.py.

   In a browser:  llmContract.check(value, shape, shapes)
   In node:       const { check } = require('./validate.js')
   For the agreement test:
                  node validate.js --batch <cases.json>                     */
'use strict';

// The id grammar the shape language's {"id": kind} reads - ids.js, beside this.
const ids = (typeof require !== 'undefined' && typeof module !== 'undefined')
  ? require('./ids.js') : globalThis.llmIds;

// A field is present only if the value OWNS it. `'constructor' in {}` is
// true in JavaScript, so a required field of that name would never be missing.
const own = (o, k) => Object.prototype.hasOwnProperty.call(o, k);

function kindOf(v) {
  if (v === null) return 'null';
  if (Array.isArray(v)) return 'array';
  return typeof v;
}

function render(v) {
  if (v === true) return 'true';
  if (v === false) return 'false';
  if (v === null) return 'null';
  if (typeof v === 'number') return String(v);
  if (typeof v === 'string') return JSON.stringify(v);
  return kindOf(v);
}

function deref(shape, shapes) {
  while (shape && typeof shape === 'object' && !Array.isArray(shape) && own(shape, 'ref'))
    shape = shapes[shape.ref];
  return shape;
}

function label(shape) {
  if (typeof shape === 'string') return shape;
  if (Array.isArray(shape)) return shape.map(label).join(' | ');
  if (own(shape, 'ref')) return shape.ref;
  if (own(shape, 'id')) return `${shape.id} id`;
  for (const k of ['enum', 'array', 'object']) if (own(shape, k)) return k;
  return '?';
}

function topKind(shape, shapes) {
  shape = deref(shape, shapes);
  if (typeof shape === 'string')
    return shape === 'integer' ? 'number' : shape === 'any' ? null : shape;
  if (shape && typeof shape === 'object' && !Array.isArray(shape)) {
    if (own(shape, 'array')) return 'array';
    if (own(shape, 'object')) return 'object';
    if (own(shape, 'id')) return 'string';
  }
  return null;
}

function check(v, shape, shapes = {}, at = '$') {
  shape = deref(shape, shapes);
  if (typeof shape === 'string') {
    if (shape === 'any') return [];
    const got = kindOf(v);
    const ok = shape === 'integer' ? got === 'number' && Number.isInteger(v)
                                   : got === shape;
    return ok ? [] : [`${at}: expected ${shape}, got ${got}`];
  }
  if (Array.isArray(shape)) {
    if (shape.some(alt => check(v, alt, shapes, at).length === 0)) return [];
    const got = kindOf(v);
    const same = shape.filter(alt => topKind(alt, shapes) === got);
    if (same.length === 1) return check(v, same[0], shapes, at);
    return [`${at}: expected ${shape.map(label).join(' | ')}, got ${got}`];
  }
  if (own(shape, 'id')) {
    if (!ids.KINDS.includes(shape.id)) return [`${at}: malformed shape`];
    if (ids.matches(shape.id, v)) return [];
    const got = typeof v === 'string' ? render(v) : kindOf(v);
    return [`${at}: expected ${ids.SHOWN[shape.id]}, got ${got}`];
  }
  if (own(shape, 'enum')) {
    if (shape.enum.some(lit => v === lit)) return [];
    return [`${at}: expected one of [${shape.enum.map(render).join(', ')}], got ${render(v)}`];
  }
  if (own(shape, 'array')) {
    if (!Array.isArray(v)) return [`${at}: expected array, got ${kindOf(v)}`];
    return v.flatMap((item, i) => check(item, shape.array, shapes, `${at}[${i}]`));
  }
  if (own(shape, 'object')) {
    if (kindOf(v) !== 'object') return [`${at}: expected object, got ${kindOf(v)}`];
    const fields = shape.object;
    const optional = new Set(shape.optional || []);
    const names = Object.keys(fields).sort();
    const out = names.filter(n => !own(v, n) && !optional.has(n))
                     .map(n => `${at}: missing required field "${n}"`);
    if (!shape.open)
      for (const n of Object.keys(v).sort())
        if (!own(fields, n)) out.push(`${at}: unexpected field "${n}"`);
    for (const n of names)
      if (own(v, n)) out.push(...check(v[n], fields[n], shapes, `${at}.${n}`));
    return out;
  }
  return [`${at}: malformed shape`];
}

if (typeof module !== 'undefined') module.exports = { check, kindOf, render };
else globalThis.llmContract = { check, kindOf, render };

if (typeof require !== 'undefined' && typeof module !== 'undefined'
    && require.main === module) {
  const i = process.argv.indexOf('--batch');
  const doc = JSON.parse(require('fs').readFileSync(process.argv[i + 1], 'utf8'));
  process.stdout.write(JSON.stringify(doc.cases.map(c => check(c.value, c.shape, doc.shapes))));
}

}).call(globalThis);
/* ---- @routes ---- */
/* generated from the contract - never edited by hand */
const ROUTES = {"server.config.get": {"method": "GET", "path": "/api/workspace/config", "request": null, "responses": {"200": {"object": {"config": [{"object": {}, "open": true}, "null"], "sha256": ["string", "null"]}}, "409": {"object": {"error": "string", "reason": {"enum": ["no_workspace"]}}}, "503": {"object": {"error": "string", "reason": {"enum": ["config_unreadable"]}}}}}, "server.config.keep": {"method": "POST", "path": "/api/workspace/config", "request": {"object": {"config": {"object": {}, "open": true}}}, "responses": {"200": {"object": {"new": "boolean", "sha256": "string"}}, "400": {"object": {"error": "string", "reason": {"enum": ["bad_request", "too_large"]}}}, "409": {"object": {"error": "string", "reason": {"enum": ["no_workspace"]}}}, "503": {"object": {"error": "string", "reason": {"enum": ["config_unwritable"]}}}}}, "server.health": {"method": "GET", "path": "/api/health", "request": null, "responses": {"200": {"object": {"actor": {"id": "actor"}, "card": {"object": {"coordinator": {"enum": ["none", "reachable", "unreachable"]}, "holding": "boolean", "waiting_since": ["number", "null"]}}, "contract_version": "string", "loaded": "boolean", "pipeline": [{"ref": "pipeline"}, "null"], "queue": {"object": {"consecutive_failures": "integer", "held": "integer", "last": [{"object": {"at": "number", "correlation_id": {"id": "correlation"}, "error": ["string", "null"], "job": {"id": "job"}, "status": {"enum": ["done", "failed", "cancelled", "claimed"]}}}, "null"], "queued": "integer", "running": "integer", "scheduled": {"object": {"for_room": "integer", "next_at": ["number", "null"], "planned": "integer", "room": ["string", "null"]}}, "store_fault": {"ref": "fault"}}, "optional": ["scheduled"]}, "runner": [{"ref": "runner"}, "null"], "worker": {"object": {"alive": "boolean", "generation": "integer", "idle_s": "number", "restart_fault": {"ref": "fault"}, "restarts": "integer", "uptime_s": "number"}}, "workspace": ["string", "null"]}}}}, "server.jobs.cancel": {"method": "POST", "path": "/api/jobs/{id}/cancel", "request": null, "responses": {"200": {"object": {"job": {"ref": "job"}, "was": {"enum": ["queued", "running", "held"]}}}, "404": {"object": {"error": "string", "reason": {"enum": ["no_such_job"]}}}, "409": {"object": {"error": "string", "reason": {"enum": ["finished", "no_workspace"]}, "status": {"enum": ["done", "failed", "cancelled", "claimed"]}}, "optional": ["status"]}, "503": {"object": {"error": "string", "reason": {"enum": ["store_unreadable"]}}}}}, "server.jobs.claim": {"method": "POST", "path": "/api/jobs/{id}/claim", "request": {"object": {"client": {"id": "actor"}}, "optional": ["client"]}, "responses": {"200": {"object": {"job": {"ref": "job"}}}, "400": {"object": {"error": "string", "reason": {"enum": ["bad_request"]}}}, "404": {"object": {"error": "string", "reason": {"enum": ["no_such_job"]}}}, "409": {"object": {"error": "string", "reason": {"enum": ["claimed_elsewhere", "not_done", "ended", "no_workspace"]}, "status": {"enum": ["queued", "running", "held", "done", "failed", "cancelled", "claimed"]}}, "optional": ["status"]}, "503": {"object": {"error": "string", "reason": {"enum": ["store_unreadable"]}}}}}, "server.jobs.forget": {"method": "POST", "path": "/api/jobs/{id}/forget", "request": null, "responses": {"200": {"object": {"forgot": {"id": "job"}}}, "404": {"object": {"error": "string", "reason": {"enum": ["no_such_job"]}}}, "409": {"object": {"error": "string", "reason": {"enum": ["unfinished", "needed", "no_workspace"]}, "status": {"enum": ["queued", "running", "held", "done", "failed", "cancelled", "claimed"]}}, "optional": ["status"]}, "503": {"object": {"error": "string", "reason": {"enum": ["store_unreadable"]}}}}}, "server.jobs.give_up": {"method": "POST", "path": "/api/jobs/{id}/give-up", "request": null, "responses": {"200": {"object": {"grace_s": "number", "job": {"ref": "job"}, "was": {"enum": ["queued", "running", "held"]}}}, "404": {"object": {"error": "string", "reason": {"enum": ["no_such_job"]}}}, "409": {"object": {"error": "string", "reason": {"enum": ["finished", "no_workspace"]}, "status": {"enum": ["done", "failed", "cancelled", "claimed"]}}, "optional": ["status"]}, "503": {"object": {"error": "string", "reason": {"enum": ["store_unreadable"]}}}}}, "server.jobs.held": {"method": "GET", "path": "/api/jobs/{id}/held", "request": null, "responses": {"200": {"object": {"held": {"ref": "held_request"}}}, "404": {"object": {"error": "string", "reason": {"enum": ["no_such_job"]}}}, "409": {"object": {"error": "string", "reason": {"enum": ["not_held", "no_workspace"]}, "status": {"enum": ["queued", "running", "done", "failed", "cancelled", "claimed"]}}, "optional": ["status"]}, "503": {"object": {"error": "string", "reason": {"enum": ["store_unreadable"]}}}}}, "server.jobs.list": {"method": "GET", "path": "/api/jobs", "request": null, "responses": {"200": {"object": {"jobs": {"array": {"ref": "job"}}}}, "400": {"object": {"error": "string", "reason": {"enum": ["bad_client", "bad_scope"]}}}, "409": {"object": {"error": "string", "reason": {"enum": ["no_workspace"]}}}, "503": {"object": {"error": "string", "reason": {"enum": ["store_unreadable"]}}}}}, "server.jobs.prune": {"method": "POST", "path": "/api/jobs/prune", "request": {"object": {"keep": "integer", "scope": "string", "statuses": {"array": {"enum": ["done", "failed", "cancelled", "claimed"]}}}, "optional": ["statuses", "keep", "scope"]}, "responses": {"200": {"object": {"left": "integer", "removed": "integer"}}, "400": {"object": {"error": "string", "reason": {"enum": ["bad_request"]}}}, "409": {"object": {"error": "string", "reason": {"enum": ["no_workspace"]}}}, "503": {"object": {"error": "string", "reason": {"enum": ["store_unreadable"]}}}}}, "server.jobs.result": {"method": "GET", "path": "/api/jobs/{id}/result", "request": null, "responses": {"200": {"object": {"candidates": {"array": {"ref": "candidate"}}, "job": {"ref": "job"}}}, "400": {"object": {"error": "string", "reason": {"enum": ["bad_wait"]}}}, "404": {"object": {"error": "string", "reason": {"enum": ["no_such_job"]}}}, "409": {"object": {"error": "string", "reason": {"enum": ["no_workspace"]}}}, "503": {"object": {"error": "string", "reason": {"enum": ["store_unreadable"]}}}}}, "server.jobs.retry": {"method": "POST", "path": "/api/jobs/{id}/retry", "request": null, "responses": {"200": {"object": {"job": {"ref": "job"}, "revived": {"array": {"id": "job"}}}}, "404": {"object": {"error": "string", "reason": {"enum": ["no_such_job"]}}}, "409": {"object": {"error": "string", "reason": {"enum": ["not_retryable", "already_retried", "no_workspace"]}, "status": {"enum": ["queued", "running", "held", "done", "failed", "cancelled", "claimed"]}}, "optional": ["status"]}, "503": {"object": {"error": "string", "reason": {"enum": ["store_unreadable"]}}}}}, "server.jobs.send": {"method": "POST", "path": "/api/jobs/{id}/send", "request": {"object": {"gen": {"object": {"adaptive_decay": "number", "adaptive_target": "number", "add_generation_prompt": "boolean", "backend_sampling": "boolean", "cache_prompt": "boolean", "chat_template_kwargs": {"object": {}, "open": true}, "continue_final_message": ["boolean", {"enum": ["content", "reasoning_content"]}], "dry_allowed_length": "integer", "dry_base": "number", "dry_multiplier": "number", "dry_penalty_last_n": "integer", "dry_sequence_breakers": {"array": "string"}, "dynatemp_exponent": "number", "dynatemp_range": "number", "frequency_penalty": "number", "grammar": "string", "id_slot": "integer", "ignore_eos": "boolean", "json_schema": {"object": {}, "open": true}, "logit_bias": [{"array": "any"}, {"object": {}, "open": true}], "lora": {"array": {"object": {"id": "integer", "scale": "number"}}}, "max_tokens": "integer", "min_keep": "integer", "min_p": "number", "mirostat": "integer", "mirostat_eta": "number", "mirostat_tau": "number", "n_cache_reuse": "integer", "n_discard": "integer", "n_indent": "integer", "n_keep": "integer", "presence_penalty": "number", "reasoning_budget_end_tags": {"array": "string"}, "reasoning_budget_message": "string", "reasoning_budget_start_tag": "string", "reasoning_budget_tokens": "integer", "reasoning_effort": "string", "repeat_last_n": "integer", "repeat_penalty": "number", "response_format": {"object": {"json_schema": {"object": {}, "open": true}, "schema": {"object": {}, "open": true}, "type": {"enum": ["text", "json_object", "json_schema"]}}, "optional": ["schema", "json_schema"]}, "samplers": [{"array": "string"}, "string"], "seed": "integer", "stop": ["string", {"array": "string"}], "t_max_predict_ms": "integer", "temperature": "number", "top_k": "integer", "top_n_sigma": "number", "top_p": "number", "typical_p": "number", "xtc_probability": "number", "xtc_threshold": "number"}, "optional": ["max_tokens", "stop", "ignore_eos", "t_max_predict_ms", "n_indent", "n_keep", "n_discard", "seed", "temperature", "dynatemp_range", "dynatemp_exponent", "top_k", "top_p", "min_p", "top_n_sigma", "typical_p", "xtc_probability", "xtc_threshold", "min_keep", "repeat_last_n", "repeat_penalty", "presence_penalty", "frequency_penalty", "dry_multiplier", "dry_base", "dry_allowed_length", "dry_penalty_last_n", "dry_sequence_breakers", "mirostat", "mirostat_tau", "mirostat_eta", "adaptive_target", "adaptive_decay", "samplers", "backend_sampling", "logit_bias", "lora", "json_schema", "grammar", "response_format", "cache_prompt", "n_cache_reuse", "id_slot", "reasoning_budget_tokens", "reasoning_budget_message", "reasoning_budget_start_tag", "reasoning_budget_end_tags", "reasoning_effort", "chat_template_kwargs", "add_generation_prompt", "continue_final_message"]}, "request": "any", "unset": {"array": "string"}}, "optional": ["request", "gen", "unset"]}, "responses": {"200": {"object": {"changed": {"array": "string"}, "job": {"ref": "job"}, "tuned": "boolean"}}, "400": {"object": {"error": "string", "reason": {"enum": ["bad_request", "bad_max_tokens", "out_of_range", "bad_audio_request"]}}}, "404": {"object": {"error": "string", "reason": {"enum": ["no_such_job"]}}}, "409": {"object": {"error": "string", "reason": {"enum": ["not_held", "no_workspace"]}, "status": {"enum": ["queued", "running", "done", "failed", "cancelled", "claimed"]}}, "optional": ["status"]}, "503": {"object": {"error": "string", "reason": {"enum": ["store_unreadable"]}}}}}, "server.jobs.submit": {"method": "POST", "path": "/api/jobs", "request": {"object": {"after": {"array": {"id": "job"}}, "card_patience_s": "number", "check": "boolean", "client": {"id": "actor"}, "correlation_id": {"id": "correlation"}, "gen": {"object": {"adaptive_decay": "number", "adaptive_target": "number", "add_generation_prompt": "boolean", "backend_sampling": "boolean", "cache_prompt": "boolean", "chat_template_kwargs": {"object": {}, "open": true}, "continue_final_message": ["boolean", {"enum": ["content", "reasoning_content"]}], "dry_allowed_length": "integer", "dry_base": "number", "dry_multiplier": "number", "dry_penalty_last_n": "integer", "dry_sequence_breakers": {"array": "string"}, "dynatemp_exponent": "number", "dynatemp_range": "number", "frequency_penalty": "number", "grammar": "string", "id_slot": "integer", "ignore_eos": "boolean", "json_schema": {"object": {}, "open": true}, "logit_bias": [{"array": "any"}, {"object": {}, "open": true}], "lora": {"array": {"object": {"id": "integer", "scale": "number"}}}, "max_tokens": "integer", "min_keep": "integer", "min_p": "number", "mirostat": "integer", "mirostat_eta": "number", "mirostat_tau": "number", "model": "string", "n": "integer", "n_cache_reuse": "integer", "n_discard": "integer", "n_indent": "integer", "n_keep": "integer", "presence_penalty": "number", "reasoning_budget_end_tags": {"array": "string"}, "reasoning_budget_message": "string", "reasoning_budget_start_tag": "string", "reasoning_budget_tokens": "integer", "reasoning_effort": "string", "repeat_last_n": "integer", "repeat_penalty": "number", "response_format": {"object": {"json_schema": {"object": {}, "open": true}, "schema": {"object": {}, "open": true}, "type": {"enum": ["text", "json_object", "json_schema"]}}, "optional": ["schema", "json_schema"]}, "retries": "integer", "samplers": [{"array": "string"}, "string"], "seed": "integer", "stop": ["string", {"array": "string"}], "t_max_predict_ms": "integer", "temperature": "number", "top_k": "integer", "top_n_sigma": "number", "top_p": "number", "typical_p": "number", "xtc_probability": "number", "xtc_threshold": "number"}, "optional": ["n", "retries", "max_tokens", "stop", "ignore_eos", "t_max_predict_ms", "n_indent", "n_keep", "n_discard", "seed", "temperature", "dynatemp_range", "dynatemp_exponent", "top_k", "top_p", "min_p", "top_n_sigma", "typical_p", "xtc_probability", "xtc_threshold", "min_keep", "repeat_last_n", "repeat_penalty", "presence_penalty", "frequency_penalty", "dry_multiplier", "dry_base", "dry_allowed_length", "dry_penalty_last_n", "dry_sequence_breakers", "mirostat", "mirostat_tau", "mirostat_eta", "adaptive_target", "adaptive_decay", "samplers", "backend_sampling", "logit_bias", "lora", "json_schema", "grammar", "response_format", "cache_prompt", "n_cache_reuse", "id_slot", "reasoning_budget_tokens", "reasoning_budget_message", "reasoning_budget_start_tag", "reasoning_budget_end_tags", "reasoning_effort", "chat_template_kwargs", "add_generation_prompt", "continue_final_message"]}, "group": "string", "group_size": "integer", "if_no_room": {"enum": ["wait", "fail"]}, "input": "any", "kind": "string", "name": "string", "not_before": "number", "priority": {"enum": ["interactive", "batch"]}, "scope": "string"}, "optional": ["input", "name", "priority", "card_patience_s", "after", "group", "group_size", "scope", "client", "correlation_id", "check", "not_before", "if_no_room"]}, "responses": {"200": {"object": {"correlation_id": {"id": "correlation"}, "correlation_source": {"enum": ["client", "server"]}, "job_id": {"id": "job"}, "queue_pos": "integer"}}, "400": {"object": {"error": "string", "reason": {"enum": ["no_kind", "no_model", "unknown_after", "bad_group_size", "bad_patience", "bad_max_tokens", "out_of_range", "bad_request", "bad_audio_request", "live_is_now"]}}}, "409": {"object": {"error": "string", "reason": {"enum": ["no_workspace", "group_full", "correlation_taken"]}}}, "503": {"object": {"error": "string", "reason": {"enum": ["store_unreadable"]}}}}}, "server.models.list": {"method": "GET", "path": "/api/models", "request": null, "responses": {"200": {"object": {"loads_from": ["string", "null"], "models": {"array": {"ref": "model"}}, "models_dir": ["string", "null"], "runner": [{"enum": ["mock", "real", "llamacpp"]}, "null"], "slow_answer": [{"object": {"factor": "number", "floor_s": "number"}}, "null"]}}}}, "server.models.load": {"method": "POST", "path": "/api/models/{key}/load", "request": null, "responses": {"200": {"object": {"model": {"ref": "model_state"}, "pipeline": {"ref": "pipeline"}}}, "400": {"object": {"error": "string", "reason": {"enum": ["bad_wait"]}}}, "404": {"object": {"error": "string", "reason": {"enum": ["no_model"]}}}, "409": {"object": {"error": "string", "reason": {"enum": ["no_runner", "no_pipeline", "too_many_models", "stopping"]}}}}}, "server.models.read_speed": {"method": "POST", "path": "/api/models/{key}/read-speed", "request": null, "responses": {"200": {"object": {"disk": ["string", "null"], "key": "string", "measure": {"ref": "read_speed"}, "path": "string"}}, "404": {"object": {"error": "string", "reason": {"enum": ["no_model"]}}}, "409": {"object": {"error": "string", "reason": {"enum": ["no_file", "busy", "unreadable", "no_runner"]}}}}}, "server.models.unload": {"method": "POST", "path": "/api/models/{key}/unload", "request": null, "responses": {"200": {"object": {"model": {"ref": "model_state"}}}, "404": {"object": {"error": "string", "reason": {"enum": ["no_model"]}}}, "409": {"object": {"error": "string", "reason": {"enum": ["no_runner", "in_use", "unload_failed"]}}}}}, "server.pipeline.close": {"method": "POST", "path": "/api/pipeline/close", "request": {"object": {"pipeline": {"id": "pipeline"}}}, "responses": {"200": {"object": {"pipeline": {"ref": "pipeline"}}}, "400": {"object": {"error": "string", "reason": {"enum": ["bad_request"]}}}, "409": {"object": {"error": "string", "reason": {"enum": ["not_open", "no_runner", "unload_failed"]}}}}}, "server.pipeline.open": {"method": "POST", "path": "/api/pipeline/open", "request": {"object": {"class": {"enum": ["responsive", "scheduled"]}, "if_no_room": {"enum": ["wait", "fail"]}, "keep_s": "number", "label": "string", "models": {"array": "string"}}, "optional": ["models", "class", "label", "keep_s", "if_no_room"]}, "responses": {"200": {"object": {"pipeline": {"ref": "pipeline"}}}, "400": {"object": {"error": "string", "reason": {"enum": ["bad_request", "unknown_model", "too_many_models", "repeated_model", "bad_keep", "bad_wait", "live_is_now"]}}}, "409": {"object": {"error": "string", "reason": {"enum": ["already_open", "no_runner", "stopping"]}}}}}, "server.pipeline.renew": {"method": "POST", "path": "/api/pipeline/renew", "request": {"object": {"pipeline": {"id": "pipeline"}}}, "responses": {"200": {"object": {"pipeline": {"ref": "pipeline"}}}, "400": {"object": {"error": "string", "reason": {"enum": ["bad_request"]}}}, "409": {"object": {"error": "string", "reason": {"enum": ["not_open", "no_runner"]}}}}}, "server.stop": {"method": "POST", "path": "/api/stop", "request": null, "responses": {"202": {"object": {"actor": {"id": "actor"}, "stopping": {"enum": [true]}}}, "403": {"object": {"error": "string", "reason": {"enum": ["from_a_page"]}}}}}, "server.workspace.bind": {"method": "POST", "path": "/api/workspace", "request": {"object": {"path": "string"}}, "responses": {"200": {"object": {"loaded": {"enum": [true]}, "workspace": "string"}}, "400": {"object": {"error": "string", "reason": {"enum": ["no_path", "unusable_folder", "bad_request"]}}}, "409": {"object": {"error": "string", "holder": [{"object": {"actor": [{"id": "actor"}, "null"], "pid": "integer", "since": "number", "url": ["string", "null"]}}, "null"], "job": {"id": "job"}, "reason": {"enum": ["busy", "folder_in_use"]}}, "optional": ["job", "holder"]}}}, "server.workspace.get": {"method": "GET", "path": "/api/workspace", "request": null, "responses": {"200": {"object": {"loaded": "boolean", "workspace": ["string", "null"]}}}}, "server.workspace.unload": {"method": "POST", "path": "/api/workspace/unload", "request": null, "responses": {"200": {"object": {"loaded": {"enum": [false]}, "was": ["string", "null"]}}, "409": {"object": {"error": "string", "job": {"id": "job"}, "reason": {"enum": ["busy"]}}}}}};
const SHAPES = {"audio_request": {"object": {"audio": "string", "fields": {"object": {"audio_ctx": "number", "beam_size": "integer", "best_of": "integer", "carry_initial_prompt": "boolean", "debug_mode": "boolean", "detect_language": "boolean", "diarize": "boolean", "duration": "integer", "entropy_thold": "number", "language": "string", "logprob_thold": "number", "max_context": "integer", "max_len": "integer", "no_language_probabilities": "boolean", "no_speech_thold": "number", "no_timestamps": "boolean", "offset_n": "integer", "offset_t": "integer", "prompt": "string", "split_on_word": "boolean", "suppress_non_speech": "boolean", "suppress_nst": "boolean", "temperature": "number", "temperature_inc": "number", "tinydiarize": "boolean", "token_timestamps": "boolean", "translate": "boolean", "vad": "boolean", "vad_max_speech_duration_s": "number", "vad_min_silence_duration_ms": "number", "vad_min_speech_duration_ms": "number", "vad_samples_overlap": "number", "vad_speech_pad_ms": "integer", "vad_threshold": "number", "word_thold": "number"}, "optional": ["offset_t", "offset_n", "duration", "max_context", "max_len", "best_of", "beam_size", "audio_ctx", "word_thold", "entropy_thold", "logprob_thold", "no_speech_thold", "debug_mode", "translate", "diarize", "tinydiarize", "split_on_word", "no_timestamps", "token_timestamps", "language", "detect_language", "prompt", "carry_initial_prompt", "temperature", "temperature_inc", "suppress_non_speech", "suppress_nst", "vad", "vad_threshold", "vad_min_speech_duration_ms", "vad_min_silence_duration_ms", "vad_max_speech_duration_s", "vad_speech_pad_ms", "vad_samples_overlap", "no_language_probabilities"]}}, "optional": ["fields"]}, "candidate": {"object": {"attempts": {"array": {"object": {"attempt": "integer", "sample": "integer", "seed": ["integer", "null"], "why": ["string", "null"]}}}, "audio_s": "number", "finish_reason": ["string", "null"], "handle": {"id": "handle"}, "load": [{"id": "actor"}, "null"], "model": "string", "reasoning": "string", "sample": "integer", "seed": ["integer", "null"], "text": "string", "timings": {"object": {"predicted_n": "integer", "predicted_per_second": "number"}, "open": true, "optional": ["predicted_n", "predicted_per_second"]}, "tokens": ["integer", "null"], "transcript": {"ref": "transcript"}, "usage": {"object": {"completion_tokens": "integer", "prompt_tokens": "integer", "total_tokens": "integer"}, "open": true, "optional": ["prompt_tokens", "completion_tokens", "total_tokens"]}, "verdict": {"object": {"why": ["string", "null"]}, "open": true, "optional": ["why"]}}, "open": true, "optional": ["sample", "seed", "handle", "attempts", "verdict", "load", "reasoning", "usage", "timings", "transcript", "audio_s"]}, "conversation": {"object": {"messages": {"array": {"object": {"content": "string", "role": {"enum": ["system", "user", "assistant"]}}}}}}, "degraded": {"object": {"error": ["string", "null"], "floor_mib": "integer", "free_mib": ["integer", "null"], "need_mib": ["integer", "null"], "outside_mib": ["integer", "null"], "reason": {"enum": ["under_floor", "memory_error", "slow_answer"]}, "since": "number", "total_mib": ["integer", "null"], "why": "string"}, "optional": ["need_mib", "outside_mib"]}, "event": {"object": {"actor": {"id": "actor"}, "at": "number", "class": {"enum": ["responsive", "scheduled"]}, "degraded": {"ref": "degraded"}, "detail": "string", "for": {"id": "correlation"}, "free_mib": "integer", "holder": "string", "if_no_room": {"enum": ["wait", "fail"]}, "kind": {"enum": ["coordinator_started", "state_rebuilt", "lock_waiting", "lock_taken", "ticket_asked", "ticket_offered", "ticket_unclaimed", "ticket_dropped", "ticket_cancelled", "ticket_passed_over", "ticket_refused", "rental_granted", "rental_processes", "rental_releasing", "rental_amended", "amend_refused", "rental_hung", "rental_degraded", "rental_restored", "rental_released", "rental_reclaiming", "rental_reclaimed", "process_ended", "process_gone", "process_left_alone", "process_not_ended", "port_granted", "port_released", "port_reclaimed"]}, "label": "string", "memory": {"ref": "memory"}, "memory_errors": "integer", "models": {"array": {"ref": "need"}}, "need_mib": "integer", "not_before": "number", "outside_mib": ["integer", "null"], "port": "integer", "process": {"ref": "named_process"}, "processes": {"array": {"ref": "named_process"}}, "purpose": "string", "seq": "integer", "ticket_id": {"id": "ticket"}, "token_sha256": "string"}, "optional": ["ticket_id", "holder", "actor", "class", "models", "label", "for", "token_sha256", "port", "purpose", "detail", "processes", "process", "memory", "degraded", "free_mib", "need_mib", "outside_mib", "memory_errors", "not_before", "if_no_room"]}, "fault": {"object": {"age_s": ["number", "null"], "at": "number", "count": "integer", "detail": "string", "kind": "string"}}, "ggml_header": {"object": {"ftype": "integer", "n_audio_ctx": "integer", "n_audio_head": "integer", "n_audio_layer": "integer", "n_audio_state": "integer", "n_mels": "integer", "n_text_ctx": "integer", "n_text_head": "integer", "n_text_layer": "integer", "n_text_state": "integer", "n_vocab": "integer", "shape": "string"}}, "gguf_header": {"object": {"architecture": "string", "bits_per_weight": ["number", "null"], "chat_template": "boolean", "trained_context": ["integer", "null"]}}, "held_request": {"object": {"audio": [{"object": {"path": "string", "seconds": ["number", "null"]}}, "null"], "count": [{"object": {"context": "integer", "prompt_tokens": "integer", "room": "integer"}}, "null"], "engine": [{"enum": ["llamacpp", "whisper"]}, "null"], "expect": [{"object": {"measure": {"enum": ["answer_s", "real_time_factor"]}, "median_s": "number", "p90_s": "number", "samples": "integer"}}, "null"], "fields": [{"object": {}, "open": true}, "null"], "fits": ["boolean", "null"], "gen": {"object": {}, "open": true}, "held_at": "number", "job": {"ref": "job"}, "messages": [{"array": {"object": {"content": "string", "role": {"enum": ["system", "user", "assistant"]}}}}, "null"], "model": "string", "params": {"array": {"object": {"from": {"enum": ["application", "model", "bootstrap"]}, "name": "string", "value": "any"}}}, "request": "any", "said": {"array": "string"}}}, "job": {"object": {"after": {"array": {"id": "job"}}, "check": {"enum": ["marked", "held", "sent", "tuned"]}, "claimed_by": [{"id": "actor"}, "null"], "client": [{"id": "actor"}, "null"], "correlation_id": {"id": "correlation"}, "correlation_source": {"enum": ["client", "server"]}, "created": "number", "error": ["string", "null"], "finished": ["number", "null"], "group": ["string", "null"], "id": {"id": "job"}, "if_no_room": {"enum": ["wait", "fail"]}, "kind": "string", "model": "string", "n": "integer", "n_done": "integer", "name": "string", "not_before": "number", "priority": {"enum": ["interactive", "batch"]}, "progress": {"ref": "progress"}, "queue_pos": "integer", "reason": {"enum": ["card_patience", "card_busy", "judge_refused", "prompt_too_long", "no_room", "never_fits", "card_degraded"]}, "retry_of": [{"id": "job"}, "null"], "scope": ["string", "null"], "stalled_for": ["number", "null"], "status": {"enum": ["queued", "running", "held", "done", "failed", "cancelled", "claimed"]}}, "optional": ["reason", "check", "not_before", "if_no_room"]}, "load_history": {"object": {"last": ["number", "null"], "last_at": ["number", "null"], "normal": ["number", "null"], "runs": "integer", "slow": "boolean"}}, "memory": {"object": {"free_mib": ["integer", "null"], "margin_mib": "integer", "need_mib": "integer", "total_mib": ["integer", "null"]}}, "model": {"object": {"context": ["integer", "null"], "engine": {"enum": ["llamacpp", "whisper"]}, "file": [{"object": {"disk": ["string", "null"], "name": "string", "path": "string", "present": "boolean", "size": ["integer", "null"]}}, "null"], "header": [{"ref": "gguf_header"}, {"ref": "ggml_header"}, "null"], "key": "string", "label": "string", "loaded": "boolean", "loads": [{"object": {"cold": {"ref": "load_history"}, "warm": {"ref": "load_history"}}}, "null"], "note": "string", "pinned": "boolean", "problems": {"array": "string"}, "read_speed": [{"ref": "read_speed"}, "null"], "state": {"ref": "model_state"}, "stats": [{"ref": "model_stats"}, "null"]}}, "model_state": {"object": {"key": "string", "load": [{"id": "actor"}, "null"], "state": {"enum": ["off", "waiting", "loading", "up", "failed"]}, "why": ["string", "null"]}}, "model_stats": {"object": {"answer_s": {"ref": "stat"}, "audio_s": {"ref": "stat"}, "first_answer_mib": {"ref": "stat"}, "first_token_s": {"ref": "stat"}, "load_claim_s": {"ref": "stat"}, "load_healthy_s": {"ref": "stat"}, "load_identity_s": {"ref": "stat"}, "load_port_s": {"ref": "stat"}, "load_s": {"ref": "stat"}, "load_spawn_s": {"ref": "stat"}, "load_wait_s": {"ref": "stat"}, "prompt_tokens_per_s": {"ref": "stat"}, "queue_s": {"ref": "stat"}, "ready_mib": {"ref": "stat"}, "real_time_factor": {"ref": "stat"}, "slow_answer": {"ref": "stat"}, "tokens": {"ref": "stat"}, "tokens_per_s": {"ref": "stat"}, "unload_memory_s": {"ref": "stat"}, "unload_s": {"ref": "stat"}}, "optional": ["load_s", "load_wait_s", "load_claim_s", "load_spawn_s", "load_port_s", "load_healthy_s", "load_identity_s", "ready_mib", "first_answer_mib", "first_token_s", "answer_s", "tokens", "tokens_per_s", "prompt_tokens_per_s", "audio_s", "real_time_factor", "slow_answer", "unload_s", "unload_memory_s", "queue_s"]}, "named_process": {"object": {"engine": "string", "mib": ["integer", "null"], "model": "string", "pid": "integer", "port": ["integer", "null"], "proof": "string", "since": "number"}}, "need": {"object": {"engine": "string", "key": "string", "need_mib": ["integer", "null"]}}, "pipeline": {"object": {"class": [{"enum": ["responsive", "scheduled"]}, "null"], "id": [{"id": "pipeline"}, "null"], "keep_s": ["number", "null"], "label": ["string", "null"], "models": {"array": "string"}, "opened_by": [{"enum": ["application", "start", "queue"]}, "null"], "since": ["number", "null"], "state": {"enum": ["closed", "waiting", "holding", "degraded", "lost"]}, "up": {"array": "string"}, "why": ["string", "null"]}}, "port": {"object": {"actor": {"id": "actor"}, "expires_at": "number", "granted_at": "number", "port": "integer", "purpose": "string"}}, "process": {"object": {"engine": "string", "mib": ["integer", "null"], "model": "string", "pid": "integer", "port": ["integer", "null"], "proof": "string"}}, "progress": {"object": {"at": "number", "budget": "integer", "count": {"object": {"context": "integer", "prompt_tokens": "integer", "room": "integer"}}, "due_s": "number", "free_mib": ["integer", "null"], "handle": {"id": "handle"}, "land_error": "string", "loading": "boolean", "need_mib": ["integer", "null"], "of": "integer", "outside_mib": ["integer", "null"], "patience_left_s": "integer", "reported": "boolean", "spent": "integer", "stage": "string", "tok_s": "number", "tokens": "integer", "variant": "integer", "waited": "integer", "waited_s": "integer", "waiting_for": "string"}, "open": true, "optional": ["variant", "of", "budget", "spent", "handle", "at", "reported", "waited", "loading", "stage", "tokens", "tok_s", "waiting_for", "waited_s", "land_error", "count", "due_s", "free_mib", "need_mib", "outside_mib", "patience_left_s"]}, "read_speed": {"object": {"at": "number", "bytes": "integer", "mb_s": "number", "offset": "integer", "past_cache": ["boolean", "null"], "said": "string", "seconds": "number"}}, "rental": {"object": {"actor": {"id": "actor"}, "class": {"enum": ["responsive", "scheduled"]}, "degraded": [{"ref": "degraded"}, "null"], "expires_at": "number", "for": [{"id": "correlation"}, "null"], "granted_at": "number", "holder": "string", "id": {"id": "ticket"}, "label": ["string", "null"], "models": {"array": {"ref": "need"}}, "processes": {"array": {"ref": "named_process"}}, "state": {"enum": ["held", "releasing", "released", "reclaiming"]}}}, "runner": {"object": {"backend": {"enum": ["mock", "real", "llamacpp"]}, "idle_unload_s": "number", "in_use": "integer", "in_use_models": {"array": "string"}, "last_used": ["number", "null"], "lease": {"object": {"held_seconds": ["number", "null"], "holder": [{"id": "actor"}, "null"], "model_key": ["string", "null"], "since": ["number", "null"]}}, "load": [{"id": "actor"}, "null"], "load_baseline_seconds": ["number", "null"], "load_cold": ["boolean", "null"], "load_seconds": "number", "load_slow": "boolean", "loaded": ["string", "null"], "loaded_at": ["number", "null"], "loaded_label": ["string", "null"], "loading": ["string", "null"], "loads_this_session": "integer", "pid": ["integer", "null"], "port": ["integer", "null"]}}, "stat": {"object": {"count": "integer", "kept": "integer", "last": "number", "last_at": "number", "least": "number", "mean": "number", "median": "number", "most": "number", "p90": "number"}}, "ticket": {"object": {"actor": {"id": "actor"}, "class": {"enum": ["responsive", "scheduled"]}, "for": [{"id": "correlation"}, "null"], "holder": "string", "id": {"id": "ticket"}, "if_no_room": {"enum": ["wait", "fail"]}, "label": ["string", "null"], "models": {"array": {"ref": "need"}}, "not_before": ["number", "null"], "offered_until": ["number", "null"], "room": [{"object": {"at": "number", "memory": {"ref": "memory"}, "outside_mib": ["integer", "null"], "words": "string"}}, "null"], "since": "number", "state": {"enum": ["waiting", "offered", "held"]}}, "optional": ["not_before", "if_no_room", "room"]}, "transcript": {"object": {"detected_language": "string", "detected_language_probability": "number", "duration": "number", "language": "string", "language_probabilities": {"object": {}, "open": true}, "segments": {"array": {"object": {"avg_logprob": ["number", "null"], "end": "number", "id": "integer", "no_speech_prob": "number", "start": "number", "temperature": "number", "text": "string", "tokens": {"array": "integer"}, "words": {"array": {"object": {"end": "number", "probability": "number", "start": "number", "t_dtw": "number", "word": "string"}, "open": true, "optional": ["start", "end", "t_dtw"]}}}, "open": true, "optional": ["start", "end", "tokens", "words"]}}, "task": "string", "text": "string"}, "open": true, "optional": ["detected_language", "detected_language_probability", "language_probabilities"]}};
/* ---- dom.js ---- */
/* The DOM, as small as it can be.

   el(tag, props, ...kids)  an element. In `props`: `on<event>` is a listener,
                            `class` the class list, `style` an object of
                            styles, anything else an attribute - `data-el`
                            above all, the control's stable name. A prop that
                            is null, undefined or false is left out; so is a
                            child that is. A string child is text.
   setKids(node, kids)      replace every child of `node`.
   named(name, root)        the element whose `data-el` is `name`.

   A view builds its piece with these and nothing else, and never keeps a
   node another view built. */

function el(tag, props, ...kids) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key.startsWith('on') && typeof value === 'function') {
      node.addEventListener(key.slice(2), value);
    } else if (key === 'class') {
      node.setAttribute('class', String(value));
    } else if (key === 'style' && typeof value === 'object') {
      for (const [name, v] of Object.entries(value)) node.style[name] = v;
    } else {
      node.setAttribute(key, value === true ? '' : String(value));
    }
  }
  appendKids(node, kids);
  return node;
}

function appendKids(node, kids) {
  for (const kid of [kids].flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    node.appendChild(typeof kid === 'object' ? kid : document.createTextNode(String(kid)));
  }
}

function setKids(node, kids) {
  while (node.firstChild) node.removeChild(node.firstChild);
  appendKids(node, kids);
}

function named(name, root) {
  return (root || document).querySelector(`[data-el="${name}"]`);
}
/* ---- store.js ---- */
/* The one store: the whole state of the page, in one value.

   store.get()                  the state now. Frozen: it is read, never written.
   store.set(fn)                the next state is fn(state) - a new value, built
                                from the old; the old is never changed in place.
   store.replace(state)         the whole state at once (the journal's rebuild).
   store.watch(select, fn)      fn(select(state)) now, and again whenever what
                                `select` returns has changed. -> stop watching.

   THIS IS HOW THE PAGE REPAINTS: nothing calls a render. A view is a watcher -
   `select` is its view model (state in, view data out, pure) and `fn` paints
   what it is given and decides nothing. A change reaches exactly the views
   whose view data changed: "changed" is compared by value, so a state change
   that leaves a view's data alone repaints nothing.

   Changes are told at the end of the turn, once: three `set`s in a row are
   one repaint of each view they touched. A view that throws is reported, and
   the others are still told. */

function createStore(initial, report) {
  let state = deepFreeze(initial);
  let pending = false;
  const watchers = new Set();
  const fail = report || ((e) => { throw e; });

  function tell() {
    pending = false;
    for (const w of [...watchers]) {
      if (!watchers.has(w)) continue;           // stopped by a view told before it
      let next;
      try {
        next = w.select(state);
      } catch (e) {
        fail(e, w.name);
        continue;
      }
      if (same(next, w.last)) continue;
      w.last = next;
      try {
        w.fn(next);
      } catch (e) {
        fail(e, w.name);
      }
    }
  }

  function changed(next) {
    if (next === state) return;
    state = deepFreeze(next);
    if (!pending) {
      pending = true;
      queueMicrotask(tell);
    }
  }

  return {
    get: () => state,
    set: (fn) => changed(fn(state)),
    replace: (next) => changed(next),
    watch(select, fn, name) {
      const w = { select, fn, name: name || select.name || 'a view', last: undefined };
      watchers.add(w);
      w.last = select(state);
      fn(w.last);
      return () => watchers.delete(w);
    },
  };
}

/* Two view data are the same when they hold the same values: JSON-like data -
   objects, arrays, strings, numbers, booleans, null - compared all the way
   down. A function or a node in view data is compared by identity. */
function same(a, b) {
  if (a === b) return true;
  if (typeof a !== 'object' || typeof b !== 'object' || a === null || b === null) {
    return Number.isNaN(a) && Number.isNaN(b);
  }
  if (Array.isArray(a) !== Array.isArray(b)) return false;
  const ka = Object.keys(a);
  const kb = Object.keys(b);
  if (ka.length !== kb.length) return false;
  return ka.every((k) => Object.prototype.hasOwnProperty.call(b, k) && same(a[k], b[k]));
}

function deepFreeze(value) {
  if (value && typeof value === 'object' && !Object.isFrozen(value)) {
    Object.freeze(value);
    for (const v of Object.values(value)) deepFreeze(v);
  }
  return value;
}

/* A copy of `obj` with the value at `path` (a list of keys) replaced by
   fn(the value there) - the one way a part builds the next state. */
function update(obj, path, fn) {
  if (!path.length) return fn(obj);
  const [key, ...rest] = path;
  const here = obj == null ? undefined : obj[key];
  const next = update(here, rest, fn);
  if (Array.isArray(obj)) {
    const copy = obj.slice();
    copy[key] = next;
    return copy;
  }
  return { ...(obj || {}), [key]: next };
}
/* ---- api.js ---- */
/* One door to the server.

   api(id, {params, query, body, quiet}) -> {status, answer}

   `id` is the route's id in the contract (`server.jobs.submit`), never a path:
   the path, the method and every answer's shape come from the contract, built
   into this page. A body the route refuses - or any body, for a route that
   takes none - is a bug on THIS side and throws at once, before anything is
   sent.

   EVERY ANSWER IS CHECKED against the shape the contract declares for its
   status. An answer the contract does not declare - an unknown status, a
   field missing, a reason not in the list - is NO ANSWER: `NoAnswer` is
   thrown, as for a server that did not answer at all. A refusal the contract
   does declare (409 busy, 400 bad_request) is an answer: it is returned, and
   the caller shows its `error` sentence and branches on its `reason`.

   WHERE IT ASKS. By default, the page's own address: a path alone, asked of
   the server the page came from - the bootstrap server serving its own page.
   A page of an application's own that shows these widgets names its bootstrap
   server in a script that runs before the page's own,

     window.llmServer = 'http://127.0.0.1:7072';

   and every request goes there. That server must be started to allow the
   page's own address (`server.py --allow-origin http://127.0.0.1:7867`), and
   it allows no other page. An address that is not one - `http://<host>:<port>`
   and nothing after it - is never guessed around, mended, or replaced by the
   page's own: nothing is asked of anybody, and every ask is no answer,
   saying why. */

class NoAnswer extends Error {}

const SERVER = serverAt(globalThis.llmServer);

/* -> {base, wrong}: `base` goes before every path - '' for the page's own
   address; `wrong` says why an address named is not one */
function serverAt(named) {
  if (named === undefined || named === null) return { base: '', wrong: null };
  const at = typeof named === 'string' ? /^(https?:\/\/[^/?#@\s]+)\/?$/i.exec(named) : null;
  if (!at) {
    return {
      base: null,
      wrong: `the page names its server as ${JSON.stringify(named)}, which is not a server's address: `
        + 'one is written http://<host>:<port>',
    };
  }
  return { base: at[1], wrong: null };
}

function routePath(route, params) {
  return route.path.replace(/\{(\w+)\}/g, (_, key) => {
    if (params[key] === undefined) throw new Error(`${route.id || route.path}: no ${key} given`);
    return encodeURIComponent(String(params[key]));
  });
}

async function api(id, { params = {}, query = null, body, quiet = false } = {}) {
  const route = ROUTES[id];
  if (!route) throw new Error(`the contract has no route called ${id}`);
  if (route.request === null && body !== undefined) throw new Error(`${id} takes no body`);
  if (route.request !== null && body !== undefined) {
    const wrong = llmContract.check(body, route.request, SHAPES);
    if (wrong.length) throw new Error(`${id}: not a body the contract allows: ${wrong[0]}`);
  }
  let path = routePath(route, params);
  const q = Object.entries(query || {}).filter(([, v]) => v !== null && v !== undefined);
  if (q.length) path += '?' + q.map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`).join('&');
  if (SERVER.wrong) throw noAnswer(SERVER.wrong, undefined, quiet);
  let resp;
  try {
    resp = await fetch(SERVER.base + path, {
      method: route.method,
      headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (e) {
    const who = SERVER.base ? `the server at ${SERVER.base}` : 'the server';
    throw noAnswer(`${who} did not answer ${id}: ${e.message}`, undefined, quiet);
  }
  let answer;
  try {
    answer = JSON.parse(await resp.text());
  } catch (e) {
    answer = undefined;
  }
  const shape = route.responses[String(resp.status)];
  const wrong = shape === undefined ? ['no shape is declared for it']
    : llmContract.check(answer, shape, SHAPES);
  if (wrong.length) {
    throw noAnswer(`${id} answered ${resp.status}, which the contract does not declare: ${wrong[0]}`,
      JSON.stringify(answer), quiet);
  }
  return { status: resp.status, answer };
}

/* No answer is said in the log, once, by the door itself - a caller that
   shows it too says it to the person, not to the log again. A caller that
   asks again and again (a feed) asks quietly, and says it itself, once per
   silence rather than once per ask. */
function noAnswer(text, detail, quiet) {
  if (!quiet) logLine('error', 'api', text, { detail });
  return new NoAnswer(text);
}
/* ---- registry.js ---- */
/* What the kit is made of. A part that brings a widget, a tool or a start
   registers it here; kit.js, the last part, places them where a page asks.

   ITEMS   {id, title, box: {w, h} in em and lines, critical,
            view(node) -> stop, panel(node) -> stop (optional)} - the widgets
           with a box: placed in a container's slot, or alone in an element
           of the page (llmKit.place), in the box they declare. `critical`:
           never shed, never put away - the way back (Customize)
   TOOLS   {id, title, rank, view(node) -> stop} - the instruments: each
           placed alone in an element of the page, filling it, or all of
           them behind one door (llmKit.place('tools', ...)), shown one at a
           time in the order of their rank - the door opens on the first
   STARTS  function() - run once, when the first thing is placed: what the
           kit must learn from the server before a person does anything
   SEEDS   {[slice]: its first value} - the part's own slice of the state

   `view(node)` builds the thing into `node` and returns what stops it: every
   watcher it started, every feed it follows. A thing that is not placed is
   stopped. An item's `panel` is shown beside it while `ui.panel` names the
   item. There is no place here for an application's own screens: an
   application builds its own page, and places these in it. */

const ITEMS = [];
const TOOLS = [];
/* how many times each tool is placed alone on the page, by its id - running
   machinery, not state (kit.js keeps it): the queue's panel says a held
   request can be checked in a Check placed on the page (queue.js) */
const PLACED = {};
const STARTS = [];
const SEEDS = {};
/* ---- log.js ---- */
/* The log: what the page did, and what went wrong, for a person to read.

   logLine(level, area, text, {detail, corr})
       level  error | warn | info
       area   one of LOG_AREAS - what the line is about
       corr   the request it belongs to, when it belongs to one

   The log is kept in the store, in memory: a reload starts it empty.

   A LINE SAID AGAIN - the same level, area and words, for the SAME request -
   within the last few is the same line counted again: its count grows and its
   time moves on. The same words for two requests are two lines: two jobs that
   failed alike are two failures, never one.

   At most LOG_CAP lines are kept; the oldest go first, and are counted. Every
   error since the log was last cleared lights the log's light.

   THE LIGHT OPENS THE LOG: behind the Tools door, when the page has one
   placed (the bootstrap's own page does) - else in the light's own panel,
   beside it, so a page with the light alone can still read what went
   wrong. */

const LOG_AREAS = Object.freeze(['page', 'api', 'workspace', 'queue', 'card', 'models', 'bench', 'check', 'layout']);
const LOG_LEVELS = Object.freeze(['error', 'warn', 'info']);
const LOG_CAP = 500;
const LOG_SHOWN = 200;
const LOG_FOLD_WITHIN = 24;

SEEDS.log = {
  lines: [], seq: 0, dropped: 0, errors: 0, paused: null, open: [],
  levels: { error: true, warn: true, info: false }, area: 'all', text: '',
};

function logLine(level, area, text, extra) {
  const x = extra || {};
  const line = {
    level: LOG_LEVELS.includes(level) ? level : 'error',
    area: LOG_AREAS.includes(area) ? area : 'page',
    text: String(text).slice(0, 200),
    detail: x.detail === undefined || x.detail === null ? null : String(x.detail).slice(0, 8000),
    corr: x.corr || null,
    at: Date.now(),
  };
  store.set((s) => update(s, ['log'], (log) => withLine(log, line)));
}

function withLine(log, line) {
  const errors = log.errors + (line.level === 'error' ? 1 : 0);
  for (let i = log.lines.length - 1; i >= Math.max(0, log.lines.length - LOG_FOLD_WITHIN); i--) {
    const l = log.lines[i];
    if (l.level === line.level && l.area === line.area && l.text === line.text && l.corr === line.corr) {
      const lines = log.lines.slice();
      lines[i] = { ...l, count: l.count + 1, at: line.at, detail: line.detail === null ? l.detail : line.detail };
      return { ...log, lines, errors };
    }
  }
  const seq = log.seq + 1;
  let lines = [...log.lines, { ...line, id: seq, count: 1 }];
  let dropped = log.dropped;
  if (lines.length > LOG_CAP) {
    dropped += lines.length - LOG_CAP;
    lines = lines.slice(lines.length - LOG_CAP);
  }
  return { ...log, lines, seq, dropped, errors };
}

/* ---------------------------------------------------------- view models */

function vmLogLight(state) {
  const n = state.log.errors;
  return {
    n,
    title: n ? `${n} error${n === 1 ? '' : 's'} since the log was cleared - open the log`
      : 'Nothing has gone wrong since the log was cleared - open the log',
  };
}

function vmLogList(state) {
  const log = state.log;
  const needle = log.text.trim().toLowerCase();
  const kept = log.lines.filter((l) => (log.paused === null || l.id <= log.paused)
    && log.levels[l.level]
    && (log.area === 'all' || l.area === log.area)
    && (!needle || `${l.text} ${l.detail || ''} ${l.corr || ''}`.toLowerCase().includes(needle)));
  const shown = kept.slice(-LOG_SHOWN).reverse();
  const waiting = log.paused === null ? 0 : log.lines.filter((l) => l.id > log.paused).length;
  const said = [`${log.lines.length} line${log.lines.length === 1 ? '' : 's'} kept`,
    `${shown.length} shown`];
  if (log.dropped) said.push(`${log.dropped} dropped (the oldest)`);
  if (log.paused !== null) said.push(`paused - ${waiting} new`);
  return {
    status: said.join(' · '),
    rows: shown.map((l) => ({
      id: l.id, level: l.level, area: l.area, text: l.text, count: l.count,
      corr: l.corr, time: clockTime(l.at), open: log.open.includes(l.id), detail: l.detail,
    })),
  };
}

function vmLogControls(state) {
  const log = state.log;
  return { levels: log.levels, area: log.area, paused: log.paused !== null };
}

function clockTime(ms) {
  const d = new Date(ms);
  const two = (n) => String(n).padStart(2, '0');
  return `${two(d.getHours())}:${two(d.getMinutes())}:${two(d.getSeconds())}`;
}

/* --------------------------------------------------------------- views */

function setLog(fn) {
  store.set((s) => update(s, ['log'], fn));
}

/* where the log opens: behind a Tools door placed on the page, else beside
   the light */
function openLog() {
  if (DOORS.n) store.set((s) => update(s, ['ui', 'tab'], () => 'log'));
  else store.set((s) => update(s, ['ui', 'panel'], (p) => (p === 'log' ? null : 'log')));
}

ITEMS.push({
  id: 'log', title: 'Log', box: { w: 4, h: 1 },
  view(node) {
    return store.watch(vmLogLight, (vm) => setKids(node, el('button', {
      'data-el': 'log-light', class: vm.n ? 'light bad' : 'light', title: vm.title,
      onclick: () => openLog(),
    }, vm.n ? `● ${vm.n}` : '●')), 'log light');
  },
  panel: (node) => logView(node),
});

TOOLS.push({ id: 'log', title: 'Log', rank: 90, view: (node) => logView(node) });

/* the log itself: its controls, and its lines */
function logView(node) {
  // the controls are built once; only their states follow the store
  const level = (name) => el('label', { class: 'check' },
    el('input', {
      type: 'checkbox', 'data-el': `log-level-${name}`,
      onchange: (e) => setLog((log) => ({ ...log, levels: { ...log.levels, [name]: e.target.checked } })),
    }), ` ${name}`);
  const area = el('select', {
    'data-el': 'log-area', onchange: (e) => setLog((log) => ({ ...log, area: e.target.value })),
  }, el('option', { value: 'all' }, 'every area'), LOG_AREAS.map((a) => el('option', { value: a }, a)));
  const text = el('input', {
    type: 'search', 'data-el': 'log-text', placeholder: 'words to find',
    oninput: (e) => setLog((log) => ({ ...log, text: e.target.value })),
  });
  const pause = el('button', {
    'data-el': 'log-pause',
    onclick: () => setLog((log) => ({ ...log, paused: log.paused === null ? log.seq : null })),
  });
  const controls = el('div', { class: 'controls' }, LOG_LEVELS.map(level), area, text, pause,
    el('button', {
      'data-el': 'log-clear', title: 'Empty the log, and put its light out',
      onclick: () => setLog((log) => ({ ...log, lines: [], dropped: 0, errors: 0, paused: null, open: [] })),
    }, 'Clear'),
    el('button', {
      'data-el': 'log-copy', title: 'Copy every line kept, as JSON',
      onclick: () => copyLog(),
    }, 'Copy JSON'));
  const status = el('div', { class: 'dim', 'data-el': 'log-status' });
  const list = el('ol', { class: 'log', 'data-el': 'log-list' });
  setKids(node, [controls, status, list]);
  const stops = [
    store.watch(vmLogControls, (vm) => {
      for (const name of LOG_LEVELS) named(`log-level-${name}`, node).checked = !!vm.levels[name];
      area.value = vm.area;
      setKids(pause, vm.paused ? 'Resume' : 'Pause');
    }, 'log controls'),
    store.watch(vmLogList, (vm) => {
      setKids(status, vm.status);
      setKids(list, vm.rows.map((r) => el('li', {
        class: `line ${r.level}`, 'data-line': r.id,
        onclick: () => setLog((log) => ({
          ...log, open: log.open.includes(r.id) ? log.open.filter((x) => x !== r.id) : [...log.open, r.id],
        })),
      },
      el('span', { class: 'time' }, r.time), ' ',
      el('span', { class: `level ${r.level}` }, r.level), ' ',
      el('span', { class: 'area' }, r.area), ' ',
      el('span', { class: 'text' }, r.text),
      r.count > 1 ? el('span', { class: 'count' }, ` ×${r.count}`) : null,
      r.corr ? el('span', { class: 'dim' }, ` ${r.corr}`) : null,
      r.open && r.detail ? el('pre', { class: 'detail' }, r.detail) : null)));
    }, 'log list'),
  ];
  return () => stops.forEach((stop) => stop());
}

/* Only ever on a click: the clipboard is the person's, never the page's to
   fill on its own. */
function copyLog() {
  const lines = store.get().log.lines;
  const clip = typeof navigator !== 'undefined' && navigator.clipboard;
  if (!clip) {
    logLine('warn', 'page', 'this browser gives the page no clipboard to copy the log to');
    return;
  }
  clip.writeText(JSON.stringify(lines, null, 2)).then(
    () => logLine('info', 'page', `copied ${lines.length} line${lines.length === 1 ? '' : 's'} of the log`),
    (e) => logLine('warn', 'page', `the log could not be copied: ${e.message}`));
}
/* ---- feeds.js ---- */
/* Feeds: what the page keeps asking the server - only while something shown
   needs it.

   follow(id) -> stop
       A view that shows a feed follows it for as long as the view is built.
       The feed is asked every `every` ms while at least one view follows it
       AND the browser is showing the page; when the last view stops, the
       asking stops. A hidden thing costs nothing.

   The answer goes into the store, `feeds.<id>` = {answer, status, at, ok}:
   `ok` is null before the first answer, true for a 200, false for a refusal
   or no answer. A feed that stops answering says so in the log ONCE, and
   once when it answers again - never a line a second.

   ONE ASK AT A TIME: a feed never has two requests out - `ask`, the one way
   in, refuses while one is.

   refresh(id)
       An action changed what a followed feed says: it is asked now, not at
       its next tick - or, if it is asking already, again the moment that
       answer is in, since the answer on its way may be from before. */

const FEEDS = {
  health: { route: 'server.health', every: 1000, into: healthInto },
  jobs: { route: 'server.jobs.list', every: 1000 },
  // every model's file and header - read by the server once per file, so
  // cheap to ask, but it changes only when a model loads or is measured
  models: { route: 'server.models.list', every: 5000 },
};

SEEDS.feeds = Object.fromEntries(Object.keys(FEEDS).map((id) =>
  [id, { answer: null, status: null, at: null, ok: null }]));

/* the feeds' clocks are the page's running machinery, not its state */
const FOLLOWING = Object.fromEntries(Object.keys(FEEDS).map((id) =>
  [id, { n: 0, timer: null, asking: false, failing: false, again: false }]));

function follow(id) {
  const f = FOLLOWING[id];
  if (!f) throw new Error(`no feed called ${id}`);
  f.n += 1;
  if (f.n === 1 && f.timer === null) ask(id);
  let stopped = false;
  return () => {
    if (stopped) return;
    stopped = true;
    f.n -= 1;
    if (f.n === 0 && f.timer !== null) {
      clearTimeout(f.timer);
      f.timer = null;
    }
  };
}

function pageShown() {
  return document.visibilityState !== 'hidden';
}

async function ask(id) {
  const f = FOLLOWING[id];
  const feed = FEEDS[id];
  f.timer = null;
  if (!f.n || !pageShown() || f.asking) return;
  f.asking = true;
  let got = null;
  let fault = null;
  try {
    got = await api(feed.route, { quiet: true });
  } catch (e) {
    fault = e;
  }
  f.asking = false;
  if (fault) {
    store.set((s) => update(s, ['feeds', id], (was) => ({ ...was, ok: false, at: Date.now() })));
    if (!f.failing) {
      f.failing = true;
      logLine('error', 'api', `${feed.route} stopped answering: ${fault.message}`);
    }
  } else {
    if (f.failing) {
      f.failing = false;
      logLine('info', 'api', `${feed.route} answers again`);
    }
    store.set((s) => {
      const next = update(s, ['feeds', id], () => ({
        answer: got.answer, status: got.status, at: Date.now(), ok: got.status === 200,
      }));
      return feed.into ? feed.into(next, got) : next;
    });
  }
  if (f.n && f.timer === null && pageShown()) f.timer = setTimeout(() => ask(id), f.again ? 0 : feed.every);
  f.again = false;
}

function refresh(id) {
  const f = FOLLOWING[id];
  if (!f) throw new Error(`no feed called ${id}`);
  if (f.asking) {
    f.again = true;
    return;
  }
  if (f.timer !== null) {
    clearTimeout(f.timer);
    f.timer = null;
  }
  ask(id);
}

/* the page shown again: every feed that is followed is asked at once */
document.addEventListener('visibilitychange', () => {
  if (!pageShown()) return;
  for (const id of Object.keys(FEEDS)) {
    const f = FOLLOWING[id];
    if (f.n && f.timer === null) ask(id);
  }
});

/* Health says which folder the server works in: the picker follows it, so
   a folder bound or let go by anybody else shows here too - unless a bind
   or an unload of this page's own is under way, which will say it. */
function healthInto(state, got) {
  if (got.status !== 200 || state.workspace.asking) return state;
  const { loaded, workspace } = got.answer;
  if (state.workspace.loaded === loaded && state.workspace.path === workspace) return state;
  return update(state, ['workspace'], (w) => ({ ...w, loaded, path: workspace }));
}
/* ---- workspace.js ---- */
/* The workspace picker: which folder the server works in.

   The server is asked when the page starts, and after every bind and unload;
   what it answers is the one truth the page shows. `loaded` is three things:
   null (not asked yet, or no answer), true, false - and "not asked" is never
   shown as "nothing bound".

   A folder is named by typing (or pasting) its path. There is no folder
   dialog: a route that opens one on the machine would open it for every test
   that walks the routes. The folders bound before are remembered by this
   browser - a convenience of the browser's, never the server's memory.

   WHAT NEEDS A FOLDER asks `vmGate`, the one derivation of it, and says the
   gate's reason when it is dark. */

const RECENT_KEY = 'llm-bootstrap.recent-folders';
const RECENT_KEPT = 8;

function recentFolders() {
  try {
    const v = JSON.parse(localStorage.getItem(RECENT_KEY) || '[]');
    return Array.isArray(v) ? v.filter((x) => typeof x === 'string').slice(0, RECENT_KEPT) : [];
  } catch (e) {
    return [];
  }
}

function keepRecent(list) {
  try {
    localStorage.setItem(RECENT_KEY, JSON.stringify(list));
  } catch (e) {
    logLine('warn', 'workspace', `this browser would not remember the recent folders: ${e.message}`);
  }
}

SEEDS.workspace = { loaded: null, path: null, recent: recentFolders(), asking: false, said: null };

function setWorkspace(fn) {
  store.set((s) => update(s, ['workspace'], fn));
}

/* ---------------------------------------------------------- the server */

async function askWorkspace() {
  setWorkspace((w) => ({ ...w, asking: true }));
  try {
    const got = await api('server.workspace.get');
    setWorkspace((w) => ({ ...w, asking: false, loaded: got.answer.loaded, path: got.answer.workspace }));
  } catch (e) {
    setWorkspace((w) => ({ ...w, asking: false, loaded: null, said: e.message }));
  }
}

async function bindFolder(path) {
  const wanted = String(path || '').trim();
  setWorkspace((w) => ({ ...w, asking: true, said: null }));
  let got;
  try {
    got = await api('server.workspace.bind', { body: { path: wanted } });
  } catch (e) {
    setWorkspace((w) => ({ ...w, asking: false, said: e.message }));
    return;
  }
  if (got.status !== 200) {
    setWorkspace((w) => ({ ...w, asking: false, said: got.answer.error }));
    logLine('warn', 'workspace', `the server would not work in ${wanted || 'that folder'}: ${got.answer.error}`);
    return;
  }
  const folder = got.answer.workspace;
  const recent = [folder, ...store.get().workspace.recent.filter((p) => p !== folder)].slice(0, RECENT_KEPT);
  keepRecent(recent);
  setWorkspace((w) => ({ ...w, asking: false, loaded: true, path: folder, recent, said: null }));
  logLine('info', 'workspace', `the server works in ${folder}`);
}

async function unloadFolder() {
  setWorkspace((w) => ({ ...w, asking: true, said: null }));
  let got;
  try {
    got = await api('server.workspace.unload');
  } catch (e) {
    setWorkspace((w) => ({ ...w, asking: false, said: e.message }));
    return;
  }
  if (got.status !== 200) {
    setWorkspace((w) => ({ ...w, asking: false, said: got.answer.error }));
    logLine('warn', 'workspace', `the server would not let its folder go: ${got.answer.error}`);
    return;
  }
  setWorkspace((w) => ({ ...w, asking: false, loaded: false, path: null, said: null }));
  if (got.answer.was) logLine('info', 'workspace', `the server let ${got.answer.was} go`);
}

STARTS.push(askWorkspace);

/* ---------------------------------------------------------- view models */

function vmGate(state) {
  const w = state.workspace;
  if (w.loaded === true) return { folder: true, why: null };
  if (w.loaded === null) return { folder: false, why: 'the server has not said which folder it works in' };
  return { folder: false, why: 'no folder is bound - choose one in the workspace picker' };
}

/* The last two names of a path: enough to tell folders apart in a small box. */
function shortPath(path) {
  const names = String(path).split(/[\\/]+/).filter(Boolean);
  return names.slice(-2).join('/') || String(path);
}

function vmWorkspaceItem(state) {
  const w = state.workspace;
  if (w.loaded === true) return { text: `📁 ${shortPath(w.path)}`, title: w.path, bound: true };
  if (w.loaded === null) {
    return { text: w.asking ? '📁 …' : '📁 ?', title: 'The server has not said which folder it works in', bound: false };
  }
  return { text: 'no folder - choose one', title: 'Choose the folder the server works in', bound: false };
}

function vmWorkspacePanel(state) {
  const w = state.workspace;
  return {
    bound: w.loaded === true ? w.path : null,
    unknown: w.loaded === null,
    asking: w.asking,
    said: w.said,
    recent: w.recent,
  };
}

/* --------------------------------------------------------------- views */

ITEMS.push({
  id: 'workspace', title: 'Workspace', box: { w: 16, h: 1 },
  view(node) {
    return store.watch(vmWorkspaceItem, (vm) => setKids(node, el('button', {
      'data-el': 'workspace', class: vm.bound ? 'clip' : 'clip dim', title: vm.title,
      onclick: () => store.set((s) => update(s, ['ui', 'panel'], (p) => (p === 'workspace' ? null : 'workspace'))),
    }, vm.text)), 'workspace item');
  },
  panel(node) {
    // the path is typed into an input built once: nothing repaints it while
    // a person types
    const path = el('input', {
      type: 'text', 'data-el': 'workspace-path', placeholder: 'a folder on this machine',
      onkeydown: (e) => { if (e.key === 'Enter') bindFolder(path.value); },
    });
    const bind = el('button', { 'data-el': 'workspace-bind', onclick: () => bindFolder(path.value) }, 'Bind');
    const unload = el('button', { 'data-el': 'workspace-unload', onclick: () => unloadFolder() }, 'Unload');
    const now = el('div', { 'data-el': 'workspace-now' });
    const said = el('div', { class: 'said', 'data-el': 'workspace-said' });
    const recent = el('div', { class: 'recent', 'data-el': 'workspace-recent' });
    setKids(node, [el('h3', {}, 'Where the server works'), now,
      el('div', { class: 'row' }, path, bind, unload), said, recent]);
    return store.watch(vmWorkspacePanel, (vm) => {
      setKids(now, vm.unknown ? 'The server has not said which folder it works in.'
        : vm.bound ? ['Bound to ', el('code', {}, vm.bound)] : 'No folder is bound.');
      if (vm.asking) bind.setAttribute('disabled', ''); else bind.removeAttribute('disabled');
      if (vm.asking || !vm.bound) unload.setAttribute('disabled', ''); else unload.removeAttribute('disabled');
      setKids(said, vm.said || []);
      setKids(recent, vm.recent.length ? [el('div', { class: 'dim' }, 'Bound before:'),
        vm.recent.map((p, i) => el('button', {
          class: 'link clip', 'data-el': `recent-${i}`, title: p, onclick: () => bindFolder(p),
        }, p))] : []);
    }, 'workspace panel');
  },
});
/* ---- queue.js ---- */
/* The scheduler graphic: what the card is doing, and what is waiting.

   A bar item in a box of fixed size - its words fit or are clipped, and the
   bar never moves because they changed. It shows
     the dot     idle · busy · a request waits for your check · a failure
                 (the server's own count of jobs that failed in a row) · no
                 answer
     the words   what the card is doing: degraded, or taken back (why on
                 hover, in the server's words: the pipeline's `why`); loading
                 a model, waiting for the card (and since when, in the
                 server's words - never who holds it), thinking or writing -
                 how many tokens, how fast - or, answers to different models
                 running at once (S17), how many (`2 answering`: each one's
                 name, model and doing on hover) - or idle, and which model is up,
                 or how many when several are (every one on hover) - and,
                 when the card has nothing else to say, how many requests
                 wait for your check (health's `queue.held`)
     the blocks  the work remaining: one block per queued job, up to eight
     the fill    the running job's progress, along the foot of the box - of
                 several, the one furthest along, named on hover: the next
                 to free its model, by the only measure the page has (a
                 time left is known of none)
   Its panel lists every unfinished job, running first, with a cancel - and
   each request held for its check (S15), with where to check it: the Check
   tool on this page, or behind its Tools door, or - a page that shows no
   Check - that it waits for a check this page cannot give. The Check tool
   itself is check.js's: this only chooses the request in it.

   It reads health (the card, the runner, the queue's counts) and the jobs
   (the running job's progress) - and only while it is in the bar. Put in the
   tray, nothing is asked for it. */

const QUEUE_BLOCKS = 8;

/* a pipeline in trouble, by its state: the words the graphic says it in */
const TROUBLE = { degraded: 'card degraded', lost: 'card taken back' };

SEEDS.queue = { said: null };

/* Every running job, as the server lists them. Answers to different models
   run at the same time (S17), so there may be several - one per model. */
function runningJobs(state) {
  const j = state.feeds.jobs;
  if (!j.ok || !j.answer) return [];
  return j.answer.jobs.filter((x) => x.status === 'running');
}

/* Of several running, the one furthest along by its fill - the next to free
   its model, by the only measure the page has - or null: none has a fill */
function furthest(jobs) {
  let best = null;
  for (const job of jobs) {
    const f = jobFill(job);
    if (f !== null && (best === null || f > jobFill(best))) best = job;
  }
  return best;
}

/* S18: what a queued scheduled job waits for, in a few words - room on the
   card (its numbers), or its time (`not_before`, this page's hour and
   minute) - or null: it waits only for its turn. `now` is seconds. */
function scheduledWords(job, now) {
  const p = (job && job.progress) || {};
  if (p.stage === 'waiting for room on the card') {
    const need = p.need_mib, free = p.free_mib;
    return need != null && free != null
      ? `waits for room: needs ${need} MiB, ${free} MiB free` : 'waits for room';
  }
  if (job && job.not_before != null && job.not_before > now) {
    return `starts at ${clockWords(job.not_before)}`;
  }
  return null;
}

function stageWords(job) {
  const p = (job && job.progress) || {};
  if (p.stage === 'writing') {
    return `writing ${p.tokens || 0} tok${p.tok_s ? ` · ${Math.round(p.tok_s)}/s` : ''}`;
  }
  if (p.stage === 'thinking') return `thinking ${p.tokens || 0} tok`;
  if (p.stage === 'waiting for the card') return 'waiting for the card';
  if (p.stage) return String(p.stage);
  return null;
}

/* How far the running job has got, 0 to 1: the work done against the most it
   can take. Its budget is the same most for each of its `of` variants; how
   long a reply will be is not known until it stops, so this is never a guess
   at the reply. A variant never takes more than its share. */
function jobFill(job) {
  const p = (job && job.progress) || {};
  if (!p.budget || !p.of || !p.variant) return null;
  return ((p.variant - 1) + Math.min(1, (p.tokens || 0) / (p.budget / p.of))) / p.of;
}

/* Every model on the card, in health's words: the one the runner used last
   first, then the others the pipeline says are up, in its order. Models
   run side by side (PLAN-4 D7), so the runner's one is never all of them. */
function modelsUp(a) {
  const up = a.runner && a.runner.loaded ? [a.runner.loaded] : [];
  for (const key of (a.pipeline && a.pipeline.up) || []) {
    if (!up.includes(key)) up.push(key);
  }
  return up;
}

/* S18: health's planned batch jobs, in words */
function plannedWords(s) {
  return `${s.planned} planned - the first at ${clockWords(s.next_at)}`;
}

/* how many requests wait for a person's check, in the bar's words */
function heldWords(n) {
  return n === 1 ? '1 request waits for your check' : `${n} requests wait for your check`;
}

/* Where a held request can be checked on this page: in a Check tool placed
   on it (`here`), behind a Tools door (`door`), or nowhere (null) - what is
   placed is running machinery (kit.js's PLACED, tools.js's DOORS), read as
   the panel is drawn. */
function checkPlace() {
  if (PLACED.check) return 'here';
  if (DOORS.n) return 'door';
  return null;
}

const CHECK_WHERE = {
  here: 'in Check, on this page',
  door: 'in Tools: Check',
};

/* ---------------------------------------------------------- view models */

function vmQueueItem(state) {
  const h = state.feeds.health;
  if (h.ok === null) {
    return { dot: 'unknown', text: '…', title: 'Asking the server', fill: null, blocks: 0, more: 0 };
  }
  if (!h.ok || !h.answer) {
    return { dot: 'unknown', text: 'no answer', title: 'The server does not answer', fill: null, blocks: 0, more: 0 };
  }
  const a = h.answer;
  const q = a.queue;
  const r = a.runner;
  // S18: scheduled work that waits - planned for a time, or for room
  const s = q.scheduled || null;
  const running = runningJobs(state);
  // one running is said in its own words; several are counted (S17)
  const job = running.length === 1 ? running[0] : null;
  const waits = running.find((x) => (x.progress || {}).stage === 'waiting for the card');
  const p = ((waits || job) && (waits || job).progress) || {};
  // the card degraded, or taken back: said before all else, why in the
  // server's own words (PLAN-4 D3, D6)
  const trouble = a.pipeline && TROUBLE[a.pipeline.state];
  const up = modelsUp(a);
  let text = 'idle';
  let title = 'Nothing is running, and nothing waits';
  if (trouble) {
    text = trouble;
    title = a.pipeline.why || text;
  } else if (a.card.waiting_since !== null || p.stage === 'waiting for the card') {
    text = 'waiting for the card';
    title = p.waiting_for ? `Waiting: ${p.waiting_for}` : 'Waiting for another application to give the card back';
  } else if (r && r.loading) {
    text = `loading ${r.loading}`;
    title = `The weights of ${r.loading} are going up`;
  } else if (running.length > 1) {
    text = `${running.length} answering`;
    title = `${text} - ${running.map((x) => `${x.name} on ${x.model}: ${stageWords(x) || 'running'}`).join('; ')}`;
  } else if (job && stageWords(job)) {
    text = stageWords(job);
    title = `${job.name} - ${text}`;
  } else if (q.running) {
    text = 'working';
    title = 'A job is running';
  } else if (q.held) {
    text = heldWords(q.held);
    title = `${text}: it is sent or cancelled by a person, and nothing runs for it until then`;
  } else if (s && s.for_room) {
    // S18: scheduled work waiting for room on the card, in the server's words
    text = 'waits for room';
    title = `Waits for room: ${s.room || 'its model does not fit on the card now'}`
      + (s.planned ? ` · ${plannedWords(s)}` : '');
  } else if (s && s.planned) {
    // S18: a batch planned for later - nothing is held for it until then
    text = `starts at ${clockWords(s.next_at)}`;
    title = `${plannedWords(s)}: it starts at the earliest then, once its model fits, and `
      + 'nothing is held for it until then';
  } else if (up.length === 1) {
    text = `idle · ${up[0]} up`;
    title = `${up[0]} is on the card, and nothing is running`;
  } else if (up.length) {
    text = `idle · ${up.length} models up`;
    title = `${up.slice(0, -1).join(', ')} and ${up[up.length - 1]} are on the card, and nothing is running`;
  }
  // several running: the fill is the one furthest along, named on hover
  const filled = running.length > 1 ? furthest(running) : job;
  const fill = jobFill(filled);
  if (fill !== null) {
    const fp = filled.progress;
    title += (running.length > 1 ? ` · filled by ${filled.name}:` : ' ·')
      + ` ${fp.tokens || 0} of at most ${fp.budget / fp.of} tokens`
      + (fp.of > 1 ? `, variant ${fp.variant} of ${fp.of}` : '');
  }
  let dot = 'idle';
  if (q.consecutive_failures) {
    dot = 'bad';
    const last = q.last && q.last.error ? ` - the last: ${q.last.error}` : '';
    title = `${q.consecutive_failures} job${q.consecutive_failures === 1 ? '' : 's'} failed in a row${last}`;
  } else if (trouble) {
    dot = 'bad';
  } else if (q.held) {
    // a person is waited on: said above the card's own work
    dot = 'check';
  } else if (q.running || q.queued > (s ? s.planned : 0)) {
    // a batch planned for later is not work under way: it holds nothing
    dot = 'busy';
  }
  if (q.queued) title += ` · ${q.queued} waiting`;
  if (q.held && !title.startsWith(heldWords(q.held))) title += ` · ${heldWords(q.held)}`;
  return {
    dot, text, title, fill,
    blocks: Math.min(q.queued, QUEUE_BLOCKS), more: Math.max(0, q.queued - QUEUE_BLOCKS),
  };
}

function vmQueuePanel(state) {
  const j = state.feeds.jobs;
  const h = state.feeds.health;
  const jobs = j.ok && j.answer ? j.answer.jobs : [];
  // in the order they will run: a running job's position is 0
  const open = jobs.filter((x) => x.status === 'queued' || x.status === 'running')
    .sort((x, y) => x.queue_pos - y.queue_pos);
  // then each held for a person's check: in no order of running - nothing
  // runs for it until it is sent - so in the order the server lists them
  const held = jobs.filter((x) => x.status === 'held');
  const at = checkPlace();
  const last = h.ok && h.answer ? h.answer.queue.last : null;
  return {
    unknown: j.ok === null,
    refused: j.ok === false ? 'The server did not give the list of jobs.' : null,
    rows: open.map((x) => ({
      id: x.id, name: x.name, model: x.model,
      where: x.status === 'running' ? 'now' : `#${x.queue_pos}`,
      doing: x.status === 'running' ? (stageWords(x) || 'running')
        : (scheduledWords(x, Date.now() / 1000) || 'waiting'),
      // S18: what a job waiting for room waits on, whole, on hover
      title: x.status === 'queued' && (x.progress || {}).waiting_for
        ? x.progress.waiting_for : null,
      check: null,
    })).concat(held.map((x) => ({
      id: x.id, name: x.name, model: x.model, where: 'held',
      doing: `waits for your check - ${at ? CHECK_WHERE[at] : 'this page shows no Check'}`,
      check: at,
    }))),
    last: last ? { status: last.status, job: last.job, error: last.error } : null,
    said: state.queue.said,
  };
}

/* --------------------------------------------------------------- actions */

/* Cancel a job, said in the log either way. -> null, or why it was not: the
   server's words. Whoever asked shows that where it asked. */
async function cancelJob(id) {
  let got;
  try {
    got = await api('server.jobs.cancel', { params: { id } });
  } catch (e) {
    return e.message;
  }
  if (got.status === 200) {
    logLine('info', 'queue', `${got.answer.job.name} (${id}) cancelled`,
      { corr: got.answer.job.correlation_id });
    return null;
  }
  logLine('warn', 'queue', `${id} could not be cancelled: ${got.answer.error}`);
  return got.answer.error;
}

/* A held request chosen in the Check tool - and, when the Check is behind
   the door, the door opened on it. What Check then does is its own. */
function checkFromPanel(id, at) {
  setCheck((c) => ({ ...c, chosen: id }));
  if (at === 'door') store.set((s) => ({ ...s, ui: { ...s.ui, tab: 'check', panel: null } }));
}

async function cancelFromPanel(id) {
  store.set((s) => update(s, ['queue', 'said'], () => null));
  const said = await cancelJob(id);
  store.set((s) => update(s, ['queue', 'said'], () => said));
}

/* --------------------------------------------------------------- views */

ITEMS.push({
  id: 'queue', title: 'Card and queue', box: { w: 20, h: 1 },
  view(node) {
    const stops = [follow('health'), follow('jobs')];
    stops.push(store.watch(vmQueueItem, (vm) => setKids(node, el('button', {
      'data-el': 'queue', class: 'graphic', title: vm.title,
      onclick: () => store.set((s) => update(s, ['ui', 'panel'], (p) => (p === 'queue' ? null : 'queue'))),
    },
    el('span', { class: `dot ${vm.dot}`, 'data-el': 'queue-dot' }),
    el('span', { class: 'clip words', 'data-el': 'queue-words' }, vm.text),
    el('span', { class: 'blocks', 'data-el': 'queue-blocks' },
      Array.from({ length: vm.blocks }, () => el('i', {})),
      vm.more ? el('span', { class: 'more' }, `+${vm.more}`) : null),
    vm.fill === null ? null
      : el('span', { class: 'fill', 'data-el': 'queue-fill', style: { width: `${Math.round(vm.fill * 100)}%` } }))),
    'queue item'));
    return () => stops.forEach((stop) => stop());
  },
  panel(node) {
    // no feed of its own: a panel is shown only while its item is in the
    // bar, and the item follows both
    const stops = [];
    const list = el('ol', { class: 'jobs', 'data-el': 'queue-list' });
    const last = el('div', { class: 'dim', 'data-el': 'queue-last' });
    const said = el('div', { class: 'said', 'data-el': 'queue-said' });
    setKids(node, [el('h3', {}, 'The queue'), said, list, last]);
    stops.push(store.watch(vmQueuePanel, (vm) => {
      setKids(said, vm.said || vm.refused || []);
      setKids(list, vm.unknown ? el('li', { class: 'dim' }, 'Asking the server…')
        : vm.rows.length ? vm.rows.map((r) => el('li', { 'data-job': r.id },
          el('span', { class: 'where' }, r.where), ' ',
          el('span', { class: 'clip name', title: r.name }, r.name), ' ',
          el('span', { class: 'dim' }, r.model), ' ',
          el('span', { class: 'doing', title: r.title || null }, r.doing), ' ',
          r.check ? el('button', { 'data-el': `check-${r.id}`, onclick: () => checkFromPanel(r.id, r.check) }, 'Check') : null,
          r.check ? ' ' : null,
          el('button', { 'data-el': `cancel-${r.id}`, onclick: () => cancelFromPanel(r.id) }, 'Cancel')))
          : el('li', { class: 'dim' }, 'Nothing is running, and nothing waits.'));
      setKids(last, vm.last ? `The last to finish: ${vm.last.job}, ${vm.last.status}`
        + (vm.last.error ? ` - ${vm.last.error}` : '') : []);
    }, 'queue panel'));
    return () => stops.forEach((stop) => stop());
  },
});
/* ---- session.js ---- */
/* The session: the card held from a pipeline's open to its close (PLAN-4
   D6), as the server this page asks says it - health's `pipeline`. Never
   the coordinator's word, and never a word of another application (D4):
   what an application is told is its own pipeline, and nothing else.

   A widget with a box. It shows
     the dot     holding · waiting · degraded or taken back, or a model it
                 keeps whose load failed · no session, no runner, no answer
     the words   what the session does with the card: holds it, waits for
                 it and since when, waits for room (S18: scheduled work whose
                 model does not fit yet), the card full, taken back, a load
                 that failed - and, while no session is open, the scheduled
                 work its queue waits to run: when it starts, or that it
                 waits for room (health's `queue.scheduled`)
     on hover    since when, its label - and why, in the server's own words:
                 the card full (degraded), what a person does - close
                 something - and that its work runs again by itself, in
                 numbers, when its server holds the work for the card (S5b)
   Its panel: the session whole - since when; why, the server's sentence;
   its label, whose it is, its class, how long it is kept without a renewal
   - and every model in it (pinned, or up, or on its way) as the server
   says each (`models`): its engine, where it stands, pinned or on demand,
   and why its load failed.

   It follows health; its panel follows the models too, while it is open.
   The Models tool shows the same session at its head. The words a model is
   said in - its engine, where it stands, pinned or on demand - are here,
   and the Models tool says them the same way. */

/* a moment the server names in seconds: its hour and minute, here */
function clockWords(seconds) {
  const d = new Date(seconds * 1000);
  const two = (n) => String(n).padStart(2, '0');
  return `${two(d.getHours())}:${two(d.getMinutes())}`;
}

/* each state of a pipeline: the widget's words, its dot, and the start of
   the panel's line - before the moment it names */
const SESSION_WORDS = {
  holding: { text: 'holding the card', dot: 'idle', line: 'Holding the card since' },
  waiting: { text: 'waiting for the card', dot: 'busy', line: 'Waiting for the card since' },
  degraded: { text: 'card full', dot: 'bad', line: 'The card has been full since' },
  lost: { text: 'card taken back', dot: 'bad', line: 'Taken back at' },
  closed: { text: 'no session', dot: 'unknown', line: 'Closed at' },
};

/* whose the pipeline is, by `opened_by` */
const WHOSE = {
  application: "the application's", start: 'opened as the server started', queue: "the queue's own, for its work",
};

const ENGINE_WORDS = { llamacpp: 'llama.cpp', whisper: 'whisper' };
const MODEL_STATE_WORDS = {
  up: 'up', loading: 'loading', waiting: 'waiting for the card', failed: 'its load failed', off: 'off',
};

/* a model as the server says it stands: its engine, its state, and whether
   the session keeps it (pinned) or a job loaded it (on demand) */
function modelStands(m) {
  const now = m.state.state;
  const kept = m.pinned ? 'pinned' : (now === 'up' || now === 'loading' ? 'on demand' : null);
  return [ENGINE_WORDS[m.engine], MODEL_STATE_WORDS[now], kept].filter(Boolean).join(' · ');
}

function keepWords(keepS) {
  if (keepS === null) return null;
  return keepS === 0 ? 'held until it is closed' : `kept ${keepS} s without a renewal`;
}

/* S18: a waiting session's why that says it waits for room - the server's
   words for scheduled work whose model does not fit yet */
const ROOM_WAIT = /waits for room/;

/* the session, in words, from health's `pipeline` - null: no runner - and
   the scheduled work its queue waits to run (health's `queue.scheduled`,
   S18), said while no session is open */
function sessionLines(p, s) {
  if (!p) {
    const said = 'No model runner is behind this server: it holds no card';
    return { dot: 'unknown', text: 'no runner', line: said, title: said, why: null, bad: false, about: null };
  }
  const w = SESSION_WORDS[p.state];
  const at = p.since === null ? null : clockWords(p.since);
  const line = p.state === 'closed' && p.id === null ? 'No session is open' : `${w.line} ${at}`;
  // a model the session keeps whose load failed: its why, while it holds the
  // card and that model is not up - said by the dot too, never on hover
  // alone (wave 8's register); loaded again since, it is not red
  const failed = p.state === 'holding' && Boolean(p.why)
    && (p.models || []).some((k) => !(p.up || []).includes(k));
  const room = p.state === 'waiting' && ROOM_WAIT.test(p.why || '');
  let text = p.state === 'waiting' ? `${w.text}, since ${at}` : w.text;
  let dot = failed ? 'bad' : w.dot;
  let title = `${[line, p.label].filter(Boolean).join(' - ')}${p.why ? `: ${p.why}` : ''}`;
  if (room) text = 'waits for room';
  if (failed) text = `${w.text} · a load failed`;
  if (p.state === 'closed' && s && (s.for_room || s.planned)) {
    // S18: no session now - the scheduled work its queue waits to run
    text = s.for_room ? 'waits for room' : `starts at ${clockWords(s.next_at)}`;
    dot = s.for_room ? 'busy' : 'idle';
    title = s.for_room ? `Waits for room: ${s.room || 'its model does not fit on the card now'}`
      : `${s.planned} planned - the first starts at ${clockWords(s.next_at)} at the earliest, `
        + 'once its model fits; nothing is held for it until then';
  }
  return {
    dot,
    text,
    line,
    title,
    why: p.why,
    bad: p.state === 'degraded' || p.state === 'lost' || failed,
    about: [p.label, WHOSE[p.opened_by], p.class, keepWords(p.keep_s)].filter(Boolean).join(' · ') || null,
  };
}

/* the session now: before health answers, and when it does not, said so */
function sessionNow(state) {
  const h = state.feeds.health;
  if (h.ok === null) {
    return { dot: 'unknown', text: '…', line: 'Asking the server', title: 'Asking the server', why: null, bad: false, about: null };
  }
  if (!h.ok || !h.answer) {
    const said = 'The server does not answer';
    return { dot: 'unknown', text: 'no answer', line: said, title: said, why: null, bad: false, about: null };
  }
  return sessionLines(h.answer.pipeline, h.answer.queue && h.answer.queue.scheduled);
}

/* ---------------------------------------------------------- view models */

function vmSession(state) {
  const s = sessionNow(state);
  return { dot: s.dot, text: s.text, title: s.title };
}

/* every model in the session - kept by it, or not off - as the server says
   each, in the server's order */
function vmSessionPanel(state) {
  const s = sessionNow(state);
  const f = state.feeds.models;
  let models;
  if (f.ok === null) models = { rows: null, wait: 'Asking the server for its models…' };
  else if (!f.ok || !f.answer) models = { rows: null, wait: 'The server did not give its models.' };
  else {
    const rows = f.answer.models.filter((m) => m.pinned || m.state.state !== 'off')
      .map((m) => `${m.label} · ${modelStands(m)}${m.state.why ? ` - ${m.state.why}` : ''}`);
    models = rows.length ? { rows, wait: null } : { rows: null, wait: 'No model is on the card, and none is kept.' };
  }
  return { line: s.line, why: s.why, bad: s.bad, about: s.about, models };
}

/* --------------------------------------------------------------- views */

ITEMS.push({
  id: 'session', title: 'The session', box: { w: 18, h: 1 },
  view(node) {
    const stops = [follow('health')];
    stops.push(store.watch(vmSession, (vm) => setKids(node, el('button', {
      'data-el': 'session', class: 'graphic', title: vm.title,
      onclick: () => store.set((s) => update(s, ['ui', 'panel'], (p) => (p === 'session' ? null : 'session'))),
    },
    el('span', { class: `dot ${vm.dot}`, 'data-el': 'session-dot' }),
    el('span', { class: 'clip words', 'data-el': 'session-words' }, vm.text))), 'session item'));
    return () => stops.forEach((stop) => stop());
  },
  panel(node) {
    // health is the item's; the models are the panel's own, while it is open
    const stops = [follow('models')];
    stops.push(store.watch(vmSessionPanel, (vm) => setKids(node, [
      el('h3', {}, 'The session'),
      el('div', { 'data-el': 'session-state' }, vm.line),
      vm.why ? el('div', { class: vm.bad ? 'bad' : 'said', 'data-el': 'session-why' }, vm.why) : null,
      vm.about ? el('div', { class: 'dim', 'data-el': 'session-about' }, vm.about) : null,
      el('ol', { class: 'jobs', 'data-el': 'session-models' }, vm.models.rows
        ? vm.models.rows.map((line) => el('li', {}, line))
        : el('li', { class: 'dim' }, vm.models.wait)),
    ]), 'session panel'));
    return () => stops.forEach((stop) => stop());
  },
});
/* ---- models.js ---- */
/* Model health: every model the runner's settings name, where each stands,
   and what can be known of each without loading it. The server is asked
   (`models`, every 5 s) while the tool is shown, and never otherwise; so is
   health, for the session at the tool's head (session.js).

   At its head, the session the models are in: holding the card, waiting
   for it, degraded or taken back - and why, in the server's words.

   Each model shows
     where it stands  its engine; up, loading, waiting for the card, its
                   load failed (and why, the server's sentence), or off; and
                   whether the session keeps it (pinned) or a job loaded it
                   (on demand) - in session.js's words
     its file      there or not, how big, on which disk
     its header    what the file says, as its engine reads it: a GGUF's -
                   the architecture, the context it was trained for beside
                   the context the settings ask of it, a chat template, bits
                   per weight - or a whisper model's ggml header - its shape,
                   and its eleven numbers
     its problems  the server's own sentences - what a load would log; the
                   page never judges a model itself
     its loads     cold and warm: how many, the normal, the last - and a last
                   load the server found slow against its normal, marked
     its statistics  its load, its first token (a transcriber's answer in one
                   piece), its tokens a second, a transcriber's real-time
                   factor: the median, the 90th percentile and the last, each
                   with its unit, and how many samples - as `/api/models`
                   gives them (PLAN-4 D12) - and its answers far slower than
                   its normal, as the server flags them: how many, how many
                   times its normal the last took, and when
     its disk      how fast the file comes off it, measured on demand, in the
                   server's own sentence - which says whether the read went
                   past the operating system's cache

   With nothing bound the server keeps no history, and the tool says so
   rather than show none. One measure at a time: while one runs, every
   other button says why it waits. */

SEEDS.models = { measuring: null, said: {} };

/* what runs the models, by the runner's backend: the stand-in; each model on
   its own engine (PLAN-4 D10); every model on llama.cpp */
const RUNNER_WORDS = {
  mock: 'Stand-in models', real: 'Each model on its own engine', llamacpp: 'llama.cpp',
};

function count(n) {
  return n === null || n === undefined ? 'unknown' : Number(n).toLocaleString('en-US');
}

/* a size in the unit that fits it: a small model is not "0.0 GB" */
function bytesWords(n) {
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)} GB`;
  if (n >= 1e6) return `${Math.round(n / 1e6)} MB`;
  return `${Math.max(1, Math.round(n / 1e3))} KB`;
}

/* a llama.cpp model's GGUF header, against the context the settings ask */
function ggufWords(h, context) {
  return [h.architecture,
    `trained for ${count(h.trained_context)}, asked ${count(context)}`,
    h.chat_template ? 'a chat template' : 'no chat template',
    h.bits_per_weight ? `${h.bits_per_weight} bits a weight` : null,
  ].filter(Boolean).join(' · ');
}

/* a whisper model's ggml header: its shape as the server names it, then
   its eleven numbers - the vocabulary, each stack's context, state, heads
   and layers, the mels, and the file's type as it stores it */
function ggmlWords(h) {
  const stack = (ctx, state, heads, layers) => `${count(ctx)} context, ${count(state)} state, ${heads} heads, ${layers} layers`;
  return [h.shape, `vocabulary ${count(h.n_vocab)}`,
    `audio: ${stack(h.n_audio_ctx, h.n_audio_state, h.n_audio_head, h.n_audio_layer)}`,
    `text: ${stack(h.n_text_ctx, h.n_text_state, h.n_text_head, h.n_text_layer)}`,
    `${h.n_mels} mels`, `ftype ${h.ftype}`].join(' · ');
}

/* ------------------------------------------------------- the statistics */

/* a time in seconds, in the unit that fits it: under a second in
   milliseconds - a few keep a decimal, so never "0 ms" */
function secondsWords(v) {
  if (v < 1) {
    const ms = v * 1000;
    return `${ms < 10 ? ms.toFixed(1) : Math.round(ms)} ms`;
  }
  return v < 100 ? `${v.toFixed(1)} s` : `${Math.round(v)} s`;
}

/* The statistics the Models tool shows (PLAN-4 D12), in the order it shows
   them - each by its measure in `/api/models`' `stats`, what it counts, and
   its unit. A load is timed from the asking to ready - a wait for the card,
   or for another model's load, included - so it is said so: the load
   history's time, above it, is the weights going up alone. The first token of an answer in one piece is its whole answer
   (a transcriber's), and is said so. Tokens a second as the server says
   them (one decimal); the real-time factor - seconds taken for a second of
   audio - to three, as the server's slow answer says it. */
const STAT_LINES = [
  { measure: 'load_s', label: () => 'load, asked to ready', noun: 'load', words: secondsWords },
  { measure: 'first_token_s', label: (m) => (m.engine === 'whisper' ? 'answer, in one piece' : 'first token'),
    noun: 'answer', words: secondsWords },
  { measure: 'tokens_per_s', label: () => 'tokens a second', noun: 'answer', words: (v) => `${v.toFixed(1)} tok/s` },
  { measure: 'real_time_factor', label: () => 'real-time factor', noun: 'answer',
    words: (v) => `${v.toFixed(3)} s per audio s` },
];

function howMany(n, noun) {
  return `${count(n)} ${noun}${n === 1 ? '' : 's'}`;
}

/* A model's statistics: a line a measure it has - its median, its 90th
   percentile and its last, each with its unit, and how many samples, with
   the median and p90 said to be of the kept ones when fewer are kept than
   were counted - and a slow answer as the server flags it (D3): how many of
   its answers were far slower than its normal, how many times its normal
   the last one took, and when - and, beside it, what far slower means: the
   thresholds the server judged it by (`/api/models`' `slow_answer`,
   `judged`), never typed here. `none` says why there are no lines. */
function vmStats(m, bound, judged) {
  const s = m.stats;
  if (!s) {
    return { none: bound ? 'the statistics could not be read' : 'no statistics - no folder bound', lines: [], slow: null, by: null };
  }
  const lines = STAT_LINES.filter((l) => s[l.measure]).map((l) => {
    const st = s[l.measure];
    return {
      measure: l.measure, label: l.label(m),
      cells: [l.words(st.median), l.words(st.p90), l.words(st.last)],
      samples: howMany(st.count, l.noun)
        + (st.kept < st.count ? ` · median and p90 of the last ${count(st.kept)}` : ''),
    };
  });
  let slow = null;
  let by = null;
  if (s.slow_answer) {
    const of = s.answer_s ? s.answer_s.count : s.slow_answer.count;
    slow = `${count(s.slow_answer.count)} of ${howMany(of, 'answer')} far slower than its normal: `
      + `the last ${s.slow_answer.last} times slower, at ${clockWords(s.slow_answer.last_at)}`;
    // what far slower means, as the server that flagged it judges (S14c R3)
    if (judged) {
      by = `far slower: at least ${judged.factor} times slower than its normal, and ${judged.floor_s} s `
        + 'lost over it - as this server judges';
    }
  }
  return { none: lines.length ? null : 'nothing measured yet', lines, slow, by };
}

function loadWords(h) {
  if (!h.runs) return 'never';
  const normal = h.normal === null ? 'no normal yet' : `normal ${h.normal} s`;
  const last = h.last === null ? '' : ` · last ${h.last} s`;
  return `${h.runs} load${h.runs === 1 ? '' : 's'} · ${normal}${last}`;
}

/* ---------------------------------------------------------- view models */

function vmModels(state) {
  const f = state.feeds.models;
  if (f.ok === null) return { head: ['Asking the server…'], rows: [] };
  if (!f.ok || !f.answer) return { head: ['The server did not give the list of models.'], rows: [] };
  const a = f.answer;
  if (a.runner === null) {
    return { head: ['No model runner is behind this server, so it has no models.'], rows: [] };
  }
  const busy = state.models.measuring;
  return {
    head: [`${RUNNER_WORDS[a.runner]}, from ${a.models_dir}`,
      a.loads_from ? `Load history from ${a.loads_from}`
        : 'No folder is bound, so there is no load history.'],
    rows: a.models.map((m) => {
      let why = null;
      if (!m.file) why = 'a stand-in has no file to measure';
      else if (!m.file.present) why = 'its file is not there';
      else if (busy === m.key) why = 'measuring…';
      else if (busy) why = 'another measure is running';
      // beside the button, the freshest thing known: a refusal, a measure
      // under way, the last measure, or why there is none
      const said = state.models.said[m.key] || null;
      let speed = { text: why || 'not measured yet', kind: 'dim' };
      if (said) speed = { text: said, kind: 'said' };
      else if (busy === m.key) speed = { text: why, kind: 'dim' };
      else if (m.read_speed) speed = { text: m.read_speed.said, kind: null };
      return {
        key: m.key, label: m.label, note: m.note, loaded: m.loaded,
        stands: modelStands(m), failed: m.state.why,
        file: !m.file ? 'no file - a stand-in'
          : m.file.present ? `${bytesWords(m.file.size)} on ${m.file.disk || 'its disk'}`
            // the path is in the server's own sentence, below
            : `missing: ${m.file.name}`,
        missing: !!m.file && !m.file.present,
        header: !m.header ? null : m.engine === 'whisper' ? ggmlWords(m.header) : ggufWords(m.header, m.context),
        problems: m.problems,
        loads: m.loads ? ['cold', 'warm'].map((kind) => ({
          kind, words: loadWords(m.loads[kind]), slow: m.loads[kind].slow,
        })) : null,
        stats: vmStats(m, !!a.loads_from, a.slow_answer),
        why,
        speed,
      };
    }),
  };
}

/* --------------------------------------------------------------- actions */

function setModels(fn) {
  store.set((s) => update(s, ['models'], fn));
}

async function measureReadSpeed(key) {
  setModels((m) => ({ ...m, measuring: key, said: { ...m.said, [key]: null } }));
  let got;
  try {
    got = await api('server.models.read_speed', { params: { key } });
  } catch (e) {
    setModels((m) => ({ ...m, measuring: null, said: { ...m.said, [key]: e.message } }));
    return;
  }
  if (got.status === 200) {
    logLine('info', 'models', `${key}: ${got.answer.measure.said}`);
    setModels((m) => ({ ...m, measuring: null }));
    refresh('models');
  } else {
    logLine('warn', 'models', `${key} could not be measured: ${got.answer.error}`);
    setModels((m) => ({ ...m, measuring: null, said: { ...m.said, [key]: got.answer.error } }));
  }
}

/* --------------------------------------------------------------- views */

/* a model's statistics: a table, its columns said once - or why there is
   none */
function statsTable(key, vm) {
  if (vm.none) return el('div', { class: 'dim', 'data-el': `model-stats-${key}` }, vm.none);
  return el('table', { class: 'stats', 'data-el': `model-stats-${key}` },
    el('tr', {}, ['', 'median', 'p90', 'last', 'samples'].map((h) => el('th', {}, h))),
    vm.lines.map((l) => el('tr', { 'data-el': `model-stat-${key}-${l.measure}` },
      el('th', {}, l.label), l.cells.map((c) => el('td', {}, c)), el('td', { class: 'dim' }, l.samples))));
}

function modelCard(r) {
  return el('section', { class: 'model', 'data-model': r.key },
    el('h4', {}, r.label, r.label !== r.key ? el('span', { class: 'dim' }, ` ${r.key}`) : null,
      r.loaded ? el('span', { class: 'badge', 'data-el': `model-loaded-${r.key}` }, 'on the card') : null),
    r.note ? el('div', { class: 'dim' }, r.note) : null,
    el('div', { 'data-el': `model-state-${r.key}` }, r.stands),
    r.failed ? el('div', { class: 'bad', 'data-el': `model-why-${r.key}` }, r.failed) : null,
    el('div', { class: r.missing ? 'bad' : null, 'data-el': `model-file-${r.key}` }, r.file),
    r.header ? el('div', { 'data-el': `model-header-${r.key}` }, r.header) : null,
    r.problems.length ? el('ul', { class: 'problems', 'data-el': `model-problems-${r.key}` },
      r.problems.map((p) => el('li', {}, p))) : null,
    el('div', { 'data-el': `model-loads-${r.key}` }, r.loads
      ? r.loads.map((l) => el('div', { class: l.slow ? 'bad' : null },
        `${l.kind}: ${l.words}${l.slow ? ' - the last was slow' : ''}`))
      : el('span', { class: 'dim' }, 'no load history - no folder bound')),
    statsTable(r.key, r.stats),
    r.stats.slow ? el('div', { class: 'bad', 'data-el': `model-slow-${r.key}` }, r.stats.slow) : null,
    r.stats.by ? el('div', { class: 'dim', 'data-el': `model-slow-by-${r.key}` }, r.stats.by) : null,
    el('div', { class: 'measure' },
      el('button', {
        'data-el': `model-measure-${r.key}`, disabled: r.why !== null,
        title: r.why || 'Read one stretch of the file, and say how fast it came off its disk',
        onclick: () => measureReadSpeed(r.key),
      }, 'Measure the disk'), ' ',
      el('span', { class: r.speed.kind, 'data-el': `model-speed-${r.key}` }, r.speed.text)));
}

/* the session the models are in, at the tool's head: one line - since
   when, its label, whose, its class, how long it is kept - and why */
function vmModelsSession(state) {
  const s = sessionNow(state);
  return { line: [s.line, s.about].filter(Boolean).join(' · '), why: s.why, bad: s.bad };
}

TOOLS.push({
  id: 'models', title: 'Models', rank: 20,
  view(node) {
    const stops = [follow('models'), follow('health')];
    const session = el('div', { class: 'session' });
    const head = el('div', { class: 'dim', 'data-el': 'models-head' });
    const list = el('div', { class: 'models', 'data-el': 'models-list' });
    setKids(node, [session, head, list]);
    stops.push(store.watch(vmModelsSession, (vm) => setKids(session, [
      el('div', { 'data-el': 'models-session' }, vm.line),
      vm.why ? el('div', { class: vm.bad ? 'bad' : 'said', 'data-el': 'models-session-why' }, vm.why) : null,
    ]), 'models session'));
    stops.push(store.watch(vmModels, (vm) => {
      setKids(head, vm.head.map((line) => el('div', {}, line)));
      setKids(list, vm.rows.map(modelCard));
    }, 'models tool'));
    return () => stops.forEach((stop) => stop());
  },
});
/* ---- bench.js ---- */
/* Bench: a raw request against a model - a tool, not a place.

   The controls: the model, the temperature, the most tokens, a seed, how
   many answers, and the prompt - sent as typed, byte for byte. Run is dark
   while it cannot run, and says why. A run is a queue job like any other:
   it shows in the scheduler graphic, can be cancelled, and leaves its
   request record in the folder - so Bench needs a folder bound.

   Each run shows its answers once its job has finished: the text, the
   reasoning folded away, why it finished, how many tokens, how fast, the
   load it ran on, and the round trip. The session's runs are kept, newest
   first, until the page is reloaded.

   A TRANSCRIPTION, for a whisper model (PLAN-4 D11): the controls are the
   audio - the path of a WAV file on the server's machine - and the fields
   whisper-server reads, a JSON object sent as typed, none of them
   mandatory; the request is the contract's `audio_request`. Its answer is
   shown whole: the text, the language asked and the one heard, the language
   table when the answer has one, every segment and every word with how sure
   the model was, the audio's length - and the answer as the server gave it.

   The models are the server's (`models`); a model whose file is missing is
   listed, and cannot be chosen. What the controls hold is kept as typed:
   the page never rewrites what a person is typing. */

const BENCH_ANSWERS = [1, 2, 3];

SEEDS.bench = {
  model: null, temperature: '0.8', max_tokens: '400', seed: '', n: 1, prompt: '',
  audio: '', fields: '', sending: false, said: null, runs: [],
};

/* the fields as typed, as whisper-server's - a JSON object, or empty for
   none: {fields}, or what is wrong with them */
function benchFields(text) {
  if (String(text).trim() === '') return { fields: {} };
  let fields;
  try {
    fields = JSON.parse(text);
  } catch (e) {
    fields = null;
  }
  if (!fields || typeof fields !== 'object' || Array.isArray(fields)) {
    return { wrong: 'the fields are a JSON object of what whisper-server reads, or empty for none' };
  }
  return { fields };
}

/* the file a path names, for a job's name */
function fileName(path) {
  return String(path).split(/[\\/]/).filter(Boolean).pop() || String(path);
}

/* -------------------------------------------------------------- reading */

/* what the controls hold, as the request's `gen` - or what is wrong with it */
function benchGen(b) {
  const number = (text) => (String(text).trim() === '' ? NaN : Number(text));
  const t = number(b.temperature);
  const most = number(b.max_tokens);
  const seed = String(b.seed).trim();
  if (!(t >= 0 && t <= 2)) return { wrong: 'the temperature is a number from 0 to 2' };
  if (!(Number.isInteger(most) && most >= 1 && most <= 4096)) {
    return { wrong: 'the most tokens is a whole number from 1 to 4096' };
  }
  if (seed !== '' && !/^-?[0-9]+$/.test(seed)) return { wrong: 'the seed is a whole number, or empty for none' };
  const gen = { temperature: t, max_tokens: most, n: b.n };
  if (seed !== '') gen.seed = Number(seed);
  return { gen };
}

/* the server's models, and which can be chosen: a stand-in, or a file there */
function benchModels(state) {
  const f = state.feeds.models;
  if (!f.ok || !f.answer) return [];
  return f.answer.models.map((m) => ({
    key: m.key, label: m.label, loaded: m.loaded, usable: !m.file || m.file.present,
    hears: m.engine === 'whisper',
  }));
}

function firstWords(text) {
  const words = String(text).trim().split(/\s+/).slice(0, 6).join(' ');
  return words.length > 40 ? `${words.slice(0, 39)}…` : words;
}

/* ---------------------------------------------------------- view models */

function vmBench(state) {
  const b = state.bench;
  const models = benchModels(state);
  const usable = models.filter((m) => m.usable);
  // the one chosen; else the one on the card; else the first that can be
  const chosen = usable.find((m) => m.key === b.model) || usable.find((m) => m.loaded) || usable[0] || null;
  const gate = vmGate(state);
  // a whisper model hears: its request is audio and fields, not a prompt
  const hears = !!chosen && chosen.hears;
  const read = hears ? benchFields(b.fields) : benchGen(b);
  let why = null;
  if (!gate.folder) why = `Bench runs are queue jobs, so it needs a folder: ${gate.why}`;
  else if (state.feeds.models.ok === null) why = 'asking the server for its models';
  else if (!chosen) why = models.length ? 'no model here has its file' : 'the server has no models';
  else if (hears && String(b.audio).trim() === '') why = "the audio is empty: the path of a WAV file on the server's machine";
  else if (!hears && b.prompt === '') why = 'the prompt is empty';
  else if (read.wrong) why = read.wrong;
  else if (b.sending) why = 'sending';
  return {
    models: models.map((m) => ({
      key: m.key, usable: m.usable, chosen: !!chosen && m.key === chosen.key,
      text: `${m.label}${m.label !== m.key ? ` (${m.key})` : ''}${m.hears ? ' · whisper' : ''}`
        + `${m.usable ? '' : ' - its file is missing'}`,
    })),
    model: chosen ? chosen.key : null,
    hears,
    why,
    said: b.said,
  };
}

function vmBenchControls(state) {
  const b = state.bench;
  return {
    temperature: b.temperature, max_tokens: b.max_tokens, seed: b.seed, n: b.n, prompt: b.prompt,
    audio: b.audio, fields: b.fields,
  };
}

function vmRun(state, r) {
  const j = state.feeds.jobs;
  const job = j.ok && j.answer ? j.answer.jobs.find((x) => x.id === r.id) : null;
  const status = r.result ? r.result.job.status : (job ? job.status : null);
  let doing = status || 'asking the server';
  if (status === 'running') doing = stageWords(job) || 'running';
  else if (status === 'queued') doing = `waiting, #${job.queue_pos}`;
  else if (!status && j.ok) doing = 'not among the jobs of the folder bound now';
  const g = r.gen;
  return {
    id: r.id,
    head: r.audio !== undefined
      ? [r.model, r.audio, Object.keys(r.fields).length ? `fields ${JSON.stringify(r.fields)}` : 'no fields'].join(' · ')
      : [r.model, `temperature ${g.temperature}`, `at most ${g.max_tokens} tokens`,
        g.seed !== undefined ? `seed ${g.seed}` : null, g.n > 1 ? `${g.n} answers` : null,
      ].filter(Boolean).join(' · '),
    doing,
    cancel: status === 'queued' || status === 'running',
    prompt: r.prompt === undefined ? null : r.prompt,
    error: r.result ? r.result.job.error : null,
    answers: r.result ? r.result.candidates.map((c) => ({
      text: c.text,
      reasoning: c.reasoning || null,
      meta: [c.finish_reason ? `finished: ${c.finish_reason}` : null,
        c.tokens !== null && c.tokens !== undefined ? `${c.tokens} tokens` : null,
        c.timings && c.timings.predicted_per_second ? `${Math.round(c.timings.predicted_per_second)} tok/s` : null,
        c.audio_s !== undefined ? `${c.audio_s} s of audio` : null,
        c.load ? `on ${c.load}` : null,
      ].filter(Boolean).join(' · '),
      heard: c.transcript ? vmTranscript(c.transcript) : null,
    })) : [],
    trip: r.result && r.result.job.finished !== null
      ? `round trip ${(r.result.job.finished - r.result.job.created).toFixed(1)} s` : null,
    said: r.said,
  };
}

/* a transcript, whole, as whisper-server answered it (the contract's
   `transcript`): the language asked and the one heard, the language table
   when the answer has one - the likeliest first - every segment and every
   word with how sure the model was, and the answer itself, for everything
   else it says */
function vmTranscript(t) {
  const two = (n) => Number(n).toFixed(2);
  const heard = t.detected_language === undefined ? null
    : `heard: ${t.detected_language}${t.detected_language_probability === undefined ? ''
      : `, ${two(t.detected_language_probability)}`}`;
  return {
    language: [`language: ${t.language}`, heard].filter(Boolean).join(' · '),
    table: t.language_probabilities === undefined ? null
      : Object.entries(t.language_probabilities).sort((x, y) => y[1] - x[1]).map(([code, p]) => `${code} ${two(p)}`),
    segments: t.segments.map((s) => ({
      when: `${two(s.start)}–${two(s.end)} s`,
      said: s.text,
      numbers: `temperature ${s.temperature} · avg logprob ${s.avg_logprob} · no speech ${s.no_speech_prob}`,
      words: s.words.map((w) => ({ text: `${w.word.trim()} ${two(w.probability)}`, when: `${two(w.start)}–${two(w.end)} s` })),
    })),
    whole: JSON.stringify(t, null, 2),
  };
}

function vmRuns(state) {
  return state.bench.runs.map((r) => vmRun(state, r));
}

/* the runs whose jobs have finished, and whose answers are not here yet */
function benchOwed(state) {
  const j = state.feeds.jobs;
  if (!j.ok || !j.answer) return [];
  const over = new Set(j.answer.jobs.filter((x) => x.status !== 'queued' && x.status !== 'running')
    .map((x) => x.id));
  return state.bench.runs.filter((r) => !r.result && !r.said && over.has(r.id)).map((r) => r.id);
}

/* --------------------------------------------------------------- actions */

function setBench(fn) {
  store.set((s) => update(s, ['bench'], fn));
}

function setRun(id, fn) {
  setBench((b) => ({ ...b, runs: b.runs.map((r) => (r.id === id ? fn(r) : r)) }));
}

async function runBench() {
  const state = store.get();
  const b = state.bench;
  const { model, hears } = vmBench(state);
  let body;
  let run;
  if (hears) {
    // the contract's audio_request: the file as typed, and the fields when
    // any are named - none is mandatory
    const { fields } = benchFields(b.fields);
    body = {
      kind: 'transcribe',
      input: Object.keys(fields).length ? { audio: b.audio, fields } : { audio: b.audio },
      gen: { model }, name: `bench: ${fileName(b.audio)}`, priority: 'interactive',
    };
    run = { model, audio: b.audio, fields, result: null, said: null };
  } else {
    const { gen } = benchGen(b);
    body = {
      kind: 'generate', input: { prompt: b.prompt }, gen: { model, ...gen },
      name: `bench: ${firstWords(b.prompt)}`, priority: 'interactive',
    };
    run = { model, gen, prompt: b.prompt, result: null, said: null };
  }
  setBench((x) => ({ ...x, sending: true, said: null }));
  let got;
  try {
    got = await api('server.jobs.submit', { body });
  } catch (e) {
    setBench((x) => ({ ...x, sending: false, said: e.message }));
    return;
  }
  if (got.status !== 200) {
    logLine('warn', 'bench', `the run was refused: ${got.answer.error}`);
    setBench((x) => ({ ...x, sending: false, said: got.answer.error }));
    return;
  }
  const queued = { id: got.answer.job_id, ...run };
  logLine('info', 'bench', `${queued.id} queued, on ${model}`, { corr: got.answer.correlation_id });
  setBench((x) => ({ ...x, sending: false, runs: [queued, ...x.runs] }));
  refresh('jobs');
}

/* a finished run's answers, asked for once - never twice while one ask is out */
const BENCH_FETCHING = new Set();

async function fetchRun(id) {
  if (BENCH_FETCHING.has(id)) return;
  BENCH_FETCHING.add(id);
  let got;
  try {
    got = await api('server.jobs.result', { params: { id } });
  } catch (e) {
    BENCH_FETCHING.delete(id);
    setRun(id, (r) => ({ ...r, said: e.message }));
    return;
  }
  BENCH_FETCHING.delete(id);
  if (got.status !== 200) {
    setRun(id, (r) => ({ ...r, said: got.answer.error }));
    return;
  }
  const job = got.answer.job;
  // a failure warns; a run done, or cancelled by the person, is said
  logLine(job.status === 'failed' ? 'warn' : 'info', 'bench',
    `${id} ${job.status}${job.error ? `: ${job.error}` : ''}`, { corr: job.correlation_id });
  setRun(id, (r) => ({ ...r, result: { job, candidates: got.answer.candidates } }));
}

async function cancelRun(id) {
  setRun(id, (r) => ({ ...r, said: null }));
  const said = await cancelJob(id);
  if (said) setRun(id, (r) => ({ ...r, said }));
  refresh('jobs');
}

/* --------------------------------------------------------------- views */

function runCard(r) {
  return el('li', { class: 'run', 'data-run': r.id },
    el('div', { class: 'head' },
      el('span', { class: 'dim' }, r.id), ' ', el('span', { 'data-el': `run-head-${r.id}` }, r.head), ' ',
      el('span', { class: 'doing', 'data-el': `run-doing-${r.id}` }, r.doing),
      r.cancel ? el('button', { 'data-el': `run-cancel-${r.id}`, onclick: () => cancelRun(r.id) }, 'Cancel') : null),
    r.prompt === null ? null
      : el('details', { class: 'prompt' }, el('summary', {}, 'the prompt'), el('pre', {}, r.prompt)),
    r.said ? el('div', { class: 'said', 'data-el': `run-said-${r.id}` }, r.said) : null,
    r.error ? el('div', { class: 'bad', 'data-el': `run-error-${r.id}` }, r.error) : null,
    r.answers.map((a, i) => el('div', { class: 'answer', 'data-el': `run-answer-${r.id}-${i}` },
      a.reasoning ? el('details', { class: 'reasoning' }, el('summary', {}, 'reasoning'),
        el('pre', { 'data-el': `run-reasoning-${r.id}-${i}` }, a.reasoning)) : null,
      el('pre', { class: 'text', 'data-el': `run-text-${r.id}-${i}` }, a.text),
      el('div', { class: 'dim', 'data-el': `run-meta-${r.id}-${i}` }, a.meta),
      a.heard ? heardCard(a.heard, `${r.id}-${i}`) : null)),
    r.trip ? el('div', { class: 'dim', 'data-el': `run-trip-${r.id}` }, r.trip) : null);
}

/* a transcript, whole: the languages, the table, the segments and their
   words - how sure the model was of each, when it was said on hover - and
   the answer as the server gave it */
function heardCard(t, at) {
  return el('div', { class: 'heard' },
    el('div', { 'data-el': `run-language-${at}` }, t.language),
    t.table ? el('details', { class: 'languages', 'data-el': `run-languages-${at}` },
      el('summary', {}, `the language table: ${t.table.slice(0, 3).join(', ')}${t.table.length > 3 ? ', …' : ''}`),
      el('ol', {}, t.table.map((line) => el('li', {}, line)))) : null,
    t.segments.length ? el('ol', { class: 'segments', 'data-el': `run-segments-${at}` },
      t.segments.map((s, k) => el('li', {},
        el('span', { class: 'when' }, s.when), ' ', el('span', { class: 'spoken' }, s.said),
        el('div', { class: 'dim', 'data-el': `run-segment-${at}-${k}` }, s.numbers),
        el('div', { class: 'words', 'data-el': `run-words-${at}-${k}` },
          s.words.map((w) => el('span', { class: 'word', title: w.when }, w.text))))))
      : el('div', { class: 'dim', 'data-el': `run-segments-${at}` }, 'No words were heard.'),
    el('details', { class: 'whole', 'data-el': `run-transcript-${at}` },
      el('summary', {}, 'the answer as the server gave it'), el('pre', {}, t.whole)));
}

TOOLS.push({
  id: 'bench', title: 'Bench', rank: 10,
  view(node) {
    const stops = [follow('models'), follow('jobs')];
    const field = (name, key, attrs) => el('input', {
      'data-el': `bench-${name}`, ...attrs,
      oninput: (e) => setBench((b) => ({ ...b, [key]: e.target.value })),
    });
    const model = el('select', {
      'data-el': 'bench-model', onchange: (e) => setBench((b) => ({ ...b, model: e.target.value })),
    });
    const temperature = field('temperature', 'temperature', { inputmode: 'decimal', size: 4 });
    const most = field('max-tokens', 'max_tokens', { inputmode: 'numeric', size: 5 });
    const seed = field('seed', 'seed', { inputmode: 'numeric', size: 8, placeholder: 'none' });
    const answers = el('select', {
      'data-el': 'bench-n', onchange: (e) => setBench((b) => ({ ...b, n: Number(e.target.value) })),
    }, BENCH_ANSWERS.map((n) => el('option', { value: String(n) }, String(n))));
    const prompt = el('textarea', {
      'data-el': 'bench-prompt', rows: 8, spellcheck: 'false',
      oninput: (e) => setBench((b) => ({ ...b, prompt: e.target.value })),
    });
    // a transcription's: the audio, and whisper-server's fields
    const audio = field('audio', 'audio', { size: 48, spellcheck: 'false', placeholder: 'D:/takes/take-1.wav' });
    const fields = el('textarea', {
      'data-el': 'bench-fields', rows: 3, spellcheck: 'false', placeholder: '{"language": "en"} - or empty for none',
      oninput: (e) => setBench((b) => ({ ...b, fields: e.target.value })),
    });
    const run = el('button', { 'data-el': 'bench-run', onclick: () => runBench() }, 'Run');
    const why = el('span', { 'data-el': 'bench-why' });
    const runs = el('ol', { class: 'runs', 'data-el': 'bench-runs' });
    const label = (text, control) => el('label', {}, el('span', { class: 'dim' }, text), control);
    // what a text model is asked with, and what a whisper model hears: built
    // once, and the one the chosen model does not take put away
    const text = el('span', { class: 'group', 'data-el': 'bench-text' },
      label('temperature', temperature), label('most tokens', most), label('seed', seed), label('answers', answers));
    const hears = el('div', { class: 'hears', 'data-el': 'bench-hears' },
      label('audio', audio), label('fields', fields));
    setKids(node, el('div', { class: 'bench' },
      el('div', { class: 'controls' }, label('model', model), text),
      prompt, hears, el('div', { class: 'go' }, run, ' ', why), runs));
    // what the controls hold follows the store - set only when it differs,
    // so a person's typing is never rewritten under the caret
    const hold = (input, value) => { if (input.value !== value) input.value = value; };
    stops.push(store.watch(vmBenchControls, (vm) => {
      hold(temperature, vm.temperature);
      hold(most, vm.max_tokens);
      hold(seed, vm.seed);
      hold(answers, String(vm.n));
      hold(prompt, vm.prompt);
      hold(audio, vm.audio);
      hold(fields, vm.fields);
    }, 'bench controls'));
    const shown = (part, on) => (on ? part.removeAttribute('hidden') : part.setAttribute('hidden', ''));
    stops.push(store.watch(vmBench, (vm) => {
      setKids(model, vm.models.map((m) => el('option', {
        value: m.key, disabled: !m.usable, selected: m.chosen,
      }, m.text)));
      if (vm.model !== null) model.value = vm.model;
      shown(text, !vm.hears);
      shown(prompt, !vm.hears);
      shown(hears, vm.hears);
      run.disabled = vm.why !== null;
      run.setAttribute('title', vm.why || 'Queue this request, as typed');
      setKids(why, vm.said ? el('span', { class: 'said' }, vm.said) : vm.why || []);
    }, 'bench'));
    stops.push(store.watch(vmRuns, (vms) => setKids(runs, vms.map(runCard)), 'bench runs'));
    stops.push(store.watch(benchOwed, (ids) => ids.forEach(fetchRun), 'bench results'));
    return () => stops.forEach((stop) => stop());
  },
});
/* ---- check.js ---- */
/* The request check (PLAN-4 D16): a held request, scanned at a glance and
   tuned for this one request, then sent or cancelled.

   A job submitted with `check: true` is held by the server once its request
   is built - at its turn, so a request made from an earlier job's answer is
   seen exactly as it will be sent. This tool lists the held jobs (the `jobs`
   feed, while it is placed) and shows the one chosen, as the server says it
   (`server.jobs.held`, asked when it is chosen):

     the model     its key and its engine
     the count     its prompt's tokens as the model counted them, the room its
                   answer needs, against the model's context - and whether it
                   fits; a request that does not fit is held to be changed
     the time      what the model's statistics expect of it
     the messages  by role, each one editable - or, for a whisper model, the
                   audio and whisper-server's fields; for a request the server
                   cannot say more of, the request itself, as JSON
     parameters    every one, its value editable, and where it came from: the
                   application, the model's settings, the bootstrap's default
     the said      why anything is not known, in the server's words
     your views    what the application registered for its own part of the
                   request (below)

   Send sends it as built, or with what was changed - only what differs from
   what was built is sent as a change - and the server says whether it was
   tuned; Cancel cancels the job. A refusal is shown in the server's words.

   THE SEAM FOR AN APPLICATION'S OWN DATA:

     llmKit.requestView(name, {title, shows(held) -> bool, view(node, held, give) -> stop})

   Each application sends different data - a brief by section, a recording
   with a player. It registers a view for its part, as a part registers
   anything here; the core arranges what every request has, and each view
   whose `shows` says yes is built below it, in the order registered, handed
   the held request as the server said it. A view is read-only, or gives
   back a changed request with `give(request)`: the request then sent is
   that one, and its fields above are no longer read. What is typed is kept
   per job until it is sent, cancelled or the page is reloaded; the page
   never rewrites what a person is typing. */

/* `gone`: the jobs this tool sent or cancelled. Each is no longer held the
   moment the server says so, while the jobs feed may still list it held
   until it is asked again - never shown, and never asked for, again (the
   server would refuse it `not_held`). */
SEEDS.check = { chosen: null, held: {}, edits: {}, sending: null, said: {}, gone: {} };

/* the application's views, in the order they were registered */
const REQUEST_VIEWS = [];

function requestView(name, spec) {
  const at = `llmKit.requestView(${JSON.stringify(name)}, ...)`;
  if (typeof name !== 'string' || !/^[a-z][a-z0-9-]*$/.test(name)) {
    throw new Error(`llmKit.requestView: a view's name is lower-case letters, digits and dashes, not ${JSON.stringify(name)}`);
  }
  if (REQUEST_VIEWS.some((v) => v.name === name)) throw new Error(`${at}: a view of that name is registered already`);
  if (!spec || typeof spec.view !== 'function') throw new Error(`${at}: its view is a function (node, held, give) -> stop`);
  if (spec.shows !== undefined && typeof spec.shows !== 'function') throw new Error(`${at}: shows is a function (held) -> bool`);
  REQUEST_VIEWS.push({ name, title: String(spec.title || name), shows: spec.shows || (() => true), view: spec.view });
  return () => {
    const i = REQUEST_VIEWS.findIndex((v) => v.name === name);
    if (i >= 0) REQUEST_VIEWS.splice(i, 1);
  };
}

const FROM_WORDS = { application: 'the application', model: "the model's settings", bootstrap: "the bootstrap's default" };

function setCheck(fn) {
  store.set((s) => update(s, ['check'], fn));
}

function num(n) {
  return Number(n).toLocaleString('en-US');
}

/* a value as it is typed: JSON, so a list stays a list and a word a string */
function asText(v) {
  return JSON.stringify(v);
}

/* ---------------------------------------------------------- view models */

/* the held jobs, as the jobs feed last said them, and the one chosen */
function vmCheckList(state) {
  const f = state.feeds.jobs;
  if (f.ok === null) return { words: 'Asking the server…', jobs: [], chosen: null };
  if (!f.ok || !f.answer) return { words: 'The server did not give its jobs.', jobs: [], chosen: null };
  const jobs = f.answer.jobs.filter((j) => j.status === 'held' && !state.check.gone[j.id])
    .map((j) => ({ id: j.id, text: `${j.name} · ${j.model}` }));
  const chosen = jobs.some((j) => j.id === state.check.chosen) ? state.check.chosen
    : jobs.length ? jobs[0].id : null;
  return {
    words: jobs.length ? `${jobs.length} held` : 'Nothing is held: a job submitted with check: true is held here once its request is built.',
    jobs: jobs.map((j) => ({ ...j, on: j.id === chosen })),
    chosen,
  };
}

/* the chosen job whose held request is still to be asked for */
function checkOwed(state) {
  const id = vmCheckList(state).chosen;
  return id !== null && state.check.held[id] === undefined ? id : null;
}

function countWords(h) {
  if (!h.count) return null;
  const c = h.count;
  return `${num(c.prompt_tokens)} tokens, and room for ${num(c.room)} more, of ${num(c.context)} - `
    + (h.fits ? 'it fits' : 'it does not fit: sent as it is, it fails prompt_too_long');
}

function expectWords(h) {
  const e = h.expect;
  if (!e) return null;
  const what = e.measure === 'answer_s' ? 'answers' : 'transcriptions';
  return `about ${e.median_s} s - 9 in 10 within ${e.p90_s} s (from ${e.samples} ${what} measured)`;
}

/* the chosen held request, as the view paints it */
function vmCheck(state) {
  const id = vmCheckList(state).chosen;
  if (id === null) return { id: null };
  const got = state.check.held[id];
  if (!got) return { id, asking: true };
  if (got.status !== 200) return { id, wrong: got.answer.error };
  const h = got.answer.held;
  return {
    id,
    head: [h.model, h.engine || 'engine not said', `${h.job.name} (${h.job.id})`].join(' · '),
    count: countWords(h), fits: h.fits,
    expect: expectWords(h),
    said: h.said,
    messages: h.messages ? h.messages.map((m) => ({ role: m.role, content: m.content })) : null,
    audio: h.audio, fields: h.fields,
    raw: !h.messages && !h.audio,
    params: h.params.map((p) => ({ name: p.name, text: asText(p.value), from: FROM_WORDS[p.from] })),
    views: REQUEST_VIEWS.filter((v) => { try { return v.shows(h); } catch (e) { return false; } }).map((v) => v.name),
  };
}

/* what Send would send - {body, changed} - or what is wrong with it */
function checkBody(h, ed) {
  const body = {};
  const edits = ed || {};
  if (edits.given !== undefined) {
    body.request = edits.given;
  } else if (h.messages && edits.messages && Object.keys(edits.messages).length) {
    const talk = h.messages.map((m, i) => ({ role: m.role, content: edits.messages[i] !== undefined ? edits.messages[i] : m.content }));
    if (JSON.stringify(talk) !== JSON.stringify(h.messages)) {
      const was = h.request;
      if (typeof was === 'string') body.request = talk[0].content;
      else if (was && typeof was === 'object' && typeof was.prompt === 'string') body.request = { ...was, prompt: talk[0].content };
      else body.request = { messages: talk };
    }
  } else if (h.audio && (edits.audio !== undefined || edits.fields !== undefined)) {
    let fields = h.fields;
    if (edits.fields !== undefined) {
      try {
        fields = JSON.parse(edits.fields);
      } catch (e) {
        return { wrong: "the fields are a JSON object of what whisper-server reads" };
      }
    }
    const audio = edits.audio !== undefined ? edits.audio : h.audio.path;
    const request = { audio, fields };
    if (JSON.stringify(request) !== JSON.stringify({ audio: h.audio.path, fields: h.fields })) body.request = request;
  } else if (edits.request !== undefined) {
    let request;
    try {
      request = JSON.parse(edits.request);
    } catch (e) {
      return { wrong: 'the request is JSON' };
    }
    if (JSON.stringify(request) !== JSON.stringify(h.request)) body.request = request;
  }
  const gen = {};
  for (const p of h.params) {
    const text = (edits.params || {})[p.name];
    if (text === undefined) continue;
    let value;
    try {
      value = JSON.parse(text);
    } catch (e) {
      return { wrong: `${p.name} is a JSON value - a number, true or false, "words", a [list]` };
    }
    if (JSON.stringify(value) === JSON.stringify(p.value)) continue;
    const dot = p.name.indexOf('.');
    if (dot < 0) {
      gen[p.name] = value;
    } else {
      const top = p.name.slice(0, dot);
      gen[top] = { ...(h.gen[top] || gen[top] || {}), [p.name.slice(dot + 1)]: value };
    }
  }
  if (Object.keys(gen).length) body.gen = gen;
  return { body, changed: Object.keys(body).length > 0 };
}

function vmCheckSend(state) {
  const id = vmCheckList(state).chosen;
  const got = id === null ? null : state.check.held[id];
  if (!got || got.status !== 200) return { id, why: 'nothing held is shown', label: 'Send', said: null };
  const sent = checkBody(got.answer.held, state.check.edits[id]);
  const busy = state.check.sending === id;
  return {
    id,
    why: busy ? 'sending…' : sent.wrong || null,
    label: sent.changed ? 'Send changed' : 'Send as built',
    said: state.check.said[id] || null,
  };
}

/* --------------------------------------------------------------- actions */

async function fetchHeld(id) {
  let got;
  try {
    got = await api('server.jobs.held', { params: { id } });
  } catch (e) {
    got = { status: 0, answer: { error: e.message } };
  }
  setCheck((c) => ({ ...c, held: { ...c.held, [id]: got } }));
}

function editCheck(id, fn) {
  setCheck((c) => ({ ...c, edits: { ...c.edits, [id]: fn(c.edits[id] || {}) } }));
}

function forget(c, id) {
  const held = { ...c.held };
  const edits = { ...c.edits };
  delete held[id];
  delete edits[id];
  return { ...c, held, edits, gone: { ...c.gone, [id]: true } };
}

async function sendHeld(id) {
  const got = store.get().check.held[id];
  if (!got || got.status !== 200) return;
  const sent = checkBody(got.answer.held, store.get().check.edits[id]);
  if (sent.wrong) return;
  setCheck((c) => ({ ...c, sending: id, said: { ...c.said, [id]: null } }));
  let answer;
  try {
    answer = await api('server.jobs.send', { params: { id }, body: sent.body });
  } catch (e) {
    setCheck((c) => ({ ...c, sending: null, said: { ...c.said, [id]: e.message } }));
    return;
  }
  if (answer.status === 200) {
    const a = answer.answer;
    logLine('info', 'check', `${id} sent ${a.tuned ? `tuned: ${a.changed.join(', ')}` : 'as built'}`);
    setCheck((c) => ({ ...forget(c, id), sending: null }));
    refresh('jobs');
  } else {
    logLine('warn', 'check', `${id} could not be sent: ${answer.answer.error}`);
    setCheck((c) => ({ ...c, sending: null, said: { ...c.said, [id]: answer.answer.error } }));
  }
}

async function cancelHeld(id) {
  let answer;
  try {
    answer = await api('server.jobs.cancel', { params: { id } });
  } catch (e) {
    setCheck((c) => ({ ...c, said: { ...c.said, [id]: e.message } }));
    return;
  }
  if (answer.status === 200) {
    logLine('info', 'check', `${id} cancelled while it was held`);
    setCheck((c) => forget(c, id));
    refresh('jobs');
  } else {
    setCheck((c) => ({ ...c, said: { ...c.said, [id]: answer.answer.error } }));
  }
}

/* --------------------------------------------------------------- views */

/* a text control holding what is typed: set from the store only when the
   store holds another text, so typing is never rewritten */
function heldInput(tag, name, value, oninput, attrs) {
  const node = el(tag, { 'data-el': name, spellcheck: 'false', ...attrs, oninput: (e) => oninput(e.target.value) });
  node.value = value;
  return node;
}

function checkRequest(vm, h, id) {
  const edits = store.get().check.edits[id] || {};
  const parts = [];
  if (vm.messages) {
    parts.push(el('div', { class: 'messages', 'data-el': 'check-messages' }, vm.messages.map((m, i) =>
      el('label', { class: 'message' }, el('span', { class: 'dim', 'data-el': `check-role-${i}` }, m.role),
        heldInput('textarea', `check-message-${i}`, (edits.messages || {})[i] !== undefined ? edits.messages[i] : m.content,
          (text) => editCheck(id, (e) => ({ ...e, messages: { ...(e.messages || {}), [i]: text } })), { rows: 3 })))));
  } else if (vm.audio) {
    // two lines (R-B2): the audio and its length, then the fields
    parts.push(el('div', { class: 'hears', 'data-el': 'check-hears' },
      el('div', { class: 'line', 'data-el': 'check-audio-line' },
        el('label', {}, el('span', { class: 'dim' }, 'audio'),
          heldInput('input', 'check-audio', edits.audio !== undefined ? edits.audio : vm.audio.path,
            (text) => editCheck(id, (e) => ({ ...e, audio: text })), { size: 48 })),
        el('span', { class: 'dim', 'data-el': 'check-seconds' }, vm.audio.seconds === null ? '' : ` ${vm.audio.seconds} s`)),
      el('label', {}, el('span', { class: 'dim' }, 'fields'),
        heldInput('textarea', 'check-fields', edits.fields !== undefined ? edits.fields : asText(vm.fields || {}),
          (text) => editCheck(id, (e) => ({ ...e, fields: text })), { rows: 2 }))));
  } else {
    parts.push(el('label', {}, el('span', { class: 'dim' }, 'the request'),
      heldInput('textarea', 'check-request', edits.request !== undefined ? edits.request : asText(h.request),
        (text) => editCheck(id, (e) => ({ ...e, request: text })), { rows: 4 })));
  }
  parts.push(el('table', { class: 'params', 'data-el': 'check-params' }, vm.params.map((p) =>
    el('tr', {},
      el('td', {}, el('code', {}, p.name)),
      el('td', {}, heldInput('input', `check-param-${p.name}`, (edits.params || {})[p.name] !== undefined ? edits.params[p.name] : p.text,
        (text) => editCheck(id, (e) => ({ ...e, params: { ...(e.params || {}), [p.name]: text } })), { size: 16 })),
      el('td', { class: 'dim', 'data-el': `check-from-${p.name}` }, p.from)))));
  if (vm.params.length) parts.push(el('div', { class: 'dim' }, 'Anything not named here is the engine\'s own.'));
  return parts;
}

TOOLS.push({
  id: 'check', title: 'Check', rank: 30,
  view(node) {
    const stops = [follow('jobs')];
    const list = el('div', { class: 'recent', 'data-el': 'check-list' });
    const words = el('div', { class: 'dim', 'data-el': 'check-words' });
    const shown = el('div', { class: 'held', 'data-el': 'check-held' });
    const send = el('button', { 'data-el': 'check-send', onclick: () => sendHeld(vmCheckSend(store.get()).id) }, 'Send');
    const cancel = el('button', { 'data-el': 'check-cancel', onclick: () => cancelHeld(vmCheckSend(store.get()).id) }, 'Cancel');
    const why = el('span', { 'data-el': 'check-why' });
    const said = el('div', { class: 'said', 'data-el': 'check-said' });
    const go = el('div', { class: 'go', 'data-el': 'check-go' }, send, ' ', cancel, ' ', why);
    setKids(node, el('div', { class: 'check-tool' }, words, list, shown, go, said));
    let viewStops = [];
    const stopViews = () => {
      viewStops.forEach((stop) => { try { stop(); } catch (e) { console.error(e); } });
      viewStops = [];
    };
    stops.push(store.watch(vmCheckList, (vm) => {
      setKids(words, vm.words);
      setKids(list, vm.jobs.map((j) => el('button', {
        class: `link${j.on ? ' on' : ''}`, 'data-el': `check-job-${j.id}`,
        onclick: () => setCheck((c) => ({ ...c, chosen: j.id })),
      }, j.text)));
    }, 'check list'));
    stops.push(store.watch(checkOwed, (id) => { if (id !== null) fetchHeld(id); }, 'check owed'));
    stops.push(store.watch(vmCheck, (vm) => {
      stopViews();
      if (vm.head) go.removeAttribute('hidden'); else go.setAttribute('hidden', '');
      if (vm.id === null) return setKids(shown, []);
      if (vm.asking) return setKids(shown, el('div', { class: 'dim' }, 'Asking the server…'));
      if (vm.wrong) return setKids(shown, el('div', { class: 'bad', 'data-el': 'check-wrong' }, vm.wrong));
      const h = store.get().check.held[vm.id].answer.held;
      const own = el('div', { class: 'views', 'data-el': 'check-views' });
      setKids(shown, [
        el('h4', { 'data-el': 'check-head' }, vm.head),
        vm.count ? el('div', { class: vm.fits ? null : 'bad', 'data-el': 'check-count' }, vm.count) : null,
        vm.expect ? el('div', { 'data-el': 'check-expect' }, vm.expect) : null,
        vm.said.length ? el('ul', { class: 'dim', 'data-el': 'check-said-server' }, vm.said.map((s) => el('li', {}, s))) : null,
        ...checkRequest(vm, h, vm.id),
        own,
      ]);
      for (const name of vm.views) {
        const v = REQUEST_VIEWS.find((x) => x.name === name);
        const box = el('section', { class: 'view', 'data-view': name }, el('h4', {}, v.title));
        own.appendChild(box);
        const id = vm.id;
        const give = (request) => editCheck(id, (e) => ({ ...e, given: JSON.parse(JSON.stringify(request)) }));
        try {
          const stop = v.view(box, JSON.parse(JSON.stringify(h)), give);
          if (typeof stop === 'function') viewStops.push(stop);
        } catch (e) {
          box.appendChild(el('div', { class: 'bad' }, `the view ${name} failed: ${e.message}`));
        }
      }
    }, 'check'));
    stops.push(store.watch(vmCheckSend, (vm) => {
      send.disabled = vm.why !== null;
      setKids(send, vm.label);
      send.setAttribute('title', vm.why || 'Send this request: as built, or with what was changed');
      setKids(why, vm.why && vm.why !== 'nothing held is shown' ? vm.why : []);
      setKids(said, vm.said || []);
    }, 'check send'));
    return () => {
      stopViews();
      stops.forEach((stop) => stop());
    };
  },
});
/* ---- tools.js ---- */
/* The instruments behind one door: the Tools menu, and the one tool shown
   below it - Bench, Models, the Log (TOOLS, by rank). A page places the door
   where it wants the instruments (llmKit.place('tools', ...)): the
   bootstrap's own page puts it under its bar, filling the rest of the
   window. Each tool can also be placed alone, in an element of its own.

   `ui.tab` names the tool shown (null: the first); `ui.tools` is whether the
   Tools menu is open. What the pane does not show is stopped - its watchers
   gone, its feeds not asked for it: a hidden thing costs nothing. */

/* how many doors are placed on the page: the log's light opens the Log
   behind one when there is one (log.js) - running machinery, not state */
const DOORS = { n: 0 };

/* ---------------------------------------------------------- view models */

/* the tools, by rank - the order a part registered them in means nothing */
function toolsInOrder() {
  return TOOLS.slice().sort((a, b) => a.rank - b.rank);
}

/* what the pane shows: the tool `ui.tab` names; else - none named yet, or a
   name no tool has - the first tool */
function vmPane(state) {
  if (TOOLS.some((t) => t.id === state.ui.tab)) return state.ui.tab;
  const tools = toolsInOrder();
  return tools[0] ? tools[0].id : null;
}

/* the strip: the Tools door, naming the tool shown */
function vmStrip(state) {
  const shown = vmPane(state);
  const tools = toolsInOrder();
  return {
    tools: tools.map((t) => ({ id: t.id, title: t.title, active: t.id === shown })),
    open: state.ui.tools,
    tool: (tools.find((t) => t.id === shown) || {}).title || null,
  };
}

/* --------------------------------------------------------------- views */

/* A region shows one thing at a time, chosen by `select`: when the choice
   changes, the old thing is stopped before the new one is built. */
function region(node, select, open, name) {
  let stop = null;
  const unwatch = store.watch(select, (key) => {
    if (stop) stop();
    stop = null;
    setKids(node, []);
    if (key !== null && key !== undefined) stop = open(node, key);
  }, name);
  return () => {
    unwatch();
    if (stop) stop();
  };
}

/* the door is drawn as the one tab of the strip, and always the one shown:
   with tools, the pane shows one */
function paintStrip(node, vm) {
  setKids(node, vm.tools.length ? el('div', { class: 'tools' },
    el('button', {
      'data-el': 'tools', class: 'tab active', 'aria-expanded': String(vm.open),
      // named from the tools themselves: a list typed here goes stale the
      // day a tool is added (a cold read added one, and it did)
      title: `The instruments: ${vm.tools.map((t) => t.title).join(', ')}`,
      onclick: () => store.set((s) => update(s, ['ui', 'tools'], (v) => !v)),
    }, `Tools: ${vm.tool} ▾`),
    vm.open ? el('div', { class: 'menu', 'data-el': 'tools-menu' }, vm.tools.map((t) => el('button', {
      'data-el': `tool-${t.id}`, class: t.active ? 'on' : null,
      onclick: () => store.set((s) => ({ ...s, ui: { ...s.ui, tab: t.id, tools: false } })),
    }, t.title))) : null) : null);
}

/* the door and its pane, built into `node` -> stop */
function toolsView(node) {
  DOORS.n += 1;
  placedMoved();
  const strip = el('nav', { 'data-el': 'strip', class: 'strip' });
  const pane = el('main', { 'data-el': 'pane', class: 'pane' });
  setKids(node, [strip, pane]);
  const stops = [
    store.watch(vmStrip, (vm) => paintStrip(strip, vm), 'strip'),
    region(pane, vmPane, (n, id) => TOOLS.find((t) => t.id === id).view(n), 'pane'),
  ];
  return () => {
    DOORS.n -= 1;
    stops.forEach((stop) => stop());
    placedMoved();
  };
}
/* ---- container.js ---- */
/* Containers: widgets side by side, in slots - the step-3 bar made general.
   Nothing here knows it is placing the bootstrap's bar (R-F13): the bar is
   one container, docked to the top of the bootstrap's own page.

   llmKit.container(where, {name, slots, shed, axis}) makes one (kit.js):
     where   'top' | 'bottom' | 'left' | 'right': docked to that edge of the
             window - fixed there, and the page's body padded on that side by
             the container's size, on top of its own, so nothing of the page
             is under it. Or an element of the page: put at its end, in the
             flow of the page
     name    what the folder keeps its layout under: one a page
     slots   {start, middle, end}: the widgets it holds - each slot's side by
             side, in order, so "next to another widget" is the same slot
     shed    the order they leave in when the room runs short, first first
     axis    'row' or 'column', for one in an element (a row unless said).
             Docked, its edge decides: top and bottom rows, left and right
             columns

   THE SIZE IS MEASURED, never guessed. The container's own observer keeps
   its length along its axis and the size of an em in the store
   (`containers.<name>`); the rows set them as the observer would. What the
   placed widgets need is the sum of their boxes along the axis - a box's
   width along a row, its lines (BOX.line em each) along a column - and the
   room between them, in the same numbers the container is painted with
   (BOX): so the sum and the paint cannot disagree. The room between boxes
   is the slot's, and around them the container's: a box has no edge of its
   own (R-F14).

   A CONTAINER HOLDS WHAT IT WAS GIVEN: its widgets are the ones its slots
   were given. A kept layout moves them, and names no other into it.

   A WIDGET THE PAGE ADDS LATER IS WHERE THE PAGE PUTS IT, never in the tray
   (the owner's ruling of 2026-09-27, S14k's R2: "the point of the widget is
   to self-contain its feature so we can place it where we will and style it
   how we want"). A layout says the widgets it has seen (`known`); one the
   container holds and its layout has not seen goes into the slot the page
   gave it - before the first widget the page gave after it there that sits
   there now, else at the slot's end - and is seen from then on. So a widget
   in no slot and seen is one the person put away, and only those are in the
   tray with the shed: the tray stays derived (R-F12). A layout kept before
   layouts said what they had seen has seen what it names: its slots and its
   drop order.

   THE DROP ORDER IS AN ORDER: `layout.<name>.shed` names widgets in the
   order they leave, first first. A widget it does not name leaves before any
   it names; a critical one (Customize) never leaves and is never put away -
   it is the way back.

   THE TRAY is every widget of the container not placed: put away (in no
   slot) or shed (no room) - derived, never stored (R-F12). Nothing in it is
   built, so nothing is asked for it; as the room comes back, so do they
   (R-F16).

   CUSTOMIZATION MODE is the whole page's: in it, a container's widgets are
   dragged to another of its slots, or to its tray, and back. Without a
   pointer: ✕ puts a widget away, the tray's Put back restores it, and the
   banner's ◀ ▶ reorder what is shed first. Every edit is one of four, each
   naming its container, and only these touch a layout: move, put away,
   restore, set priority. */

const SLOTS = ['start', 'middle', 'end'];
const EDGES = ['top', 'bottom', 'left', 'right'];
/* in em: the padding along the axis (both ends together), the room between
   widgets in a slot, between slots, the tray's button along each axis, and
   the length of a box's line */
const BOX = { pad: 1.2, gap: 0.5, between: 1, tray: { row: 3, column: 1.6 }, line: 1.6 };

/* the containers on the page now, by name - running machinery, not state:
   {axis, edge, members} */
const CONTAINERS = {};
/* each container's layout as its page gave it, by name - where restore puts
   a widget back, and where the journal begins when the folder kept none */
const LAYOUT_SEEDS = {};

SEEDS.layout = {};
SEEDS.containers = {};

/* the Customize button: somewhere on the page, whatever the room */
ITEMS.unshift({
  id: 'customize', title: 'Customize', box: { w: 8, h: 1 }, critical: true,
  view(node) {
    return store.watch(vmCustomize, (vm) => setKids(node, el('button', {
      'data-el': 'customize', class: vm.on ? 'on' : null, 'aria-pressed': vm.on,
      title: vm.on ? 'Leave Customization Mode (Esc)' : 'Customize where the widgets sit',
      onclick: () => store.set((s) => update(s, ['ui', 'customizing'], (v) => !v)),
    }, vm.on ? 'Done' : 'Customize')), 'customize');
  },
});

function vmCustomize(state) {
  return { on: state.ui.customizing };
}

/* ------------------------------------------------------------ the edits */

function withoutItem(slots, id) {
  return Object.fromEntries(SLOTS.map((name) => [name, slots[name].filter((x) => x !== id)]));
}

/* the four edits: each takes the layouts and gives the next ones, changing
   its own container's alone */
const LAYOUT_EDITS = {
  move(layout, { container, id, slot, before }) {
    const was = layout[container];
    const slots = withoutItem(was.slots, id);
    const at = before ? slots[slot].indexOf(before) : -1;
    slots[slot] = at < 0 ? [...slots[slot], id] : [...slots[slot].slice(0, at), id, ...slots[slot].slice(at)];
    return { ...layout, [container]: { ...was, slots } };
  },
  putAway(layout, { container, id }) {
    const was = layout[container];
    return { ...layout, [container]: { ...was, slots: withoutItem(was.slots, id) } };
  },
  restore(layout, { container, id }) {
    // back where its page first had it, at the end of that slot
    const seed = LAYOUT_SEEDS[container];
    const home = SLOTS.find((name) => seed.slots[name].includes(id)) || 'end';
    return LAYOUT_EDITS.move(layout, { container, id, slot: home, before: null });
  },
  setPriority(layout, { container, id, by }) {
    const was = layout[container];
    const order = was.shed.filter((x) => x !== id);
    const now = was.shed.indexOf(id);
    const at = Math.max(0, Math.min(order.length, (now < 0 ? 0 : now) + by));
    return { ...layout, [container]: { ...was, shed: [...order.slice(0, at), id, ...order.slice(at)] } };
  },
};

/* the widgets a layout has seen: those it says, or - kept before layouts
   said so - those it names */
function layoutSeen(layout) {
  return Array.isArray(layout.known) ? layout.known
    : [...SLOTS.flatMap((name) => layout.slots[name]), ...layout.shed];
}

/* the layouts, every container on the page having seen its widgets: one it
   has not seen is put where its page put it, and seen. Never an edit - no
   journal records it, and no undo takes it back */
function layoutsSeeing(layouts) {
  let out = layouts;
  for (const [name, c] of Object.entries(CONTAINERS)) {
    if (!out[name]) continue;
    const seen = layoutSeen(out[name]);
    const fresh = c.members.filter((id) => !seen.includes(id));
    let now = { ...out[name], known: [...seen, ...fresh] };
    for (const id of fresh) {
      const given = LAYOUT_SEEDS[name].slots;
      const home = SLOTS.find((slot) => given[slot].includes(id));
      const after = given[home].slice(given[home].indexOf(id) + 1);
      const before = after.find((x) => now.slots[home].includes(x)) || null;
      now = LAYOUT_EDITS.move({ [name]: now }, { container: name, id, slot: home, before })[name];
    }
    out = { ...out, [name]: now };
  }
  return out;
}

/* an edit refused before it is made: a widget the container does not hold,
   and the way back put away */
function editRefused(op, args) {
  const c = CONTAINERS[args.container];
  if (!c) return `no container called ${args.container}`;
  if (!c.members.includes(args.id)) return `${args.id} is not a widget of ${args.container}`;
  const item = ITEMS.find((it) => it.id === args.id);
  if (item.critical && op === 'putAway') return `${item.title} is never put away: it is the way back`;
  return null;
}

/* every edit is the journal's (journal.js): recorded, undoable, kept */
function editLayout(op, args) {
  if (editRefused(op, args)) return;
  journalDo(op, args);
}

/* ---------------------------------------------------------- view models */

/* a box's length along an axis, in em */
function along(axis, box) {
  return axis === 'row' ? box.w : box.h * BOX.line;
}

/* what the placed widgets need along the axis, in em - painted with the
   same numbers */
function containerNeed(axis, placed, known, trayShown) {
  let need = BOX.pad + BOX.between * (SLOTS.length - 1);
  for (const name of SLOTS) {
    const inSlot = placed.filter((p) => p.slot === name);
    need += inSlot.reduce((sum, p) => sum + along(axis, known.get(p.id).box), 0);
    need += BOX.gap * Math.max(0, inSlot.length - 1);
  }
  return trayShown ? need + BOX.between + BOX.tray[axis] : need;
}

/* what is placed, what is shed for want of room, and what was put away */
function containerPlacement(state, name) {
  const c = CONTAINERS[name];
  const known = new Map(ITEMS.map((it) => [it.id, it]));
  const layout = state.layout[name];
  let placed = SLOTS.flatMap((slot) => layout.slots[slot].filter((id) => c.members.includes(id))
    .map((id) => ({ id, slot })));
  const away = c.members.filter((id) => !placed.some((p) => p.id === id));
  const shed = [];
  const { size, em } = state.containers[name];
  if (typeof size === 'number' && size > 0) {
    const room = size / em;
    const named = layout.shed.filter((id) => c.members.includes(id));
    const order = [...placed.map((p) => p.id).filter((id) => !named.includes(id)), ...named]
      .filter((id) => !known.get(id).critical);
    for (const id of order) {
      // the tray shows while it holds anything - and, in Customization
      // Mode, always: it is where a widget is dropped to be put away
      if (containerNeed(c.axis, placed, known, away.length + shed.length > 0 || state.ui.customizing) <= room) break;
      if (!placed.some((p) => p.id === id)) continue;
      placed = placed.filter((p) => p.id !== id);
      shed.push(id);
    }
  }
  return { placed, shed, away };
}

function vmContainer(state, name) {
  const known = new Map(ITEMS.map((it) => [it.id, it]));
  const { placed, shed, away } = containerPlacement(state, name);
  const slot = (s) => placed.filter((p) => p.slot === s).map((p) => {
    const it = known.get(p.id);
    return { id: p.id, title: it.title, w: it.box.w, h: it.box.h, critical: !!it.critical };
  });
  const tray = [...shed.map((id) => ({ id, why: 'no room' })), ...away.map((id) => ({ id, why: 'put away' }))]
    .map((t) => ({ ...t, title: known.get(t.id).title }));
  return {
    axis: CONTAINERS[name].axis,
    customizing: state.ui.customizing,
    slots: Object.fromEntries(SLOTS.map((s) => [s, slot(s)])),
    tray,
    trayShown: tray.length > 0 || state.ui.customizing,
    trayOpen: state.containers[name].tray && tray.length > 0,
  };
}

/* the banner in Customization Mode: the order a container's widgets are
   shed in */
function vmShedOrder(state, name) {
  const c = CONTAINERS[name];
  const known = new Map(ITEMS.map((it) => [it.id, it]));
  const named = state.layout[name].shed.filter((id) => c.members.includes(id) && !known.get(id).critical);
  const unnamed = c.members.filter((id) => !known.get(id).critical && !named.includes(id));
  const order = [...unnamed, ...named];
  return {
    on: state.ui.customizing,
    order: order.map((id, i) => ({ id, title: known.get(id).title, first: i === 0, last: i === order.length - 1 })),
  };
}

/* the banner: undo and redo, whether the layout is kept, and what is shed
   first */
function vmBanner(state, name) {
  return { ...vmShedOrder(state, name), ...vmJournal(state) };
}

/* the panel shown beside a container: the one `ui.panel` names, if that
   widget has one and is in one of its slots */
function vmPanelShown(state, name) {
  const id = state.ui.panel;
  const item = ITEMS.find((it) => it.id === id && it.panel);
  const placed = CONTAINERS[name].members.includes(id)
    && SLOTS.some((slot) => state.layout[name].slots[slot].includes(id));
  return item && placed ? id : null;
}

/* --------------------------------------------------------------- views */

/* the box a widget declared: its width, and its lines */
function boxStyle(box) {
  return { width: `${box.w}em`, height: `${box.h * BOX.line}em` };
}

/* the widget a drop landed on, if it landed on one */
function itemUnder(node) {
  for (let n = node; n; n = n.parentNode) {
    if (n.getAttribute && n.getAttribute('data-item')) return n.getAttribute('data-item');
  }
  return null;
}

function dropped(e) {
  e.preventDefault();
  return e.dataTransfer ? e.dataTransfer.getData('text/plain') : '';
}

function paintSlots(node, vm, name) {
  node.setAttribute('class', vm.customizing ? 'llmk-slots customizing' : 'llmk-slots');
  node.style.gap = `${BOX.between}em`;
  node.style.padding = vm.axis === 'row' ? `0.35em ${BOX.pad / 2}em` : `${BOX.pad / 2}em 0.35em`;
  const stops = [];
  const slot = (s, items) => el('div', {
    class: `slot slot-${s}`, 'data-el': `slot-${s}`, style: { gap: `${BOX.gap}em` },
    ondragover: vm.customizing ? (e) => e.preventDefault() : null,
    ondrop: vm.customizing ? (e) => {
      const id = dropped(e);
      const before = itemUnder(e.target);
      if (id && id !== before) editLayout('move', { container: name, id, slot: s, before });
    } : null,
  }, items.map((it) => {
    // the widget's view paints its own node - never the box, which holds the ✕
    const inner = el('div', { class: 'inner' });
    const box = el('div', {
      class: 'item', 'data-item': it.id, draggable: vm.customizing ? 'true' : null, style: boxStyle(it),
      ondragstart: vm.customizing ? (e) => e.dataTransfer.setData('text/plain', it.id) : null,
    }, inner);
    stops.push(ITEMS.find((x) => x.id === it.id).view(inner));
    if (vm.customizing && !it.critical) {
      box.appendChild(el('button', {
        class: 'away', 'data-el': `away-${it.id}`, title: `Put ${it.title} in the tray`,
        onclick: () => editLayout('putAway', { container: name, id: it.id }),
      }, '✕'));
    }
    return box;
  }));
  const tray = vm.trayShown ? el('div', { class: 'tray' },
    el('button', {
      'data-el': 'tray',
      title: vm.tray.length ? `In the tray: ${vm.tray.map((t) => `${t.title} (${t.why})`).join(', ')}`
        : 'The tray is empty - drop a widget here to put it away',
      style: { width: `${BOX.tray.row}em` },
      onclick: () => store.set((s) => update(s, ['containers', name, 'tray'], (v) => !v)),
      ondragover: vm.customizing ? (e) => e.preventDefault() : null,
      ondrop: vm.customizing ? (e) => {
        const id = dropped(e);
        if (id) editLayout('putAway', { container: name, id });
      } : null,
    }, `⋯ ${vm.tray.length}`),
    vm.trayOpen ? el('ul', { class: 'menu', 'data-el': 'tray-list' }, vm.tray.map((t) => el('li', {
      'data-tray': t.id, draggable: vm.customizing ? 'true' : null,
      ondragstart: vm.customizing ? (e) => e.dataTransfer.setData('text/plain', t.id) : null,
    }, t.title, ' ', el('span', { class: 'dim' }, `(${t.why})`),
    t.why === 'put away' ? el('button', {
      'data-el': `restore-${t.id}`, onclick: () => editLayout('restore', { container: name, id: t.id }),
    }, 'Put back') : null))) : null) : null;
  for (const stop of node.__stops || []) stop();
  node.__stops = stops;
  setKids(node, [...SLOTS.map((s) => slot(s, vm.slots[s])), tray]);
}

function paintBanner(node, vm, name) {
  setKids(node, vm.on ? [
    'Customization Mode - drag an item to another slot, or to the tray. Esc leaves. ',
    el('button', {
      'data-el': 'undo', disabled: vm.undo === null, title: vm.undo || 'Nothing to undo',
      onclick: () => journalUndo(),
    }, 'Undo'), ' ',
    el('button', {
      'data-el': 'redo', disabled: vm.redo === null, title: vm.redo || 'Nothing to redo',
      onclick: () => journalRedo(),
    }, 'Redo'), ' ',
    el('span', { class: 'dim', 'data-el': 'kept' }, vm.kept), ' ',
    el('span', { class: 'dim' }, 'When the room runs short, shed first: '),
    vm.order.map((o) => el('span', { class: 'shed', 'data-el': `shed-${o.id}` },
      o.first ? null : el('button', {
        'data-el': `shed-earlier-${o.id}`, title: `Shed ${o.title} sooner`,
        onclick: () => editLayout('setPriority', { container: name, id: o.id, by: -1 }),
      }, '◀'),
      ` ${o.title} `,
      o.last ? null : el('button', {
        'data-el': `shed-later-${o.id}`, title: `Keep ${o.title} longer`,
        onclick: () => editLayout('setPriority', { container: name, id: o.id, by: 1 }),
      }, '▶'))),
  ] : []);
}

/* ------------------------------------------------------------- the edges */

const SIDE = { top: 'paddingTop', bottom: 'paddingBottom', left: 'paddingLeft', right: 'paddingRight' };

/* Docked to an edge: the page's body is padded on that side by the
   container's size, on top of the body's own padding as the page styled it,
   so nothing of the page is under the container. And the dock says its size
   to the page, as `--llmk-dock-<edge>` on the body: a column docked left or
   right stands between the rows docked top and bottom by it (kit.css), so
   two docks that meet at a corner never cover each other - and a page's own
   fixed things can keep clear of the docks by it too. Undone, the body is
   as it was.

   ITS SIZE IS THE BOX AS MEASURED, ROUNDED UP to a whole pixel - never its
   offset, which the browser rounds to the nearest: a bar 33.17 px high has
   an offsetHeight of 33, and padded by that, 0.17 px of the page was under
   it (seen in a real browser, S14k). */
function dockAt(edge) {
  const side = SIDE[edge];
  const said = `--llmk-dock-${edge}`;
  const body = document.body;
  const was = body.style[side] || '';
  const own = typeof getComputedStyle === 'function' ? parseFloat(getComputedStyle(body)[side]) : NaN;
  const base = own > 0 ? own : 0;
  return {
    pad(root) {
      const box = root.getBoundingClientRect();
      const size = Math.ceil(edge === 'top' || edge === 'bottom' ? box.height : box.width);
      if (typeof size !== 'number' || !(size >= 0)) return;
      body.style[side] = `${base + size}px`;
      body.style.setProperty(said, `${size}px`);
    },
    undo() {
      body.style[side] = was;
      body.style.removeProperty(said);
    },
  };
}

/* the container's own observer: its length along its axis and an em, kept
   in the store - and, docked, the body's padding following its size.
   -> stop */
function observeContainer(root, strip, name, axis, dock) {
  const measure = () => {
    if (dock) dock.pad(root);
    const size = axis === 'row' ? strip.clientWidth : strip.clientHeight;
    const em = typeof getComputedStyle === 'function' ? parseFloat(getComputedStyle(strip).fontSize) : NaN;
    if (typeof size !== 'number' || !(size > 0) || !(em > 0)) return;
    // the same size again tells nobody anything: the store compares
    store.set((s) => update(s, ['containers', name], (c) => ({ ...c, size, em })));
  };
  const seen = typeof ResizeObserver === 'function' ? new ResizeObserver(measure) : null;
  if (seen) {
    seen.observe(strip);
    seen.observe(root);
  }
  window.addEventListener('resize', measure);
  // a hidden page runs no rendering steps, so neither of those is heard: a
  // page first measured while hidden (seen: a bar measured at 22 px in a
  // pane folded away, and never again) is measured again when it is shown
  document.addEventListener('visibilitychange', measure);
  measure();
  return () => {
    if (seen) seen.disconnect();
    window.removeEventListener('resize', measure);
    document.removeEventListener('visibilitychange', measure);
  };
}

/* ----------------------------------------------------- making one, checked */

/* the kit's widgets, by the names a page places them by */
function widgetNames() {
  const tools = TOOLS.filter((t) => !ITEMS.some((it) => it.id === t.id)).map((t) => t.id);
  return [...ITEMS.map((it) => it.id), ...tools, 'tools'].join(', ');
}

/* what a page asked for, checked whole before anything is placed: a
   container that cannot be what was asked is refused, saying why, and a
   setting it would not read is refused rather than ignored */
function containerOptions(where, options) {
  const o = options || {};
  const fault = (why) => new Error(`llmKit.container: ${why}`);
  for (const key of Object.keys(o)) {
    if (!['name', 'slots', 'shed', 'axis'].includes(key)) {
      throw fault(`it reads name, slots, shed and axis - not ${key}`);
    }
  }
  const edge = EDGES.includes(where) ? where : null;
  if (!edge && !(where && typeof where.appendChild === 'function')) {
    throw fault(`${JSON.stringify(where)} is neither an edge of the window (${EDGES.join(', ')}) `
      + 'nor an element of the page');
  }
  if (typeof o.name !== 'string' || !o.name) throw fault('a container is given a name: the folder keeps its layout under it');
  if (CONTAINERS[o.name]) throw fault(`the page has a container called ${o.name} already`);
  const edgeAxis = edge === 'top' || edge === 'bottom' ? 'row' : 'column';
  if (edge && Object.values(CONTAINERS).some((c) => c.edge === edge)) {
    throw fault(`another container is docked to the ${edge} already`);
  }
  if (edge && o.axis !== undefined) throw fault(`docked to the ${edge}, it is a ${edgeAxis}: its edge decides, not axis`);
  const axis = edge ? edgeAxis : (o.axis === undefined ? 'row' : o.axis);
  if (axis !== 'row' && axis !== 'column') throw fault(`axis is row or column, not ${JSON.stringify(o.axis)}`);
  const given = o.slots || {};
  for (const key of Object.keys(given)) {
    if (!SLOTS.includes(key)) throw fault(`its slots are start, middle and end - not ${key}`);
    if (!Array.isArray(given[key])) throw fault(`its ${key} slot is a list of widgets`);
  }
  const slots = Object.fromEntries(SLOTS.map((s) => [s, (given[s] || []).slice()]));
  const members = SLOTS.flatMap((s) => slots[s]);
  for (const id of members) {
    if (!ITEMS.some((it) => it.id === id)) {
      const tool = TOOLS.find((t) => t.id === id) || (id === 'tools' ? { title: 'The Tools door' } : null);
      if (tool) throw fault(`${tool.title} has no box: it goes in an element of the page (llmKit.place), not in a slot`);
      throw fault(`the kit has no widget called ${id} - it has ${widgetNames()}`);
    }
    if (members.indexOf(id) !== members.lastIndexOf(id)) throw fault(`${id} is in it twice`);
  }
  const shed = o.shed === undefined ? [] : o.shed;
  if (!Array.isArray(shed)) throw fault('its drop order (shed) is a list of its widgets');
  for (const id of shed) {
    if (!members.includes(id)) throw fault(`its drop order names ${id}, which it does not hold`);
  }
  return { edge, element: edge ? null : where, name: o.name, axis, slots, shed: shed.slice(), members };
}

/* a container, as checked by containerOptions: built, placed, watched. -> stop */
function makeContainer(o) {
  const root = el('div', {
    class: `llmk llmk-container axis-${o.axis}${o.edge ? ` dock dock-${o.edge}` : ''}`, 'data-container': o.name,
  });
  const strip = el('div', { class: 'llmk-slots', 'data-el': o.name });
  const banner = el('div', { class: 'banner', 'data-el': 'customizing' });
  const panel = el('section', { class: 'panel', 'data-el': 'panel' });
  setKids(root, [strip, banner, panel]);
  CONTAINERS[o.name] = { axis: o.axis, edge: o.edge, members: o.members };
  LAYOUT_SEEDS[o.name] = { slots: o.slots, shed: o.shed };
  store.set((s) => ({
    ...s, layout: journalLayout(s.journal), containers: { ...s.containers, [o.name]: { size: null, em: 16, tray: false } },
  }));
  const body = document.body;
  if (o.edge === 'top' || o.edge === 'left') body.insertBefore(root, body.firstChild);
  else if (o.edge) body.appendChild(root);
  else o.element.appendChild(root);
  const dock = o.edge ? dockAt(o.edge) : null;
  const stops = [
    store.watch((s) => vmContainer(s, o.name), (vm) => paintSlots(strip, vm, o.name), o.name),
    store.watch((s) => vmBanner(s, o.name), (vm) => paintBanner(banner, vm, o.name), `${o.name} banner`),
    region(panel, (s) => vmPanelShown(s, o.name), (node, id) => ITEMS.find((it) => it.id === id).panel(node),
      `${o.name} panel`),
    observeContainer(root, strip, o.name, o.axis, dock),
  ];
  return () => {
    stops.forEach((stop) => stop());
    for (const stop of strip.__stops || []) stop();
    strip.__stops = [];
    if (root.parentNode) root.parentNode.removeChild(root);
    if (dock) dock.undo();
    delete CONTAINERS[o.name];
  };
}
/* ---- journal.js ---- */
/* The journal: every change to a container's layout is an edit on its
   record - done, undone, done again - and the layouts are kept in the bound
   folder. One journal for the page, whatever the containers on it.

   AN EDIT is one of the four (container.js: move, put away, restore, set
   priority), naming its container, with an id the log names: `op-7: move
   Log to the start slot`. A layout is changed no other way.

   UNDO AND REDO MOVE A POINTER. The layouts are rebuilt from where the
   journal began - `base`, what the folder kept, over each container's own
   (LAYOUT_SEEDS) - by doing the edits before the pointer again, and
   replaced whole: every watcher sees the new layouts without being told. An
   edit after an undo drops what was undone. The oldest edits past
   JOURNAL_KEPT fold into the base.

   KEPT IN THE FOLDER: after every change the layouts are kept
   (`server.config.keep`), every container's by its name - `{v: 2, layout:
   {<name>: {slots, shed, known}}}`, `known` the widgets it has seen (read
   without it from a layout kept before it was said) - and a container of
   another page, kept in the folder, is carried as it was. One keep at a time, and the last asked for
   is the last kept. A folder bound, what it kept is read, and the journal
   begins there; nothing bound, nothing is kept, and Customization Mode says
   so. What a folder kept that this page cannot read is said, and the pages'
   own are used. Step 3's page kept one bar, left to right (`v: 1`): it is
   read as the bar's, its left the start and its right the end.

   The journal starts with the first container: a page with none keeps no
   layout, and asks for none. */

const JOURNAL_KEPT = 200;

SEEDS.journal = { base: null, ops: [], at: 0, n: 0, kept: null, fault: null };

/* the keeping's running machinery - not state anything shows */
const KEEPING = { busy: false, again: false, following: false };

/* where a journal begins: what the folder kept, over each container's own,
   every container having seen its widgets (container.js) */
function journalStart(base) {
  return layoutsSeeing({ ...LAYOUT_SEEDS, ...(base || {}) });
}

function journalLayout(j) {
  return j.ops.slice(0, j.at).reduce((layout, o) => LAYOUT_EDITS[o.op](layout, o.args), journalStart(j.base));
}

function editWords(op, args) {
  const title = (id) => (ITEMS.find((it) => it.id === id) || { title: id }).title;
  if (op === 'move') {
    return `move ${title(args.id)} to the ${args.slot} slot${args.before ? `, before ${title(args.before)}` : ''}`;
  }
  if (op === 'putAway') return `put ${title(args.id)} away`;
  if (op === 'restore') return `put ${title(args.id)} back`;
  return args.by < 0 ? `shed ${title(args.id)} sooner` : `keep ${title(args.id)} longer`;
}

/* a new journal: the layouts it begins from, the name they were kept under */
function journalBegin(base, kept, fault) {
  store.set((s) => {
    const journal = { base, ops: [], at: 0, n: s.journal.n, kept, fault };
    return { ...s, journal, layout: journalLayout(journal) };
  });
}

function journalDo(op, args) {
  const id = `op-${store.get().journal.n + 1}`;
  store.set((s) => {
    const j = s.journal;
    let ops = [...j.ops.slice(0, j.at), { id, op, args }];
    let base = j.base;
    while (ops.length > JOURNAL_KEPT) {
      base = LAYOUT_EDITS[ops[0].op](journalStart(base), ops[0].args);
      ops = ops.slice(1);
    }
    const journal = { ...j, base, ops, at: ops.length, n: j.n + 1 };
    return { ...s, journal, layout: journalLayout(journal) };
  });
  logLine('info', 'layout', `${id}: ${editWords(op, args)}`);
  keepLayout();
}

function journalMove(by) {
  const j = store.get().journal;
  const o = by < 0 ? j.ops[j.at - 1] : j.ops[j.at];
  if (!o) return;
  store.set((s) => {
    const journal = { ...s.journal, at: s.journal.at + by };
    return { ...s, journal, layout: journalLayout(journal) };
  });
  logLine('info', 'layout', `${by < 0 ? 'undo' : 'redo'} ${o.id}: ${editWords(o.op, o.args)}`);
  keepLayout();
}

function journalUndo() { journalMove(-1); }
function journalRedo() { journalMove(1); }

async function keepLayout() {
  if (!store.get().workspace.loaded) return;
  if (KEEPING.busy) {
    KEEPING.again = true;
    return;
  }
  KEEPING.busy = true;
  const layout = store.get().layout;
  let why = null;
  let sha = null;
  try {
    const got = await api('server.config.keep', { body: { config: { v: 2, layout } } });
    if (got.status === 200) sha = got.answer.sha256;
    else why = got.answer.error;
  } catch (e) {
    why = e.message;
  }
  KEEPING.busy = false;
  if (why) logLine('warn', 'layout', `the layout could not be kept: ${why}`);
  store.set((s) => update(s, ['journal'], (j) => ({ ...j, kept: why ? j.kept : sha, fault: why })));
  if (KEEPING.again) {
    KEEPING.again = false;
    keepLayout();
  }
}

/* one container's slots and drop order, as a folder kept them, under the
   slot names given */
function keptSlots(x, names) {
  const ids = (a) => Array.isArray(a) && a.every((v) => typeof v === 'string');
  return !!x && typeof x === 'object' && !!x.slots && typeof x.slots === 'object'
    && names.every((name) => ids(x.slots[name])) && ids(x.shed) && (x.known === undefined || ids(x.known));
}

/* what a folder kept, as layouts by container - or null: not one this page
   can read */
function keptLayouts(config) {
  const l = config.layout;
  if (config.v === 2 && !!l && typeof l === 'object' && !Array.isArray(l)
    && Object.values(l).every((c) => keptSlots(c, SLOTS))) return l;
  if (config.v === 1 && keptSlots(l, ['left', 'middle', 'right'])) {
    return { bar: { slots: { start: l.slots.left, middle: l.slots.middle, end: l.slots.right }, shed: l.shed } };
  }
  return null;
}

async function loadLayout() {
  let got;
  try {
    got = await api('server.config.get');
  } catch (e) {
    journalBegin(null, null, e.message);
    return;
  }
  if (got.status === 409) {
    journalBegin(null, null, null);
    return;
  }
  if (got.status !== 200) {
    logLine('warn', 'layout', `the kept layout could not be read: ${got.answer.error} - the page's own is used`);
    journalBegin(null, null, got.answer.error);
    return;
  }
  const config = got.answer.config;
  const layouts = config ? keptLayouts(config) : null;
  if (layouts) {
    journalBegin(layouts, got.answer.sha256, null);
    return;
  }
  if (config) {
    logLine('warn', 'layout', "the layout kept in the folder is not one this page can read - the page's own is used");
  }
  journalBegin(null, null, null);
}

/* ---------------------------------------------------------- view models */

function vmJournal(state) {
  const j = state.journal;
  const undo = j.ops[j.at - 1];
  const redo = j.ops[j.at];
  let kept = "the page's own layout";
  if (!state.workspace.loaded) kept = 'not kept - no folder is bound';
  else if (j.fault) kept = `not kept - ${j.fault}`;
  else if (j.kept) kept = 'kept in the folder';
  return {
    undo: undo ? `Undo: ${editWords(undo.op, undo.args)}` : null,
    redo: redo ? `Redo: ${editWords(redo.op, redo.args)}` : null,
    kept,
  };
}

/* the folder the server works in: a new one, a new journal - none (or not
   said yet), the page's own */
function boundFolder(state) {
  return state.workspace.loaded ? state.workspace.path : null;
}

/* the journal follows the folder from the first container on */
function journalFollow() {
  if (KEEPING.following) return;
  KEEPING.following = true;
  store.watch(boundFolder, (folder) => {
    if (folder === null) journalBegin(null, null, null);
    else loadLayout();
  }, 'the kept layout');
}
/* ---- kit.js ---- */
/* The kit's calls: how a page puts the widgets in it - an application's own
   page, and the bootstrap's own (page.js makes the same calls). Last of the
   parts: every part has registered what it brings.

   llmKit.place(name, element) -> stop()
       One widget, put at the end of `element`, an element of the page:
         a widget with a box - 'session' (the card the pipeline holds, and
         the models in it), 'queue' (the model's progress), 'log' (the
         log's light), 'workspace' (the folder picker), 'customize' - in the
         box it declares, its panel opening beside it;
         an instrument - 'bench', 'models', 'check' (the request check:
         the held requests, each scanned and tuned, then sent or
         cancelled) - filling the element;
         'tools' - the instruments behind one door (Bench, Models, Check,
         the Log), and the one shown.
       It follows its own feeds while it is placed and the browser shows the
       page (R-F3). stop() takes it out of the element, and stops everything
       it watched and asked.

   llmKit.container(where, {name, slots, shed, axis}) -> stop()
       Widgets side by side in slots, docked to an edge of the window or in
       an element; they leave for its tray as the room runs short, and come
       back as it grows; Customize moves them, every move undoable and kept
       in the bound folder (container.js, journal.js). A widget added to it
       after a folder kept its layout is where the page puts it: only one
       the person put away is in the tray.

   llmKit.requestView(name, {title, shows(held), view(node, held, give)}) -> remove()
       The request check's seam for an application's own data (PLAN-4
       D16): a view of its own part of a held request - a brief by
       section, a recording with a player - built in the Check widget
       below what every request has, for each held request `shows` says
       yes to. Read-only, or giving back a changed request with
       `give(request)`, which is then what Send sends (check.js).

   A call the kit cannot do as asked - a name it does not have, a place that
   is not an element - throws at once, saying why, before anything is
   placed. WHERE IT ASKS is said once, before the kit's script: the page's
   own address, or `window.llmServer` (api.js).

   The kit's look is put in the page, and what the kit must learn from the
   server as it starts (STARTS) is asked, when the first thing is placed:
   loaded and not used, the kit costs the page nothing. */

/* `ui.tab` names the tool the door shows (null: the first); `ui.tools` is
   whether the Tools menu is open; `ui.panel` the widget whose panel is open;
   `ui.customizing` whether Customization Mode is on - off at every load */
const SEED = {
  ui: { tab: null, customizing: false, panel: null, tools: false },
  ...SEEDS,
};

const store = createStore(SEED, (e, where) => console.error(`${where}:`, e));

/* whether the first thing has been placed - running machinery, not state */
const KIT = { started: false };

/* The first thing placed, once it is built: the kit's look put in the page,
   its keys heard, and what it must learn from the server asked - once. After
   the building, as the page before the kit did: a widget's own feeds are
   asked first, and what the server says as it starts has the last word. */
/* What is placed on the page (PLACED, DOORS) is running machinery, not
   state: a change to it is told to every view at once - a view that reads
   it (the queue's panel: where a held request is checked) is drawn again
   now, never at its feed's next poll (H8's register). */
function placedMoved() {
  store.set((s) => ({ ...s }));
}

function kitStart() {
  if (KIT.started) return;
  KIT.started = true;
  document.head.appendChild(el('style', { 'data-llmk': true }, KIT_CSS));
  document.addEventListener('keydown', onKey);
  for (const start of STARTS) start();
}

/* in Customization Mode, the layout's undo and redo - but never in a field
   someone types in: there the keys are the field's own. And Esc: one key
   leaves one thing - the mode first, then the Tools menu, then a panel */
function onKey(e) {
  const typing = e.target && ['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target.tagName);
  if (store.get().ui.customizing && (e.ctrlKey || e.metaKey) && !typing) {
    const k = String(e.key).toLowerCase();
    if (k === 'z' && !e.shiftKey) {
      e.preventDefault();
      journalUndo();
    } else if (k === 'y' || (k === 'z' && e.shiftKey)) {
      e.preventDefault();
      journalRedo();
    }
    return;
  }
  if (e.key !== 'Escape') return;
  const ui = store.get().ui;
  if (ui.customizing) store.set((s) => update(s, ['ui', 'customizing'], () => false));
  else if (ui.tools) store.set((s) => update(s, ['ui', 'tools'], () => false));
  else if (ui.panel) store.set((s) => update(s, ['ui', 'panel'], () => null));
}

/* a thing built into a wrapper of its own, at the end of `element` -> stop */
function placeInto(element, wrap, build) {
  element.appendChild(wrap);
  const stop = build();
  return () => {
    stop();
    if (wrap.parentNode) wrap.parentNode.removeChild(wrap);
  };
}

/* a widget with a box, alone: its box, and its panel beside it */
function placeItem(item, element) {
  const inner = el('div', { class: 'inner' });
  const box = el('div', { class: 'item', 'data-item': item.id, style: boxStyle(item.box) }, inner);
  const panel = item.panel ? el('section', { class: 'panel', 'data-el': 'panel' }) : null;
  const wrap = el('div', { class: 'llmk llmk-one', 'data-placed': item.id }, box, panel);
  return placeInto(element, wrap, () => {
    const stops = [item.view(inner)];
    if (panel) {
      stops.push(region(panel, (s) => (s.ui.panel === item.id ? item.id : null),
        (node) => item.panel(node), `${item.id} panel`));
    }
    return () => stops.forEach((stop) => stop());
  });
}

function place(name, element) {
  const item = ITEMS.find((it) => it.id === name);
  const tool = item ? null : TOOLS.find((t) => t.id === name);
  if (!item && !tool && name !== 'tools') {
    throw new Error(`llmKit.place: the kit has no widget called ${name} - it has ${widgetNames()}`);
  }
  if (!element || typeof element.appendChild !== 'function') {
    throw new Error(`llmKit.place('${name}', ...): the second is the element of the page to put it in`);
  }
  let stop;
  if (item) {
    stop = placeItem(item, element);
  } else {
    const wrap = el('div', { class: `llmk ${tool ? 'llmk-tool' : 'llmk-tools'}`, 'data-placed': name });
    stop = placeInto(element, wrap, () => {
      if (!tool) return toolsView(wrap);
      const halt = tool.view(wrap);
      PLACED[name] = (PLACED[name] || 0) + 1;
      placedMoved();
      return () => {
        PLACED[name] -= 1;
        halt();
        placedMoved();
      };
    });
  }
  kitStart();
  return stop;
}

function container(where, options) {
  const stop = makeContainer(containerOptions(where, options));
  kitStart();
  journalFollow();
  return stop;
}

window.llmKit = Object.freeze({ place, container, requestView });

/* the kit's door for its rows and a person's console - never for its own
   parts, which reach each other by name */
window.llmClient = {
  store, api, NoAnswer, ROUTES, ITEMS, TOOLS,
  logLine, update, vmGate, vmBench, firstWords, itemUnder, editLayout, journalUndo,
};
}());

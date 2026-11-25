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

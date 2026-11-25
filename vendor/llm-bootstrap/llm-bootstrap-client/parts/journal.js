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

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

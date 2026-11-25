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

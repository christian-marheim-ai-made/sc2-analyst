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

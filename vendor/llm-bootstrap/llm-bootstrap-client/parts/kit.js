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

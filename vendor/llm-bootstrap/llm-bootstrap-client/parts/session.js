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

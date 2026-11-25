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

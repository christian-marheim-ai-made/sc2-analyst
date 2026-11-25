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

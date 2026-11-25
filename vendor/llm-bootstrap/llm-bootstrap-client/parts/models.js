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

/* Bench: a raw request against a model - a tool, not a place.

   The controls: the model, the temperature, the most tokens, a seed, how
   many answers, and the prompt - sent as typed, byte for byte. Run is dark
   while it cannot run, and says why. A run is a queue job like any other:
   it shows in the scheduler graphic, can be cancelled, and leaves its
   request record in the folder - so Bench needs a folder bound.

   Each run shows its answers once its job has finished: the text, the
   reasoning folded away, why it finished, how many tokens, how fast, the
   load it ran on, and the round trip. The session's runs are kept, newest
   first, until the page is reloaded.

   A TRANSCRIPTION, for a whisper model (PLAN-4 D11): the controls are the
   audio - the path of a WAV file on the server's machine - and the fields
   whisper-server reads, a JSON object sent as typed, none of them
   mandatory; the request is the contract's `audio_request`. Its answer is
   shown whole: the text, the language asked and the one heard, the language
   table when the answer has one, every segment and every word with how sure
   the model was, the audio's length - and the answer as the server gave it.

   The models are the server's (`models`); a model whose file is missing is
   listed, and cannot be chosen. What the controls hold is kept as typed:
   the page never rewrites what a person is typing. */

const BENCH_ANSWERS = [1, 2, 3];

SEEDS.bench = {
  model: null, temperature: '0.8', max_tokens: '400', seed: '', n: 1, prompt: '',
  audio: '', fields: '', sending: false, said: null, runs: [],
};

/* the fields as typed, as whisper-server's - a JSON object, or empty for
   none: {fields}, or what is wrong with them */
function benchFields(text) {
  if (String(text).trim() === '') return { fields: {} };
  let fields;
  try {
    fields = JSON.parse(text);
  } catch (e) {
    fields = null;
  }
  if (!fields || typeof fields !== 'object' || Array.isArray(fields)) {
    return { wrong: 'the fields are a JSON object of what whisper-server reads, or empty for none' };
  }
  return { fields };
}

/* the file a path names, for a job's name */
function fileName(path) {
  return String(path).split(/[\\/]/).filter(Boolean).pop() || String(path);
}

/* -------------------------------------------------------------- reading */

/* what the controls hold, as the request's `gen` - or what is wrong with it */
function benchGen(b) {
  const number = (text) => (String(text).trim() === '' ? NaN : Number(text));
  const t = number(b.temperature);
  const most = number(b.max_tokens);
  const seed = String(b.seed).trim();
  if (!(t >= 0 && t <= 2)) return { wrong: 'the temperature is a number from 0 to 2' };
  if (!(Number.isInteger(most) && most >= 1 && most <= 4096)) {
    return { wrong: 'the most tokens is a whole number from 1 to 4096' };
  }
  if (seed !== '' && !/^-?[0-9]+$/.test(seed)) return { wrong: 'the seed is a whole number, or empty for none' };
  const gen = { temperature: t, max_tokens: most, n: b.n };
  if (seed !== '') gen.seed = Number(seed);
  return { gen };
}

/* the server's models, and which can be chosen: a stand-in, or a file there */
function benchModels(state) {
  const f = state.feeds.models;
  if (!f.ok || !f.answer) return [];
  return f.answer.models.map((m) => ({
    key: m.key, label: m.label, loaded: m.loaded, usable: !m.file || m.file.present,
    hears: m.engine === 'whisper',
  }));
}

function firstWords(text) {
  const words = String(text).trim().split(/\s+/).slice(0, 6).join(' ');
  return words.length > 40 ? `${words.slice(0, 39)}…` : words;
}

/* ---------------------------------------------------------- view models */

function vmBench(state) {
  const b = state.bench;
  const models = benchModels(state);
  const usable = models.filter((m) => m.usable);
  // the one chosen; else the one on the card; else the first that can be
  const chosen = usable.find((m) => m.key === b.model) || usable.find((m) => m.loaded) || usable[0] || null;
  const gate = vmGate(state);
  // a whisper model hears: its request is audio and fields, not a prompt
  const hears = !!chosen && chosen.hears;
  const read = hears ? benchFields(b.fields) : benchGen(b);
  let why = null;
  if (!gate.folder) why = `Bench runs are queue jobs, so it needs a folder: ${gate.why}`;
  else if (state.feeds.models.ok === null) why = 'asking the server for its models';
  else if (!chosen) why = models.length ? 'no model here has its file' : 'the server has no models';
  else if (hears && String(b.audio).trim() === '') why = "the audio is empty: the path of a WAV file on the server's machine";
  else if (!hears && b.prompt === '') why = 'the prompt is empty';
  else if (read.wrong) why = read.wrong;
  else if (b.sending) why = 'sending';
  return {
    models: models.map((m) => ({
      key: m.key, usable: m.usable, chosen: !!chosen && m.key === chosen.key,
      text: `${m.label}${m.label !== m.key ? ` (${m.key})` : ''}${m.hears ? ' · whisper' : ''}`
        + `${m.usable ? '' : ' - its file is missing'}`,
    })),
    model: chosen ? chosen.key : null,
    hears,
    why,
    said: b.said,
  };
}

function vmBenchControls(state) {
  const b = state.bench;
  return {
    temperature: b.temperature, max_tokens: b.max_tokens, seed: b.seed, n: b.n, prompt: b.prompt,
    audio: b.audio, fields: b.fields,
  };
}

function vmRun(state, r) {
  const j = state.feeds.jobs;
  const job = j.ok && j.answer ? j.answer.jobs.find((x) => x.id === r.id) : null;
  const status = r.result ? r.result.job.status : (job ? job.status : null);
  let doing = status || 'asking the server';
  if (status === 'running') doing = stageWords(job) || 'running';
  else if (status === 'queued') doing = `waiting, #${job.queue_pos}`;
  else if (!status && j.ok) doing = 'not among the jobs of the folder bound now';
  const g = r.gen;
  return {
    id: r.id,
    head: r.audio !== undefined
      ? [r.model, r.audio, Object.keys(r.fields).length ? `fields ${JSON.stringify(r.fields)}` : 'no fields'].join(' · ')
      : [r.model, `temperature ${g.temperature}`, `at most ${g.max_tokens} tokens`,
        g.seed !== undefined ? `seed ${g.seed}` : null, g.n > 1 ? `${g.n} answers` : null,
      ].filter(Boolean).join(' · '),
    doing,
    cancel: status === 'queued' || status === 'running',
    prompt: r.prompt === undefined ? null : r.prompt,
    error: r.result ? r.result.job.error : null,
    answers: r.result ? r.result.candidates.map((c) => ({
      text: c.text,
      reasoning: c.reasoning || null,
      meta: [c.finish_reason ? `finished: ${c.finish_reason}` : null,
        c.tokens !== null && c.tokens !== undefined ? `${c.tokens} tokens` : null,
        c.timings && c.timings.predicted_per_second ? `${Math.round(c.timings.predicted_per_second)} tok/s` : null,
        c.audio_s !== undefined ? `${c.audio_s} s of audio` : null,
        c.load ? `on ${c.load}` : null,
      ].filter(Boolean).join(' · '),
      heard: c.transcript ? vmTranscript(c.transcript) : null,
    })) : [],
    trip: r.result && r.result.job.finished !== null
      ? `round trip ${(r.result.job.finished - r.result.job.created).toFixed(1)} s` : null,
    said: r.said,
  };
}

/* a transcript, whole, as whisper-server answered it (the contract's
   `transcript`): the language asked and the one heard, the language table
   when the answer has one - the likeliest first - every segment and every
   word with how sure the model was, and the answer itself, for everything
   else it says */
function vmTranscript(t) {
  const two = (n) => Number(n).toFixed(2);
  const heard = t.detected_language === undefined ? null
    : `heard: ${t.detected_language}${t.detected_language_probability === undefined ? ''
      : `, ${two(t.detected_language_probability)}`}`;
  return {
    language: [`language: ${t.language}`, heard].filter(Boolean).join(' · '),
    table: t.language_probabilities === undefined ? null
      : Object.entries(t.language_probabilities).sort((x, y) => y[1] - x[1]).map(([code, p]) => `${code} ${two(p)}`),
    segments: t.segments.map((s) => ({
      when: `${two(s.start)}–${two(s.end)} s`,
      said: s.text,
      numbers: `temperature ${s.temperature} · avg logprob ${s.avg_logprob} · no speech ${s.no_speech_prob}`,
      words: s.words.map((w) => ({ text: `${w.word.trim()} ${two(w.probability)}`, when: `${two(w.start)}–${two(w.end)} s` })),
    })),
    whole: JSON.stringify(t, null, 2),
  };
}

function vmRuns(state) {
  return state.bench.runs.map((r) => vmRun(state, r));
}

/* the runs whose jobs have finished, and whose answers are not here yet */
function benchOwed(state) {
  const j = state.feeds.jobs;
  if (!j.ok || !j.answer) return [];
  const over = new Set(j.answer.jobs.filter((x) => x.status !== 'queued' && x.status !== 'running')
    .map((x) => x.id));
  return state.bench.runs.filter((r) => !r.result && !r.said && over.has(r.id)).map((r) => r.id);
}

/* --------------------------------------------------------------- actions */

function setBench(fn) {
  store.set((s) => update(s, ['bench'], fn));
}

function setRun(id, fn) {
  setBench((b) => ({ ...b, runs: b.runs.map((r) => (r.id === id ? fn(r) : r)) }));
}

async function runBench() {
  const state = store.get();
  const b = state.bench;
  const { model, hears } = vmBench(state);
  let body;
  let run;
  if (hears) {
    // the contract's audio_request: the file as typed, and the fields when
    // any are named - none is mandatory
    const { fields } = benchFields(b.fields);
    body = {
      kind: 'transcribe',
      input: Object.keys(fields).length ? { audio: b.audio, fields } : { audio: b.audio },
      gen: { model }, name: `bench: ${fileName(b.audio)}`, priority: 'interactive',
    };
    run = { model, audio: b.audio, fields, result: null, said: null };
  } else {
    const { gen } = benchGen(b);
    body = {
      kind: 'generate', input: { prompt: b.prompt }, gen: { model, ...gen },
      name: `bench: ${firstWords(b.prompt)}`, priority: 'interactive',
    };
    run = { model, gen, prompt: b.prompt, result: null, said: null };
  }
  setBench((x) => ({ ...x, sending: true, said: null }));
  let got;
  try {
    got = await api('server.jobs.submit', { body });
  } catch (e) {
    setBench((x) => ({ ...x, sending: false, said: e.message }));
    return;
  }
  if (got.status !== 200) {
    logLine('warn', 'bench', `the run was refused: ${got.answer.error}`);
    setBench((x) => ({ ...x, sending: false, said: got.answer.error }));
    return;
  }
  const queued = { id: got.answer.job_id, ...run };
  logLine('info', 'bench', `${queued.id} queued, on ${model}`, { corr: got.answer.correlation_id });
  setBench((x) => ({ ...x, sending: false, runs: [queued, ...x.runs] }));
  refresh('jobs');
}

/* a finished run's answers, asked for once - never twice while one ask is out */
const BENCH_FETCHING = new Set();

async function fetchRun(id) {
  if (BENCH_FETCHING.has(id)) return;
  BENCH_FETCHING.add(id);
  let got;
  try {
    got = await api('server.jobs.result', { params: { id } });
  } catch (e) {
    BENCH_FETCHING.delete(id);
    setRun(id, (r) => ({ ...r, said: e.message }));
    return;
  }
  BENCH_FETCHING.delete(id);
  if (got.status !== 200) {
    setRun(id, (r) => ({ ...r, said: got.answer.error }));
    return;
  }
  const job = got.answer.job;
  // a failure warns; a run done, or cancelled by the person, is said
  logLine(job.status === 'failed' ? 'warn' : 'info', 'bench',
    `${id} ${job.status}${job.error ? `: ${job.error}` : ''}`, { corr: job.correlation_id });
  setRun(id, (r) => ({ ...r, result: { job, candidates: got.answer.candidates } }));
}

async function cancelRun(id) {
  setRun(id, (r) => ({ ...r, said: null }));
  const said = await cancelJob(id);
  if (said) setRun(id, (r) => ({ ...r, said }));
  refresh('jobs');
}

/* --------------------------------------------------------------- views */

function runCard(r) {
  return el('li', { class: 'run', 'data-run': r.id },
    el('div', { class: 'head' },
      el('span', { class: 'dim' }, r.id), ' ', el('span', { 'data-el': `run-head-${r.id}` }, r.head), ' ',
      el('span', { class: 'doing', 'data-el': `run-doing-${r.id}` }, r.doing),
      r.cancel ? el('button', { 'data-el': `run-cancel-${r.id}`, onclick: () => cancelRun(r.id) }, 'Cancel') : null),
    r.prompt === null ? null
      : el('details', { class: 'prompt' }, el('summary', {}, 'the prompt'), el('pre', {}, r.prompt)),
    r.said ? el('div', { class: 'said', 'data-el': `run-said-${r.id}` }, r.said) : null,
    r.error ? el('div', { class: 'bad', 'data-el': `run-error-${r.id}` }, r.error) : null,
    r.answers.map((a, i) => el('div', { class: 'answer', 'data-el': `run-answer-${r.id}-${i}` },
      a.reasoning ? el('details', { class: 'reasoning' }, el('summary', {}, 'reasoning'),
        el('pre', { 'data-el': `run-reasoning-${r.id}-${i}` }, a.reasoning)) : null,
      el('pre', { class: 'text', 'data-el': `run-text-${r.id}-${i}` }, a.text),
      el('div', { class: 'dim', 'data-el': `run-meta-${r.id}-${i}` }, a.meta),
      a.heard ? heardCard(a.heard, `${r.id}-${i}`) : null)),
    r.trip ? el('div', { class: 'dim', 'data-el': `run-trip-${r.id}` }, r.trip) : null);
}

/* a transcript, whole: the languages, the table, the segments and their
   words - how sure the model was of each, when it was said on hover - and
   the answer as the server gave it */
function heardCard(t, at) {
  return el('div', { class: 'heard' },
    el('div', { 'data-el': `run-language-${at}` }, t.language),
    t.table ? el('details', { class: 'languages', 'data-el': `run-languages-${at}` },
      el('summary', {}, `the language table: ${t.table.slice(0, 3).join(', ')}${t.table.length > 3 ? ', …' : ''}`),
      el('ol', {}, t.table.map((line) => el('li', {}, line)))) : null,
    t.segments.length ? el('ol', { class: 'segments', 'data-el': `run-segments-${at}` },
      t.segments.map((s, k) => el('li', {},
        el('span', { class: 'when' }, s.when), ' ', el('span', { class: 'spoken' }, s.said),
        el('div', { class: 'dim', 'data-el': `run-segment-${at}-${k}` }, s.numbers),
        el('div', { class: 'words', 'data-el': `run-words-${at}-${k}` },
          s.words.map((w) => el('span', { class: 'word', title: w.when }, w.text))))))
      : el('div', { class: 'dim', 'data-el': `run-segments-${at}` }, 'No words were heard.'),
    el('details', { class: 'whole', 'data-el': `run-transcript-${at}` },
      el('summary', {}, 'the answer as the server gave it'), el('pre', {}, t.whole)));
}

TOOLS.push({
  id: 'bench', title: 'Bench', rank: 10,
  view(node) {
    const stops = [follow('models'), follow('jobs')];
    const field = (name, key, attrs) => el('input', {
      'data-el': `bench-${name}`, ...attrs,
      oninput: (e) => setBench((b) => ({ ...b, [key]: e.target.value })),
    });
    const model = el('select', {
      'data-el': 'bench-model', onchange: (e) => setBench((b) => ({ ...b, model: e.target.value })),
    });
    const temperature = field('temperature', 'temperature', { inputmode: 'decimal', size: 4 });
    const most = field('max-tokens', 'max_tokens', { inputmode: 'numeric', size: 5 });
    const seed = field('seed', 'seed', { inputmode: 'numeric', size: 8, placeholder: 'none' });
    const answers = el('select', {
      'data-el': 'bench-n', onchange: (e) => setBench((b) => ({ ...b, n: Number(e.target.value) })),
    }, BENCH_ANSWERS.map((n) => el('option', { value: String(n) }, String(n))));
    const prompt = el('textarea', {
      'data-el': 'bench-prompt', rows: 8, spellcheck: 'false',
      oninput: (e) => setBench((b) => ({ ...b, prompt: e.target.value })),
    });
    // a transcription's: the audio, and whisper-server's fields
    const audio = field('audio', 'audio', { size: 48, spellcheck: 'false', placeholder: 'D:/takes/take-1.wav' });
    const fields = el('textarea', {
      'data-el': 'bench-fields', rows: 3, spellcheck: 'false', placeholder: '{"language": "en"} - or empty for none',
      oninput: (e) => setBench((b) => ({ ...b, fields: e.target.value })),
    });
    const run = el('button', { 'data-el': 'bench-run', onclick: () => runBench() }, 'Run');
    const why = el('span', { 'data-el': 'bench-why' });
    const runs = el('ol', { class: 'runs', 'data-el': 'bench-runs' });
    const label = (text, control) => el('label', {}, el('span', { class: 'dim' }, text), control);
    // what a text model is asked with, and what a whisper model hears: built
    // once, and the one the chosen model does not take put away
    const text = el('span', { class: 'group', 'data-el': 'bench-text' },
      label('temperature', temperature), label('most tokens', most), label('seed', seed), label('answers', answers));
    const hears = el('div', { class: 'hears', 'data-el': 'bench-hears' },
      label('audio', audio), label('fields', fields));
    setKids(node, el('div', { class: 'bench' },
      el('div', { class: 'controls' }, label('model', model), text),
      prompt, hears, el('div', { class: 'go' }, run, ' ', why), runs));
    // what the controls hold follows the store - set only when it differs,
    // so a person's typing is never rewritten under the caret
    const hold = (input, value) => { if (input.value !== value) input.value = value; };
    stops.push(store.watch(vmBenchControls, (vm) => {
      hold(temperature, vm.temperature);
      hold(most, vm.max_tokens);
      hold(seed, vm.seed);
      hold(answers, String(vm.n));
      hold(prompt, vm.prompt);
      hold(audio, vm.audio);
      hold(fields, vm.fields);
    }, 'bench controls'));
    const shown = (part, on) => (on ? part.removeAttribute('hidden') : part.setAttribute('hidden', ''));
    stops.push(store.watch(vmBench, (vm) => {
      setKids(model, vm.models.map((m) => el('option', {
        value: m.key, disabled: !m.usable, selected: m.chosen,
      }, m.text)));
      if (vm.model !== null) model.value = vm.model;
      shown(text, !vm.hears);
      shown(prompt, !vm.hears);
      shown(hears, vm.hears);
      run.disabled = vm.why !== null;
      run.setAttribute('title', vm.why || 'Queue this request, as typed');
      setKids(why, vm.said ? el('span', { class: 'said' }, vm.said) : vm.why || []);
    }, 'bench'));
    stops.push(store.watch(vmRuns, (vms) => setKids(runs, vms.map(runCard)), 'bench runs'));
    stops.push(store.watch(benchOwed, (ids) => ids.forEach(fetchRun), 'bench results'));
    return () => stops.forEach((stop) => stop());
  },
});

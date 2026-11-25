/* The ids every part of llm-bootstrap mints and reads. No dependencies.

   The same rules as ids.py, which describes them, producing the same answers
   character for character - test_contract.py runs both over one table.

   In a browser:  llmIds.mintActor('cli')
   In node:       const ids = require('./ids.js')
   For the agreement test:
                  node ids.js --batch <cases.json>                            */
'use strict';

const ALPHABET = 'abcdefghijkmnpqrstuvwxyz23456789';
const ROLES = ['srv', 'cli', 'crd', 'load'];
const L = '[a-km-np-z2-9]';
const PATTERNS = {
  actor: `(?:srv|cli|crd|load)-${L}{8}`,
  correlation: `c-[0-9]{8}-[0-9]{6}-${L}{8}`,
  job: 'j-[0-9]{4,}',
  handle: `(srv-${L}{8}):(j-[0-9]{4,})#([0-9]{2,})`,
  ticket: `t-([0-9]{4,})-${L}{4}`,
  pipeline: `pipe-${L}{8}`,
};
const KINDS = Object.keys(PATTERNS);
const SHOWN = {
  actor: 'an actor id',
  correlation: 'a correlation id',
  job: 'a job id',
  handle: 'a handle',
  ticket: 'a ticket id',
  pipeline: 'a pipeline id',
};
const RE = Object.fromEntries(KINDS.map(k => [k, new RegExp(`^(?:${PATTERNS[k]})$`)]));

function defaultDraw() {
  const c = globalThis.crypto;
  if (c && c.getRandomValues) {
    // rejection sampling is unnecessary: 32 divides 256 evenly
    return c.getRandomValues(new Uint8Array(1))[0] % ALPHABET.length;
  }
  return Math.floor(Math.random() * ALPHABET.length);
}

function letters(n, draw) {
  draw = draw || defaultDraw;
  let out = '';
  for (let i = 0; i < n; i++) out += ALPHABET[draw() % ALPHABET.length];
  return out;
}

function matches(kind, value) {
  if (!Object.prototype.hasOwnProperty.call(RE, kind)) throw new Error(`no id kind '${kind}'`);
  return typeof value === 'string' && RE[kind].test(value);
}

function kindOf(value) {
  return KINDS.find(k => matches(k, value)) || null;
}

function mintActor(role, draw) {
  if (!ROLES.includes(role)) throw new Error(`no actor role '${role}': one of ${ROLES.join(', ')}`);
  return `${role}-${letters(8, draw)}`;
}

const pad = (n, w) => String(n).padStart(w, '0');

function mintCorrelation(now, draw) {
  const d = new Date(Math.floor((now === undefined || now === null ? Date.now() / 1000 : now)) * 1000);
  return `c-${d.getFullYear()}${pad(d.getMonth() + 1, 2)}${pad(d.getDate(), 2)}-`
       + `${pad(d.getHours(), 2)}${pad(d.getMinutes(), 2)}${pad(d.getSeconds(), 2)}-`
       + letters(8, draw);
}

function mintTicket(seq, draw) {
  return `t-${pad(Math.trunc(seq), 4)}-${letters(4, draw)}`;
}

function mintPipeline(draw) {
  return `pipe-${letters(8, draw)}`;
}

function handle(server, job, sample) {
  const h = `${server}:${job}#${pad(Math.trunc(sample), 2)}`;
  if (!matches('handle', h))
    throw new Error(`not a handle: '${h}' - the server must be a srv- actor id and the job a job id`);
  return h;
}

function parseHandle(value) {
  const m = typeof value === 'string' ? RE.handle.exec(value) : null;
  return m ? { server: m[1], job: m[2], sample: Number(m[3]) } : null;
}

function ticketSeq(value) {
  const m = typeof value === 'string' ? RE.ticket.exec(value) : null;
  return m ? Number(m[1]) : null;
}

const api = { ALPHABET, ROLES, KINDS, SHOWN, matches, kindOf, mintActor, mintCorrelation,
              mintTicket, mintPipeline, handle, parseHandle, ticketSeq };
if (typeof module !== 'undefined') module.exports = api;
else globalThis.llmIds = api;

if (typeof require !== 'undefined' && typeof module !== 'undefined'
    && require.main === module) {
  const i = process.argv.indexOf('--batch');
  const doc = JSON.parse(require('fs').readFileSync(process.argv[i + 1], 'utf8'));
  // each case: {op, args, draws?} - draws script the random letters, so a
  // mint can be compared across the two languages
  const out = doc.cases.map(c => {
    const queue = (c.draws || []).slice();
    const draw = () => queue.shift();
    const fn = { matches, kindOf, parseHandle, ticketSeq, handle,
                 mintActor: (r) => mintActor(r, draw),
                 mintCorrelation: (n) => mintCorrelation(n, draw),
                 mintTicket: (s) => mintTicket(s, draw),
                 mintPipeline: () => mintPipeline(draw) }[c.op];
    try { return { ok: fn(...c.args) }; } catch (e) { return { error: true }; }
  });
  process.stdout.write(JSON.stringify(out));
}

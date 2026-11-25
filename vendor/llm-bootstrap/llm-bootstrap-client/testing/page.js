/* The page, run in node: its scripts - built from the parts as the rows
   start, byte for byte what `build.py` writes - in a sandbox with the test
   DOM, a clock the row moves, and a `fetch` the row chooses (by default the
   contract's own examples). The rows run the parts as they are NOW: whether
   the committed files are stale is the gate's check, not theirs.

     const page = loadPage({fetch})     the bootstrap's own page, index.html
     const page = loadKit({fetch})      the kit alone, as an application's page
                                        includes it - nothing placed yet
     const page = loadHtml(file, {fetch})   a page of its own (an application's):
                                        its markup put in the document, its
                                        scripts run in order - a script whose
                                        src is the kit runs the kit built now
     page.client          the kit's door for rows and a console (window.llmClient)
     page.kit             the kit's calls (window.llmKit)
     page.named(name)     the element whose data-el is `name`
     page.text(name)      its text
     await page.settle()  let every promise and every change be told
     page.clock.advance(ms)  move time; due timers run

   `storage` is the browser's local storage (a fresh one by default - pass one
   to two pages to be a reload); `clipboard` records what the page copies
   (null: a browser that gives the page none). Neither ever reaches the
   machine's: a row must not touch what is the person's. `server`, when given,
   is the address the page is told to ask, as an application's page tells it
   before the kit's script runs (`window.llmServer`); not given, the page is
   told nothing, as on the bootstrap server's own page. `search` is the page's
   own address's query (`location.search`), for a page that reads it. */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { execFileSync } = require('child_process');
const { Document, makeEvent, readHtml } = require('./dom.js');
const { contractFetch } = require('./contract.js');

const CLIENT = path.resolve(__dirname, '..');
const KIT_FILE = path.join(CLIENT, 'llm-bootstrap-kit.js');

const built = {};

function build(flag) {
  if (built[flag] === undefined) {
    built[flag] = execFileSync(process.env.PYTHON || 'python', ['build.py', flag],
      { cwd: CLIENT, maxBuffer: 64 * 1024 * 1024 }).toString('utf8');
  }
  return built[flag];
}

/* index.html, as the parts build it now */
function thePage() { return build('--print'); }

/* llm-bootstrap-kit.js, as the parts build it now */
function theKit() { return build('--print-kit'); }

/* the scripts of the bootstrap's own page, in order */
function pageScripts() {
  return [...thePage().matchAll(/<script>\n([\s\S]*?)<\/script>/g)].map((m) => m[1]);
}

function makeClock() {
  let now = 1_800_000_000_000;
  let seq = 0;
  const timers = new Map();
  const clock = {
    now: () => now,
    setTimeout(fn, ms) { const id = ++seq; timers.set(id, { at: now + (ms || 0), fn, every: null }); return id; },
    setInterval(fn, ms) { const id = ++seq; timers.set(id, { at: now + ms, fn, every: ms }); return id; },
    clearTimeout(id) { timers.delete(id); },
    clearInterval(id) { timers.delete(id); },
    pending: () => timers.size,
    advance(ms) {
      const end = now + ms;
      for (;;) {
        let next = null;
        for (const [id, t] of timers) if (t.at <= end && (!next || t.at < next[1].at)) next = [id, t];
        if (!next) break;
        const [id, t] = next;
        now = t.at;
        if (t.every) t.at += t.every; else timers.delete(id);
        t.fn();
      }
      now = end;
    },
  };
  return clock;
}

function fakeStorage() {
  const m = new Map();
  return {
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => { m.set(k, String(v)); },
    removeItem: (k) => { m.delete(k); },
  };
}

function fakeClipboard() {
  const copied = [];
  return { copied, writeText: async (t) => { copied.push(t); } };
}

/* A sandbox with the test DOM: the window a page's scripts run in. */
function sandboxed({ fetch, clock, storage, clipboard, server, search } = {}) {
  const document = new Document();
  const time = clock || makeClock();
  const errors = [];
  const logged = [];
  // the window's own events (resize): kept, as a browser keeps them - a
  // container's size comes from the store, set by a row as the observer would
  const windowEvents = {};
  const RealDate = Date;
  class FakeDate extends RealDate {
    constructor(...a) { super(...(a.length ? a : [time.now()])); }
    static now() { return time.now(); }
  }
  const sandbox = {
    document,
    fetch: fetch || contractFetch(),
    console: {
      log: (...a) => logged.push(a.join(' ')),
      info: (...a) => logged.push(a.join(' ')),
      warn: (...a) => logged.push(a.join(' ')),
      error: (...a) => errors.push(a.map((x) => (x && x.stack) || String(x)).join(' ')),
    },
    setTimeout: time.setTimeout, clearTimeout: time.clearTimeout,
    setInterval: time.setInterval, clearInterval: time.clearInterval,
    queueMicrotask, Date: FakeDate, performance: { now: () => time.now() },
    URLSearchParams, Promise, JSON, Math, Object, Array, Map, Set, Error, TypeError,
    localStorage: storage || fakeStorage(),
    navigator: clipboard === null ? {} : { clipboard: clipboard || fakeClipboard() },
    location: { search: search || '' },
    addEventListener: (type, fn) => { (windowEvents[type] = windowEvents[type] || []).push(fn); },
    removeEventListener: (type, fn) => {
      windowEvents[type] = (windowEvents[type] || []).filter((f) => f !== fn);
    },
  };
  sandbox.window = sandbox;
  if (server !== undefined) sandbox.llmServer = server;
  const context = vm.createContext(sandbox);
  const named = (name) => document.querySelector(`[data-el="${name}"]`);
  const page = {
    window: sandbox,
    document,
    clock: time,
    errors,
    logged,
    named,
    text: (name) => { const n = named(name); return n ? n.textContent : null; },
    /* run one script of the page, as the browser runs it */
    run(text, filename) { vm.runInContext(text, context, { filename }); },
    /* what an expression is, in the page's global scope - a name any script
       of the page could see */
    evaluate(expression) { return vm.runInContext(expression, context); },
    /* an event of the window's own, as the browser sends it (resize) */
    fire(type) { (windowEvents[type] || []).forEach((fn) => fn({ type })); },
    click(name) {
      const n = named(name);
      if (!n) throw new Error(`nothing on the page is named ${name}`);
      n.click();
    },
    key(key, mods) { document.dispatchEvent(makeEvent('keydown', { key, ...mods })); },
    async settle() {
      for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r));
    },
    /* the names inside one container of the page: a name is unique within
       its container, and a page may have several */
    within(container) {
      const root = document.querySelector(`[data-container="${container}"]`);
      const find = (name) => (root ? root.querySelector(`[data-el="${name}"]`) : null);
      return {
        root,
        named: find,
        text: (name) => { const n = find(name); return n ? n.textContent : null; },
        click(name) {
          const n = find(name);
          if (!n) throw new Error(`nothing in ${container} is named ${name}`);
          n.click();
        },
      };
    },
  };
  // the kit's door and calls, once its script has run
  Object.defineProperty(page, 'client', { get: () => sandbox.llmClient });
  Object.defineProperty(page, 'kit', { get: () => sandbox.llmKit });
  return page;
}

/* the bootstrap's own page: index.html's body, and its scripts */
function loadPage(opts = {}) {
  const page = sandboxed(opts);
  const app = page.document.createElement('div');
  app.setAttribute('id', 'app');
  page.document.body.appendChild(app);
  pageScripts().forEach((s, i) => page.run(s, `index.html#script${i + 1}`));
  return page;
}

/* the kit alone, as an application's page includes it: nothing placed */
function loadKit(opts = {}) {
  const page = sandboxed(opts);
  page.run(theKit(), 'llm-bootstrap-kit.js');
  return page;
}

/* a page of its own - an application's - read from its file: its markup put
   in the document, its scripts run in order. A script whose src is the kit
   runs the kit as the parts build it now; any other src, its file. */
function loadHtml(file, opts = {}) {
  const page = sandboxed(opts);
  const scripts = readHtml(page.document, fs.readFileSync(file, 'utf8'));
  scripts.forEach((s, i) => {
    if (s.src === null) {
      page.run(s.text, `${path.basename(file)}#script${i + 1}`);
      return;
    }
    const at = path.resolve(path.dirname(file), s.src);
    page.run(at === KIT_FILE ? theKit() : fs.readFileSync(at, 'utf8'), s.src);
  });
  return page;
}

module.exports = {
  loadPage, loadKit, loadHtml, makeClock, fakeStorage, fakeClipboard, thePage, theKit, pageScripts,
  CLIENT,
};

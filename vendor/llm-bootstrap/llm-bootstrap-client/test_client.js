/* The client's rows - run on the built page itself, in node, with no package.

     node test_client.js      (test_client.py runs it, after checking the build)

   Every row runs the page's own bytes (testing/page.js) against the
   contract's examples as its server (testing/contract.js). A row prints PASS
   or FAIL with its name; the last line is the tally the gate reads. */
'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync } = require('child_process');
const { loadPage, loadKit, loadHtml, thePage, theKit, pageScripts, CLIENT } = require('./testing/page.js');
const { contractFetch, serverRoutes } = require('./testing/contract.js');

const results = [];
const rows = [];

function row(name, fn) { rows.push({ name, fn }); }

function check(name, ok, detail) {
  results.push({ name, ok: !!ok, detail: ok ? '' : String(detail === undefined ? '' : detail) });
}

// ================================================================= the page

row('the page', async () => {
  const page = loadPage();
  await page.settle();
  check('the page builds its bar, its strip with the Tools door, and its pane',
    page.named('bar') && page.named('strip') && page.named('tools') && page.named('pane'),
    page.document.body.textContent);
  check('...and the bar holds the Customize button, in its own box',
    page.named('customize') && page.client.itemUnder(page.named('customize')) === 'customize');
  check('nothing on the page failed to start', page.errors.length === 0, page.errors.join('\n'));
  check('the page names every server route of the contract, by id',
    JSON.stringify(Object.keys(page.client.ROUTES).sort())
      === JSON.stringify(serverRoutes().map((r) => r.id).sort()),
    Object.keys(page.client.ROUTES).join(', '));
});

row('the page asks for no icon', async () => {
  // a browser asks the server for /favicon.ico unless the page names an icon;
  // it is no route there, and every load of the page logged a 404
  const head = /<head>([\s\S]*?)<\/head>/.exec(thePage());
  check("the page's head names an icon that is no request: the browser asks the server for none",
    head !== null && /<link rel="icon" href="data:,">/.test(head[1]), head && head[1]);
});

/* The build, run on a copy of the client's parts and the contract it embeds,
   in a folder of the row's own: `edit` is made to the copy's parts first;
   then, when `after` is given, the copy is built (`build.py`, writing its
   two files) and `after` changes the copy's client folder; then `build.py`
   runs with `args`. -> {code, said, out} of that last run */
function buildCopy(edit, { args = ['--print'], after = null } = {}) {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'llmb-client-build-'));
  const run = (cwd, argv) => {
    try {
      const out = execFileSync(process.env.PYTHON || 'python', ['build.py', ...argv],
        { cwd, stdio: 'pipe', maxBuffer: 64 * 1024 * 1024 });
      return { code: 0, said: '', out: out.toString('utf8') };
    } catch (e) {
      return { code: e.status, said: `${e.stderr || ''}${e.stdout || ''}` };
    }
  };
  try {
    const client = path.join(tmp, 'llm-bootstrap-client');
    const contract = path.join(tmp, 'llm-bootstrap-contract');
    fs.mkdirSync(client);
    fs.copyFileSync(path.join(CLIENT, 'build.py'), path.join(client, 'build.py'));
    fs.cpSync(path.join(CLIENT, 'parts'), path.join(client, 'parts'), { recursive: true });
    const theirs = path.join(CLIENT, '..', 'llm-bootstrap-contract');
    fs.mkdirSync(contract);
    for (const f of ['ids.js', 'validate.js']) fs.copyFileSync(path.join(theirs, f), path.join(contract, f));
    for (const d of ['routes', 'shapes']) fs.cpSync(path.join(theirs, d), path.join(contract, d), { recursive: true });
    edit(path.join(client, 'parts'));
    if (after) {
      const built = run(client, []);
      if (built.code !== 0) return built;
      after(client);
    }
    return run(client, args);
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
}

row("a part that would end the page's script is refused by the build", async () => {
  // every script part is inside one inline <script>: the characters that end
  // it, anywhere in a part - a comment included - cut the page there, and
  // every row failed far from the cause
  const control = buildCopy(() => {});
  check('the parts as they are build, on a copy', control.code === 0 && control.out === thePage(), control.said);
  const cut = buildCopy((parts) => fs.appendFileSync(path.join(parts, 'dom.js'),
    "\n// a page names its server: <script>window.llmServer = 'http://127.0.0.1:7072';</script>\n"));
  check('a script part holding </script> is refused, naming the part and why',
    cut.code !== 0 && cut.said.includes('parts/dom.js') && /ends? the page's script/.test(cut.said), cut.said);
  const loud = buildCopy((parts) => fs.appendFileSync(path.join(parts, 'queue.js'), '\n/* </SCRIPT > */\n'));
  check('...written in any case, as a browser reads it', loud.code !== 0 && loud.said.includes('parts/queue.js'),
    loud.said);
  const own = buildCopy((parts) => fs.appendFileSync(path.join(parts, 'page.js'), '\n// </script>\n'));
  check("...in the page's own script, after the kit's", own.code !== 0 && own.said.includes('parts/page.js'), own.said);
  const look = buildCopy((parts) => fs.appendFileSync(path.join(parts, 'kit.css'), '\n/* </script> */\n'));
  check("...and in the kit's look, which the kit carries inside its script", look.code !== 0
    && look.said.includes('parts/kit.css'), look.said);
});

row('the build: the two files the MANIFEST names, and nothing it cannot build', async () => {
  const manifest = (fn) => (parts) => {
    const f = path.join(parts, 'MANIFEST');
    fs.writeFileSync(f, fn(fs.readFileSync(f, 'utf8')));
  };
  for (const [what, edit, said] of [
    ['html in the kit: the kit is a script', manifest((m) => m.replace('\nkit.css\n', '\nkit.css\npage-body.html\n')),
      /page-body\.html is html, and the kit is a script/],
    ['the kit inside itself', manifest((m) => m.replace('\nkit.css\n', '\nkit.css\n@kit\n')), /cannot hold itself/],
    ['a file it does not build', manifest((m) => m.replace('[index.html]', '[other.html]')),
      /\[other\.html\] is not a file this builds/],
    ['a part under no file', manifest((m) => `dom.js\n${m}`), /dom\.js is under no \[file\]/],
    ['a MANIFEST naming no page', manifest((m) => m.slice(0, m.indexOf('[index.html]'))), /names no \[index\.html\]/],
  ]) {
    const got = buildCopy(edit);
    check(`the build refuses ${what}`, got.code !== 0 && said.test(got.said), got.said);
  }
  const fresh = buildCopy(() => {}, { args: ['--check'], after: () => {} });
  check('--check, just built: both files are what the parts build', fresh.code === 0
    && /llm-bootstrap-kit\.js and index\.html are what the parts build/.test(fresh.out), fresh.said || fresh.out);
  const stale = buildCopy(() => {}, {
    args: ['--check'], after: (client) => fs.appendFileSync(path.join(client, 'llm-bootstrap-kit.js'), '\n'),
  });
  check('--check: a kit that is not what the parts build is named, and fails the check', stale.code === 1
    && /llm-bootstrap-kit\.js is not what the parts build/.test(stale.said) && !/index\.html is not/.test(stale.said),
  stale.said);
});

row('Customization Mode', async () => {
  const page = loadPage();
  page.click('customize');
  await page.settle();
  check('Customize turns the mode on, and the button says how to leave it',
    page.client.store.get().ui.customizing === true && page.text('customize') === 'Done',
    page.text('customize'));
  check('...the banner says what can be done, and that Esc leaves',
    /Esc leaves/.test(page.text('customizing')), page.text('customizing'));
  check('...and the bar shows it is being customized',
    page.named('bar').classList.contains('customizing'));
  page.key('Escape');
  await page.settle();
  check('Esc leaves it', page.client.store.get().ui.customizing === false
    && page.text('customizing') === '', page.text('customizing'));
  page.click('customize');
  await page.settle();
  const again = loadPage();
  await again.settle();
  check('a page loaded again starts with the mode off - F5 is always a way out',
    again.client.store.get().ui.customizing === false);
});

// ================================================================ the store

row('the store', async () => {
  const page = loadPage();
  const store = page.client.store;
  const seen = [];
  store.watch((s) => ({ tab: s.ui.tab }), (vm) => seen.push(vm.tab), 'a row');
  check('a watcher hears the state at once', seen.length === 1 && seen[0] === null, seen);
  store.set((s) => ({ ...s, other: 1 }));
  await page.settle();
  check('...and hears nothing when what it reads did not change', seen.length === 1, seen);
  store.set((s) => ({ ...s, ui: { ...s.ui, tab: 'a' } }));
  store.set((s) => ({ ...s, ui: { ...s.ui, tab: 'b' } }));
  store.set((s) => ({ ...s, ui: { ...s.ui, tab: 'c' } }));
  check('changes are told at the end of the turn, not as they are made', seen.length === 1, seen);
  await page.settle();
  check('...once, with the last of them', JSON.stringify(seen) === '[null,"c"]', seen);
  store.replace({ ...store.get(), ui: { ...store.get().ui, tab: 'whole' } });
  await page.settle();
  check('a state replaced whole is seen by every watcher, untold', seen[seen.length - 1] === 'whole', seen);
  let threw = null;
  try {
    store.get().ui.tab = 'changed in place';
  } catch (e) {
    threw = e;
  }
  check('the state cannot be changed in place - only replaced', threw instanceof page.window.TypeError
    || (threw && threw.name === 'TypeError'), threw);
  const told = [];
  store.watch((s) => s.ui.tab, () => { if (told.length) throw new Error('a view that breaks'); told.push(1); }, 'breaks');
  store.watch((s) => s.ui.tab, (t) => told.push(t), 'after it');
  store.set((s) => ({ ...s, ui: { ...s.ui, tab: 'x' } }));
  await page.settle();
  check('a view that throws is reported, and the views after it are still told',
    told[told.length - 1] === 'x' && page.errors.some((e) => /a view that breaks/.test(e)),
    `${JSON.stringify(told)} ${page.errors}`);
});

// ============================================================ the one door

row('the one door', async () => {
  let page = loadPage();
  let got = await page.client.api('server.health');
  check('an answer the contract declares is handed back', got.status === 200 && got.answer.queue,
    JSON.stringify(got));
  page = loadPage({ fetch: contractFetch({ 'server.health': () => ({ status: 418, body: { error: 'x', reason: 'teapot' } }) }) });
  let err = null;
  try { await page.client.api('server.health'); } catch (e) { err = e; }
  check('a status the contract does not declare is no answer', err instanceof page.client.NoAnswer
    && /answered 418/.test(err.message), err && err.message);
  page = loadPage({ fetch: contractFetch({ 'server.health': () => ({ status: 200, body: { queue: {} } }) }) });
  err = null;
  try { await page.client.api('server.health'); } catch (e) { err = e; }
  check('an answer missing what its shape requires is no answer', err instanceof page.client.NoAnswer
    && /does not declare/.test(err.message), err && err.message);
  const fetch = contractFetch();
  page = loadPage({ fetch });
  got = await page.client.api('server.jobs.claim', { params: { id: 'j-0007' }, body: { client: 'cli-k3x9m2p7' } });
  // the page asks things of its own meanwhile: a row finds its requests by route
  const mine = (id) => fetch.asked.filter((a) => a.id === id).map((a) => a.path);
  check('the path is built from the route and its parameters',
    JSON.stringify(mine('server.jobs.claim')) === '["/api/jobs/j-0007/claim"]', JSON.stringify(mine('server.jobs.claim')));
  await page.client.api('server.jobs.list', { query: { scope: 'book one', client: null } });
  check('...and a query from what is given, and nothing that is not',
    mine('server.jobs.list').includes('/api/jobs?scope=book%20one')
      && !mine('server.jobs.list').some((path) => /client=/.test(path)), JSON.stringify(mine('server.jobs.list')));
  const before = fetch.asked.length;
  err = null;
  try { await page.client.api('server.jobs.submit', { body: { kind: 7 } }); } catch (e) { err = e; }
  check('a body the route refuses throws before anything is sent',
    err && /not a body the contract allows/.test(err.message) && fetch.asked.length === before,
    err && err.message);
  err = null;
  try {
    await loadPage({ fetch: async () => { throw new Error('connection refused'); } }).client.api('server.health');
  } catch (e) { err = e; }
  check('a server that does not answer is no answer, said so',
    err && err.name !== 'TypeError' && /did not answer server.health: connection refused/.test(err.message),
    err && err.message);
});

// ====================================================== the server's address

row('the page asks its own address, unless it is told another', async () => {
  let fetch = contractFetch();
  await started({ fetch });
  check("told nothing, every request goes to the page's own address: a path alone",
    fetch.asked.length > 0 && fetch.asked.every((a) => a.server === '' && a.path.startsWith('/api/')),
    JSON.stringify(fetch.asked.map((a) => a.server + a.path)));
  fetch = contractFetch({ 'server.workspace.get': () => ({ status: 200, body: { loaded: false, workspace: null } }) });
  const page = await started({ fetch, server: 'http://127.0.0.1:7072' });
  await tick(page, 1000);
  page.click('workspace');
  await page.settle();
  page.named('workspace-path').value = 'D:/work/book';
  page.click('workspace-bind');
  await page.settle();
  const where = [...new Set(fetch.asked.map((a) => a.server))];
  const which = [...new Set(fetch.asked.map((a) => a.id))];
  check('told its server, every request goes there: as it starts, from every feed, and for what a person does',
    JSON.stringify(where) === '["http://127.0.0.1:7072"]'
      && ['server.workspace.get', 'server.health', 'server.jobs.list', 'server.models.list', 'server.workspace.bind']
        .every((id) => which.includes(id)),
    `${JSON.stringify(where)} ${which.join(', ')}`);
  fetch = contractFetch();
  await started({ fetch, server: 'http://127.0.0.1:7072/' });
  check('...written with a slash at its end, the slash is not doubled',
    fetch.asked.length > 0 && fetch.asked.every((a) => a.server === 'http://127.0.0.1:7072'
      && a.path.startsWith('/api/') && a.id !== null),
    JSON.stringify(fetch.asked.map((a) => a.server + a.path)));
  const silent = loadPage({ fetch: async () => { throw new Error('connection refused'); }, server: 'http://127.0.0.1:7072' });
  let err = null;
  try { await silent.client.api('server.health'); } catch (e) { err = e; }
  check('...and a server that does not answer is said by the address asked',
    err instanceof silent.client.NoAnswer
      && err.message === 'the server at http://127.0.0.1:7072 did not answer server.health: connection refused',
    err && err.message);
});

row('an address that is not one is never guessed around', async () => {
  for (const wrong of ['localhost:7072', 'http://127.0.0.1:7072/bootstrap', 'http://me@127.0.0.1:7072',
    ['http://127.0.0.1:7072']]) {
    const fetch = contractFetch();
    const page = await started({ fetch, server: wrong });
    await tick(page, 1000);
    let err = null;
    try { await page.client.api('server.health'); } catch (e) { err = e; }
    check(`told ${JSON.stringify(wrong)}: nothing is asked of any address - not the page's own either`,
      fetch.asked.length === 0, JSON.stringify(fetch.asked.map((a) => a.server + a.path)));
    check('...every ask is no answer, saying why',
      err instanceof page.client.NoAnswer && err.message.includes(`${JSON.stringify(wrong)}, which is not`),
      err && err.message);
    check('...said in the log, and a server that could not be asked is never shown as one with nothing bound',
      logged(page).some((t) => /^error api /.test(t) && t.includes(JSON.stringify(wrong)))
        && page.text('workspace') === '📁 ?', `${page.text('workspace')} ${logged(page).join(' | ')}`);
  }
});

// ============================================================== the regions

row('a tool not shown is stopped', async () => {
  const page = loadPage();
  const { TOOLS, store } = page.client;
  const alive = { one: 0, two: 0 };
  const tool = (id, rank) => ({
    id, title: id, rank,
    view(node) {
      alive[id] += 1;
      const stop = store.watch((s) => s.ui.tab, () => {}, id);
      return () => { alive[id] -= 1; stop(); };
    },
  });
  TOOLS.push(tool('one', 1), tool('two', 2));
  store.set((s) => ({ ...s, ui: { ...s.ui, tab: 'one' } }));
  await page.settle();
  check('the tool shown is built', alive.one === 1 && alive.two === 0, JSON.stringify(alive));
  await openTool(page, 'two');
  check('...and the one left is stopped when another is shown',
    alive.one === 0 && alive.two === 1 && store.get().ui.tab === 'two', JSON.stringify(alive));
});

// ================================================================== the log

function logged(page) {
  return page.client.store.get().log.lines.map((l) => `${l.level} ${l.area} ${l.text}`);
}

function shownLines(page) {
  const list = page.named('log-list');
  return list ? list.children.map((li) => li.textContent) : [];
}

row('the log', async () => {
  const page = loadPage();
  await page.settle();
  const { store } = page.client;
  store.set((s) => ({ ...s, ui: { ...s.ui, tab: 'log' } }));
  page.client.logLine('warn', 'queue', 'the first');
  page.client.logLine('error', 'bench', 'the second', { detail: 'what the second was about' });
  await page.settle();
  let shown = shownLines(page);
  check('a line is kept with its level, area and words, and shown newest first',
    shown.length >= 2 && /error bench the second/.test(shown[0]) && /warn queue the first/.test(shown[1]),
    shown.join(' | '));

  page.client.logLine('error', 'bench', 'the same failure', { corr: 'c-20260925-120000-aaaaaaaa' });
  page.client.logLine('error', 'bench', 'the same failure', { corr: 'c-20260925-120000-aaaaaaaa' });
  page.client.logLine('error', 'bench', 'the same failure', { corr: 'c-20260925-120000-bbbbbbbb' });
  await page.settle();
  const same = store.get().log.lines.filter((l) => l.text === 'the same failure');
  check('a line said again for the same request is that line counted again',
    same.length === 2 && same[0].count === 2, JSON.stringify(same.map((l) => [l.corr, l.count])));
  check('...and the same words for another request are a line of their own',
    same[1].corr === 'c-20260925-120000-bbbbbbbb' && same[1].count === 1);
  check('...the count shown beside it', shownLines(page).some((t) => /the same failure ×2/.test(t)),
    shownLines(page).join(' | '));

  check('every error lights the log, with the count - a warning does not',
    page.text('log-light') === '● 4' && page.named('log-light').classList.contains('bad'),
    page.text('log-light'));

  check('info is hidden until asked for', !shownLines(page).some((t) => /info/.test(t)));
  page.client.logLine('info', 'page', 'a quiet thing');
  await page.settle();
  check('...still hidden when said', !shownLines(page).some((t) => /a quiet thing/.test(t)));
  const info = page.named('log-level-info');
  info.checked = true;
  info.dispatchEvent({ type: 'change', target: info });
  await page.settle();
  check('...and shown once info is ticked', shownLines(page).some((t) => /a quiet thing/.test(t)));
  const area = page.named('log-area');
  area.value = 'queue';
  area.dispatchEvent({ type: 'change', target: area });
  await page.settle();
  check('an area narrows the list to its lines', shownLines(page).length === 1
    && /the first/.test(shownLines(page)[0]), shownLines(page).join(' | '));
  area.value = 'all';
  area.dispatchEvent({ type: 'change', target: area });
  const words = page.named('log-text');
  words.value = 'SECOND';
  words.dispatchEvent({ type: 'input', target: words });
  await page.settle();
  check('words narrow it to the lines that hold them, whatever their case',
    shownLines(page).length === 1 && /the second/.test(shownLines(page)[0]), shownLines(page).join(' | '));
  words.value = '';
  words.dispatchEvent({ type: 'input', target: words });
  await page.settle();

  const second = page.named('log-list').children.find((li) => /the second/.test(li.textContent));
  second.click();
  await page.settle();
  check("a line's detail opens when it is clicked",
    shownLines(page).some((t) => /what the second was about/.test(t)));
  page.named('log-list').children.find((li) => /the second/.test(li.textContent)).click();
  await page.settle();
  check('...and closes on the next', !shownLines(page).some((t) => /what the second was about/.test(t)));

  page.click('log-pause');
  page.client.logLine('warn', 'page', 'said while paused');
  await page.settle();
  check('paused, a new line is kept but not shown, and the status says how many wait',
    logged(page).some((t) => /said while paused/.test(t))
      && !shownLines(page).some((t) => /said while paused/.test(t))
      && /paused - 1 new/.test(page.text('log-status')) && page.text('log-pause') === 'Resume',
    page.text('log-status'));
  page.click('log-pause');
  await page.settle();
  check('...and shown once the log is resumed', shownLines(page).some((t) => /said while paused/.test(t)));

  page.click('log-clear');
  await page.settle();
  check('Clear empties the log and puts its light out',
    store.get().log.lines.length === 0 && page.text('log-light') === '●'
      && !page.named('log-light').classList.contains('bad'), page.text('log-light'));

  for (let i = 0; i < 510; i++) page.client.logLine('warn', 'page', `line ${i}`);
  await page.settle();
  check('at most 500 lines are kept - the oldest go, and are counted',
    store.get().log.lines.length === 500 && store.get().log.dropped === 10
      && store.get().log.lines[0].text === 'line 10', store.get().log.dropped);
  check('...and the status says so, and shows at most 200',
    /10 dropped/.test(page.text('log-status')) && shownLines(page).length === 200, page.text('log-status'));
});

row('the log is copied only when asked', async () => {
  const clipboard = { copied: [], writeText: async (t) => { clipboard.copied.push(t); } };
  const page = loadPage({ clipboard });
  await page.settle();
  page.client.store.set((s) => ({ ...s, ui: { ...s.ui, tab: 'log' } }));
  page.client.logLine('warn', 'page', 'to be copied');
  await page.settle();
  check('nothing is copied until the button is pressed', clipboard.copied.length === 0);
  page.click('log-copy');
  await page.settle();
  const copied = clipboard.copied.length === 1 ? JSON.parse(clipboard.copied[0]) : [];
  check('Copy JSON copies every line kept, as JSON',
    copied.some((l) => l.text === 'to be copied' && l.level === 'warn'), clipboard.copied);
  const bare = loadPage({ clipboard: null });
  await bare.settle();
  bare.client.store.set((s) => ({ ...s, ui: { ...s.ui, tab: 'log' } }));
  await bare.settle();
  bare.click('log-copy');
  await bare.settle();
  check('a browser with no clipboard for the page is said, not a failure',
    logged(bare).some((t) => /no clipboard/.test(t)), logged(bare));
});

row('the door says no answer in the log', async () => {
  // a route no feed of the page asks: the feeds say their own silences
  const page = loadPage({ fetch: contractFetch({ 'server.jobs.result': () => ({ status: 418, body: {} }) }) });
  await page.settle();
  try { await page.client.api('server.jobs.result', { params: { id: 'j-0007' } }); } catch (e) { /* the row reads the log */ }
  await page.settle();
  const lines = logged(page).filter((t) => /server.jobs.result answered 418/.test(t));
  check('an answer the contract does not declare is an error in the log, once',
    lines.length === 1 && /^error api /.test(lines[0]), logged(page));
});

// ======================================================== the workspace picker

async function started(opts) {
  const page = loadPage(opts);
  await page.settle();
  return page;
}

/* a tool opened as a person opens it: through the Tools door */
async function openTool(page, id) {
  if (!page.client.store.get().ui.tools) {
    page.click('tools');
    await page.settle();
  }
  page.click(`tool-${id}`);
  await page.settle();
}

row('the workspace, as the server says it', async () => {
  const fetch = contractFetch();
  let page = await started({ fetch });
  check('the page asks the server where it works as it starts',
    fetch.asked.some((a) => a.id === 'server.workspace.get'), JSON.stringify(fetch.asked));
  check('...and the bar names the folder by its last two names, the whole path on hover',
    page.text('workspace') === '📁 work/book' && page.named('workspace').getAttribute('title') === 'D:/work/book',
    page.text('workspace'));
  page = await started({ fetch: contractFetch({ 'server.workspace.get': () => ({ status: 200, body: { loaded: false, workspace: null } }) }) });
  check('with nothing bound the bar says so', page.text('workspace') === 'no folder - choose one',
    page.text('workspace'));
  check("...and the gate says why what needs a folder is dark",
    /no folder is bound/.test(page.client.vmGate(page.client.store.get()).why));
  page = await started({ fetch: async () => { throw new Error('connection refused'); } });
  check('a server that does not answer is never shown as one with nothing bound',
    page.text('workspace') === '📁 ?' && page.client.store.get().workspace.loaded === null
      && /has not said/.test(page.client.vmGate(page.client.store.get()).why), page.text('workspace'));
});

row('binding a folder', async () => {
  const storage = { m: new Map(), getItem(k) { return this.m.has(k) ? this.m.get(k) : null; }, setItem(k, v) { this.m.set(k, String(v)); }, removeItem(k) { this.m.delete(k); } };
  const fetch = contractFetch({ 'server.workspace.get': () => ({ status: 200, body: { loaded: false, workspace: null } }) });
  const page = await started({ fetch, storage });
  page.click('workspace');
  await page.settle();
  check('the picker opens its panel', page.named('workspace-path') !== null);
  check('...where Unload is dark with nothing bound', page.named('workspace-unload').disabled);
  const path = page.named('workspace-path');
  path.value = 'D:/work/book';
  page.click('workspace-bind');
  await page.settle();
  const asked = fetch.asked.filter((a) => a.id === 'server.workspace.bind');
  check('Bind asks the server to work in the typed folder',
    asked.length === 1 && asked[0].body.path === 'D:/work/book', JSON.stringify(asked));
  check('...and the bar names it', page.text('workspace') === '📁 work/book', page.text('workspace'));
  check('...the folder is remembered as bound before', page.text('workspace-recent').includes('D:/work/book'));
  check('...and said in the log', logged(page).some((t) => /^info workspace the server works in D:\/work\/book/.test(t)),
    logged(page));
  path.value = 'Z:/nowhere';
  path.dispatchEvent({ type: 'keydown', key: 'Enter', target: path });
  await page.settle();
  check('Enter binds too - and a refusal shows the server\'s own sentence',
    page.text('workspace-said') === 'cannot use that folder: it does not exist', page.text('workspace-said'));
  check('...nothing it had changes', page.text('workspace') === '📁 work/book'
    && page.client.store.get().workspace.path === 'D:/work/book');
  check('...and the refusal is a warning in the log, not an error',
    logged(page).some((t) => /^warn workspace .*does not exist/.test(t)) && page.text('log-light') === '●',
    logged(page));
  path.value = 'D:/work/other';
  page.click('workspace-bind');
  await page.settle();
  check('a folder another job is still using is refused in its words',
    /a job is unfinished/.test(page.text('workspace-said')), page.text('workspace-said'));
  const again = await started({ fetch: contractFetch(), storage });
  check('a page loaded again in this browser lists the folders bound before',
    JSON.stringify(again.client.store.get().workspace.recent) === '["D:/work/book"]',
    JSON.stringify(again.client.store.get().workspace.recent));
  again.click('workspace');
  await again.settle();
  again.click('recent-0');
  await again.settle();
  check('...and binds one on a click', again.client.store.get().workspace.path === 'D:/work/book');
});

row('letting a folder go', async () => {
  let fetch = contractFetch();
  let page = await started({ fetch });
  page.click('workspace');
  await page.settle();
  page.click('workspace-unload');
  await page.settle();
  check('Unload lets the folder go, and the bar says there is none',
    page.text('workspace') === 'no folder - choose one' && page.client.store.get().workspace.loaded === false,
    page.text('workspace'));
  check('...and asks with no body - the route takes none',
    fetch.asked.some((a) => a.id === 'server.workspace.unload' && a.body === undefined));
  fetch = contractFetch({ 'server.workspace.unload': () => ({ status: 409, body: { error: 'a job is unfinished: part 2', reason: 'busy', job: 'j-0008' } }) });
  page = await started({ fetch });
  page.click('workspace');
  await page.settle();
  page.click('workspace-unload');
  await page.settle();
  check('an unload refused while a job runs shows why, and keeps the folder',
    page.text('workspace-said') === 'a job is unfinished: part 2' && page.text('workspace') === '📁 work/book',
    page.text('workspace-said'));
});

row('Esc', async () => {
  const page = await started();
  page.click('workspace');
  await page.settle();
  page.click('customize');
  await page.settle();
  page.key('Escape');
  await page.settle();
  check('Esc leaves Customization Mode first, and leaves the panel open',
    page.client.store.get().ui.customizing === false && page.named('workspace-path') !== null);
  page.key('Escape');
  await page.settle();
  check('...then closes the panel', page.named('workspace-path') === null
    && page.client.store.get().ui.panel === null);
});

row('a panel follows its item', async () => {
  const page = await started();
  page.click('workspace');
  await page.settle();
  check('the panel is open', page.named('workspace-path') !== null);
  page.client.store.set((s) => page.client.update(s, ['layout', 'bar', 'slots', 'start'], () => []));
  await page.settle();
  check('an item taken out of the bar takes its open panel with it', page.named('workspace-path') === null);
});

row('a route that takes no body', async () => {
  const fetch = contractFetch();
  const page = loadPage({ fetch });
  const before = fetch.asked.length;
  let err = null;
  try { await page.client.api('server.workspace.unload', { body: {} }); } catch (e) { err = e; }
  check('a body for a route that takes none throws before anything is sent',
    err && /takes no body/.test(err.message) && fetch.asked.length === before, err && err.message);
});

// ============================================================ the feeds

function example(routeId, name) {
  const route = serverRoutes().find((r) => r.id === routeId);
  const ex = route.examples.find((e) => e.name === name);
  return JSON.parse(JSON.stringify(ex.response));
}

/* health as the idle example says it, with `over` laid on it, field by field */
function health(over) {
  const h = example('server.health', 'bound, idle, no coordinator');
  const lay = (to, from) => {
    for (const [k, v] of Object.entries(from || {})) {
      if (v && typeof v === 'object' && !Array.isArray(v) && to[k] && typeof to[k] === 'object') lay(to[k], v);
      else to[k] = v;
    }
    return to;
  };
  return lay(h, over);
}

function aJob(over) {
  const jobs = example('server.jobs.list', 'one done, one waiting on it, one running').jobs;
  return { ...jobs.find((j) => j.status === 'running'), ...over };
}

function asked(fetch, id) {
  return fetch.asked.filter((a) => a.id === id).length;
}

async function tick(page, ms) {
  page.clock.advance(ms);
  await page.settle();
}

row('health is asked only while the graphic is shown', async () => {
  const fetch = contractFetch();
  const page = await started({ fetch });
  await openTool(page, 'log');                    // a tool that follows no feed
  check('the graphic in the bar asks for health as the page starts',
    asked(fetch, 'server.health') === 1, asked(fetch, 'server.health'));
  await tick(page, 1000);
  await tick(page, 1000);
  await tick(page, 1000);
  check('...and every second after', asked(fetch, 'server.health') === 4, asked(fetch, 'server.health'));
  page.client.store.set((s) => page.client.update(s, ['layout', 'bar', 'slots', 'middle'], () => []));
  await page.settle();
  check('taken out of the bar, its clock stops at once', page.clock.pending() === 0, page.clock.pending());
  const out = asked(fetch, 'server.health');
  await tick(page, 5000);
  check('...and nothing more is asked for it', asked(fetch, 'server.health') === out,
    `${out} then ${asked(fetch, 'server.health')}`);
});

row('a hidden page asks nothing', async () => {
  const fetch = contractFetch();
  const page = await started({ fetch });
  page.document.visibilityState = 'hidden';
  const before = asked(fetch, 'server.health');
  await tick(page, 1000);
  await tick(page, 5000);
  // the clock set before it was hidden still runs out - and asks nothing
  check('while the browser hides the page, health is not asked', asked(fetch, 'server.health') === before,
    `${before} then ${asked(fetch, 'server.health')}`);
  const hidden = asked(fetch, 'server.health');
  page.document.visibilityState = 'visible';
  page.document.dispatchEvent({ type: 'visibilitychange' });
  await page.settle();
  check('...shown again, it is asked at once', asked(fetch, 'server.health') === hidden + 1,
    `${hidden} then ${asked(fetch, 'server.health')}`);
  await tick(page, 1000);
  check('...and every second again', asked(fetch, 'server.health') === hidden + 2);
});

row('hidden while an answer is awaited', async () => {
  const held = [];
  const holding = (make) => () => new Promise((resolve) => { held.push(() => resolve({ status: 200, body: make() })); });
  const fetch = contractFetch({
    'server.health': holding(() => health()),
    'server.jobs.list': holding(() => example('server.jobs.list', 'one done, one waiting on it, one running')),
  });
  const page = await started({ fetch });
  check('both feeds are asked, and their answers awaited', held.length === 2, held.length);
  await openTool(page, 'log');                    // a tool that follows no feed
  page.document.visibilityState = 'hidden';
  held.forEach((answer) => answer());
  await page.settle();
  check('the answers come while the page is hidden: no clock is left running for a next ask',
    page.clock.pending() === 0, page.clock.pending());
  page.document.visibilityState = 'visible';
  page.document.dispatchEvent({ type: 'visibilitychange' });
  await page.settle();
  check('...shown again, both are asked at once', held.length === 4, held.length);
});

row('one ask at a time', async () => {
  let pending = 0;
  const fetch = contractFetch({ 'server.health': () => { pending += 1; return new Promise(() => {}); } });
  const page = await started({ fetch });
  await tick(page, 1000);
  await tick(page, 5000);
  check('a feed whose answer has not come asks nothing more', pending === 1, pending);
  const slots = page.client.store.get().layout.bar.slots;
  page.client.store.set((s) => page.client.update(s, ['layout', 'bar', 'slots', 'middle'], () => []));
  await page.settle();
  page.client.store.set((s) => page.client.update(s, ['layout', 'bar', 'slots'], () => slots));
  await page.settle();
  check('...not even when a view stops following it and one starts again while it waits',
    pending === 1, pending);
});

row('a bind under way is not undone by health', async () => {
  let bound = null;
  const fetch = contractFetch({
    'server.health': () => ({ status: 200, body: health({ loaded: false, workspace: null }) }),
    'server.workspace.get': () => ({ status: 200, body: { loaded: false, workspace: null } }),
    'server.workspace.bind': () => new Promise((resolve) => { bound = resolve; }),
  });
  const page = await started({ fetch });
  page.click('workspace');
  await page.settle();
  page.named('workspace-path').value = 'D:/work/book';
  page.click('workspace-bind');
  await page.settle();
  page.client.store.set((s) => page.client.update(s, ['workspace', 'path'], () => 'D:/while/binding'));
  await tick(page, 1000);
  check("health does not overwrite the folder while this page's own bind is under way",
    page.client.store.get().workspace.path === 'D:/while/binding', page.client.store.get().workspace.path);
  bound({ status: 200, body: { loaded: true, workspace: 'D:/work/book' } });
  await page.settle();
  check('...and the bind says the folder when it answers', page.text('workspace') === '📁 work/book',
    page.text('workspace'));
});

row('a feed that stops answering', async () => {
  let up = true;
  const fetch = contractFetch({
    'server.health': () => (up ? { status: 200, body: health() } : Promise.reject(new Error('connection refused'))),
  });
  const page = await started({ fetch });
  up = false;
  await tick(page, 1000);
  await tick(page, 1000);
  await tick(page, 1000);
  const errors = logged(page).filter((t) => /server.health stopped answering/.test(t));
  check('says so in the log once, however many times it is asked',
    errors.length === 1 && page.client.store.get().log.lines.find((l) => /stopped answering/.test(l.text)).count === 1,
    logged(page));
  check('...and nothing else in the log says it: a feed asks the door quietly',
    logged(page).filter((t) => /^error/.test(t) && /server.health/.test(t)).length === 1
      && page.client.store.get().log.errors === 1, logged(page));
  check('...and the graphic says there is no answer', page.text('queue-words') === 'no answer'
    && page.named('queue-dot').classList.contains('unknown'), page.text('queue-words'));
  up = true;
  await tick(page, 1000);
  await tick(page, 1000);
  check('once it answers again, says that once too',
    logged(page).filter((t) => /server.health answers again/.test(t)).length === 1, logged(page));
});

row('the picker follows health', async () => {
  let where = { loaded: true, workspace: 'D:/work/book' };
  const fetch = contractFetch({ 'server.health': () => ({ status: 200, body: health(where) }) });
  const page = await started({ fetch });
  check('the folder health names is the one shown', page.text('workspace') === '📁 work/book');
  where = { loaded: false, workspace: null };
  await tick(page, 1000);
  check('a folder let go by anybody else shows here too', page.text('workspace') === 'no folder - choose one',
    page.text('workspace'));
});

// ====================================================== the scheduler graphic

async function graphic(h, jobs) {
  const fetch = contractFetch({
    'server.health': () => ({ status: 200, body: health(h) }),
    'server.jobs.list': () => ({ status: 200, body: { jobs: jobs || [] } }),
  });
  const page = await started({ fetch });
  await tick(page, 1000);
  return page;
}

row('the graphic says what the card is doing', async () => {
  let page = await graphic({ runner: { loaded: null, loading: null }, pipeline: { up: [] } });
  check('idle: a green dot, and the word', page.text('queue-words') === 'idle'
    && page.named('queue-dot').classList.contains('idle'), page.text('queue-words'));
  page = await graphic({});
  check('idle with a model up: which one', page.text('queue-words') === 'idle · story-12b up',
    page.text('queue-words'));
  page = await graphic({ runner: { loaded: null, loading: 'story-12b' }, queue: { running: 1 } },
    [aJob({ progress: {} })]);
  check('loading: which model', page.text('queue-words') === 'loading story-12b'
    && page.named('queue-dot').classList.contains('busy'), page.text('queue-words'));
  // a wait, in the server's own words (D4): that it waits, and since when -
  // never who holds the card, which no application is told
  page = await graphic({ card: { waiting_since: 1790172000.0 }, queue: { running: 1 } },
    [aJob({ progress: { stage: 'waiting for the card', waiting_for: 'waiting for the card, since 10:02' } })]);
  check('waiting for the card: and since when, on hover - in the server\'s words',
    page.text('queue-words') === 'waiting for the card'
      && page.named('queue').getAttribute('title') === 'Waiting: waiting for the card, since 10:02',
    page.named('queue').getAttribute('title'));
  page = await graphic({ queue: { running: 1 } }, [aJob({ progress: { stage: 'writing', tokens: 42, tok_s: 38.4 } })]);
  check('writing: how many tokens, and how fast', page.text('queue-words') === 'writing 42 tok · 38/s',
    page.text('queue-words'));
  page = await graphic({ queue: { running: 1 } }, [aJob({ progress: { stage: 'thinking', tokens: 7 } })]);
  check('thinking: how many tokens', page.text('queue-words') === 'thinking 7 tok', page.text('queue-words'));
  page = await graphic({ queue: { consecutive_failures: 2, last: { status: 'failed', error: 'the model went away' } } });
  check('jobs failing in a row: a red dot, and the count and the last error on hover',
    page.named('queue-dot').classList.contains('bad')
      && /2 jobs failed in a row - the last: the model went away/.test(page.named('queue').getAttribute('title')),
    page.named('queue').getAttribute('title'));
});

row('idle with several models up side by side: how many, and every one on hover', async () => {
  let page = await graphic({ runner: { loaded: 'large-v3' }, pipeline: { up: ['story-12b', 'large-v3', 'draft-7b'] } });
  check('three up: how many, never one of them alone',
    page.text('queue-words') === 'idle · 3 models up', page.text('queue-words'));
  check('...and on hover every one, the one used last first',
    page.named('queue').getAttribute('title') === 'large-v3, story-12b and draft-7b are on the card, and nothing is running',
    page.named('queue').getAttribute('title'));
  page = await graphic({ runner: { loaded: 'story-12b' }, pipeline: { up: ['story-12b', 'large-v3'] } });
  check('two up: both on hover',
    page.text('queue-words') === 'idle · 2 models up'
      && page.named('queue').getAttribute('title') === 'story-12b and large-v3 are on the card, and nothing is running',
    `${page.text('queue-words')} | ${page.named('queue').getAttribute('title')}`);
  page = await graphic({ runner: { loaded: 'story-12b' }, pipeline: { state: 'closed', up: [] } });
  check('no pipeline open, one model up: which one, as before',
    page.text('queue-words') === 'idle · story-12b up'
      && page.named('queue').getAttribute('title') === 'story-12b is on the card, and nothing is running',
    `${page.text('queue-words')} | ${page.named('queue').getAttribute('title')}`);
  page = await graphic({ runner: { loaded: null }, pipeline: { up: ['large-v3'] } });
  check('one up that the runner names not: the pipeline\'s word counts',
    page.text('queue-words') === 'idle · large-v3 up', page.text('queue-words'));
});

row("the graphic says a degraded card, and one taken back, in the server's words", async () => {
  const over = example('server.health', 'the card over-full: the pipeline degraded, its work failing saying so, and the card still held');
  let page = await graphic({ pipeline: over.pipeline, runner: over.runner });
  check('degraded: a red dot, the words, and why on hover - the server\'s sentence, whole',
    page.text('queue-words') === 'card degraded' && page.named('queue-dot').classList.contains('bad')
      && page.named('queue').getAttribute('title') === over.pipeline.why,
    `${page.text('queue-words')} | ${page.named('queue').getAttribute('title')}`);
  const lost = example('server.health', 'the card taken back: its models off, and a new ticket asked');
  page = await graphic({ pipeline: lost.pipeline, runner: lost.runner, card: lost.card });
  check('taken back: a red dot, the words, and why on hover - before the new ticket\'s wait',
    page.text('queue-words') === 'card taken back' && page.named('queue-dot').classList.contains('bad')
      && page.named('queue').getAttribute('title') === lost.pipeline.why,
    `${page.text('queue-words')} | ${page.named('queue').getAttribute('title')}`);
  page = await graphic({ pipeline: { ...over.pipeline, why: null }, queue: { queued: 2 } });
  check('...a server that gives no why: the words are the title, never "null"',
    page.named('queue').getAttribute('title') === 'card degraded · 2 waiting', page.named('queue').getAttribute('title'));
  page = await graphic({ pipeline: { ...over.pipeline, state: 'holding', why: null } });
  check('holding again: the graphic is back to what the card is doing', page.text('queue-words') === 'idle · story-12b up'
    && page.named('queue-dot').classList.contains('idle'), page.text('queue-words'));
});

row('the work remaining', async () => {
  let page = await graphic({ queue: { queued: 3, running: 1 } }, [aJob({ progress: {} })]);
  check('one block for each job that waits', page.named('queue-blocks').children.length === 3,
    page.named('queue-blocks').children.length);
  page = await graphic({ queue: { queued: 11, running: 1 } }, [aJob({ progress: {} })]);
  const blocks = page.named('queue-blocks');
  check('...eight at most, and how many more',
    blocks.children.filter((c) => c.tagName === 'I').length === 8 && /\+3/.test(blocks.textContent),
    blocks.textContent);
});

row("the running job's progress fills the box", async () => {
  const fill = (page) => page.named('queue-fill') && page.named('queue-fill').style.width;
  const title = (page) => page.named('queue').getAttribute('title');
  let page = await graphic({ queue: { running: 1 } },
    [aJob({ progress: { variant: 1, of: 1, budget: 400, tokens: 100, stage: 'writing' } })]);
  check('a quarter of the most it can take is a quarter of the box', fill(page) === '25%', fill(page));
  check('...and on hover, how many of at most how many', /100 of at most 400 tokens/.test(title(page)), title(page));
  page = await graphic({ queue: { running: 1 } },
    [aJob({ progress: { variant: 2, of: 3, budget: 1200, tokens: 200, stage: 'writing' } })]);
  check('a fan: the variants before this one count whole, this one by its tokens', fill(page) === '50%', fill(page));
  check('...and on hover, which variant', /200 of at most 400 tokens, variant 2 of 3/.test(title(page)), title(page));
  page = await graphic({ queue: { running: 1 } },
    [aJob({ progress: { variant: 1, of: 2, budget: 800, tokens: 500, stage: 'writing' } })]);
  check('a variant never takes more than its share of the box', fill(page) === '50%', fill(page));
  page = await graphic({ queue: { running: 1 } }, [aJob({ progress: {} })]);
  check('no budget yet: no fill', page.named('queue-fill') === null, fill(page));
});

/* S17: answers to different models run at once. One running job is said as
   ever; several are counted in the bar, each named on hover and in the
   panel, and the fill is the one furthest along - the next to free its
   model, by the only measure the page has */
row('several answers at once: how many, each on hover and in the panel, the fill the one furthest along', async () => {
  const two = example('server.jobs.list',
    'two answers at once, on two models: a recording transcribed while the text before it is translated').jobs;
  const fill = (page) => page.named('queue-fill') && page.named('queue-fill').style.width;
  const title = (page) => page.named('queue').getAttribute('title');
  let page = await graphic({ queue: { running: 2 } }, two);
  check('two answering: how many, never one of them alone',
    page.text('queue-words') === '2 answering' && page.named('queue-dot').classList.contains('busy'),
    page.text('queue-words'));
  check('...and on hover every one: its name, its model, what it is doing',
    title(page).startsWith('2 answering - take 412, in English on story-12b: writing 37 tok · 21/s; '
      + 'take 413 on nb-large: answering'), title(page));
  check('...the fill the one furthest along, and whose it is on hover',
    fill(page) === '6%' && / · filled by take 412, in English: 37 of at most 600 tokens$/.test(title(page)),
    `${fill(page)} | ${title(page)}`);
  const later = [two[0], { ...two[1], progress: { ...two[1].progress, stage: 'writing', tokens: 300 } }];
  page = await graphic({ queue: { running: 2 } }, later);
  check('another one further along: the fill is its', fill(page) === '50%'
    && / · filled by take 413: 300 of at most 600 tokens$/.test(title(page)), `${fill(page)} | ${title(page)}`);
  page = await graphic({ queue: { running: 2, queued: 1 } }, two);
  check('...and what waits is said after them', / · 1 waiting$/.test(title(page)), title(page));
  page = await graphic({ queue: { running: 1 } }, [two[0]]);
  check('one alone: said as ever, in its own words', page.text('queue-words') === 'writing 37 tok · 21/s',
    page.text('queue-words'));
  page = await graphic({ queue: { running: 2 } },
    [two[0], { ...two[1], progress: { stage: 'waiting for the card', waiting_for: 'the card, since 10:02' } }]);
  check('one of them waiting for the card: said first, as ever', page.text('queue-words') === 'waiting for the card'
    && title(page).startsWith('Waiting: the card, since 10:02 · filled by take 412'),
  `${page.text('queue-words')} | ${title(page)}`);
  const fetch = contractFetch({
    'server.health': () => ({ status: 200, body: health({ queue: { running: 2 } }) }),
    'server.jobs.list': () => ({ status: 200, body: { jobs: two } }),
  });
  page = await started({ fetch });
  page.click('queue');
  await tick(page, 1000);
  const rows = page.named('queue-list').children;
  check('the panel lists each, now, saying what each is doing',
    rows.length === 2 && rows.every((li) => /^now /.test(li.textContent))
      && /writing 37 tok/.test(rows[0].textContent) && /answering/.test(rows[1].textContent),
    rows.map((li) => li.textContent).join(' | '));
});

// ========================================================== the queue panel

row('the queue panel', async () => {
  // as the server lists them - in the order they were submitted
  const jobs = [
    aJob({ id: 'j-0021', name: 'part 3', status: 'queued', queue_pos: 2, progress: {} }),
    aJob({ id: 'j-0020', name: 'part 2', status: 'queued', queue_pos: 1, progress: {} }),
    aJob({ id: 'j-0019', name: 'part 1', status: 'running', queue_pos: 0, progress: { stage: 'writing', tokens: 5, tok_s: 40 } }),
    aJob({ id: 'j-0018', name: 'done already', status: 'done', queue_pos: 0, progress: {} }),
  ];
  const fetch = contractFetch({
    'server.health': () => ({ status: 200, body: health({ queue: { queued: 2, running: 1 } }) }),
    'server.jobs.list': () => ({ status: 200, body: { jobs } }),
    'server.jobs.cancel': (req) => (req.path.includes('j-0021')
      ? { status: 409, body: { error: 'j-0021 is done: there is nothing to stop', reason: 'finished', status: 'done' } }
      : { status: 200, body: { job: { ...jobs[1], status: 'cancelled' }, was: 'queued' } }),
  });
  const page = await started({ fetch });
  page.click('queue');
  await tick(page, 1000);
  const rows = page.named('queue-list').children.map((li) => li.getAttribute('data-job'));
  check('lists the unfinished jobs, the running one first, then in their order',
    JSON.stringify(rows) === '["j-0019","j-0020","j-0021"]', JSON.stringify(rows));
  check('...each saying what it is doing', /writing 5 tok/.test(page.named('queue-list').children[0].textContent));
  page.click('cancel-j-0020');
  await page.settle();
  check('Cancel asks the server to cancel that job',
    fetch.asked.some((a) => a.id === 'server.jobs.cancel' && a.path === '/api/jobs/j-0020/cancel'));
  check('...and says so in the log', logged(page).some((t) => /^info queue part 2 \(j-0020\) cancelled/.test(t)),
    logged(page));
  page.click('cancel-j-0021');
  await page.settle();
  check('a cancel refused is shown in the server\'s words, and warned in the log',
    page.text('queue-said') === 'j-0021 is done: there is nothing to stop'
      && logged(page).some((t) => /^warn queue j-0021 could not be cancelled/.test(t)), page.text('queue-said'));
});

/* H8 (S15 RS2): a request held for its check waits on a person - the bar
   says so in its words and its dot, and the panel says where to check it */
row('the graphic says a request waits for your check', async () => {
  let page = await graphic({ queue: { held: 1 }, runner: { loaded: null }, pipeline: { up: [] } });
  check('one held, nothing else: the words, and a dot of its own',
    page.text('queue-words') === '1 request waits for your check'
      && page.named('queue-dot').classList.contains('check') && !page.named('queue-dot').classList.contains('idle'),
    `${page.text('queue-words')} | ${page.named('queue-dot').className}`);
  page = await graphic({ queue: { held: 3 } });
  check('three held: how many, before the model up',
    page.text('queue-words') === '3 requests wait for your check', page.text('queue-words'));
  page = await graphic({ queue: { held: 2, running: 1, queued: 1 } }, [aJob({ progress: { stage: 'writing', tokens: 42, tok_s: 38.4 } })]);
  check('held beside a running job: the card\'s work in words, the check in the dot and on hover',
    page.text('queue-words') === 'writing 42 tok · 38/s' && page.named('queue-dot').classList.contains('check')
      && / · 2 requests wait for your check/.test(page.named('queue').getAttribute('title')),
    `${page.named('queue-dot').className} | ${page.named('queue').getAttribute('title')}`);
  page = await graphic({ queue: { held: 1, consecutive_failures: 1, last: { status: 'failed', error: 'gone' } } });
  check('jobs failing in a row still say so first', page.named('queue-dot').classList.contains('bad'),
    page.named('queue-dot').className);
});

function heldWorld() {
  const world = checkWorld({ 'server.health': () => ({ status: 200, body: health({ queue: { held: 4, running: 1 } }) }) });
  return world;
}

row('the queue panel lists each held request, and where to check it', async () => {
  const world = heldWorld();
  let page = await started({ fetch: world.fetch });
  page.click('queue');
  await tick(page, 1000);
  const ids = page.named('queue-list').children.map((li) => li.getAttribute('data-job'));
  check('the running job first, then every held one', JSON.stringify(ids) === JSON.stringify(world.jobs.map((j) => j.id)),
    JSON.stringify(ids));
  const li = page.named('queue-list').children[2];
  check('...each held one saying it waits for your check, and where: the Check tool, behind the door',
    /waits for your check/.test(li.textContent) && /in Tools: Check/.test(li.textContent), li.textContent);
  page.click('check-j-0052');
  await page.settle();
  check('its Check opens the Check tool behind the door, on that request',
    page.client.store.get().ui.tab === 'check' && page.text('check-head').endsWith('(j-0052)'),
    `${page.client.store.get().ui.tab} | ${page.text('check-head')}`);
  // an application's page with the progress widget and nothing else
  page = loadKit({ fetch: heldWorld().fetch });
  page.kit.place('queue', spot(page, 'queue'));
  await tick(page, 1000);
  page.click('queue');
  await page.settle();
  const alone = page.named('queue-list').children[1];
  check('with no Check on the page: it says so, and offers no Check it cannot open',
    /waits for your check/.test(alone.textContent) && /this page shows no Check/.test(alone.textContent)
      && page.named('check-j-0012') === null, alone.textContent);
  check('...and its Cancel is still there', page.named('cancel-j-0012') !== null);
  // an application's page with the progress widget and the Check tool beside it
  page = loadKit({ fetch: heldWorld().fetch });
  page.kit.place('queue', spot(page, 'queue'));
  const taken = page.kit.place('check', spot(page, 'check'));
  await tick(page, 1000);
  page.click('queue');
  await page.settle();
  check('with the Check tool placed on the page: where, in words',
    /in Check, on this page/.test(page.named('queue-list').children[2].textContent),
    page.named('queue-list').children[2].textContent);
  page.click('check-j-0052');
  await page.settle();
  check('...and its Check shows that request there', page.text('check-head').endsWith('(j-0052)'), page.text('check-head'));
  taken();
  await page.settle();
  // H8's register: the panel read what is placed only at its next redraw -
  // the next jobs poll. It says so the moment a Check is taken out or placed
  check('the Check taken out of the page: no longer offered - at once, not at the next poll',
    /this page shows no Check/.test(page.named('queue-list').children[2].textContent) && page.named('check-j-0052') === null,
    page.named('queue-list').children[2].textContent);
  const again = page.kit.place('check', spot(page, 'check-again'));
  await page.settle();
  check('...and one placed again is offered at once',
    /in Check, on this page/.test(page.named('queue-list').children[2].textContent),
    page.named('queue-list').children[2].textContent);
  again();
  await tick(page, 1000);
  // ...and the same for the Tools door: taken out, its Check is no longer offered, at once
  const doorPage = loadKit({ fetch: heldWorld().fetch });
  doorPage.kit.place('queue', spot(doorPage, 'queue'));
  const door = doorPage.kit.place('tools', spot(doorPage, 'tools'));
  await tick(doorPage, 1000);
  doorPage.click('queue');
  await doorPage.settle();
  const before = doorPage.named('queue-list').children[1].textContent;
  door();
  await doorPage.settle();
  check('the Tools door taken out of the page: its Check no longer offered - at once, not at the next poll',
    /in Tools: Check/.test(before) && /this page shows no Check/.test(doorPage.named('queue-list').children[1].textContent),
    `${before} -> ${doorPage.named('queue-list').children[1].textContent}`);
  check('the Check taken out of the page: no longer offered',
    /this page shows no Check/.test(page.named('queue-list').children[2].textContent) && page.named('check-j-0052') === null,
    page.named('queue-list').children[2].textContent);
});

row('the jobs are asked only while something shows them', async () => {
  const fetch = contractFetch();
  const page = await started({ fetch });
  await openTool(page, 'log');                    // Bench follows the jobs too
  page.client.store.set((s) => page.client.update(s, ['layout', 'bar', 'slots', 'middle'], () => []));
  await page.settle();
  const out = asked(fetch, 'server.jobs.list');
  await tick(page, 3000);
  check('with the graphic out of the bar and its panel shut, the jobs are not asked',
    asked(fetch, 'server.jobs.list') === out, `${out} then ${asked(fetch, 'server.jobs.list')}`);
});

// ============================================================ the models tab

async function onModels(fetch) {
  const page = await started({ fetch });
  await openTool(page, 'models');
  return page;
}

function listed(name) {
  return () => ({ status: 200, body: example('server.models.list', name) });
}

row('the models are asked only while a tool that shows them is shown', async () => {
  const fetch = contractFetch();
  const page = await started({ fetch });
  await openTool(page, 'log');                    // a tool that follows no feed
  const n = () => asked(fetch, 'server.models.list');
  const quiet = n();
  await tick(page, 20000);
  check('another tool shown, the models are not asked', n() === quiet, `${quiet} then ${n()}`);
  await openTool(page, 'models');
  check('their tool shown, they are asked at once', n() === quiet + 1, n());
  await tick(page, 5000);
  check('...and every five seconds', n() === quiet + 2, n());
  await openTool(page, 'log');
  const out = n();
  await tick(page, 20000);
  check('another tool shown again, they are asked no more', n() === out, `${out} then ${n()}`);
});

row('each model, as the server says it', async () => {
  const page = await onModels(contractFetch());
  check('the head: the runner, where the models are, and where the history is from',
    page.text('models-head') === 'llama.cpp, from D:/modelsLoad history from D:/work/book/runner/model_loads.json',
    page.text('models-head'));
  check('a model on the card says so, and only that one',
    page.named('model-loaded-story-12b') !== null && page.named('model-loaded-draft-7b') === null
      && page.named('model-loaded-judge-4b') === null);
  check('its file: how big, and on which disk', page.text('model-file-story-12b') === '8.6 GB on D:',
    page.text('model-file-story-12b'));
  check('its header: the architecture, the context it was trained for beside the context asked, a template, bits a weight',
    page.text('model-header-story-12b') === 'gemma3 · trained for 131,072, asked 16,384 · a chat template · 5.7 bits a weight',
    page.text('model-header-story-12b'));
  check("a problem is the server's own sentence, word for word",
    page.text('model-problems-draft-7b') === 'the settings ask for ctx 65,536 but the model was trained for 32,768 - llama-server may refuse or produce nonsense past that',
    page.text('model-problems-draft-7b'));
  check('...and a model with none shows no list of them', page.named('model-problems-story-12b') === null);
  check('its loads, cold and warm: how many, the normal, the last',
    page.text('model-loads-story-12b') === 'cold: 4 loads · normal 21.3 s · last 22 swarm: 12 loads · normal 6.1 s · last 5.8 s',
    page.text('model-loads-story-12b'));
  const draft = page.named('model-loads-draft-7b');
  check('a last load the server found slow is marked',
    draft.children[0].textContent === 'cold: 5 loads · normal 14.2 s · last 41.7 s - the last was slow'
      && draft.children[0].classList.contains('bad') && !draft.children[1].classList.contains('bad'),
    draft.textContent);
  check('...under three loads there is no normal yet',
    draft.children[1].textContent === 'warm: 2 loads · no normal yet · last 3.1 s', draft.children[1].textContent);
  check('a model never loaded: never', page.text('model-loads-judge-4b') === 'cold: neverwarm: never',
    page.text('model-loads-judge-4b'));
  check('a file that is not there is said, and marked',
    page.text('model-file-judge-4b') === 'missing: judge-4b.Q4_K_M.gguf'
      && page.named('model-file-judge-4b').classList.contains('bad'), page.text('model-file-judge-4b'));
  check('...and the path is in the problem, the server sentence',
    page.text('model-problems-judge-4b') === 'the file is not there: D:/models/judge-4b.Q4_K_M.gguf',
    page.text('model-problems-judge-4b'));
  check('...and cannot be measured, saying why',
    page.named('model-measure-judge-4b').disabled && page.text('model-speed-judge-4b') === 'its file is not there',
    page.text('model-speed-judge-4b'));
  check('the last measure is the server sentence',
    page.text('model-speed-story-12b') === "67 MB in 0.41 s: 163 MB/s from D:, past the operating system's cache"
      && !page.named('model-measure-story-12b').disabled, page.text('model-speed-story-12b'));
  check('a model not measured yet says so', page.text('model-speed-draft-7b') === 'not measured yet',
    page.text('model-speed-draft-7b'));
});

row('each model on its own engine: the head says so, never one engine for all', async () => {
  const page = await onModels(contractFetch({
    'server.models.list': listed('each model on its own engine: a transcriber beside a text model'),
  }));
  check('the head: each model on its own engine, where the models are, the history',
    page.text('models-head') === 'Each model on its own engine, from D:/modelsLoad history from D:/work/desk/runner/model_loads.json',
    page.text('models-head'));
  check('...and the transcriber is listed beside the text model',
    page.named('model-loaded-nb-large') !== null && page.named('model-file-story-12b') !== null);
});

row('nothing bound, no runner', async () => {
  let page = await onModels(contractFetch({ 'server.models.list': listed('the stand-in, nothing bound') }));
  check('nothing bound: the head says there is no history',
    page.text('models-head') === 'Stand-in models, from C:/llm/modelsNo folder is bound, so there is no load history.',
    page.text('models-head'));
  check('...and each model says it has none', page.text('model-loads-demo') === 'no load history - no folder bound',
    page.text('model-loads-demo'));
  check('the stand-in: no file, and nothing to measure, saying why',
    page.text('model-file-demo') === 'no file - a stand-in' && page.named('model-measure-demo').disabled
      && page.text('model-speed-demo') === 'a stand-in has no file to measure', page.text('model-speed-demo'));
  page = await onModels(contractFetch({ 'server.models.list': listed('no runner behind the server') }));
  check('no runner: said, and no models',
    page.text('models-head') === 'No model runner is behind this server, so it has no models.'
      && page.named('models-list').children.length === 0, page.text('models-head'));
});

const IN_A_SESSION = 'a session: two kept - one up, one whose load failed - one loaded on demand for a job, and one loading for another; story-12b up since before this folder was bound, so none of its loads is on the record of this folder';

/* a local clock's hours and minutes, as the page shows a moment the server
   names in seconds */
function hhmm(seconds) {
  const d = new Date(seconds * 1000);
  const two = (n) => String(n).padStart(2, '0');
  return `${two(d.getHours())}:${two(d.getMinutes())}`;
}

row('each model: where it stands, pinned or on demand, on its engine', async () => {
  const page = await onModels(contractFetch({ 'server.models.list': listed(IN_A_SESSION) }));
  check('kept by the session and up: its engine, up, pinned',
    page.text('model-state-large-v3') === 'whisper · up · pinned', page.text('model-state-large-v3'));
  check('loaded for a job: on demand', page.text('model-state-story-12b') === 'llama.cpp · up · on demand',
    page.text('model-state-story-12b'));
  check('its weights going up for another job: loading, on demand',
    page.text('model-state-draft-7b') === 'llama.cpp · loading · on demand', page.text('model-state-draft-7b'));
  check('kept, and its load failed: said so, and why - the server\'s sentence, marked',
    page.text('model-state-nb-medium') === 'whisper · its load failed · pinned'
      && page.text('model-why-nb-medium') === 'the file is not there: D:/models/ggml-nb-medium.bin'
      && page.named('model-why-nb-medium').classList.contains('bad'), page.text('model-why-nb-medium'));
  check('...and a model with nothing wrong says no why', page.named('model-why-large-v3') === null);
  const waiting = await onModels(contractFetch({
    'server.models.list': listed('a session waiting for the card: the models it keeps wait with it, and a file that is not a whisper model is said in its reader\'s words'),
  }));
  check('a session waiting for the card: the model it keeps waits with it',
    waiting.text('model-state-large-v3') === 'whisper · waiting for the card · pinned', waiting.text('model-state-large-v3'));
  check('a model off, and kept by nobody: its engine and off, neither pinned nor on demand',
    waiting.text('model-state-nb-small') === 'whisper · off', waiting.text('model-state-nb-small'));
});

row("each model's file's header, as its engine reads it", async () => {
  const page = await onModels(contractFetch({ 'server.models.list': listed(IN_A_SESSION) }));
  check('a whisper model: its shape, and the eleven numbers of the ggml header',
    page.text('model-header-large-v3') === 'large v3 · vocabulary 51,866 · audio: 1,500 context, 1,280 state, 20 heads, 32 layers'
      + ' · text: 448 context, 1,280 state, 20 heads, 32 layers · 128 mels · ftype 1',
    page.text('model-header-large-v3'));
  check('a llama.cpp model: the GGUF header, as before',
    page.text('model-header-story-12b') === 'gemma3 · trained for 131,072, asked 16,384 · a chat template · 5.7 bits a weight',
    page.text('model-header-story-12b'));
  const odd = example('server.models.list', IN_A_SESSION);
  odd.models[1].header = { ...odd.models[1].header, shape: '80 mels, 12 audio layers, 12 text layers', n_mels: 80 };
  const other = await onModels(contractFetch({ 'server.models.list': () => ({ status: 200, body: odd }) }));
  check('...a shape the server does not name is said in its own numbers, as the server says it',
    /^80 mels, 12 audio layers, 12 text layers · vocabulary/.test(other.text('model-header-large-v3')),
    other.text('model-header-large-v3'));
  const waiting = await onModels(contractFetch({
    'server.models.list': listed('a session waiting for the card: the models it keeps wait with it, and a file that is not a whisper model is said in its reader\'s words'),
  }));
  check('a file that is not a whisper model: no header, and the server\'s sentence',
    waiting.named('model-header-nb-small') === null
      && waiting.text('model-problems-nb-small') === "nb-small.gguf is not a whisper model: it does not start with the ggml magic 0x67676d6c (its first bytes are b'GGUF')",
    waiting.text('model-problems-nb-small'));
});

/* one measure's line of a model's statistics, cell by cell */
function statCells(page, key, measure) {
  const tr = page.named(`model-stat-${key}-${measure}`);
  return tr ? tr.children.map((c) => c.textContent) : null;
}

const WAITING_SESSION = 'a session waiting for the card: the models it keeps wait with it, and a file that is not a whisper model is said in its reader\'s words';

row("each model's statistics: its loads, its first token, its pace - every number with its unit, every line with its count", async () => {
  const page = await onModels(contractFetch());
  const story = page.named('model-stats-story-12b');
  check('a table a model, its columns said once',
    story !== null && story.tagName === 'TABLE'
      && story.children[0].children.map((c) => c.textContent).join('|') === '|median|p90|last|samples',
    story && story.textContent);
  const load = statCells(page, 'story-12b', 'load_s');
  check('its loads: the median, the 90th percentile, the last, in seconds - and how many',
    load && load.join('|') === 'load, asked to ready|6.1 s|21.3 s|5.8 s|16 loads', load);
  const first = statCells(page, 'story-12b', 'first_token_s');
  check('its first token: under a second in milliseconds - and the count, with the median and p90 of the kept samples when fewer are kept',
    first && first.join('|') === 'first token|600 ms|1.4 s|520 ms|240 answers · median and p90 of the last 200', first);
  const pace = statCells(page, 'story-12b', 'tokens_per_s');
  check('its pace: tokens a second, each number with its unit',
    pace && pace.join('|') === 'tokens a second|48.4 tok/s|51.2 tok/s|47.9 tok/s|240 answers · median and p90 of the last 200', pace);
  check('a text model has no real-time factor', page.named('model-stat-story-12b-real_time_factor') === null);
  check('what the widget does not show is not a line: the load\'s steps, the memory, the queue\'s share',
    story && story.children.length === 4, story && story.children.length);
  const slow = page.named('model-slow-story-12b');
  check('a slow answer, flagged as the server flags it: how many of how many, how many times its normal, and when - marked',
    slow !== null && slow.textContent === `1 of 240 answers far slower than its normal: the last 4.17 times slower, at ${hhmm(1790172111)}`
      && slow.classList.contains('bad'), slow && slow.textContent);
  const draft = statCells(page, 'draft-7b', 'load_s');
  check('a model loaded and never asked: its loads alone',
    draft && draft.join('|') === 'load, asked to ready|14.2 s|41.7 s|41.7 s|7 loads'
      && page.named('model-stat-draft-7b-first_token_s') === null && page.named('model-slow-draft-7b') === null, draft);
  check('a model with nothing measured says so',
    page.text('model-stats-judge-4b') === 'nothing measured yet' && page.named('model-stats-judge-4b').tagName !== 'TABLE',
    page.text('model-stats-judge-4b'));
});

/* H8 (S14c R3): what "far slower" means, said where it is flagged - the
   server's own thresholds, read from its answer, never typed here */
row('a slow answer says the thresholds the server judged it by', async () => {
  let page = await onModels(contractFetch());
  const by = page.named('model-slow-by-story-12b');
  check('beside the flag: how many times its normal, and how much lost, as the server judges',
    by !== null && by.textContent === 'far slower: at least 3 times slower than its normal, and 0.25 s lost over it - as this server judges',
    by && by.textContent);
  check('...and none for a model with no slow answer', page.named('model-slow-by-draft-7b') === null);
  const other = example('server.models.list', 'llama.cpp: one up, one missing, one that asks for too much');
  other.slow_answer = { factor: 5, floor_s: 1.5 };
  page = await onModels(contractFetch({ 'server.models.list': () => ({ status: 200, body: other }) }));
  check('a server that judges by other thresholds: its own, said',
    page.text('model-slow-by-story-12b') === 'far slower: at least 5 times slower than its normal, and 1.5 s lost over it - as this server judges',
    page.text('model-slow-by-story-12b'));
});

row("a transcriber's statistics: its answer in one piece, and its real-time factor", async () => {
  const page = await onModels(contractFetch({
    'server.models.list': listed('each model on its own engine: a transcriber beside a text model'),
  }));
  const load = statCells(page, 'nb-large', 'load_s');
  check('its loads, as a text model\'s', load && load.join('|') === 'load, asked to ready|4.5 s|5.1 s|4.5 s|12 loads', load);
  const once = statCells(page, 'nb-large', 'first_token_s');
  check('its first token is its whole answer, and is said so',
    once && once.join('|') === 'answer, in one piece|360 ms|900 ms|310 ms|64 answers', once);
  const rtf = statCells(page, 'nb-large', 'real_time_factor');
  check('its real-time factor: seconds taken for a second of audio, each with its unit',
    rtf && rtf.join('|') === 'real-time factor|0.075 s per audio s|0.088 s per audio s|0.078 s per audio s|64 answers', rtf);
  check('...and no pace in tokens', page.named('model-stat-nb-large-tokens_per_s') === null);
  const waiting = await onModels(contractFetch({ 'server.models.list': listed(WAITING_SESSION) }));
  const slow = waiting.named('model-slow-large-v3');
  check('a transcriber\'s slow answer, as the server flags it',
    slow !== null && slow.textContent === `1 of 88 answers far slower than its normal: the last 3.598 times slower, at ${hhmm(1790334460)}`
      && slow.classList.contains('bad'), slow && slow.textContent);
});

row("statistics where there are none: nothing bound, unreadable, one of each, none since the folder", async () => {
  let page = await onModels(contractFetch({ 'server.models.list': listed('the stand-in, nothing bound') }));
  check('nothing bound: no statistics, and why',
    page.text('model-stats-demo') === 'no statistics - no folder bound', page.text('model-stats-demo'));
  const unread = example('server.models.list', 'the stand-in, nothing bound');
  unread.loads_from = 'D:/work/book/runner/model_loads.json';
  page = await onModels(contractFetch({ 'server.models.list': () => ({ status: 200, body: unread }) }));
  check('a folder bound and no statistics: they could not be read',
    page.text('model-stats-demo') === 'the statistics could not be read', page.text('model-stats-demo'));
  const one = example('server.models.list', 'the stand-in, nothing bound');
  one.loads_from = 'D:/work/book/runner/model_loads.json';
  const once = { count: 1, mean: 3, least: 3, most: 3, last: 3, last_at: 1790172000.0, median: 3, p90: 3, kept: 1 };
  one.models[0].stats = { load_s: { ...once, p90: 118.24 }, first_token_s: { ...once, median: 0.0042, p90: 0.0042, last: 0.0042 },
    answer_s: once, slow_answer: { ...once, count: 1, last: 5 } };
  page = await onModels(contractFetch({ 'server.models.list': () => ({ status: 200, body: one }) }));
  check('one load, one answer: said in the singular - and past a hundred seconds, in whole seconds',
    (statCells(page, 'demo', 'load_s') || []).join('|') === 'load, asked to ready|3.0 s|118 s|3.0 s|1 load'
      && (statCells(page, 'demo', 'first_token_s') || [])[4] === '1 answer', statCells(page, 'demo', 'first_token_s'));
  check('a few milliseconds keep a decimal: never "0 ms"',
    (statCells(page, 'demo', 'first_token_s') || [])[1] === '4.2 ms', statCells(page, 'demo', 'first_token_s'));
  check('...and the one slow answer of one',
    page.text('model-slow-demo') === `1 of 1 answer far slower than its normal: the last 5 times slower, at ${hhmm(1790172000)}`,
    page.text('model-slow-demo'));
  const session = await onModels(contractFetch({ 'server.models.list': listed(IN_A_SESSION) }));
  check('a model up since before its folder was bound: its answers, and no load line',
    session.named('model-stat-story-12b-load_s') === null
      && (statCells(session, 'story-12b', 'tokens_per_s') || []).join('|') === 'tokens a second|49.4 tok/s|51.0 tok/s|49.6 tok/s|6 answers',
    statCells(session, 'story-12b', 'tokens_per_s'));
  check('the session\'s transcriber, and a model loading or failed with nothing measured',
    (statCells(session, 'large-v3', 'real_time_factor') || []).join('|') === 'real-time factor|0.082 s per audio s|0.097 s per audio s|0.083 s per audio s|87 answers'
      && session.text('model-stats-draft-7b') === 'nothing measured yet' && session.text('model-stats-nb-medium') === 'nothing measured yet',
    statCells(session, 'large-v3', 'real_time_factor'));
});

row('the Models tool says the session its models are in, as health says it', async () => {
  const held = example('server.health', 'the coordinator stopped answering; the server keeps its rental');
  const fetch = contractFetch({ 'server.health': () => ({ status: 200, body: held }) });
  const page = await onModels(fetch);
  check('the session: holding the card, since when, whose, its class, how long it is kept',
    page.text('models-session') === `Holding the card since ${hhmm(held.pipeline.since)} · plotting session`
      + " · the application's · responsive · kept 120 s without a renewal",
    page.text('models-session'));
  check('...and no why when there is none', page.named('models-session-why') === null);
  const bad = example('server.health', 'the card over-full: the pipeline degraded, its work failing saying so, and the card still held');
  const worse = await onModels(contractFetch({ 'server.health': () => ({ status: 200, body: bad }) }));
  check('degraded: said, and why in the server\'s words, marked',
    // CHANGED ON PURPOSE (S5b): the session's words for a degraded card
    worse.text('models-session').startsWith(`The card has been full since ${hhmm(bad.pipeline.since)}`)
      && worse.text('models-session-why') === bad.pipeline.why
      && worse.named('models-session-why').classList.contains('bad'), worse.text('models-session-why'));
  // health made the tool's own: the model's progress out of the bar
  page.client.store.set((s) => page.client.update(s, ['layout', 'bar', 'slots', 'middle'], () => []));
  await page.settle();
  let out = asked(fetch, 'server.health');
  await tick(page, 1000);
  check('...the tool alone asks health, every second while it is shown', asked(fetch, 'server.health') === out + 1,
    `${out} then ${asked(fetch, 'server.health')}`);
  await openTool(page, 'log');
  out = asked(fetch, 'server.health');
  await tick(page, 5000);
  check('another tool shown: health is asked no more, and the session is gone from the page',
    asked(fetch, 'server.health') === out && page.named('models-session') === null,
    `${out} then ${asked(fetch, 'server.health')}`);
});

row('a measure', async () => {
  let answer = null;
  const fetch = contractFetch({
    'server.models.read_speed': () => new Promise((resolve) => { answer = resolve; }),
  });
  const page = await onModels(fetch);
  // two seconds into the feed's five: a clock the refresh left behind would
  // ask three seconds after it
  await tick(page, 2000);
  const before = asked(fetch, 'server.models.list');
  page.click('model-measure-story-12b');
  await page.settle();
  check('asks the server to measure that model',
    fetch.asked.some((a) => a.id === 'server.models.read_speed' && a.path === '/api/models/story-12b/read-speed'));
  check('while it runs, its own button says so',
    page.named('model-measure-story-12b').disabled && page.text('model-speed-story-12b') === 'measuring…',
    page.text('model-speed-story-12b'));
  check('...and every other waits, saying why',
    page.named('model-measure-draft-7b').disabled && page.text('model-speed-draft-7b') === 'another measure is running',
    page.text('model-speed-draft-7b'));
  const measured = example('server.models.read_speed', 'where the platform cannot go past the cache');
  answer({ status: 200, body: measured });
  await page.settle();
  check("its sentence goes in the log, the server's words",
    logged(page).some((t) => t === `info models story-12b: ${measured.measure.said}`), logged(page));
  check('...the list is asked again at once, not at its next tick',
    asked(fetch, 'server.models.list') === before + 1, `${before} then ${asked(fetch, 'server.models.list')}`);
  check('...and every button is free again', !page.named('model-measure-draft-7b').disabled);
  await tick(page, 4999);
  check('...then at its own pace: nothing more before five seconds',
    asked(fetch, 'server.models.list') === before + 1, asked(fetch, 'server.models.list'));
  await tick(page, 1);
  check('...and one ask at five - the clock it had before the measure is gone',
    asked(fetch, 'server.models.list') === before + 2, asked(fetch, 'server.models.list'));
  await tick(page, 5000);
  check('...one a tick after that', asked(fetch, 'server.models.list') === before + 3,
    asked(fetch, 'server.models.list'));
});

row('a measure refused', async () => {
  const fetch = contractFetch({
    'server.models.read_speed': () => ({ status: 409, body: { error: 'a model is loading: a measure now would measure the load as well', reason: 'busy' } }),
  });
  const page = await onModels(fetch);
  page.click('model-measure-story-12b');
  await page.settle();
  check("the refusal is shown beside its button, in the server's words",
    page.text('model-speed-story-12b') === 'a model is loading: a measure now would measure the load as well',
    page.text('model-speed-story-12b'));
  check('...warned in the log',
    logged(page).some((t) => t === 'warn models story-12b could not be measured: a model is loading: a measure now would measure the load as well'),
    logged(page));
  check('...and the button is free for another try', !page.named('model-measure-story-12b').disabled);
});

row('an answer already on its way is followed by another', async () => {
  let listing = null;
  let lists = 0;
  const fetch = contractFetch({
    'server.models.list': () => {
      lists += 1;
      return lists === 2 ? new Promise((resolve) => { listing = resolve; }) : listed('llama.cpp: one up, one missing, one that asks for too much')();
    },
  });
  const page = await onModels(fetch);
  await tick(page, 5000);
  check('the second listing is on its way', lists === 2 && listing !== null, lists);
  page.click('model-measure-story-12b');
  await page.settle();
  check('a measure that answers meanwhile asks nothing more yet: one ask at a time', lists === 2, lists);
  listing(listed('llama.cpp: one up, one missing, one that asks for too much')());
  await page.settle();
  await tick(page, 0);
  check('...the listing in, it is asked again at once - the one on its way may be from before',
    lists === 3, lists);
  await tick(page, 4999);
  check('...and then at its own pace again', lists === 3, lists);
  await tick(page, 1);
  check('...five seconds on', lists === 4, lists);
});

row('a measure the server never answers', async () => {
  const fetch = contractFetch({
    'server.models.read_speed': () => { throw new Error('connection refused'); },
  });
  const page = await onModels(fetch);
  page.click('model-measure-story-12b');
  await page.settle();
  check('no answer is said beside its button',
    page.text('model-speed-story-12b') === 'the server did not answer server.models.read_speed: connection refused',
    page.text('model-speed-story-12b'));
  check('...and the button is free for another try', !page.named('model-measure-story-12b').disabled);
});

row('a size in the unit that fits it', async () => {
  const body = example('server.models.list', 'llama.cpp: one up, one missing, one that asks for too much');
  const sized = (key, size) => body.models.find((m) => m.key === key).file.size = size;
  sized('draft-7b', 117964800);
  sized('story-12b', 589824);
  const page = await onModels(contractFetch({ 'server.models.list': () => ({ status: 200, body }) }));
  check('millions of bytes in MB', page.text('model-file-draft-7b') === '118 MB on D:', page.text('model-file-draft-7b'));
  check('fewer in KB - never "0.0 GB"', page.text('model-file-story-12b') === '590 KB on D:',
    page.text('model-file-story-12b'));
});

// ============================================================= the Tools door

row('the tools: one door, not tabs', async () => {
  const page = await started({ fetch: contractFetch() });
  check('the page opens on its first tool: Bench',
    page.named('bench-prompt') !== null, page.text('pane').slice(0, 80));
  check('...and its strip holds one door, naming the tool shown',
    page.named('strip').children.length === 1 && page.text('tools') === 'Tools: Bench ▾', page.text('strip'));
  check('the menu is shut until asked', page.named('tools-menu') === null);
  check('...and the door names every tool, from the tools themselves',
    page.named('tools').getAttribute('title') === 'The instruments: Bench, Models, Check, Log',
    page.named('tools').getAttribute('title'));
  page.click('tools');
  await page.settle();
  const menu = page.named('tools-menu');
  check('opened, it lists the tools by their rank - not the order the parts came in',
    menu !== null && menu.children.map((b) => b.textContent).join(', ') === 'Bench, Models, Check, Log',
    menu && menu.textContent);
  page.click('tool-models');
  await page.settle();
  check('a tool chosen is shown, the menu shut, the door naming it',
    page.named('models-head') !== null && page.named('tools-menu') === null
      && page.text('tools') === 'Tools: Models ▾', page.text('tools'));
  page.click('workspace');
  await page.settle();
  page.click('tools');
  await page.settle();
  page.key('Escape');
  await page.settle();
  check('Esc shuts the menu first, and leaves a panel open',
    page.named('tools-menu') === null && page.named('workspace-path') !== null);
  page.key('Escape');
  await page.settle();
  check('...then the panel', page.named('workspace-path') === null);
  page.click('log-light');
  await page.settle();
  check('the log light opens the Log tool', page.named('log-list') !== null && page.text('tools') === 'Tools: Log ▾',
    page.text('tools'));
});

// ==================================================================== Bench

/* A server whose jobs list is the row's to change: `world.jobs` */
function benchWorld(own = {}) {
  const world = { jobs: [], bodies: [], results: 0 };
  world.fetch = contractFetch({
    'server.jobs.submit': (req) => {
      world.bodies.push(req.body);
      const body = example('server.jobs.submit', 'a single prompt');
      body.job_id = `j-${String(7 + world.bodies.length - 1).padStart(4, '0')}`;
      return { status: 200, body };
    },
    'server.jobs.list': () => ({ status: 200, body: { jobs: world.jobs } }),
    'server.jobs.result': (req) => {
      world.results += 1;
      return { status: 200, body: example('server.jobs.result', 'done, one candidate') };
    },
    ...own,
  });
  return world;
}

async function onBench(world) {
  const page = await started({ fetch: world.fetch });
  return page;
}

function type(page, name, value) {
  const control = page.named(name);
  control.value = value;
  control.dispatchEvent({ type: control.tagName === 'SELECT' ? 'change' : 'input', target: control });
}

row('Bench says why Run is dark', async () => {
  let page = await onBench(benchWorld({
    'server.health': () => ({ status: 200, body: health({ loaded: false, workspace: null }) }),
    'server.workspace.get': () => ({ status: 200, body: { loaded: false, workspace: null } }),
  }));
  check('no folder: dark - a run is a queue job',
    page.named('bench-run').disabled
      && page.text('bench-why') === 'Bench runs are queue jobs, so it needs a folder: no folder is bound - choose one in the workspace picker',
    page.text('bench-why'));
  page = await onBench(benchWorld());
  check('an empty prompt: dark', page.named('bench-run').disabled && page.text('bench-why') === 'the prompt is empty',
    page.text('bench-why'));
  type(page, 'bench-prompt', 'a haiku about rain');
  await page.settle();
  check('a prompt, a folder, a model: Run is lit, and says nothing',
    !page.named('bench-run').disabled && page.text('bench-why') === '', page.text('bench-why'));
  for (const [name, value, why] of [
    ['bench-temperature', '', 'the temperature is a number from 0 to 2'],
    ['bench-temperature', '2.5', 'the temperature is a number from 0 to 2'],
    ['bench-temperature', 'warm', 'the temperature is a number from 0 to 2'],
    ['bench-max-tokens', '0', 'the most tokens is a whole number from 1 to 4096'],
    ['bench-max-tokens', '12.5', 'the most tokens is a whole number from 1 to 4096'],
    ['bench-max-tokens', '4097', 'the most tokens is a whole number from 1 to 4096'],
    ['bench-seed', '7x', 'the seed is a whole number, or empty for none'],
  ]) {
    const was = page.named(name).value;
    type(page, name, value);
    await page.settle();
    check(`${name.slice(6)} "${value}": dark, saying so`,
      page.named('bench-run').disabled && page.text('bench-why') === why, page.text('bench-why'));
    type(page, name, was);
    await page.settle();
  }
  check('...each put right, lit again', !page.named('bench-run').disabled, page.text('bench-why'));
  page = await onBench(benchWorld({ 'server.models.list': () => new Promise(() => {}) }));
  check('the models not yet told: dark, saying so',
    page.named('bench-run').disabled && page.text('bench-why') === 'asking the server for its models', page.text('bench-why'));
  page = await onBench(benchWorld({ 'server.models.list': listed('no runner behind the server') }));
  check('no models: dark, saying so',
    page.named('bench-run').disabled && page.text('bench-why') === 'the server has no models', page.text('bench-why'));
  const gone = example('server.models.list', 'llama.cpp: one up, one missing, one that asks for too much');
  gone.models.forEach((m) => { m.file.present = false; m.file.size = null; });
  page = await onBench(benchWorld({ 'server.models.list': () => ({ status: 200, body: gone }) }));
  check('no model with its file: dark, saying so',
    page.named('bench-run').disabled && page.text('bench-why') === 'no model here has its file', page.text('bench-why'));
  let submitted = null;
  page = await onBench(benchWorld({
    'server.jobs.submit': () => new Promise((resolve) => { submitted = resolve; }),
  }));
  type(page, 'bench-prompt', 'x');
  await page.settle();
  page.click('bench-run');
  await page.settle();
  check('while a run is being sent: dark - one click, one run',
    page.named('bench-run').disabled && page.text('bench-why') === 'sending', page.text('bench-why'));
  submitted({ status: 200, body: example('server.jobs.submit', 'a single prompt') });
  await page.settle();
  check('...sent, lit again', !page.named('bench-run').disabled, page.text('bench-why'));
  check('a long prompt names its job by its first words, cut at forty',
    page.client.firstWords('supercalifragilistic expialidocious antidisestablishmentarianism x y z w')
      === 'supercalifragilistic expialidocious ant…',
    page.client.firstWords('supercalifragilistic expialidocious antidisestablishmentarianism x y z w'));
});

row("Bench's models are the server's", async () => {
  const page = await onBench(benchWorld());
  const options = page.named('bench-model').children;
  check('every model the server names is listed',
    options.map((o) => o.getAttribute('value')).join(',') === 'draft-7b,judge-4b,story-12b',
    options.map((o) => o.textContent).join(' | '));
  const judge = options.find((o) => o.getAttribute('value') === 'judge-4b');
  check('...a model whose file is missing cannot be chosen, and says why',
    judge.disabled && judge.textContent === 'Judge 4B (judge-4b) - its file is missing', judge.textContent);
  check('...the one on the card is chosen first',
    page.client.vmBench(page.client.store.get()).model === 'story-12b');
  page.client.store.set((s) => page.client.update(s, ['bench', 'model'], () => 'judge-4b'));
  await page.settle();
  check('a model with no file named anyhow is not the one run',
    page.client.vmBench(page.client.store.get()).model === 'story-12b');
  type(page, 'bench-model', 'draft-7b');
  await page.settle();
  check('another chosen is the one run', page.client.vmBench(page.client.store.get()).model === 'draft-7b');
});

row('a run: the prompt as typed, a queue job', async () => {
  const world = benchWorld();
  const page = await onBench(world);
  const prompt = '  Line one,\n\tindented\n    and a trailing space \n';
  type(page, 'bench-prompt', prompt);
  type(page, 'bench-temperature', '0.3');
  type(page, 'bench-max-tokens', '64');
  type(page, 'bench-seed', '7');
  type(page, 'bench-n', '2');
  await page.settle();
  const before = asked(world.fetch, 'server.jobs.list');
  page.click('bench-run');
  await page.settle();
  check('the request: the prompt byte for byte, the model, what the controls hold - an interactive job',
    JSON.stringify(world.bodies) === JSON.stringify([{
      kind: 'generate', input: { prompt }, gen: { model: 'story-12b', temperature: 0.3, max_tokens: 64, n: 2, seed: 7 },
      name: 'bench: Line one, indented and a trailing', priority: 'interactive',
    }]), JSON.stringify(world.bodies));
  check('the run is listed first, with what it asked for',
    page.text('run-head-j-0007') === 'story-12b · temperature 0.3 · at most 64 tokens · seed 7 · 2 answers',
    page.text('run-head-j-0007'));
  check('...and the log says it was queued',
    logged(page).some((t) => t === 'info bench j-0007 queued, on story-12b'), logged(page));
  check('...and the jobs are asked at once, not at their next tick',
    asked(world.fetch, 'server.jobs.list') === before + 1, `${before} then ${asked(world.fetch, 'server.jobs.list')}`);
  type(page, 'bench-seed', '');
  await page.settle();
  page.click('bench-run');
  await page.settle();
  check('an empty seed is none: no seed is sent', !('seed' in world.bodies[1].gen), JSON.stringify(world.bodies[1]));
  check('the newest run is listed first',
    page.named('bench-runs').children.map((li) => li.getAttribute('data-run')).join(',') === 'j-0008,j-0007',
    page.named('bench-runs').children.map((li) => li.getAttribute('data-run')).join(','));
});

row('a run, followed to its answers', async () => {
  const world = benchWorld();
  const page = await onBench(world);
  type(page, 'bench-prompt', 'a haiku about rain');
  await page.settle();
  page.click('bench-run');
  await page.settle();
  check("a job the folder's list does not hold says so",
    page.text('run-doing-j-0007') === 'not among the jobs of the folder bound now', page.text('run-doing-j-0007'));
  world.jobs = [aJob({ id: 'j-0007', status: 'queued', queue_pos: 2, progress: {} })];
  await tick(page, 1000);
  check('queued: where it waits', page.text('run-doing-j-0007') === 'waiting, #2', page.text('run-doing-j-0007'));
  world.jobs = [aJob({ id: 'j-0007', status: 'running', queue_pos: 0, progress: { stage: 'writing', tokens: 5, tok_s: 40 } })];
  await tick(page, 1000);
  check('running: what it is doing', page.text('run-doing-j-0007') === 'writing 5 tok · 40/s', page.text('run-doing-j-0007'));
  check('...with a Cancel', page.named('run-cancel-j-0007') !== null);
  check('no answers are asked for while it runs', world.results === 0, world.results);
  world.jobs = [aJob({ id: 'j-0007', status: 'done', queue_pos: 0, progress: {} })];
  await tick(page, 1000);
  await tick(page, 1000);
  check('done: its answers are asked for once', world.results === 1, world.results);
  check('the answer: its text', page.text('run-text-j-0007-0') === 'Rain on the tin roof.', page.text('run-text-j-0007-0'));
  check('...its reasoning, folded away', page.text('run-reasoning-j-0007-0') === 'A haiku wants an image. Rain, a roof.'
    && page.named('run-reasoning-j-0007-0').parentNode.tagName === 'DETAILS', page.text('run-reasoning-j-0007-0'));
  check('...why it finished, how many tokens, how fast, the load it ran on',
    page.text('run-meta-j-0007-0') === 'finished: stop · 7 tokens · 41 tok/s · on load-q2w8e4r6',
    page.text('run-meta-j-0007-0'));
  check('...the round trip', page.text('run-trip-j-0007') === 'round trip 18.8 s', page.text('run-trip-j-0007'));
  check('...no Cancel once it is over', page.named('run-cancel-j-0007') === null);
  check('...and the log says it is done', logged(page).some((t) => t === 'info bench j-0007 done'), logged(page));
});

row('answers asked for once, while another run finishes', async () => {
  let answer = null;
  const world = benchWorld({
    'server.jobs.result': () => {
      world.results += 1;
      return new Promise((resolve) => { answer = resolve; });
    },
  });
  const page = await onBench(world);
  type(page, 'bench-prompt', 'one');
  await page.settle();
  page.click('bench-run');
  await page.settle();
  world.jobs = [aJob({ id: 'j-0007', status: 'done', progress: {} })];
  await tick(page, 1000);
  check('the first run over, its answers asked for', world.results === 1, world.results);
  // a second run, finishing while the first's answers are on their way
  page.client.store.set((s) => page.client.update(s, ['bench', 'runs'], (runs) => [
    { id: 'j-0100', model: 'story-12b', gen: { temperature: 0.8, max_tokens: 400, n: 1 }, prompt: 'two', result: null, said: null },
    ...runs]));
  world.jobs = [...world.jobs, aJob({ id: 'j-0100', status: 'done', progress: {} })];
  await tick(page, 1000);
  check("the second's are asked for, the first's not again while one ask is out", world.results === 2, world.results);
  answer({ status: 200, body: example('server.jobs.result', 'done, one candidate') });
  await page.settle();
  answer({ status: 200, body: example('server.jobs.result', 'done, one candidate') });
  await page.settle();
  // a third run finishes: only its answers are asked for - never again those in
  page.click('bench-run');
  await page.settle();
  world.jobs = [...world.jobs, aJob({ id: 'j-0008', status: 'done', progress: {} })];
  await tick(page, 1000);
  check('answers in are never asked for again', world.results === 3, world.results);
});

row('a run that failed, and a run refused', async () => {
  const failed = example('server.jobs.result', 'done, one candidate');
  failed.job.status = 'failed';
  failed.job.error = 'the model could not be loaded: out of memory';
  failed.candidates = [];
  const world = benchWorld({ 'server.jobs.result': () => ({ status: 200, body: failed }) });
  let page = await onBench(world);
  type(page, 'bench-prompt', 'x');
  await page.settle();
  page.click('bench-run');
  await page.settle();
  world.jobs = [aJob({ id: 'j-0007', status: 'failed', progress: {} })];
  await tick(page, 1000);
  check('failed: the error, in the server\'s words',
    page.text('run-error-j-0007') === 'the model could not be loaded: out of memory', page.text('run-error-j-0007'));
  check('...warned in the log', logged(page).some((t) => t === 'warn bench j-0007 failed: the model could not be loaded: out of memory'),
    logged(page));
  page = await onBench(benchWorld({
    'server.jobs.submit': () => ({ status: 409, body: example('server.jobs.submit', 'nothing bound') }),
  }));
  type(page, 'bench-prompt', 'x');
  await page.settle();
  page.click('bench-run');
  await page.settle();
  check('a run the server refuses: beside Run, in its words',
    page.text('bench-why') === 'no folder is bound: choose one first', page.text('bench-why'));
  check('...warned in the log', logged(page).some((t) => t === 'warn bench the run was refused: no folder is bound: choose one first'),
    logged(page));
  check('...and no run is listed', page.named('bench-runs').children.length === 0);
});

row('a run cancelled', async () => {
  const world = benchWorld({
    'server.jobs.cancel': () => ({ status: 409, body: { error: 'j-0007 is done: there is nothing to stop', reason: 'finished', status: 'done' } }),
  });
  const page = await onBench(world);
  type(page, 'bench-prompt', 'x');
  await page.settle();
  page.click('bench-run');
  await page.settle();
  world.jobs = [aJob({ id: 'j-0007', status: 'running', progress: {} })];
  await tick(page, 1000);
  page.click('run-cancel-j-0007');
  await page.settle();
  check('Cancel asks the server to cancel that job',
    world.fetch.asked.some((a) => a.id === 'server.jobs.cancel' && a.path === '/api/jobs/j-0007/cancel'));
  check("...and a refusal is shown on the run, in the server's words",
    page.text('run-said-j-0007') === 'j-0007 is done: there is nothing to stop', page.text('run-said-j-0007'));
});

row('what a person types is never rewritten', async () => {
  const page = await onBench(benchWorld());
  const prompt = page.named('bench-prompt');
  let writes = 0;
  let value = prompt.value;
  Object.defineProperty(prompt, 'value', { get: () => value, set: (v) => { writes += 1; value = v; } });
  value = 'typed';
  prompt.dispatchEvent({ type: 'input', target: prompt });
  await page.settle();
  await tick(page, 3000);
  check('typing, and the feeds ticking: the page never sets what the prompt holds', writes === 0, writes);
  page.client.store.set((s) => page.client.update(s, ['bench', 'prompt'], () => 'set by the page'));
  await page.settle();
  check('...but a change from elsewhere is shown', value === 'set by the page' && writes === 1, `${writes} ${value}`);
});

row('no places of its own: the pane shows a tool, and only a tool', async () => {
  const page = await started();
  check("the page's door offers its tools, and no place for an application's own screens - those are its own page",
    Array.isArray(page.client.TOOLS) && !('TABS' in page.client), Object.keys(page.client).join(', '));
  page.client.store.set((s) => ({ ...s, ui: { ...s.ui, tab: 'desk' } }));
  await page.settle();
  check('a name no tool has shows the first tool, and nothing fails',
    page.named('bench-prompt') !== null && page.text('tools') === 'Tools: Bench ▾' && page.errors.length === 0,
    `${page.text('tools')} ${page.errors.join(' | ')}`);
});

row('a run cancelled by the person is said, not warned', async () => {
  const cancelled = example('server.jobs.result', 'done, one candidate');
  cancelled.job.status = 'cancelled';
  cancelled.candidates = [];
  const world = benchWorld({ 'server.jobs.result': () => ({ status: 200, body: cancelled }) });
  const page = await onBench(world);
  type(page, 'bench-prompt', 'x');
  await page.settle();
  page.click('bench-run');
  await page.settle();
  world.jobs = [aJob({ id: 'j-0007', status: 'cancelled', progress: {} })];
  await tick(page, 1000);
  check('cancelled: said in the log as what happened',
    logged(page).some((t) => t === 'info bench j-0007 cancelled'), logged(page));
  check('...and nothing warned', !logged(page).some((t) => /^warn bench/.test(t)), logged(page));
  check('...its card says it', page.text('run-doing-j-0007') === 'cancelled', page.text('run-doing-j-0007'));
});

// ================================================================== the bar

const TRANSCRIBER = 'each model on its own engine: a transcriber beside a text model';
const TRANSCRIPT = "a transcription, done: its answer whole, whisper-server's own beside the text";

/* Bench, on a server whose models are a transcriber beside a text model */
function hearingWorld(own = {}) {
  return benchWorld({ 'server.models.list': listed(TRANSCRIBER), ...own });
}

row('Bench for a transcription: a WAV file on this machine, and the fields whisper-server reads', async () => {
  const world = hearingWorld();
  const page = await onBench(world);
  check('a whisper model on the card is chosen first, and listed as one',
    page.client.vmBench(page.client.store.get()).model === 'nb-large'
      && page.named('bench-model').children.find((o) => o.getAttribute('value') === 'nb-large').textContent === 'NB large (nb-large) · whisper',
    page.named('bench-model').children.map((o) => o.textContent).join(' | '));
  check('...the audio and the fields are asked for; the prompt and the samplers are not shown',
    !page.named('bench-hears').hasAttribute('hidden') && page.named('bench-text').hasAttribute('hidden')
      && page.named('bench-prompt').hasAttribute('hidden'));
  check('no audio: dark, saying what it wants', page.named('bench-run').disabled
    && page.text('bench-why') === "the audio is empty: the path of a WAV file on the server's machine", page.text('bench-why'));
  type(page, 'bench-audio', 'D:/takes/take-1.wav');
  await page.settle();
  check('a file named, no fields: lit - none is mandatory', !page.named('bench-run').disabled && page.text('bench-why') === '',
    page.text('bench-why'));
  for (const bad of ['{"language": }', '[1, 2]', '"no"', 'null']) {
    type(page, 'bench-fields', bad);
    await page.settle();
    check(`fields ${bad}: dark, saying what they are`, page.named('bench-run').disabled
      && page.text('bench-why') === 'the fields are a JSON object of what whisper-server reads, or empty for none',
      page.text('bench-why'));
  }
  type(page, 'bench-fields', '{"language": "no", "temperature": 0, "no_language_probabilities": false}');
  await page.settle();
  page.click('bench-run');
  await page.settle();
  check("the request: the file and the fields as typed - the contract's audio request - a transcription job",
    JSON.stringify(world.bodies) === JSON.stringify([{
      kind: 'transcribe',
      input: { audio: 'D:/takes/take-1.wav', fields: { language: 'no', temperature: 0, no_language_probabilities: false } },
      gen: { model: 'nb-large' }, name: 'bench: take-1.wav', priority: 'interactive',
    }]), JSON.stringify(world.bodies));
  const shapes = Object.fromEntries(fs.readdirSync(path.join(__dirname, '..', 'llm-bootstrap-contract', 'shapes'))
    .filter((f) => f.endsWith('.json')).map((f) => [f.slice(0, -5),
      JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'llm-bootstrap-contract', 'shapes', f), 'utf8'))]));
  const wrong = page.window.llmContract.check(world.bodies[0].input, { ref: 'audio_request' }, shapes);
  check("...the input is the shape the contract declares for it", wrong.length === 0, wrong.join('; '));
  check('the run is listed with what it asked for',
    page.text('run-head-j-0007') === 'nb-large · D:/takes/take-1.wav · fields {"language":"no","temperature":0,"no_language_probabilities":false}',
    page.text('run-head-j-0007'));
  type(page, 'bench-fields', '');
  await page.settle();
  page.click('bench-run');
  await page.settle();
  check('no fields: none sent', JSON.stringify(world.bodies[1].input) === JSON.stringify({ audio: 'D:/takes/take-1.wav' })
    && page.text('run-head-j-0008') === 'nb-large · D:/takes/take-1.wav · no fields', page.text('run-head-j-0008'));
  type(page, 'bench-model', 'story-12b');
  await page.settle();
  check('a text model chosen: the prompt and the samplers again, and the audio put away',
    page.named('bench-hears').hasAttribute('hidden') && !page.named('bench-text').hasAttribute('hidden')
      && !page.named('bench-prompt').hasAttribute('hidden') && page.text('bench-why') === 'the prompt is empty',
    page.text('bench-why'));
  check("...and what was typed for the transcriber is kept, as typed", page.named('bench-audio').value === 'D:/takes/take-1.wav');
});

row('a transcription, shown whole: segments, words, the language table, the audio', async () => {
  // the language table as a server may give it: in no order of likelihood
  const answer = example('server.jobs.result', TRANSCRIPT);
  const t = answer.candidates[0].transcript;
  t.language_probabilities = Object.fromEntries(Object.entries(t.language_probabilities).reverse());
  const world = hearingWorld({ 'server.jobs.result': () => ({ status: 200, body: answer }) });
  const page = await onBench(world);
  type(page, 'bench-audio', 'D:/work/desk/takes/take-0412.wav');
  await page.settle();
  page.click('bench-run');
  await page.settle();
  world.jobs = [aJob({ id: 'j-0007', status: 'done', queue_pos: 0, progress: {} })];
  await tick(page, 1000);
  const said = answer.candidates[0];
  check('the text, as heard', page.text('run-text-j-0007-0') === said.text, page.text('run-text-j-0007-0'));
  check('...why it finished, the tokens, the audio it heard, the load it ran on',
    page.text('run-meta-j-0007-0') === 'finished: stop · 13 tokens · 3.2 s of audio · on load-e5g7k2m9',
    page.text('run-meta-j-0007-0'));
  check("...the language asked for, and the one heard, with how sure - whisper's own words",
    page.text('run-language-j-0007-0') === 'language: norwegian · heard: english, 0.90', page.text('run-language-j-0007-0'));
  const table = page.named('run-languages-j-0007-0');
  check('...the language table, asked for: every language, the likeliest first',
    table !== null && table.querySelectorAll('li').map((li) => li.textContent).join(' | ') === 'en 0.90 | no 0.05 | de 0.02',
    table && table.textContent);
  const segments = page.named('run-segments-j-0007-0').children;
  check('...every segment: when, and what', segments.length === 2
    && segments[0].querySelector('.when').textContent === '0.00–1.60 s'
    && segments[0].querySelector('.spoken').textContent === ' Rain on the tin roof again,'
    && segments[1].querySelector('.when').textContent === '1.60–3.20 s', segments.map((s) => s.textContent).join(' | '));
  check("...each segment's own numbers", page.text('run-segment-j-0007-0-0') === 'temperature 0 · avg logprob -0.2 · no speech 0.01',
    page.text('run-segment-j-0007-0-0'));
  const words = page.named('run-words-j-0007-0-1').children;
  check('...and every word, with how sure, and when on hover', words.length === 7
    && words[0].textContent === 'a 0.90' && words[0].getAttribute('title') === '1.60–1.82 s'
    && words[6].textContent === 'window. 0.90', words.map((w) => w.textContent).join(' | '));
  const whole = page.named('run-transcript-j-0007-0');
  check('...and the answer as the server gave it, whole, for everything else it says',
    whole !== null && JSON.stringify(JSON.parse(whole.querySelector('pre').textContent)) === JSON.stringify(said.transcript),
    whole && whole.textContent.slice(0, 80));
});

row('a transcription of silence: an answer, with no words, and no table not asked for', async () => {
  const world = hearingWorld({
    'server.jobs.result': () => ({ status: 200, body: example('server.jobs.result', 'a transcription of silence: no words, and an answer all the same') }),
  });
  const page = await onBench(world);
  type(page, 'bench-audio', 'D:/work/desk/takes/take-0413.wav');
  await page.settle();
  page.click('bench-run');
  await page.settle();
  world.jobs = [aJob({ id: 'j-0007', status: 'done', queue_pos: 0, progress: {} })];
  await tick(page, 1000);
  check('no words heard: said, never an error', page.text('run-segments-j-0007-0') === 'No words were heard.'
    && page.named('run-error-j-0007') === null, page.text('run-segments-j-0007-0'));
  check('...the audio it heard, all the same', page.text('run-meta-j-0007-0') === 'finished: stop · 0 tokens · 2 s of audio · on load-e5g7k2m9',
    page.text('run-meta-j-0007-0'));
  check('...and no language table: the answer has none', page.named('run-languages-j-0007-0') === null
    && page.text('run-language-j-0007-0') === 'language: english', page.text('run-language-j-0007-0'));
});

/* the bar's width, set as its own observer would set it: the bootstrap's
   bar is a container like any other, named `bar` */
async function barAt(page, px, em = 16) {
  page.client.store.set((s) => page.client.update(s, ['containers', 'bar'], (b) => ({ ...b, size: px, em })));
  await page.settle();
}

function placedIds(page) {
  return ['start', 'middle', 'end'].map((slot) =>
    page.named(`slot-${slot}`).children.map((box) => box.getAttribute('data-item')).join(' ')).join(' | ');
}

row('what fits is placed; what does not is shed, lowest priority first', async () => {
  const page = await started({ fetch: contractFetch() });
  // 16 px an em. Everything: 1.2 + 2 + 16 + 20 + (4 + 0.5 + 8) = 51.7 em = 827.2 px
  await barAt(page, 828);
  check('wide enough: every item placed, and no tray',
    placedIds(page) === 'workspace | queue | log customize' && page.named('tray') === null, placedIds(page));
  await barAt(page, 827);
  check('a pixel short: the lowest priority is shed first - the folder name',
    placedIds(page) === ' | queue | log customize' && page.text('tray') === '⋯ 1', placedIds(page));
  check('...and the tray says what it holds, and why',
    page.named('tray').getAttribute('title') === 'In the tray: Workspace (no room)', page.named('tray').getAttribute('title'));
  // without the folder: 1.2 + 2 + 20 + 12.5 = 35.7 em, and the tray's button 1 + 3 = 39.7 em = 635.2 px
  await barAt(page, 636);
  check('the tray button takes room too: at 636 px the graphic still fits', placedIds(page) === ' | queue | log customize',
    placedIds(page));
  await barAt(page, 635);
  check('...at 635 px it is shed', placedIds(page) === ' |  | log customize' && page.text('tray') === '⋯ 2', placedIds(page));
  await barAt(page, 316);
  check('the log light next: at 316 px it fits', placedIds(page) === ' |  | log customize', placedIds(page));
  await barAt(page, 315);
  check('...at 315 px it is shed', placedIds(page) === ' |  | customize', placedIds(page));
  await barAt(page, 100);
  check('Customize is never shed, however narrow', placedIds(page) === ' |  | customize', placedIds(page));
  await barAt(page, 828);
  check('wide again: all back, the tray gone',
    placedIds(page) === 'workspace | queue | log customize' && page.named('tray') === null, placedIds(page));
  await barAt(page, 1000, 20);
  check('a larger em: the same 1000 px holds less (50 em)', placedIds(page) === ' | queue | log customize',
    placedIds(page));
  await barAt(page, 880, 16);
  check('at 880 px (55 em), everything fits', placedIds(page) === 'workspace | queue | log customize', placedIds(page));
  page.click('customize');
  await page.settle();
  check('...in Customization Mode the tray shows, and takes its room: the folder name is shed',
    placedIds(page) === ' | queue | log customize' && page.text('tray') === '⋯ 1', placedIds(page));
  page.key('Escape');
  await page.settle();
});

row('a width that changes nothing placed rebuilds nothing', async () => {
  const page = await started({ fetch: contractFetch() });
  await barAt(page, 1200);
  const graphic = page.named('queue');
  await barAt(page, 1100);
  await barAt(page, 900);
  check('the same items placed: the very same graphic, never built again', page.named('queue') === graphic);
});

row('a shed item is not built, and asks nothing', async () => {
  const fetch = contractFetch();
  const page = await started({ fetch });
  await openTool(page, 'log');                     // Bench follows the jobs too
  await barAt(page, 635);
  const out = asked(fetch, 'server.health');
  await tick(page, 5000);
  check('the graphic shed: health is not asked for it', asked(fetch, 'server.health') === out,
    `${out} then ${asked(fetch, 'server.health')}`);
  check('...and nothing of it is on the page', page.named('queue') === null);
});

row('priority is an order the person sets', async () => {
  const page = await started({ fetch: contractFetch() });
  page.client.store.set((s) => page.client.update(s, ['layout', 'bar', 'shed'], () => ['queue', 'workspace', 'log']));
  await barAt(page, 827);
  check('the graphic named first: it is shed before the folder name',
    placedIds(page) === 'workspace |  | log customize', placedIds(page));
  // a widget the order does not name: a container holds only what it was
  // given, so it is the row's own - the bar's widgets, and one more
  page.client.ITEMS.push({ id: 'extra', title: 'Extra', box: { w: 2, h: 1 }, view: () => () => {} });
  page.kit.container(spot(page, 'more'), {
    name: 'more', slots: { start: ['workspace', 'extra'], middle: ['queue'], end: ['log', 'customize'] },
    shed: ['queue', 'workspace', 'log'],
  });
  await sized(page, 'more', 860);
  // shedding the small one brings the tray's button, which takes more room
  // than it gave: the next in the order goes too - the tray lists both, in
  // the order they were shed
  check('an item the order does not name is shed before any it names',
    page.within('more').named('tray').getAttribute('title') === 'In the tray: Extra (no room), Card and queue (no room)',
    page.within('more').named('tray').getAttribute('title'));
});

row('the tray', async () => {
  const fetch = contractFetch();
  const page = await started({ fetch });
  await openTool(page, 'log');
  page.client.store.set((s) => page.client.update(s, ['layout', 'bar', 'slots', 'middle'], () => []));
  await barAt(page, 1200);
  check('an item in no slot is in the tray, put away', page.text('tray') === '⋯ 1'
    && page.named('tray').getAttribute('title') === 'In the tray: Card and queue (put away)', page.named('tray').getAttribute('title'));
  const out = asked(fetch, 'server.health');
  await tick(page, 3000);
  check('...and asks nothing', asked(fetch, 'server.health') === out);
  page.click('tray');
  await page.settle();
  check('opened, the tray lists it, and why',
    page.text('tray-list') === 'Card and queue (put away)Put back', page.text('tray-list'));
  page.click('restore-queue');
  await page.settle();
  check('Put back: it is where the page first had it', placedIds(page) === 'workspace | queue | log customize',
    placedIds(page));
  check('...and the tray, empty, is gone', page.named('tray') === null);
});

/* a drag, as a browser sends it: the data travels with the events */
function drag(page, from, to) {
  const data = {};
  const dt = { setData: (k, v) => { data[k] = v; }, getData: (k) => data[k] || '' };
  from.dispatchEvent({ type: 'dragstart', target: from, dataTransfer: dt });
  let stopped = false;
  to.dispatchEvent({ type: 'dragover', target: to, dataTransfer: dt, preventDefault: () => { stopped = true; } });
  to.dispatchEvent({ type: 'drop', target: to, dataTransfer: dt, preventDefault: () => {} });
  return stopped;
}

function box(page, id) {
  return page.document.querySelector(`[data-item="${id}"]`);
}

row('Customization Mode: drag, and the keys that do what a drag does', async () => {
  const page = await started({ fetch: contractFetch() });
  await barAt(page, 1200);
  check('outside the mode nothing can be dragged', box(page, 'log').getAttribute('draggable') === null
    && page.named('away-log') === null);
  check('...and a drop is not taken', drag(page, box(page, 'log'), page.named('slot-start')) === false
    && placedIds(page) === 'workspace | queue | log customize', placedIds(page));
  // text dragged in from elsewhere may carry an item's name: no dragstart of
  // the bar's own, and still nothing may move outside the mode
  const slot = page.named('slot-start');
  slot.dispatchEvent({ type: 'drop', target: slot, preventDefault: () => {},
    dataTransfer: { getData: () => 'log', setData: () => {} } });
  await page.settle();
  check('...not even one carrying an item\'s name from elsewhere', placedIds(page) === 'workspace | queue | log customize',
    placedIds(page));
  page.click('customize');
  await page.settle();
  check('in the mode, the items can be dragged', box(page, 'log').getAttribute('draggable') === 'true');
  check('...and the tray shows though empty: it is where an item is put away',
    page.text('tray') === '⋯ 0', page.text('tray'));
  page.click('tray');
  await page.settle();
  check('...an empty tray opens no list', page.named('tray-list') === null);
  page.click('tray');
  await page.settle();
  drag(page, box(page, 'log'), box(page, 'log'));
  await page.settle();
  check('an item dropped on itself stays where it was - not at its slot\'s end',
    placedIds(page) === 'workspace | queue | log customize', placedIds(page));
  drag(page, box(page, 'log'), page.named('slot-start'));
  await page.settle();
  check('an item dropped on a slot goes to its end', placedIds(page) === 'workspace log | queue | customize',
    placedIds(page));
  drag(page, box(page, 'queue'), box(page, 'workspace'));
  await page.settle();
  check('...dropped on an item, it goes before that item', placedIds(page) === 'queue workspace log |  | customize',
    placedIds(page));
  drag(page, box(page, 'workspace'), page.named('slot-middle'));
  await page.settle();
  drag(page, box(page, 'log'), page.named('tray'));
  await page.settle();
  check('dropped on the tray, it is put away', placedIds(page) === 'queue | workspace | customize'
    && page.text('tray') === '⋯ 1', placedIds(page));
  page.click('tray');
  await page.settle();
  const inTray = page.document.querySelector('[data-tray="log"]');
  check('in the mode, what is in the tray can be dragged', inTray.getAttribute('draggable') === 'true');
  drag(page, inTray, page.named('slot-end'));
  await page.settle();
  check('...dragged back to a slot, it is placed', placedIds(page) === 'queue | workspace | customize log', placedIds(page));
  drag(page, box(page, 'customize'), page.named('tray'));
  await page.settle();
  check('Customize is never put away - it is the way back', placedIds(page) === 'queue | workspace | customize log'
    && page.named('away-customize') === null, placedIds(page));
  page.click('away-workspace');
  await page.settle();
  check('✕ puts an item away without a drag', placedIds(page) === 'queue |  | customize log', placedIds(page));
  page.key('Escape');
  await page.settle();
  check('Esc leaves the mode: nothing draggable, no ✕', box(page, 'log').getAttribute('draggable') === null
    && page.named('away-log') === null);
});

row('Customization Mode: what is shed first, set by the keys', async () => {
  const page = await started({ fetch: contractFetch() });
  page.click('customize');
  await page.settle();
  const order = () => page.named('customizing').children.filter((c) => c.getAttribute && c.getAttribute('data-el')
    && c.getAttribute('data-el').startsWith('shed-')).map((c) => c.getAttribute('data-el').slice(5)).join(',');
  check('the banner says what is shed first', order() === 'workspace,queue,log', order());
  check('...the first cannot go sooner, the last cannot stay longer',
    page.named('shed-earlier-workspace') === null && page.named('shed-later-log') === null
      && page.named('shed-later-workspace') !== null && page.named('shed-earlier-log') !== null);
  page.click('shed-later-workspace');
  await page.settle();
  check('▶ keeps an item longer', order() === 'queue,workspace,log'
    && page.client.store.get().layout.bar.shed.join(',') === 'queue,workspace,log', order());
  page.click('shed-earlier-log');
  await page.settle();
  check('◀ sheds it sooner', order() === 'queue,log,workspace', order());
  check('Customize is not in the order: it is never shed', page.named('shed-customize') === null);
});

row("the page's own observer keeps the bar's width", async () => {
  const page = await started({ fetch: contractFetch() });
  const bar = page.named('bar');
  bar.clientWidth = 480;
  page.window.getComputedStyle = () => ({ fontSize: '20px' });
  let changes = 0;
  page.client.store.watch((s) => s.containers.bar, () => { changes += 1; }, 'a row');
  await page.settle();
  const before = changes;
  page.fire('resize');
  await page.settle();
  check('a resize measures the bar: its width, and an em', page.client.store.get().containers.bar.size === 480
    && page.client.store.get().containers.bar.em === 20, JSON.stringify(page.client.store.get().containers.bar));
  page.fire('resize');
  await page.settle();
  check('...the same size measured again changes nothing', changes === before + 1, changes - before);
  bar.clientWidth = 900;
  page.document.dispatchEvent({ type: 'visibilitychange' });
  await page.settle();
  check('a page shown again measures its bar again - a hidden page hears no resize',
    page.client.store.get().containers.bar.size === 900, page.client.store.get().containers.bar.size);
});

// ============================================================== the journal

/* A server that keeps what it is given: `world.config` is what the folder
   kept, `world.kept` every configuration the page asked it to keep */
function keptWorld(own = {}) {
  const world = { kept: [], config: null };
  world.fetch = contractFetch({
    'server.config.get': () => ({ status: 200, body: { config: world.config, sha256: world.config ? 'a'.repeat(64) : null } }),
    'server.config.keep': (req) => {
      world.kept.push(req.body.config);
      return { status: 200, body: { sha256: 'b'.repeat(64), new: true } };
    },
    ...own,
  });
  return world;
}

/* the bar's layout, moved: as the folder keeps it, by the container's name */
const MOVED_LAYOUT = {
  bar: { slots: { start: ['log'], middle: ['queue'], end: ['customize', 'workspace'] }, shed: ['queue', 'workspace', 'log'] },
};

async function customizing(page) {
  page.click('customize');
  await page.settle();
}

row("a folder's kept layout is the one the page starts with", async () => {
  const world = keptWorld();
  world.config = { v: 2, layout: MOVED_LAYOUT };
  const page = await started({ fetch: world.fetch });
  await barAt(page, 1400);
  check('the folder kept a layout: the page starts with it', placedIds(page) === 'log | queue | customize workspace',
    placedIds(page));
  await customizing(page);
  check('...and says it is kept in the folder', page.text('kept') === 'kept in the folder', page.text('kept'));
  check('...with nothing to undo yet', page.named('undo').disabled && page.named('redo').disabled);
});

row("nothing kept, the page's own; nothing bound, nothing kept", async () => {
  let world = keptWorld();
  let page = await started({ fetch: world.fetch });
  await barAt(page, 1400);
  await customizing(page);
  check("nothing kept yet: the page's own layout, said so",
    placedIds(page) === 'workspace | queue | log customize' && page.text('kept') === "the page's own layout",
    page.text('kept'));
  world = keptWorld({
    'server.workspace.get': () => ({ status: 200, body: { loaded: false, workspace: null } }),
    'server.health': () => ({ status: 200, body: health({ loaded: false, workspace: null }) }),
  });
  page = await started({ fetch: world.fetch });
  await customizing(page);
  check('nothing bound: not kept, and said why', page.text('kept') === 'not kept - no folder is bound', page.text('kept'));
  page.click('away-log');
  await page.settle();
  check('...an edit is made, and nothing is asked to keep it',
    world.kept.length === 0 && asked(world.fetch, 'server.config.get') === 0, world.kept.length);
});

row('an edit is recorded, named in the log, and kept', async () => {
  const world = keptWorld();
  const page = await started({ fetch: world.fetch });
  await customizing(page);
  page.click('away-log');
  await page.settle();
  check('the log names the edit, and its id', logged(page).some((t) => t === 'info layout op-1: put Log away'), logged(page));
  check('...it is kept: the whole layout, as the configuration',
    world.kept.length === 1 && JSON.stringify(world.kept[0])
      === JSON.stringify({ v: 2, layout: page.client.store.get().layout }), JSON.stringify(world.kept));
  check('...and Undo names it', !page.named('undo').disabled && page.named('undo').getAttribute('title') === 'Undo: put Log away',
    page.named('undo').getAttribute('title'));
});

row('undo and redo move a pointer', async () => {
  const world = keptWorld();
  const page = await started({ fetch: world.fetch });
  await barAt(page, 1400);
  await customizing(page);
  const base = JSON.stringify(page.client.store.get().layout);
  page.click('away-log');
  await page.settle();
  page.click('away-workspace');
  await page.settle();
  check('two edits', placedIds(page) === ' | queue | customize', placedIds(page));
  page.click('undo');
  await page.settle();
  check('Undo: the last edit undone', placedIds(page) === 'workspace | queue | customize', placedIds(page));
  check('...said in the log, with its id', logged(page).some((t) => t === 'info layout undo op-2: put Workspace away'),
    logged(page));
  check('...and kept', world.kept.length === 3 && JSON.stringify(world.kept[2].layout)
    === JSON.stringify(page.client.store.get().layout), world.kept.length);
  check('Redo names what it would do again', page.named('redo').getAttribute('title') === 'Redo: put Workspace away',
    page.named('redo').getAttribute('title'));
  page.click('redo');
  await page.settle();
  check('Redo: done again', placedIds(page) === ' | queue | customize'
    && logged(page).some((t) => t === 'info layout redo op-2: put Workspace away'), placedIds(page));
  page.click('undo');
  await page.settle();
  page.click('undo');
  await page.settle();
  check('undone to the start: the layout the journal began from, exactly',
    JSON.stringify(page.client.store.get().layout) === base && page.named('undo').disabled, placedIds(page));
  const lines = logged(page).length;
  page.key('z', { ctrlKey: true });
  await page.settle();
  check('...and nothing more to undo: Ctrl+Z changes nothing, and says nothing',
    JSON.stringify(page.client.store.get().layout) === base && logged(page).length === lines, logged(page).slice(lines));
  page.click('redo');
  await page.settle();
  page.click('away-queue');
  await page.settle();
  check('an edit after an undo drops what was undone: nothing to redo',
    page.named('redo').disabled && placedIds(page) === 'workspace |  | customize', placedIds(page));
  check('...and its id is new', logged(page).some((t) => t === 'info layout op-3: put Card and queue away'), logged(page));
});

row('a refused edit is not recorded', async () => {
  const page = await started({ fetch: keptWorld().fetch });
  await customizing(page);
  drag(page, box(page, 'customize'), page.named('tray'));
  await page.settle();
  check('the way back is not put away, and there is nothing to undo',
    page.named('undo').disabled && !logged(page).some((t) => /^info layout/.test(t)), logged(page));
});

row('the keys: in Customization Mode, and never in a field', async () => {
  const page = await started({ fetch: keptWorld().fetch });
  await barAt(page, 1400);
  await customizing(page);
  page.click('away-log');
  await page.settle();
  page.key('z', { ctrlKey: true });
  await page.settle();
  check('Ctrl+Z undoes', placedIds(page) === 'workspace | queue | log customize', placedIds(page));
  page.key('y', { ctrlKey: true });
  await page.settle();
  check('Ctrl+Y redoes', placedIds(page) === 'workspace | queue | customize', placedIds(page));
  page.key('z', { ctrlKey: true });
  await page.settle();
  page.key('Z', { ctrlKey: true, shiftKey: true });
  await page.settle();
  check('Ctrl+Shift+Z redoes too', placedIds(page) === 'workspace | queue | customize', placedIds(page));
  const prompt = page.named('bench-prompt');
  prompt.dispatchEvent({ type: 'keydown', key: 'z', ctrlKey: true, target: prompt, bubbles: true,
    preventDefault() {}, stopPropagation() {} });
  await page.settle();
  check('...but typed in a field, the keys are the field\'s own', placedIds(page) === 'workspace | queue | customize',
    placedIds(page));
  page.key('Escape');
  await page.settle();
  page.key('z', { ctrlKey: true });
  await page.settle();
  check('outside the mode, Ctrl+Z is not the layout\'s', placedIds(page) === 'workspace | queue | customize',
    placedIds(page));
});

row('one keep at a time, and the last asked for is the last kept', async () => {
  const held = [];
  const world = keptWorld({
    'server.config.keep': (req) => new Promise((resolve) => {
      world.kept.push(req.body.config);
      held.push(() => resolve({ status: 200, body: { sha256: 'c'.repeat(64), new: true } }));
    }),
  });
  const page = await started({ fetch: world.fetch });
  await customizing(page);
  page.click('away-log');
  await page.settle();
  page.click('away-workspace');
  await page.settle();
  page.click('away-queue');
  await page.settle();
  check('three edits while the first keep is out: one keep asked', world.kept.length === 1, world.kept.length);
  held[0]();
  await page.settle();
  check('...its answer in, one more - with the layout as it is now',
    world.kept.length === 2 && JSON.stringify(world.kept[1].layout) === JSON.stringify(page.client.store.get().layout),
    world.kept.length);
  held[1]();
  await page.settle();
  check('...and no more', world.kept.length === 2, world.kept.length);
});

row('a keep refused, and a kept layout that cannot be used', async () => {
  let world = keptWorld({
    'server.config.keep': () => ({ status: 503, body: example('server.config.keep', 'the folder cannot be written') }),
  });
  let page = await started({ fetch: world.fetch });
  await customizing(page);
  page.click('away-log');
  await page.settle();
  check('a keep refused: warned in the log, in the server\'s words',
    logged(page).some((t) => t === 'warn layout the layout could not be kept: the configuration could not be kept: [Errno 13] Permission denied'),
    logged(page));
  check('...and the banner says it is not kept, and why',
    page.text('kept') === 'not kept - the configuration could not be kept: [Errno 13] Permission denied', page.text('kept'));
  world = keptWorld();
  world.config = { v: 1, layout: { slots: 'left' } };
  page = await started({ fetch: world.fetch });
  await barAt(page, 1400);
  check("a kept layout this page cannot read: said, and the page's own used",
    logged(page).some((t) => t === "warn layout the layout kept in the folder is not one this page can read - the page's own is used")
      && placedIds(page) === 'workspace | queue | log customize', logged(page));
  world = keptWorld();
  world.config = { v: 1, layout: { slots: { left: [7], middle: [], right: ['customize'] }, shed: [] } };
  page = await started({ fetch: world.fetch });
  await barAt(page, 1400);
  check("...nor one whose slots hold what is not an item's name",
    logged(page).some((t) => /^warn layout the layout kept in the folder is not one this page can read/.test(t))
      && placedIds(page) === 'workspace | queue | log customize', placedIds(page));
  world = keptWorld();
  world.config = { v: 2, layout: { bar: MOVED_LAYOUT.bar, other: { slots: { start: [7], middle: [], end: [] }, shed: [] } } };
  page = await started({ fetch: world.fetch });
  await barAt(page, 1400);
  check("...nor layouts by container, one of which is not a layout: none of them is read",
    logged(page).some((t) => /^warn layout the layout kept in the folder is not one this page can read/.test(t))
      && placedIds(page) === 'workspace | queue | log customize', placedIds(page));
  page = await started({ fetch: keptWorld({
    'server.config.get': () => ({ status: 503, body: example('server.config.get', 'changed since it was kept') }),
  }).fetch });
  check("a kept layout the server cannot give back: said, in its words, and the page's own used",
    logged(page).some((t) => /^warn layout the kept layout could not be read: configs\/[0-9a-f]{64}\.json does not hash to its name/.test(t)),
    logged(page));
});

row('another folder, another journal', async () => {
  // the server's folder, as health and the workspace say it
  let folder = 'D:/work/book';
  const world = keptWorld({
    'server.health': () => ({ status: 200, body: health({ workspace: folder }) }),
    'server.workspace.get': () => ({ status: 200, body: { loaded: true, workspace: folder } }),
  });
  const page = await started({ fetch: world.fetch });
  await barAt(page, 1400);
  await customizing(page);
  page.click('away-log');
  await page.settle();
  const reads = asked(world.fetch, 'server.config.get');
  world.config = { v: 2, layout: MOVED_LAYOUT };
  folder = 'D:/work/other';                        // bound by another client
  await tick(page, 1000);
  check("the folder changed: its kept layout is read, once", asked(world.fetch, 'server.config.get') === reads + 1,
    `${reads} then ${asked(world.fetch, 'server.config.get')}`);
  check('...and the journal begins there: its layout, nothing to undo',
    placedIds(page) === 'log | queue | customize workspace' && page.named('undo').disabled, placedIds(page));
});

row('the oldest edits fold into where the journal began', async () => {
  const page = await started({ fetch: keptWorld().fetch });
  await barAt(page, 1400);
  const edit = (i) => page.client.editLayout(i % 2 ? 'restore' : 'putAway', { container: 'bar', id: 'log' });
  for (let i = 0; i < 205; i += 1) edit(i);
  await page.settle();
  const j = page.client.store.get().journal;
  check('at most 200 edits are kept to undo', j.ops.length === 200 && j.at === 200, j.ops.length);
  check('...the layout is every one of them, done', placedIds(page) === 'workspace | queue | customize', placedIds(page));
  for (let i = 0; i < 200; i += 1) page.client.journalUndo();
  await page.settle();
  check('...undone as far as it goes: where the first five left it, and no further',
    page.client.store.get().journal.at === 0 && placedIds(page) === 'workspace | queue | customize', placedIds(page));
});

row('the door names a tool added after its words were written', async () => {
  const page = loadPage();
  page.client.TOOLS.push({ id: 'extra', title: 'Extra', rank: 50, view: () => () => {} });
  // any change of the state, and the strip is asked again what it shows
  page.client.store.set((s) => ({ ...s, ui: { ...s.ui, customizing: false } }));
  await page.settle();
  check('a new tool is named on the door, in the order of its rank',
    page.named('tools').getAttribute('title') === 'The instruments: Bench, Models, Check, Extra, Log',
    page.named('tools').getAttribute('title'));
});

// ================================================================== the kit

/* an element of an application's own page, for a widget to go in */
function spot(page, id) {
  const node = page.document.createElement('div');
  node.setAttribute('id', id);
  page.document.body.appendChild(node);
  return node;
}

/* a container's size along its axis, and an em, as its observer would set them */
async function sized(page, name, px, em = 16) {
  page.client.store.set((s) => page.client.update(s, ['containers', name], (c) => ({ ...c, size: px, em })));
  await page.settle();
}

/* what each slot of a container holds: start | middle | end */
function slotted(page, name) {
  const w = page.within(name);
  return ['start', 'middle', 'end'].map((slot) =>
    w.named(`slot-${slot}`).children.map((b) => b.getAttribute('data-item')).join(' ')).join(' | ');
}

function thrown(fn) {
  try {
    fn();
    return null;
  } catch (e) {
    return e.message;
  }
}

row('the kit: one script, and nothing until something is placed', async () => {
  const fetch = contractFetch();
  const page = loadKit({ fetch });
  await page.settle();
  check('the kit gives an application its calls', page.kit && typeof page.kit.place === 'function'
    && typeof page.kit.container === 'function', page.kit && Object.keys(page.kit).join(', '));
  check('...and, loaded, asks the server nothing while nothing is placed', fetch.asked.length === 0,
    JSON.stringify(fetch.asked.map((a) => a.path)));
  check("...nor puts its look in the page", page.document.head.children.length === 0,
    page.document.head.children.map((c) => c.tagName).join(' '));
  check('nothing failed to start', page.errors.length === 0, page.errors.join('\n'));
});

row('the kit keeps its names to itself', async () => {
  // an application's page has names of its own - its own `store`, its own
  // `api` - and a kit whose names were the page's would break it
  const bare = loadKit({ server: 'http://127.0.0.1:7072' });
  const leaked = ['el', 'store', 'api', 'update', 'follow', 'ITEMS', 'TOOLS', 'SEEDS', 'logLine', 'NoAnswer']
    .filter((name) => bare.evaluate(`typeof ${name}`) !== 'undefined');
  check("none of the kit's own names is one the page can see", leaked.length === 0, leaked.join(', '));
  // the window's own names, as the sandbox gives them before any script
  const window = ['document', 'fetch', 'console', 'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval',
    'queueMicrotask', 'Date', 'performance', 'URLSearchParams', 'Promise', 'JSON', 'Math', 'Object', 'Array',
    'Map', 'Set', 'Error', 'TypeError', 'localStorage', 'navigator', 'location', 'addEventListener',
    'removeEventListener', 'window', 'llmServer'];
  const shown = Object.keys(bare.window).filter((k) => !window.includes(k)).sort();
  check('...it gives the page two names of its own - its calls, and its door for a console - and the '
    + "contract's two", JSON.stringify(shown) === '["llmClient","llmContract","llmIds","llmKit"]', JSON.stringify(shown));
});

row("the kit's colours are names a page may set: every colour its look paints", async () => {
  const page = loadKit({ fetch: contractFetch() });
  const stop = page.kit.place('session', spot(page, 'status'));
  await page.settle();
  const look = page.document.head.children[0].textContent.replace(/\/\*[\s\S]*?\*\//g, '');
  const names = /(^|\n)\.llmk \{([^}]*)\}/.exec(look);
  const declared = names ? [...names[2].matchAll(/(--llmk-[a-z-]+):/g)].map((m) => m[1]) : [];
  const rest = names ? look.replace(names[0], '') : look;
  const painted = [...rest.matchAll(/#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(/g)].map((m) => m[0]);
  check('no colour is written in a rule: each is a name, declared once on .llmk', names !== null
    && painted.length === 0, JSON.stringify(painted));
  const used = [...new Set([...rest.matchAll(/var\((--llmk-[a-z-]+)\)/g)].map((m) => m[1]))];
  check('...and every name a rule paints with is declared there',
    used.length > 0 && used.every((n) => declared.includes(n)), JSON.stringify(used.filter((n) => !declared.includes(n))));
  const button = /\.llmk button \{([^}]*)\}/.exec(look);
  check("...the buttons' background too - the session and the model's progress are buttons",
    button !== null && /background:\s*var\(--llmk-button\)/.test(button[1]), button && button[1]);
  stop();
});

row('a widget placed: in its box, with its own feeds, until it is stopped', async () => {
  const fetch = contractFetch();
  const page = loadKit({ fetch });
  const here = spot(page, 'status');
  const stop = page.kit.place('queue', here);
  await page.settle();
  const box = here.querySelector('[data-item="queue"]');
  check("the model's progress is put in the element the page names, in the box it declares",
    box !== null && box.style.width === '20em' && box.style.height === '1.6em' && page.named('queue-words') !== null,
    box && JSON.stringify(box.style));
  const styles = page.document.head.children;
  check("...and the kit's look is put in the page, once", styles.length === 1 && styles[0].tagName === 'STYLE'
    && styles[0].hasAttribute('data-llmk') && /\.llmk/.test(styles[0].textContent), styles.length);
  check('...it asks what it shows: health and the jobs',
    asked(fetch, 'server.health') === 1 && asked(fetch, 'server.jobs.list') === 1,
    `${asked(fetch, 'server.health')} ${asked(fetch, 'server.jobs.list')}`);
  await tick(page, 1000);
  check('...and asks them again every second', asked(fetch, 'server.health') === 2, asked(fetch, 'server.health'));
  // the contract's examples: a job running, writing
  check('...shown, it says what the server says', page.text('queue-words') === 'writing 120 tok · 39/s',
    page.text('queue-words'));
  stop();
  await page.settle();
  check('stopped, it leaves the element', here.children.length === 0, here.children.length);
  const out = asked(fetch, 'server.health');
  await tick(page, 5000);
  check('...and nothing more is asked for it: nobody watches it',
    asked(fetch, 'server.health') === out && page.clock.pending() === 0, `${out} then ${asked(fetch, 'server.health')}`);
});

row('every widget, placed by its name', async () => {
  const fetch = contractFetch();
  const page = loadKit({ fetch });
  const stops = [['workspace', 'a'], ['log', 'b'], ['customize', 'c'], ['bench', 'd'], ['models', 'e'],
    ['tools', 'f']].map(([name, id]) => page.kit.place(name, spot(page, id)));
  await page.settle();
  const inside = (id, name) => page.document.getElementById(id).querySelector(`[data-el="${name}"]`);
  check('the folder picker, saying the folder', inside('a', 'workspace') !== null
    && page.text('workspace') === '📁 work/book', page.text('workspace'));
  check('the log light', inside('b', 'log-light') !== null);
  check('Customize', inside('c', 'customize') !== null);
  check('Bench, in its element', inside('d', 'bench-prompt') !== null);
  check('Models, filling its element, as the server says them', inside('e', 'models-head') !== null
    && /llama\.cpp, from D:\/models/.test(page.text('models-head')), page.text('models-head'));
  check('the instruments, behind one door, opening on Bench', inside('f', 'tools') !== null
    && inside('f', 'strip') !== null && page.text('tools') === 'Tools: Bench ▾', page.text('tools'));
  check("the kit's look, once for them all", page.document.head.children.length === 1,
    page.document.head.children.length);
  check('...and with no container on the page, no layout is asked for: there is none to keep',
    asked(fetch, 'server.config.get') === 0, asked(fetch, 'server.config.get'));
  check('each is its own: Bench alone and Bench behind the door are two',
    page.document.querySelectorAll('[data-el="bench-prompt"]').length === 2);
  stops.forEach((stop) => stop());
  await page.settle();
  check('every one stopped: the elements are empty, and nothing is asked',
    ['a', 'b', 'c', 'd', 'e', 'f'].every((id) => page.document.getElementById(id).children.length === 0)
      && page.clock.pending() === 0, page.clock.pending());
  check('nothing failed', page.errors.length === 0, page.errors.join('\n'));
});

row('a name the kit does not have is refused', async () => {
  const fetch = contractFetch();
  const page = loadKit({ fetch });
  const said = thrown(() => page.kit.place('clock', spot(page, 'x')));
  check('refused at once, naming what it has', said !== null && /no widget called clock/.test(said)
    && /queue/.test(said) && /bench/.test(said) && /tools/.test(said), said);
  const nowhere = thrown(() => page.kit.place('queue', null));
  check('...and a place that is not an element of the page', nowhere !== null && /element/.test(nowhere), nowhere);
  await page.settle();
  check('...and nothing was placed, or asked', page.document.body.children.every((c) => c.children.length === 0)
    && fetch.asked.length === 0, JSON.stringify(fetch.asked.map((a) => a.path)));
});

row('what the kit learns from the server is asked once, when the first thing is placed', async () => {
  const fetch = contractFetch();
  const page = loadKit({ fetch });
  await page.settle();
  page.kit.place('log', spot(page, 'a'));
  await page.settle();
  check('the first widget placed: where the server works is asked', asked(fetch, 'server.workspace.get') === 1,
    asked(fetch, 'server.workspace.get'));
  page.kit.place('customize', spot(page, 'b'));
  page.kit.container('top', { name: 'bar', slots: { start: ['log'] } });
  await page.settle();
  check('...once: more placed asks it no more, and the look is put in no more',
    asked(fetch, 'server.workspace.get') === 1 && page.document.head.children.length === 1,
    `${asked(fetch, 'server.workspace.get')} ${page.document.head.children.length}`);
});

row('a widget alone brings its panel beside it', async () => {
  const page = loadKit({ fetch: contractFetch() });
  const here = spot(page, 'folder');
  page.kit.place('workspace', here);
  await page.settle();
  check('its panel is shut', page.named('workspace-path') === null);
  page.click('workspace');
  await page.settle();
  const path = page.named('workspace-path');
  check('clicked, its panel opens beside it, in the same element', path !== null
    && here.querySelector('[data-el="workspace-path"]') === path);
  page.key('Escape');
  await page.settle();
  check('...and Esc shuts it', page.named('workspace-path') === null);
});

/* the session widget placed alone, health answered with `h` */
async function sessionOn(h, own = {}) {
  const fetch = contractFetch({ 'server.health': () => ({ status: 200, body: h }), ...own });
  const page = loadKit({ fetch });
  const here = spot(page, 'status');
  page.kit.place('session', here);
  await page.settle();
  return { page, fetch, here };
}

row('the session, placed: what the server says of the card its pipeline holds', async () => {
  const held = example('server.health', 'the coordinator stopped answering; the server keeps its rental');
  let { page, fetch, here } = await sessionOn(held);
  const box = here.querySelector('[data-item="session"]');
  check('placed in the element the page names, in the box it declares', box !== null && box.style.width === '18em'
    && box.style.height === '1.6em', box && JSON.stringify(box.style));
  check('holding: a green dot, the words, and since when and its label on hover',
    page.text('session-words') === 'holding the card' && page.named('session-dot').classList.contains('idle')
      && page.named('session').getAttribute('title') === `Holding the card since ${hhmm(held.pipeline.since)} - plotting session`,
    page.named('session').getAttribute('title'));
  check('...it asks health, and nothing else, until its panel opens',
    asked(fetch, 'server.health') === 1 && asked(fetch, 'server.models.list') === 0 && asked(fetch, 'server.jobs.list') === 0,
    JSON.stringify(fetch.asked.map((a) => a.id)));
  const queued = example('server.health', 'work queued, waiting for another application to give the card back');
  ({ page } = await sessionOn(queued));
  check("waiting: an amber dot, and since when - D4's words, naming no one",
    page.text('session-words') === `waiting for the card, since ${hhmm(queued.pipeline.since)}`
      && page.named('session-dot').classList.contains('busy'), page.text('session-words'));
  const over = example('server.health', 'the card over-full: the pipeline degraded, its work failing saying so, and the card still held');
  ({ page } = await sessionOn(over));
  const title = page.named('session').getAttribute('title');
  // CHANGED ON PURPOSE (S5b): a degraded card is said as the owner says it -
  // the card full - and since when it has been
  check("the card full (degraded): a red dot, and why on hover - the server's sentence, whole",
    page.text('session-words') === 'card full' && page.named('session-dot').classList.contains('bad')
      && title === `The card has been full since ${hhmm(over.pipeline.since)} - plotting session: ${over.pipeline.why}`, title);
  // ADDED (S5b): held for the card's memory, the server's sentence says what
  // a person does and that the work runs again by itself, in numbers
  const held5b = example('server.health', "the card full: the session held for the card's memory, its work waiting to run again by itself");
  ({ page } = await sessionOn(held5b));
  const told = page.named('session').getAttribute('title');
  check("held for the card's memory: the numbers, what to do, and that it runs again by itself, on hover",
    page.text('session-words') === 'card full'
      && told === `The card has been full since ${hhmm(held5b.pipeline.since)} - plotting session: ${held5b.pipeline.why}`
      && told.includes('1.2 GB free, 6.4 GB needed. 9.8 GB is used by programs outside the bootstrap. '
        + 'Close something, and this runs again by itself.'), told);
  const lost = example('server.health', 'the card taken back: its models off, and a new ticket asked');
  ({ page } = await sessionOn(lost));
  check("taken back: a red dot, and why on hover - the server's sentence, whole",
    page.text('session-words') === 'card taken back' && page.named('session-dot').classList.contains('bad')
      && page.named('session').getAttribute('title').endsWith(`: ${lost.pipeline.why}`),
    page.named('session').getAttribute('title'));
  ({ page } = await sessionOn(example('server.health', 'nothing bound')));
  check('never opened: no session, and nothing wrong',
    page.text('session-words') === 'no session' && page.named('session-dot').classList.contains('unknown')
      && page.named('session').getAttribute('title') === 'No session is open', page.named('session').getAttribute('title'));
  ({ page } = await sessionOn(example('server.health', 'the last two jobs failed, and the store could not be read a moment ago')));
  check('no runner behind the server: said, and nothing else',
    page.text('session-words') === 'no runner'
      && page.named('session').getAttribute('title') === 'No model runner is behind this server: it holds no card',
    page.named('session').getAttribute('title'));
  ({ page } = await sessionOn(held, { 'server.health': () => ({ status: 502, body: {} }) }));
  check('a server that does not answer: said', page.text('session-words') === 'no answer'
    && page.named('session-dot').classList.contains('unknown'), page.text('session-words'));
});

row('a model with no room now fails loudly: the progress widget and the session say it whole', async () => {
  // H9, the owner's ruling of 2026-09-28 ("Fail loudly"): the contract's own
  // words for a job whose model does not fit on the card now
  const failed = example('server.jobs.result', 'failed for a reason of its own: its model does not fit on the card now').job;
  const loud = failed.error;
  check('the contract says it loudly: what programs outside hold, and what to do',
    /programs outside the bootstrap hold \d+ MiB/.test(loud) && /Close programs that use the card, or reboot, then ask again/.test(loud), loud);
  let page = await graphic({ queue: { consecutive_failures: 1, last: { status: 'failed', job: failed.id, error: loud } } });
  check('the progress widget: a failure - its dot', page.named('queue-dot').classList.contains('bad'), page.named('queue-dot').className);
  check('...its words on hover, whole', page.named('queue').getAttribute('title').endsWith(`the last: ${loud}`),
    page.named('queue').getAttribute('title'));
  page.click('queue');
  await page.settle();
  check("...and the panel's last failure, whole", page.text('queue-last') === `The last to finish: ${failed.id}, failed - ${loud}`,
    page.text('queue-last'));
  const why = `writer-24b could not be loaded: ${loud.replace('the model could not be loaded: ', '')}`;
  const h = health({ pipeline: { id: 'pipe-d4h6j9s3', state: 'holding', why, since: 1790170200.0, label: 'desk',
    models: ['writer-24b'], up: [], class: 'responsive', keep_s: 0, opened_by: 'application' } });
  ({ page } = await sessionOn(h));
  check('the session: on hover, whole', page.named('session').getAttribute('title').endsWith(`: ${why}`),
    page.named('session').getAttribute('title'));
  page.click('session');
  await page.settle();
  check('...and in its panel, whole', page.text('session-why') === why, page.text('session-why'));
});

row("the session's panel: the session whole, and each model in it", async () => {
  const h = health({ pipeline: { id: 'pipe-d4h6j9s3', state: 'holding', since: 1790170200.0, label: 'dictation',
    models: ['large-v3', 'nb-medium'], up: ['large-v3', 'story-12b'], class: 'responsive', keep_s: 0, opened_by: 'application' } });
  const { page, fetch } = await sessionOn(h, { 'server.models.list': listed(IN_A_SESSION) });
  check('the panel is shut, and the models are not asked', page.named('session-state') === null
    && asked(fetch, 'server.models.list') === 0);
  page.click('session');
  await page.settle();
  check('opened: the session - holding, since when', page.text('session-state') === `Holding the card since ${hhmm(1790170200)}`,
    page.text('session-state'));
  check("...its label, whose it is, its class, and how long it is kept", page.text('session-about')
    === "dictation · the application's · responsive · held until it is closed", page.text('session-about'));
  check('...no why when there is none', page.named('session-why') === null);
  const lines = page.named('session-models').children.map((li) => li.textContent);
  check('...and every model in it, as the server says each: kept, loaded on demand, loading, or failed - why too',
    JSON.stringify(lines) === JSON.stringify([
      'Draft 7B · llama.cpp · loading · on demand',
      'Large v3 · whisper · up · pinned',
      'NB medium · whisper · its load failed · pinned - the file is not there: D:/models/ggml-nb-medium.bin',
      'Story 12B · llama.cpp · up · on demand',
    ]), JSON.stringify(lines));
  check('...the models asked for it once it opened', asked(fetch, 'server.models.list') === 1,
    asked(fetch, 'server.models.list'));
  // a model the settings name, off and kept by nobody, is not in the session
  const beside = await sessionOn(h, { 'server.models.list': listed(TRANSCRIBER) });
  beside.page.click('session');
  await beside.page.settle();
  const inIt = beside.page.named('session-models').children.map((li) => li.textContent);
  check('...and a model off, kept by nobody, is not in it: the text model beside the transcriber is not listed',
    JSON.stringify(inIt) === JSON.stringify(['NB large · whisper · up · pinned']), JSON.stringify(inIt));
  page.key('Escape');
  await page.settle();
  const out = asked(fetch, 'server.models.list');
  await tick(page, 10000);
  check('shut again: the models are asked no more', page.named('session-state') === null
    && asked(fetch, 'server.models.list') === out, `${out} then ${asked(fetch, 'server.models.list')}`);
  const over = example('server.health', 'the card over-full: the pipeline degraded, its work failing saying so, and the card still held');
  const bad = await sessionOn(over);
  bad.page.click('session');
  await bad.page.settle();
  check("degraded: the panel says why, in the server's words, marked",
    bad.page.text('session-why') === over.pipeline.why && bad.page.named('session-why').classList.contains('bad'),
    bad.page.text('session-why'));
});

row('the log light, on a page with no Tools door, opens the log beside it', async () => {
  const page = loadKit({ fetch: contractFetch() });
  page.kit.place('log', spot(page, 'light'));
  await page.settle();
  page.client.logLine('error', 'page', 'something broke');
  await page.settle();
  check('the light counts the error', page.text('log-light') === '● 1', page.text('log-light'));
  page.click('log-light');
  await page.settle();
  const list = page.named('log-list');
  check('clicked, the log opens beside it - there is no Tools door on this page to open it behind',
    list !== null && page.document.getElementById('light').querySelector('[data-el="log-list"]') === list
      && /something broke/.test(list.textContent), list && list.textContent);
  page.click('log-light');
  await page.settle();
  check('...and clicked again, it shuts', page.named('log-list') === null);
  const door = page.kit.place('tools', spot(page, 'door'));
  await page.settle();
  page.click('log-light');
  await page.settle();
  check('with a Tools door on the page, the light opens the Log behind it, as on the bootstrap\'s own page',
    page.text('tools') === 'Tools: Log ▾'
      && page.document.getElementById('light').querySelector('[data-el="log-list"]') === null, page.text('tools'));
  door();
  await page.settle();
  page.click('log-light');
  await page.settle();
  check('...and with the door taken away again, the light opens the log beside it once more',
    page.document.getElementById('light').querySelector('[data-el="log-list"]') !== null);
});

row('a container docked to an edge of the window', async () => {
  const fetch = contractFetch();
  const page = loadKit({ fetch });
  // the page's own look: its body padded 8 px at the bottom
  page.window.getComputedStyle = (node) => (node === page.document.body ? { paddingBottom: '8px' } : { fontSize: '16px' });
  const stop = page.kit.container('bottom', { name: 'progress', slots: { middle: ['queue'] } });
  await page.settle();
  const root = page.within('progress').root;
  check('it is put in the page, fixed to the bottom edge', root !== null && root.parentNode === page.document.body
    && root.classList.contains('dock') && root.classList.contains('dock-bottom'), root && root.className);
  check("...its slots hold what the page gave it: the model's progress, in the middle",
    slotted(page, 'progress') === ' | queue | ', slotted(page, 'progress'));
  check('...which asks what it shows', asked(fetch, 'server.health') === 1, asked(fetch, 'server.health'));
  root.offsetHeight = 38;
  page.fire('resize');
  await page.settle();
  check("...and the page's body is padded on that side by its height, on top of its own: nothing of "
    + 'the page is under it', page.document.body.style.paddingBottom === '46px' && !page.document.body.style.paddingTop,
  JSON.stringify(page.document.body.style));
  root.offsetHeight = 60;
  page.fire('resize');
  await page.settle();
  check('...grown, the padding grows with it', page.document.body.style.paddingBottom === '68px',
    page.document.body.style.paddingBottom);
  stop();
  await page.settle();
  check('stopped, it leaves the page, and the body is as it was',
    root.parentNode === null && page.document.body.style.paddingBottom === '', page.document.body.style.paddingBottom);
  const out = asked(fetch, 'server.health');
  await tick(page, 3000);
  check('...and nothing is asked for it', asked(fetch, 'server.health') === out, `${out} then ${asked(fetch, 'server.health')}`);
});

row('each edge: its side, its axis, its place in the page', async () => {
  for (const [edge, side, axis, first] of [['top', 'paddingTop', 'row', true], ['bottom', 'paddingBottom', 'row', false],
    ['left', 'paddingLeft', 'column', true], ['right', 'paddingRight', 'column', false]]) {
    const page = loadKit();
    spot(page, 'the-page');
    page.kit.container(edge, { name: 'c', slots: { start: ['log'] } });
    await page.settle();
    const root = page.within('c').root;
    root.offsetHeight = 30;
    root.offsetWidth = 200;
    page.fire('resize');
    await page.settle();
    const kids = page.document.body.children;
    check(`docked to the ${edge}: fixed there, a ${axis}, the body padded on its ${edge} by its `
      + `${axis === 'row' ? 'height' : 'width'}, and ${first ? 'first' : 'last'} in the page`,
    root.classList.contains(`dock-${edge}`) && root.classList.contains(`axis-${axis}`)
      && page.document.body.style[side] === (axis === 'row' ? '30px' : '200px')
      && (first ? kids[0] === root : kids[kids.length - 1] === root),
    `${root.className} ${JSON.stringify(page.document.body.style)}`);
  }
});

row('docked to two edges that meet: a column stands between the rows, never under them', async () => {
  // seen in a real browser: a column docked left ran the window's whole
  // height, and a row docked to the bottom covered its end - Customize, the
  // way back, with it
  const page = loadKit();
  page.kit.container('top', { name: 'head', slots: { start: ['workspace'] } });
  const foot = page.kit.container('bottom', { name: 'foot', slots: { middle: ['queue'] } });
  page.kit.container('left', { name: 'side', slots: { start: ['log'], end: ['customize'] } });
  await page.settle();
  page.within('head').root.offsetHeight = 30;
  page.within('foot').root.offsetHeight = 40;
  page.within('side').root.offsetWidth = 120;
  page.fire('resize');
  await page.settle();
  const said = (edge) => page.document.body.style.getPropertyValue(`--llmk-dock-${edge}`);
  check('each dock says its size to the page, by its edge: the top 30 px, the bottom 40, the left 120',
    said('top') === '30px' && said('bottom') === '40px' && said('left') === '120px' && said('right') === '',
    JSON.stringify(page.document.body.style));
  const look = page.document.head.children[0].textContent;
  const rule = /\.llmk-container\.dock-left, \.llmk-container\.dock-right \{([^}]*)\}/.exec(look);
  check("...and the kit's look stands a column between them: below the top dock, above the bottom one",
    rule !== null && /top: var\(--llmk-dock-top, 0px\);/.test(rule[1]) && /bottom: var\(--llmk-dock-bottom, 0px\);/.test(rule[1]),
    rule ? rule[1] : 'no rule for a docked column');
  foot();
  await page.settle();
  check('a dock taken away says so: the column reaches the bottom again', said('bottom') === '' && said('top') === '30px',
    JSON.stringify(page.document.body.style));
});

row('a dock pads the page by its size rounded up: never a fraction of a pixel of the page under it', async () => {
  // seen in a real browser (S14k): a bar 33.17 px high padded the page by its
  // offsetHeight, 33 - so 0.17 px of the page was under the bar
  const page = loadKit();
  page.kit.container('top', { name: 'head', slots: { start: ['log'] } });
  page.kit.container('left', { name: 'side', slots: { start: ['customize'] } });
  await page.settle();
  const head = page.within('head').root;
  const side = page.within('side').root;
  head.offsetHeight = 33;                          // a browser rounds an offset
  head.rect = { width: 800, height: 33.17 };       // ...and measures the box as it is
  side.offsetWidth = 119;
  side.rect = { width: 119.4, height: 600 };
  page.fire('resize');
  await page.settle();
  const said = (edge) => page.document.body.style.getPropertyValue(`--llmk-dock-${edge}`);
  check('a row 33.17 px high: the page padded by 34, and the dock says 34',
    page.document.body.style.paddingTop === '34px' && said('top') === '34px', JSON.stringify(page.document.body.style));
  check('a column 119.4 px wide: padded by 120 - its size rounded up, never down',
    page.document.body.style.paddingLeft === '120px' && said('left') === '120px', JSON.stringify(page.document.body.style));
  head.rect = { width: 800, height: 40 };
  page.fire('resize');
  await page.settle();
  check('a whole size is padded as it is', page.document.body.style.paddingTop === '40px' && said('top') === '40px',
    page.document.body.style.paddingTop);
});

row('a container in an element: its slots side by side, in order', async () => {
  const page = loadKit();
  const here = spot(page, 'header');
  here.appendChild(page.document.createElement('h1'));
  page.kit.container(here, { name: 'head', slots: { end: ['log', 'customize'], start: ['workspace', 'queue'] } });
  await page.settle();
  const root = page.within('head').root;
  check('it goes at the end of the element, in the flow of the page - not docked, a row',
    root.parentNode === here && here.children[1] === root && !root.classList.contains('dock')
      && root.classList.contains('axis-row'), root.className);
  check('...its slots in order - start, middle, end - each holding its widgets in the order given',
    slotted(page, 'head') === 'workspace queue |  | log customize', slotted(page, 'head'));
  const w = page.within('head');
  const boxes = ['workspace', 'queue', 'log', 'customize'].map((id) => root.querySelector(`[data-item="${id}"]`).style);
  check('each widget in the box it declares, and no edge of its own',
    boxes.map((s) => s.width).join(' ') === '16em 20em 4em 8em' && boxes.every((s) => s.height === '1.6em'
      && s.margin === undefined && s.padding === undefined), JSON.stringify(boxes));
  check("...the room between them is the slot's", w.named('slot-start').style.gap === '0.5em'
    && w.named('slot-end').style.gap === '0.5em', JSON.stringify(w.named('slot-start').style));
  check('...and asked, a column stacks them', (() => {
    const col = spot(page, 'side');
    page.kit.container(col, { name: 'col', axis: 'column', slots: { start: ['queue'] } });
    return page.within('col').root.classList.contains('axis-column');
  })());
});

row('a column: its size is its height, and its boxes stack', async () => {
  const page = loadKit();
  page.kit.container('left', {
    name: 'side', slots: { start: ['workspace', 'queue'], end: ['log', 'customize'] }, shed: ['workspace', 'queue', 'log'],
  });
  await page.settle();
  // along a column a box takes its lines, 1.6 em each. Everything: 1.2 + 2 +
  // (1.6 + 0.5 + 1.6) + (1.6 + 0.5 + 1.6) = 10.6 em = 169.6 px at 16 px an em
  await sized(page, 'side', 170);
  check('tall enough: every widget placed, and no tray', slotted(page, 'side') === 'workspace queue |  | log customize'
    && page.within('side').named('tray') === null, slotted(page, 'side'));
  await sized(page, 'side', 169);
  // the tray's button, a line tall, takes room too: 3.2 + 1.6 + 3.7 + 1 +
  // 1.6 = 11.1 em is more than 10.56, so the next in the order goes as well
  check('a pixel short: the first in the drop order leaves for the tray - and, the tray taking its line, the next',
    slotted(page, 'side') === ' |  | log customize' && page.within('side').text('tray') === '⋯ 2', slotted(page, 'side'));
  await sized(page, 'side', 152);
  check('at 152 px (9.5 em) the log light and Customize still fit, beside the tray',
    slotted(page, 'side') === ' |  | log customize', slotted(page, 'side'));
  await sized(page, 'side', 151);
  check('...at 151 px the light goes; Customize never does', slotted(page, 'side') === ' |  | customize',
    slotted(page, 'side'));
  await sized(page, 'side', 170);
  check('tall again: all come back, the tray gone', slotted(page, 'side') === 'workspace queue |  | log customize'
    && page.within('side').named('tray') === null, slotted(page, 'side'));
  const strip = page.within('side').named('side');
  strip.clientWidth = 50;
  strip.clientHeight = 300;
  page.window.getComputedStyle = () => ({ fontSize: '20px' });
  page.fire('resize');
  await page.settle();
  check("a column's observer measures its height, never its width",
    page.client.store.get().containers.side.size === 300 && page.client.store.get().containers.side.em === 20,
    JSON.stringify(page.client.store.get().containers.side));
});

row('a container refuses what it cannot hold, before anything is placed', async () => {
  const fetch = contractFetch();
  const page = loadKit({ fetch });
  const at = spot(page, 'here');
  const refused = (where, opts) => thrown(() => page.kit.container(where, opts));
  for (const [what, where, opts, said] of [
    ['an instrument - it has no box - in a slot', at, { name: 'a', slots: { start: ['bench'] } }, /Bench has no box/],
    ['a widget the kit does not have', at, { name: 'a', slots: { start: ['clock'] } }, /no widget called clock/],
    ['a widget twice', at, { name: 'a', slots: { start: ['log'], end: ['log'] } }, /log.*twice/],
    ['a slot that is not start, middle or end', at, { name: 'a', slots: { left: ['log'] } }, /start, middle and end/],
    ['no name', at, { slots: { start: ['log'] } }, /name/],
    ['a setting nothing reads', at, { name: 'a', slots: { start: ['log'] }, size: 3 }, /size/],
    ['an axis for a docked container: its edge decides', 'left', { name: 'a', axis: 'row', slots: { start: ['log'] } }, /edge/],
    ['an axis that is not row or column', at, { name: 'a', axis: 'diagonal', slots: { start: ['log'] } }, /row or column/],
    ['a drop order naming a widget not in it', at, { name: 'a', slots: { start: ['log'] }, shed: ['queue'] }, /queue/],
    ['a place that is neither an edge nor an element', 'middle', { name: 'a', slots: { start: ['log'] } }, /edge/],
  ]) {
    const got = refused(where, opts);
    check(`refused: ${what}`, got !== null && said.test(got), got);
  }
  page.kit.container('top', { name: 'one', slots: { start: ['log'] } });
  check('refused: a name another container on the page has',
    /one/.test(refused(at, { name: 'one', slots: { start: ['queue'] } }) || ''));
  check('refused: an edge another container is docked to',
    /top/.test(refused('top', { name: 'two', slots: { start: ['queue'] } }) || ''));
  await page.settle();
  check('...and none of those was put in the page, or asked for',
    page.document.querySelectorAll('[data-container]').length === 1 && asked(fetch, 'server.health') === 0,
    page.document.querySelectorAll('[data-container]').length);
});

row('a container holds only the widgets it was given', async () => {
  const world = keptWorld();
  world.config = { v: 2, layout: { head: { slots: { start: ['workspace', 'queue'], middle: [], end: ['log'] }, shed: [] } } };
  const page = loadKit({ fetch: world.fetch });
  page.kit.container(spot(page, 'h'), { name: 'head', slots: { start: ['log'] } });
  await page.settle();
  check('a kept layout naming widgets it was not given places its own, where it kept them - and none of the others',
    slotted(page, 'head') === ' |  | log', slotted(page, 'head'));
  page.client.store.set((s) => page.client.update(s, ['ui', 'customizing'], () => true));
  await page.settle();
  const slot = page.within('head').named('slot-start');
  slot.dispatchEvent({ type: 'drop', target: slot, preventDefault: () => {},
    dataTransfer: { getData: () => 'queue', setData: () => {} } });
  await page.settle();
  check('...nor does a drop of one it was not given: nothing moves, nothing is recorded',
    slotted(page, 'head') === ' |  | log' && page.client.store.get().journal.ops.length === 0, slotted(page, 'head'));
  page.client.editLayout('putAway', { container: 'nowhere', id: 'log' });
  await page.settle();
  check('...nor an edit naming a container not on the page', page.client.store.get().journal.ops.length === 0
    && page.errors.length === 0, page.errors.join('\n'));
  check('...and its tray holds only its own', page.within('head').text('tray') === '⋯ 0', page.within('head').text('tray'));
  page.client.store.set((s) => page.client.update(s, ['ui', 'customizing'], () => false));
  const alone = spot(page, 'folder');
  page.kit.place('workspace', alone);
  await page.settle();
  page.click('workspace');
  await page.settle();
  check("...nor opens the panel of a widget it was not given, though its kept layout names it: the panel is the "
    + "widget's own, beside it", alone.querySelector('[data-el="workspace-path"]') !== null
    && page.within('head').named('workspace-path') === null);
});

row('a widget the application adds later appears where the application puts it: only one the person put away is in the tray', async () => {
  // the owner's ruling of 2026-09-27 on S14k's R2: "the point of the widget
  // is to self-contain its feature so we can place it where we will and
  // style it how we want"
  const world = keptWorld();
  // kept before the application gave the container its folder picker and its
  // session: the log and Customize placed, the model's progress put away -
  // in no slot, and named by the drop order the layout kept
  world.config = { v: 2, layout: { head: { slots: { start: ['log'], middle: [], end: ['customize'] }, shed: ['queue'] } } };
  const given = { name: 'head', slots: { start: ['workspace', 'log', 'session'], middle: ['queue'], end: ['customize'] }, shed: ['queue'] };
  const page = loadKit({ fetch: world.fetch });
  page.kit.container(spot(page, 'h'), given);
  await page.settle();
  check('the widgets the kept layout never saw are where the application puts them: each in its slot, before the '
    + 'one it gave after it, or at the end when none of those is there',
    slotted(page, 'head') === 'workspace log session |  | customize', slotted(page, 'head'));
  const tray = (p) => `${p.within('head').text('tray')} ${p.within('head').named('tray').getAttribute('title')}`;
  check('...and the one the person put away stays in the tray, the only one there',
    tray(page) === '⋯ 1 In the tray: Card and queue (put away)', tray(page));
  page.client.editLayout('putAway', { container: 'head', id: 'workspace' });
  await page.settle();
  check('put away by the person, a widget that came later goes to the tray like any other',
    slotted(page, 'head') === 'log session |  | customize'
      && tray(page) === '⋯ 2 In the tray: Workspace (put away), Card and queue (put away)', `${slotted(page, 'head')} / ${tray(page)}`);
  const kept = world.kept[world.kept.length - 1];
  check('...and the layout kept says every widget it has seen - the tray is not kept: nothing names what is in it',
    !!kept && JSON.stringify([...kept.layout.head.known].sort())
      === JSON.stringify(['customize', 'log', 'queue', 'session', 'workspace'])
      && JSON.stringify(Object.keys(kept.layout.head).sort()) === JSON.stringify(['known', 'shed', 'slots']),
    JSON.stringify(kept));
  // the next page from the same folder
  world.config = kept;
  const again = loadKit({ fetch: world.fetch });
  again.kit.container(spot(again, 'h'), given);
  await again.settle();
  check('the next page from the same folder: what the person put away is still away, the one that came later too',
    slotted(again, 'head') === 'log session |  | customize'
      && tray(again) === '⋯ 2 In the tray: Workspace (put away), Card and queue (put away)', `${slotted(again, 'head')} / ${tray(again)}`);
  again.client.journalUndo();
  await again.settle();
  check('...and nothing to undo there: seeing a widget is not an edit', again.client.store.get().journal.ops.length === 0
    && slotted(again, 'head') === 'log session |  | customize', slotted(again, 'head'));
  // the oldest edits fold into where the journal began: the one that came
  // later, put away first, is folded with what the layout had seen
  world.config = { v: 2, layout: { head: { slots: { start: ['log'], middle: [], end: ['customize'] }, shed: ['queue'] } } };
  const folded = loadKit({ fetch: world.fetch });
  folded.kit.container(spot(folded, 'h'), given);
  await folded.settle();
  folded.client.editLayout('putAway', { container: 'head', id: 'workspace' });
  for (let i = 0; i < 204; i += 1) folded.client.editLayout(i % 2 ? 'restore' : 'putAway', { container: 'head', id: 'log' });
  await folded.settle();
  check('...folded past the 200 kept to undo, the one put away that came later is still away',
    // (the log, put back last, at the end of the slot its page gave it)
    folded.client.store.get().journal.ops.length === 200 && slotted(folded, 'head') === 'session log |  | customize'
      && tray(folded) === '⋯ 2 In the tray: Workspace (put away), Card and queue (put away)',
    `${slotted(folded, 'head')} / ${tray(folded)}`);
  // what a layout has seen is a list of widgets, or the layout is not one this page can read
  world.config = { v: 2, layout: { head: { slots: { start: ['log'], middle: [], end: ['customize'] }, shed: [], known: 'log' } } };
  const odd = loadKit({ fetch: world.fetch });
  odd.kit.container(spot(odd, 'h'), given);
  await odd.settle();
  check("a kept layout whose widgets seen are not a list: not read - said, and the page's own used",
    slotted(odd, 'head') === 'workspace log session | queue | customize'
      && logged(odd).includes("warn layout the layout kept in the folder is not one this page can read - the page's own is used"),
    `${slotted(odd, 'head')} / ${logged(odd).join(' / ')}`);
});

row('several containers: one journal, and every one kept in the folder by its name', async () => {
  const world = keptWorld();
  const elsewhere = { slots: { start: ['log'], middle: [], end: [] }, shed: [] };
  world.config = { v: 2, layout: { elsewhere } };
  const page = loadKit({ fetch: world.fetch });
  page.kit.container('top', { name: 'one', slots: { start: ['workspace'], end: ['customize'] } });
  page.kit.container(spot(page, 'x'), { name: 'two', slots: { middle: ['queue', 'log'] } });
  await page.settle();
  check("the folder's layouts are asked for once, whatever the containers on the page",
    asked(world.fetch, 'server.config.get') === 1, asked(world.fetch, 'server.config.get'));
  page.click('customize');
  await page.settle();
  check('Customization Mode is the whole page\'s: every container shows it',
    page.within('one').named('one').classList.contains('customizing')
      && page.within('two').named('two').classList.contains('customizing'));
  page.within('two').click('away-log');
  await page.settle();
  check('an edit in one container changes it alone', slotted(page, 'two') === ' | queue | '
    && slotted(page, 'one') === 'workspace |  | customize', `${slotted(page, 'one')} / ${slotted(page, 'two')}`);
  check('...recorded by the one journal, named in the log',
    logged(page).some((t) => t === 'info layout op-1: put Log away'), logged(page));
  const kept = world.kept[0] || {};
  // each with the widgets it has seen (S14b: the owner's ruling on S14k's
  // R2) - the log put away among them; another page's is not touched
  const two = { slots: { start: [], middle: ['queue'], end: [] }, shed: [], known: ['queue', 'log'] };
  const one = { slots: { start: ['workspace'], middle: [], end: ['customize'] }, shed: [], known: ['workspace', 'customize'] };
  check("...and kept whole: each container by its name - and another page's, kept in the folder, carried as it was",
    kept.v === 2 && JSON.stringify(Object.keys(kept.layout || {}).sort()) === '["elsewhere","one","two"]'
      && JSON.stringify(kept.layout.elsewhere) === JSON.stringify(elsewhere)
      && JSON.stringify(kept.layout.one) === JSON.stringify(one) && JSON.stringify(kept.layout.two) === JSON.stringify(two),
    JSON.stringify(kept));
  page.within('one').click('undo');
  await page.settle();
  check("Undo, in either container's banner, undoes the page's last edit", slotted(page, 'two') === ' | queue log | ',
    slotted(page, 'two'));
});

row("a layout kept by step 3's page is read as the bar's", async () => {
  const world = keptWorld();
  world.config = { v: 1, layout: { slots: { left: ['log'], middle: ['queue'], right: ['customize', 'workspace'] }, shed: ['queue', 'workspace', 'log'] } };
  const page = await started({ fetch: world.fetch });
  await barAt(page, 1400);
  check('its left is the start, its right the end', placedIds(page) === 'log | queue | customize workspace',
    placedIds(page));
  check('...its drop order is the bar\'s', JSON.stringify(page.client.store.get().layout.bar.shed)
    === '["queue","workspace","log"]', JSON.stringify(page.client.store.get().layout.bar));
  check('...and nothing is said of it: it is a layout this page reads',
    !logged(page).some((t) => /^warn layout/.test(t)), logged(page));
});

row('a panel opens beside its own container', async () => {
  const page = loadKit({ fetch: contractFetch() });
  page.kit.container('top', { name: 'one', slots: { start: ['workspace'] } });
  page.kit.container('bottom', { name: 'two', slots: { middle: ['queue'] } });
  await page.settle();
  page.within('two').click('queue');
  await page.settle();
  check("the model's progress, clicked: its panel opens in its own container",
    page.within('two').named('queue-list') !== null && page.within('one').named('queue-list') === null);
  page.within('one').click('workspace');
  await page.settle();
  check("...and one panel at a time: the folder's opens in the other, the first shuts",
    page.within('one').named('workspace-path') !== null && page.within('two').named('queue-list') === null);
});

row("the bootstrap's own page is the kit's own calls", async () => {
  const scripts = pageScripts();
  check('its first script is the kit, byte for byte', scripts.length === 2 && scripts[0] === theKit(),
    scripts.map((s) => s.length).join(' '));
  check("...and its own script places what it shows with the kit's calls",
    /llmKit\.container\('top', \{\s+name: 'bar'/.test(scripts[1]) && /llmKit\.place\('tools', /.test(scripts[1]), scripts[1]);
  const page = await started();
  const root = page.within('bar').root;
  check('its bar is a container docked to the top of the window, first in the page',
    root !== null && root.classList.contains('dock-top') && page.document.body.children[0] === root
      && page.named('bar').parentNode === root, root && root.className);
  check('...its instruments fill the rest', page.document.getElementById('app').querySelector('[data-el="strip"]') !== null);
});

row("an application's own page, from another address: two widgets", async () => {
  const fetch = contractFetch();
  const page = loadHtml(path.join(CLIENT, 'example', 'index.html'), { fetch, search: '?server=http://127.0.0.1:29571' });
  await page.settle();
  await tick(page, 1000);
  check('its words and its layout are its own', /An application's own page/.test(page.document.querySelector('h1').textContent));
  check("the model's progress, in a container docked to the bottom edge of the window",
    page.within('progress').root !== null && page.within('progress').root.classList.contains('dock-bottom')
      && slotted(page, 'progress') === ' | queue | ' && page.text('queue-words') === 'writing 120 tok · 39/s',
    page.text('queue-words'));
  check('Bench, inside the element the page gave it', page.document.getElementById('bench')
    .querySelector('[data-el="bench-prompt"]') !== null);
  check('...and nothing else of the kit', page.named('tools') === null && page.named('log-light') === null
    && page.named('workspace') === null && page.document.querySelectorAll('[data-container]').length === 1);
  check('every request goes to the server the page named', fetch.asked.length > 0
    && fetch.asked.every((a) => a.server === 'http://127.0.0.1:29571'), JSON.stringify([...new Set(fetch.asked.map((a) => a.server))]));
  check('nothing failed', page.errors.length === 0, page.errors.join('\n'));
});

// ======================================================= the request check

/* a server holding the three held requests its contract shows - a
   conversation, a transcription, one its backend says nothing more of - and
   keeping every send and cancel it is asked */
function checkWorld(own = {}) {
  const held = ['a conversation built from the job before it, counted, and what the statistics expect',
    'a transcription: the audio, its length, whisper-server\'s own fields',
    'held by a server whose backend says nothing more than the queue knows',
    'a prompt that does not fit, held to be changed: the bootstrap\'s own length, no statistics yet']
    .map((name) => example('server.jobs.held', name).held);
  const world = { held, sends: [], cancels: [] };
  // and a job running beside them, which is not held
  world.jobs = [aJob(), ...held.map((h) => h.job)];
  world.fetch = contractFetch({
    'server.jobs.list': () => ({ status: 200, body: { jobs: world.jobs } }),
    'server.jobs.send': (req) => {
      world.sends.push({ path: req.path, body: req.body });
      const id = req.path.split('/')[3];
      world.jobs = world.jobs.filter((j) => j.id !== id);
      return { status: 200, body: example('server.jobs.send', 'sent as it was built') };
    },
    'server.jobs.cancel': (req) => {
      world.cancels.push(req.path);
      const id = req.path.split('/')[3];
      world.jobs = world.jobs.filter((j) => j.id !== id);
      return { status: 200, body: example('server.jobs.cancel', 'a held request, cancelled: it ends at once') };
    },
    ...own,
  });
  return world;
}

async function checkPlaced(world, before) {
  const page = loadKit({ fetch: world.fetch });
  if (before) before(page);
  const stop = page.kit.place('check', spot(page, 'check'));
  await page.settle();
  return { page, stop };
}

row('the request check: a held request, readable at a glance', async () => {
  const world = checkWorld();
  const { page } = await checkPlaced(world);
  check('placed alone, it follows the jobs and lists the held ones',
    asked(world.fetch, 'server.jobs.list') === 1 && page.text('check-words') === '4 held' && page.named('check-job-j-0010') === null
      && page.named('check-job-j-0012') !== null && page.named('check-job-j-0052') !== null,
    page.text('check-list'));
  check('...and asks the server for the first one, held as it would be sent',
    world.fetch.asked.filter((a) => a.id === 'server.jobs.held').map((a) => a.path).join() === '/api/jobs/j-0012/held',
    JSON.stringify(world.fetch.asked.map((a) => a.path)));
  check('the model, its engine, the job', page.text('check-head') === 'story-12b · llamacpp · part 2 (j-0012)',
    page.text('check-head'));
  check('the count against the model\'s context, and that it fits',
    page.text('check-count') === '1,834 tokens, and room for 900 more, of 8,192 - it fits', page.text('check-count'));
  check('what the statistics expect', page.text('check-expect') === 'about 21.4 s - 9 in 10 within 30.2 s (from 14 answers measured)',
    page.text('check-expect'));
  check('the messages by role, as built', page.text('check-role-0') === 'system' && page.text('check-role-1') === 'user'
    && page.named('check-message-1').value === 'Write part 2 from this plan: the ferry leaves at dawn; Ada is not on it.',
    page.named('check-message-1') && page.named('check-message-1').value);
  check('every parameter, its value, and where it came from',
    page.named('check-param-max_tokens').value === '900' && page.text('check-from-max_tokens') === 'the application'
      && page.named('check-param-top_p').value === '0.95' && page.text('check-from-top_p') === "the model's settings"
      && page.named('check-param-chat_template_kwargs.enable_thinking').value === 'false'
      && page.named('check-param-stop').value === '["## Part 3"]',
    page.text('check-params'));
  check('...Send says it sends it as built', page.text('check-send') === 'Send as built' && !page.named('check-send').disabled,
    page.text('check-send'));
  check('nothing failed', page.errors.length === 0, page.errors.join('\n'));
});

/* is `node` `box` itself, or somewhere inside it? */
function inside(box, node) {
  for (let n = node; n; n = n.parentNode) if (n === box) return true;
  return false;
}

row('the request check: a transcription, a request it cannot say more of, and nothing held', async () => {
  const world = checkWorld();
  const { page } = await checkPlaced(world);
  page.click('check-job-j-0052');
  await page.settle();
  check('a transcription: its audio and its length, whisper-server\'s fields, no messages, no count',
    page.named('check-audio').value === 'D:/work/takes/take-413.wav' && page.text('check-seconds') === ' 7.5 s'
      && page.named('check-fields').value === '{"language":"no","temperature":0}'
      && page.named('check-messages') === null && page.named('check-count') === null,
    page.text('check-held'));
  // R-B2: the audio line ran its length into the next label ("2 sfields"),
  // and the fields box sat above the line: two lines now
  const line = page.named('check-audio-line');
  const hears = page.named('check-hears');
  check('...laid out as two lines: the audio and its length on one, the fields on the next',
    line !== null && line.parentNode === hears && hears.children.length === 2 && hears.children[0] === line
      && inside(line, page.named('check-audio')) && inside(line, page.named('check-seconds'))
      && !inside(line, page.named('check-fields')) && inside(hears.children[1], page.named('check-fields')),
    hears && hears.children.map((c) => c.textContent).join(' | '));
  const css = fs.readFileSync(path.join(CLIENT, 'parts', 'kit.css'), 'utf8');
  check('...the Check tool lays them out, one under the other, the audio line in a row of its own',
    /\.llmk \.check-tool \.hears \{[^}]*display: grid/.test(css) && /\.llmk \.check-tool \.hears \.line \{[^}]*display: flex/.test(css),
    'kit.css has no rule for the Check tool\'s audio lines');
  check('...what its statistics expect, and why it has no count, in the server\'s words',
    page.text('check-expect') === 'about 0.9 s - 9 in 10 within 1.2 s (from 9 transcriptions measured)'
      && page.text('check-said-server') === 'a transcription is not counted in tokens: its length is its audio',
    page.text('check-held'));
  page.click('check-job-j-0014');
  await page.settle();
  check('a request its server says nothing more of: the request itself, as JSON',
    page.named('check-request').value === '{"prompt":"A haiku about rain."}'
      && page.text('check-head') === 'demo-7b · engine not said · a haiku (j-0014)',
    page.text('check-held'));
  page.click('check-job-j-0013');
  await page.settle();
  check('a request that does not fit: said, marked, and held to be changed',
    page.text('check-count') === '3,900 tokens, and room for 600 more, of 4,096 - it does not fit: sent as it is, it fails prompt_too_long'
      && page.named('check-count').getAttribute('class') === 'bad'
      && page.text('check-from-max_tokens') === "the bootstrap's default" && page.named('check-expect') === null,
    page.text('check-held'));
  const empty = checkWorld({ 'server.jobs.list': () => ({ status: 200, body: { jobs: [] } }) });
  const { page: none } = await checkPlaced(empty);
  check('nothing held: it says what would be, asks for nothing, and offers nothing to send',
    none.text('check-words').startsWith('Nothing is held') && asked(empty.fetch, 'server.jobs.held') === 0
      && none.named('check-go').hasAttribute('hidden'),
    none.text('check-words'));
});

row('the request check: sent as built, or with only what changed', async () => {
  let world = checkWorld();
  let { page } = await checkPlaced(world);
  page.click('check-send');
  await page.settle();
  check('as built: an empty body, to the job shown',
    world.sends.length === 1 && world.sends[0].path === '/api/jobs/j-0012/send' && JSON.stringify(world.sends[0].body) === '{}',
    JSON.stringify(world.sends));
  check('...the jobs asked again, and the next held one shown',
    page.text('check-head') === 'whisper-small · whisper · take 413 (j-0052)', page.text('check-head'));
  check('...and the log says so', logged(page).includes('info check j-0012 sent as built'), JSON.stringify(logged(page)));
  check('...and the request just sent is never asked for again: it is no longer held, and the server would refuse it '
    + "not_held (a 409 in the page's console)",
    world.fetch.asked.filter((x) => x.path === '/api/jobs/j-0012/held').length === 1,
    JSON.stringify(world.fetch.asked.map((x) => x.path)));

  world = checkWorld();
  ({ page } = await checkPlaced(world));
  type(page, 'check-param-temperature', '0.9');
  type(page, 'check-param-max_tokens', '900');
  type(page, 'check-param-chat_template_kwargs.enable_thinking', 'true');
  type(page, 'check-message-1', 'Write part 2: Ada is on it.');
  await page.settle();
  check('changed: Send says so', page.text('check-send') === 'Send changed', page.text('check-send'));
  page.click('check-send');
  await page.settle();
  const body = world.sends[0] && world.sends[0].body;
  check('only what differs is sent: the request rebuilt as a conversation, the changed parameters, a '
    + "sub-key under its parent - and a value typed back to what it was is no change",
    JSON.stringify(body) === JSON.stringify({
      request: { messages: [
        { role: 'system', content: "You are the book's editor. Keep its voice: plain, close, no ornament." },
        { role: 'user', content: 'Write part 2: Ada is on it.' }] },
      gen: { temperature: 0.9, chat_template_kwargs: { enable_thinking: true } },
    }), JSON.stringify(body));

  world = checkWorld();
  ({ page } = await checkPlaced(world));
  page.click('check-job-j-0052');
  await page.settle();
  type(page, 'check-fields', '{"language": "en"}');
  await page.settle();
  page.click('check-send');
  await page.settle();
  check('a transcription changed: its audio request, whole',
    JSON.stringify(world.sends[0] && world.sends[0].body) === JSON.stringify({
      request: { audio: 'D:/work/takes/take-413.wav', fields: { language: 'en' } } }),
    JSON.stringify(world.sends));

  world = checkWorld();
  ({ page } = await checkPlaced(world));
  type(page, 'check-param-temperature', 'warm');
  await page.settle();
  check('a value that is not JSON: Send is dark, saying why, and nothing is sent',
    page.named('check-send').disabled && page.text('check-why').startsWith('temperature is a JSON value'),
    page.text('check-why'));
  page.click('check-send');
  await page.settle();
  check('...nothing sent', world.sends.length === 0, JSON.stringify(world.sends));
});

row('the request check: a refusal in the server\'s words; a cancel', async () => {
  const refusal = example('server.jobs.send', 'a length llama-server refuses: refused, never sent');
  let world = checkWorld({ 'server.jobs.send': () => ({ status: 400, body: refusal }) });
  let { page } = await checkPlaced(world);
  type(page, 'check-param-max_tokens', '-2');
  await page.settle();
  page.click('check-send');
  await page.settle();
  check('refused: the server\'s own sentence, where it was asked, and the job still shown',
    page.text('check-said') === refusal.error && page.text('check-head').startsWith('story-12b'),
    page.text('check-said'));
  world = checkWorld();
  ({ page } = await checkPlaced(world));
  page.click('check-cancel');
  await page.settle();
  check('cancelled: the job shown is cancelled, and the next held one shown',
    world.cancels.join() === '/api/jobs/j-0012/cancel' && page.text('check-head').startsWith('whisper-small'),
    `${world.cancels} ${page.text('check-head')}`);
  check('...and the request just cancelled is never asked for again',
    world.fetch.asked.filter((x) => x.path === '/api/jobs/j-0012/held').length === 1,
    JSON.stringify(world.fetch.asked.map((x) => x.path)));
});

row("the request check's seam: an application's own view of its part of the request", async () => {
  const world = checkWorld();
  const seen = { built: [], stopped: 0 };
  const { page, stop } = await checkPlaced(world, (p) => {
    p.kit.requestView('brief', {
      title: 'The brief, by section',
      shows: (held) => held.job.kind === 'write',
      view(node, held, give) {
        seen.built.push(held.job.id);
        const b = p.document.createElement('button');
        b.setAttribute('data-el', 'brief-ferry');
        b.addEventListener('click', () => give({ prompt: 'the ferry, from the brief' }));
        node.appendChild(b);
        return () => { seen.stopped += 1; };
      },
    });
    p.kit.requestView('player', { shows: (held) => held.engine === 'whisper', view: () => () => {} });
  });
  check('a view the application registered is built below the core, for a request it shows',
    JSON.stringify(seen.built) === '["j-0012"]' && page.named('brief-ferry') !== null
      && page.named('check-views').children.map((c) => c.getAttribute('data-view')).join() === 'brief',
    JSON.stringify(seen.built));
  page.named('brief-ferry').dispatchEvent({ type: 'click' });
  await page.settle();
  page.click('check-send');
  await page.settle();
  check('...what it gives back is the request sent',
    JSON.stringify(world.sends[0] && world.sends[0].body) === '{"request":{"prompt":"the ferry, from the brief"}}',
    JSON.stringify(world.sends));
  check('...and it is stopped when the request it shows is no longer shown', seen.stopped === 1, seen.stopped);
  check('another view, for another kind of request, only there',
    page.named('check-views').children.map((c) => c.getAttribute('data-view')).join() === 'player',
    page.text('check-held'));
  stop();
  check('placing a view wrongly throws, saying why', [
    thrown(() => page.kit.requestView('Brief', { view: () => {} })),
    thrown(() => page.kit.requestView('brief', { view: () => {} })),
    thrown(() => page.kit.requestView('score', {})),
  ].join(' | ') === 'llmKit.requestView: a view\'s name is lower-case letters, digits and dashes, not "Brief"'
    + ' | llmKit.requestView("brief", ...): a view of that name is registered already'
    + ' | llmKit.requestView("score", ...): its view is a function (node, held, give) -> stop',
  'the three refusals');
});

// ============================================ S18: scheduled work, as a person sees it

/* the page's own clock, in seconds: an hour from its now is still to come */
const PAGE_NOW_S = 1_800_000_000;

row('scheduled work: the progress widget says when a batch starts and what it waits for', async () => {
  const tonight = example('server.health', "tonight's batch: one job waiting for room on the card, one planned for later - the card held by nobody meanwhile");
  const listedJobs = example('server.jobs.list', "tonight's batch: one job waiting for room on the card, the next planned for its time - both queued, holding back nobody").jobs;
  const planned = { ...listedJobs[1], not_before: PAGE_NOW_S + 3600 };
  let page = await graphic(tonight, [listedJobs[0], planned]);
  const title = page.named('queue').getAttribute('title');
  check('waiting for room: said in words, the numbers on hover in the server\'s words, and the batch planned beside it',
    page.text('queue-words') === 'waits for room'
      && title.startsWith(`Waits for room: ${tonight.queue.scheduled.room}`)
      && title.includes(`1 planned - the first at ${hhmm(tonight.queue.scheduled.next_at)}`)
      && page.named('queue-dot').classList.contains('busy'), `${page.text('queue-words')} / ${title}`);
  page.click('queue');
  await page.settle();
  const rows = [...page.named('queue-list').querySelectorAll('li')];
  const doing = rows.map((li) => li.querySelector('.doing'));
  check('its panel: the job waiting for room, and what it waits on - the numbers, and the words whole on hover',
    doing[0].textContent === 'waits for room: needs 15360 MiB, 6100 MiB free'
      && doing[0].getAttribute('title') === listedJobs[0].progress.waiting_for, doing[0] && doing[0].textContent);
  check('...and the job planned for later: when it starts, as this page tells the time',
    doing[1].textContent === `starts at ${hhmm(PAGE_NOW_S + 3600)}`, doing[1] && doing[1].textContent);
  const next = PAGE_NOW_S + 7200;
  page = await graphic({ queue: { queued: 2, scheduled: { planned: 2, next_at: next, for_room: 0, room: null } },
    runner: { loaded: null, loading: null }, pipeline: { up: [] } });
  check('a batch planned for later, nothing else: when the first starts - and the dot says nothing is under way',
    page.text('queue-words') === `starts at ${hhmm(next)}` && page.named('queue-dot').classList.contains('idle')
      && page.named('queue').getAttribute('title').startsWith(`2 planned - the first at ${hhmm(next)}`),
    `${page.text('queue-words')} / ${page.named('queue').getAttribute('title')}`);
});

row('scheduled work: the session says when a batch starts and what it waits for, and a failed load turns its dot red', async () => {
  const tonight = example('server.health', "tonight's batch: one job waiting for room on the card, one planned for later - the card held by nobody meanwhile");
  let { page } = await sessionOn(tonight);
  check('no session open, a job waiting for room: said, the room on hover in the server\'s words',
    page.text('session-words') === 'waits for room' && page.named('session-dot').classList.contains('busy')
      && page.named('session').getAttribute('title') === `Waits for room: ${tonight.queue.scheduled.room}`,
    `${page.text('session-words')} / ${page.named('session').getAttribute('title')}`);
  const next = PAGE_NOW_S + 3600;
  ({ page } = await sessionOn(health({ queue: { queued: 1, scheduled: { planned: 1, next_at: next, for_room: 0, room: null } },
    pipeline: { id: 'pipe-q2r5t8w3', state: 'closed', why: 'its work is done', since: 1790205600.0, label: null,
      models: [], up: [], class: 'scheduled', keep_s: null, opened_by: 'queue' } })));
  check('no session open, a batch planned for later: when it starts, and nothing wrong',
    page.text('session-words') === `starts at ${hhmm(next)}` && page.named('session-dot').classList.contains('idle'),
    page.text('session-words'));
  const opened = example('server.pipeline.open', 'scheduled work that waits for room: the pipeline waiting, its why the numbers it waits on').pipeline;
  ({ page } = await sessionOn(health({ pipeline: opened })));
  check('a scheduled session waiting for room: said, and the numbers on hover, whole',
    page.text('session-words') === 'waits for room'
      && page.named('session').getAttribute('title').endsWith(`: ${opened.why}`),
    `${page.text('session-words')} / ${page.named('session').getAttribute('title')}`);
  const why = 'writer-24b could not be loaded: not enough free VRAM for writer-24b';
  ({ page } = await sessionOn(health({ pipeline: { id: 'pipe-d4h6j9s3', state: 'holding', why, since: 1790170200.0,
    label: 'desk', models: ['writer-24b'], up: [], class: 'responsive', keep_s: 0, opened_by: 'application' } })));
  check("a model it keeps whose load failed: the dot is red, and the words say it - never on hover alone (wave 8's register)",
    page.named('session-dot').classList.contains('bad') && page.text('session-words') === 'holding the card · a load failed',
    `${page.named('session-dot').className} / ${page.text('session-words')}`);
  ({ page } = await sessionOn(health({ pipeline: { id: 'pipe-d4h6j9s3', state: 'holding', why, since: 1790170200.0,
    label: 'desk', models: ['writer-24b'], up: ['writer-24b'], class: 'responsive', keep_s: 0, opened_by: 'application' } })));
  check('...and once that model is up again, the dot is green: its why is history, on hover',
    page.named('session-dot').classList.contains('idle') && page.text('session-words') === 'holding the card',
    `${page.named('session-dot').className} / ${page.text('session-words')}`);
});

// ==================================================================== run

// An error nobody could catch - thrown in a turn the page gave up, like a
// view's repaint - is a row that failed, never a suite that died without its
// tally: a crash is not a verdict.
process.on('uncaughtException', (e) => {
  check('nothing on the page threw where nothing could catch it', false, (e && e.stack) || e);
});
process.on('unhandledRejection', (e) => {
  check('no promise of the page failed where nothing could catch it', false, (e && e.stack) || e);
});

(async () => {
  for (const r of rows) {
    try {
      await r.fn();
    } catch (e) {
      check(`the ${r.name} rows ran to their end`, false, (e && e.stack) || e);
    }
  }
  const width = Math.max(...results.map((r) => r.name.length));
  for (const r of results) {
    console.log(`${r.ok ? 'PASS' : 'FAIL'}  ${r.name.padEnd(width)}${r.ok ? '' : `   ${r.detail}`}`);
  }
  const failed = results.filter((r) => !r.ok).length;
  console.log('-'.repeat(60));
  console.log(`${results.length - failed} passed, ${failed} failed, ${results.length} total`);
  process.exitCode = failed ? 1 : 0;
})();

/* Feeds: what the page keeps asking the server - only while something shown
   needs it.

   follow(id) -> stop
       A view that shows a feed follows it for as long as the view is built.
       The feed is asked every `every` ms while at least one view follows it
       AND the browser is showing the page; when the last view stops, the
       asking stops. A hidden thing costs nothing.

   The answer goes into the store, `feeds.<id>` = {answer, status, at, ok}:
   `ok` is null before the first answer, true for a 200, false for a refusal
   or no answer. A feed that stops answering says so in the log ONCE, and
   once when it answers again - never a line a second.

   ONE ASK AT A TIME: a feed never has two requests out - `ask`, the one way
   in, refuses while one is.

   refresh(id)
       An action changed what a followed feed says: it is asked now, not at
       its next tick - or, if it is asking already, again the moment that
       answer is in, since the answer on its way may be from before. */

const FEEDS = {
  health: { route: 'server.health', every: 1000, into: healthInto },
  jobs: { route: 'server.jobs.list', every: 1000 },
  // every model's file and header - read by the server once per file, so
  // cheap to ask, but it changes only when a model loads or is measured
  models: { route: 'server.models.list', every: 5000 },
};

SEEDS.feeds = Object.fromEntries(Object.keys(FEEDS).map((id) =>
  [id, { answer: null, status: null, at: null, ok: null }]));

/* the feeds' clocks are the page's running machinery, not its state */
const FOLLOWING = Object.fromEntries(Object.keys(FEEDS).map((id) =>
  [id, { n: 0, timer: null, asking: false, failing: false, again: false }]));

function follow(id) {
  const f = FOLLOWING[id];
  if (!f) throw new Error(`no feed called ${id}`);
  f.n += 1;
  if (f.n === 1 && f.timer === null) ask(id);
  let stopped = false;
  return () => {
    if (stopped) return;
    stopped = true;
    f.n -= 1;
    if (f.n === 0 && f.timer !== null) {
      clearTimeout(f.timer);
      f.timer = null;
    }
  };
}

function pageShown() {
  return document.visibilityState !== 'hidden';
}

async function ask(id) {
  const f = FOLLOWING[id];
  const feed = FEEDS[id];
  f.timer = null;
  if (!f.n || !pageShown() || f.asking) return;
  f.asking = true;
  let got = null;
  let fault = null;
  try {
    got = await api(feed.route, { quiet: true });
  } catch (e) {
    fault = e;
  }
  f.asking = false;
  if (fault) {
    store.set((s) => update(s, ['feeds', id], (was) => ({ ...was, ok: false, at: Date.now() })));
    if (!f.failing) {
      f.failing = true;
      logLine('error', 'api', `${feed.route} stopped answering: ${fault.message}`);
    }
  } else {
    if (f.failing) {
      f.failing = false;
      logLine('info', 'api', `${feed.route} answers again`);
    }
    store.set((s) => {
      const next = update(s, ['feeds', id], () => ({
        answer: got.answer, status: got.status, at: Date.now(), ok: got.status === 200,
      }));
      return feed.into ? feed.into(next, got) : next;
    });
  }
  if (f.n && f.timer === null && pageShown()) f.timer = setTimeout(() => ask(id), f.again ? 0 : feed.every);
  f.again = false;
}

function refresh(id) {
  const f = FOLLOWING[id];
  if (!f) throw new Error(`no feed called ${id}`);
  if (f.asking) {
    f.again = true;
    return;
  }
  if (f.timer !== null) {
    clearTimeout(f.timer);
    f.timer = null;
  }
  ask(id);
}

/* the page shown again: every feed that is followed is asked at once */
document.addEventListener('visibilitychange', () => {
  if (!pageShown()) return;
  for (const id of Object.keys(FEEDS)) {
    const f = FOLLOWING[id];
    if (f.n && f.timer === null) ask(id);
  }
});

/* Health says which folder the server works in: the picker follows it, so
   a folder bound or let go by anybody else shows here too - unless a bind
   or an unload of this page's own is under way, which will say it. */
function healthInto(state, got) {
  if (got.status !== 200 || state.workspace.asking) return state;
  const { loaded, workspace } = got.answer;
  if (state.workspace.loaded === loaded && state.workspace.path === workspace) return state;
  return update(state, ['workspace'], (w) => ({ ...w, loaded, path: workspace }));
}

/* The one store: the whole state of the page, in one value.

   store.get()                  the state now. Frozen: it is read, never written.
   store.set(fn)                the next state is fn(state) - a new value, built
                                from the old; the old is never changed in place.
   store.replace(state)         the whole state at once (the journal's rebuild).
   store.watch(select, fn)      fn(select(state)) now, and again whenever what
                                `select` returns has changed. -> stop watching.

   THIS IS HOW THE PAGE REPAINTS: nothing calls a render. A view is a watcher -
   `select` is its view model (state in, view data out, pure) and `fn` paints
   what it is given and decides nothing. A change reaches exactly the views
   whose view data changed: "changed" is compared by value, so a state change
   that leaves a view's data alone repaints nothing.

   Changes are told at the end of the turn, once: three `set`s in a row are
   one repaint of each view they touched. A view that throws is reported, and
   the others are still told. */

function createStore(initial, report) {
  let state = deepFreeze(initial);
  let pending = false;
  const watchers = new Set();
  const fail = report || ((e) => { throw e; });

  function tell() {
    pending = false;
    for (const w of [...watchers]) {
      if (!watchers.has(w)) continue;           // stopped by a view told before it
      let next;
      try {
        next = w.select(state);
      } catch (e) {
        fail(e, w.name);
        continue;
      }
      if (same(next, w.last)) continue;
      w.last = next;
      try {
        w.fn(next);
      } catch (e) {
        fail(e, w.name);
      }
    }
  }

  function changed(next) {
    if (next === state) return;
    state = deepFreeze(next);
    if (!pending) {
      pending = true;
      queueMicrotask(tell);
    }
  }

  return {
    get: () => state,
    set: (fn) => changed(fn(state)),
    replace: (next) => changed(next),
    watch(select, fn, name) {
      const w = { select, fn, name: name || select.name || 'a view', last: undefined };
      watchers.add(w);
      w.last = select(state);
      fn(w.last);
      return () => watchers.delete(w);
    },
  };
}

/* Two view data are the same when they hold the same values: JSON-like data -
   objects, arrays, strings, numbers, booleans, null - compared all the way
   down. A function or a node in view data is compared by identity. */
function same(a, b) {
  if (a === b) return true;
  if (typeof a !== 'object' || typeof b !== 'object' || a === null || b === null) {
    return Number.isNaN(a) && Number.isNaN(b);
  }
  if (Array.isArray(a) !== Array.isArray(b)) return false;
  const ka = Object.keys(a);
  const kb = Object.keys(b);
  if (ka.length !== kb.length) return false;
  return ka.every((k) => Object.prototype.hasOwnProperty.call(b, k) && same(a[k], b[k]));
}

function deepFreeze(value) {
  if (value && typeof value === 'object' && !Object.isFrozen(value)) {
    Object.freeze(value);
    for (const v of Object.values(value)) deepFreeze(v);
  }
  return value;
}

/* A copy of `obj` with the value at `path` (a list of keys) replaced by
   fn(the value there) - the one way a part builds the next state. */
function update(obj, path, fn) {
  if (!path.length) return fn(obj);
  const [key, ...rest] = path;
  const here = obj == null ? undefined : obj[key];
  const next = update(here, rest, fn);
  if (Array.isArray(obj)) {
    const copy = obj.slice();
    copy[key] = next;
    return copy;
  }
  return { ...(obj || {}), [key]: next };
}

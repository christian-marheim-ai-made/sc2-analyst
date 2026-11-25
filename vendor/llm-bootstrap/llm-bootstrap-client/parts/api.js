/* One door to the server.

   api(id, {params, query, body, quiet}) -> {status, answer}

   `id` is the route's id in the contract (`server.jobs.submit`), never a path:
   the path, the method and every answer's shape come from the contract, built
   into this page. A body the route refuses - or any body, for a route that
   takes none - is a bug on THIS side and throws at once, before anything is
   sent.

   EVERY ANSWER IS CHECKED against the shape the contract declares for its
   status. An answer the contract does not declare - an unknown status, a
   field missing, a reason not in the list - is NO ANSWER: `NoAnswer` is
   thrown, as for a server that did not answer at all. A refusal the contract
   does declare (409 busy, 400 bad_request) is an answer: it is returned, and
   the caller shows its `error` sentence and branches on its `reason`.

   WHERE IT ASKS. By default, the page's own address: a path alone, asked of
   the server the page came from - the bootstrap server serving its own page.
   A page of an application's own that shows these widgets names its bootstrap
   server in a script that runs before the page's own,

     window.llmServer = 'http://127.0.0.1:7072';

   and every request goes there. That server must be started to allow the
   page's own address (`server.py --allow-origin http://127.0.0.1:7867`), and
   it allows no other page. An address that is not one - `http://<host>:<port>`
   and nothing after it - is never guessed around, mended, or replaced by the
   page's own: nothing is asked of anybody, and every ask is no answer,
   saying why. */

class NoAnswer extends Error {}

const SERVER = serverAt(globalThis.llmServer);

/* -> {base, wrong}: `base` goes before every path - '' for the page's own
   address; `wrong` says why an address named is not one */
function serverAt(named) {
  if (named === undefined || named === null) return { base: '', wrong: null };
  const at = typeof named === 'string' ? /^(https?:\/\/[^/?#@\s]+)\/?$/i.exec(named) : null;
  if (!at) {
    return {
      base: null,
      wrong: `the page names its server as ${JSON.stringify(named)}, which is not a server's address: `
        + 'one is written http://<host>:<port>',
    };
  }
  return { base: at[1], wrong: null };
}

function routePath(route, params) {
  return route.path.replace(/\{(\w+)\}/g, (_, key) => {
    if (params[key] === undefined) throw new Error(`${route.id || route.path}: no ${key} given`);
    return encodeURIComponent(String(params[key]));
  });
}

async function api(id, { params = {}, query = null, body, quiet = false } = {}) {
  const route = ROUTES[id];
  if (!route) throw new Error(`the contract has no route called ${id}`);
  if (route.request === null && body !== undefined) throw new Error(`${id} takes no body`);
  if (route.request !== null && body !== undefined) {
    const wrong = llmContract.check(body, route.request, SHAPES);
    if (wrong.length) throw new Error(`${id}: not a body the contract allows: ${wrong[0]}`);
  }
  let path = routePath(route, params);
  const q = Object.entries(query || {}).filter(([, v]) => v !== null && v !== undefined);
  if (q.length) path += '?' + q.map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`).join('&');
  if (SERVER.wrong) throw noAnswer(SERVER.wrong, undefined, quiet);
  let resp;
  try {
    resp = await fetch(SERVER.base + path, {
      method: route.method,
      headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (e) {
    const who = SERVER.base ? `the server at ${SERVER.base}` : 'the server';
    throw noAnswer(`${who} did not answer ${id}: ${e.message}`, undefined, quiet);
  }
  let answer;
  try {
    answer = JSON.parse(await resp.text());
  } catch (e) {
    answer = undefined;
  }
  const shape = route.responses[String(resp.status)];
  const wrong = shape === undefined ? ['no shape is declared for it']
    : llmContract.check(answer, shape, SHAPES);
  if (wrong.length) {
    throw noAnswer(`${id} answered ${resp.status}, which the contract does not declare: ${wrong[0]}`,
      JSON.stringify(answer), quiet);
  }
  return { status: resp.status, answer };
}

/* No answer is said in the log, once, by the door itself - a caller that
   shows it too says it to the person, not to the log again. A caller that
   asks again and again (a feed) asks quietly, and says it itself, once per
   silence rather than once per ask. */
function noAnswer(text, detail, quiet) {
  if (!quiet) logLine('error', 'api', text, { detail });
  return new NoAnswer(text);
}

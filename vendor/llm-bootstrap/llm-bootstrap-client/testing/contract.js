/* The contract's examples, as the server the client's rows talk to - there is
   no second, hand-written server for the client to trust. A request is
   answered with an example of the route it asks: the one whose path and body
   are the request's own, else the route's first 200.

     contractFetch({[routeId]: (req) => ({status, body})})   answer one route
                                                              the row's own way
     fetch.asked      every request, in order: {id, method, path, body, server} -
                      `server` the address it was sent to ('' for a path alone:
                      the page's own address), `path` what follows it

   A request no route of the contract takes is answered 404 no_route, as the
   real server answers it. Whatever address a request names, this answers it:
   where the page asks is the row's to read, in `asked`. */
'use strict';

const fs = require('fs');
const path = require('path');

const CONTRACT = path.resolve(__dirname, '..', '..', 'llm-bootstrap-contract');

function serverRoutes() {
  const dir = path.join(CONTRACT, 'routes', 'server');
  return fs.readdirSync(dir).filter((f) => f.endsWith('.json')).sort()
    .map((f) => JSON.parse(fs.readFileSync(path.join(dir, f), 'utf8')));
}

function pattern(route) {
  const re = route.path.replace(/[.*+?^$()|[\]\\]/g, '\\$&').replace(/\\?\{\w+\\?\}|\{\w+\}/g, '[^/]+');
  return new RegExp(`^${re}$`);
}

function same(a, b) { return JSON.stringify(a) === JSON.stringify(b); }

function contractFetch(own = {}) {
  const routes = serverRoutes();
  const asked = [];
  const fetch = async (url, opts = {}) => {
    const method = (opts.method || 'GET').toUpperCase();
    const at = /^(https?:\/\/[^/?#]+)(.*)$/i.exec(String(url));
    const [server, rest] = at ? [at[1], at[2]] : ['', String(url)];
    const [p] = rest.split('?');
    const body = opts.body === undefined ? undefined : JSON.parse(opts.body);
    const route = routes.find((r) => r.method === method && pattern(r).test(p));
    asked.push({ id: route ? route.id : null, method, path: rest, body, server });
    let status;
    let answer;
    if (!route) {
      status = 404;
      answer = { error: `no route ${p}`, reason: 'no_route' };
    } else if (own[route.id]) {
      ({ status, body: answer } = await own[route.id]({ method, path: rest, body, server }));
    } else {
      const ex = route.examples.find((e) => (e.path === undefined || e.path === p)
          && (route.request === null || same(e.request, body)))
        || route.examples.find((e) => e.status === 200);
      status = ex.status;
      answer = ex.response;
    }
    const raw = answer === undefined ? '' : JSON.stringify(answer);
    return { status, ok: status < 400, text: async () => raw, json: async () => JSON.parse(raw) };
  };
  fetch.asked = asked;
  return fetch;
}

module.exports = { contractFetch, serverRoutes };

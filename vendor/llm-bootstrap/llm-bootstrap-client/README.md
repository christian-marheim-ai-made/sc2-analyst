# llm-bootstrap-client

The widgets: a kit an application's page includes - one script, one call per
widget - and the bootstrap's own page, made with the same calls and served by
the server. Both speak the contract.

    python build.py               build llm-bootstrap-kit.js and index.html from parts/ (MANIFEST order)
    python build.py --check       is each what the parts build?
    python test_client.py         the rows - on the built kit and page themselves, in node, no packages
    python ../llm-bootstrap-server/server.py --backend mock --client .
                                  serve the page at http://127.0.0.1:7072/ with the stand-in model
    python ../llm-bootstrap-server/server.py --backend mock --allow-origin http://127.0.0.1:7867
    python -m http.server 7867 --bind 127.0.0.1
                                  the example page (example/) at http://127.0.0.1:7867/example/,
                                  asking the stand-in model from another address

## What is here

| file | what it is |
| --- | --- |
| `parts/` | the source, one concern a file; `MANIFEST` names the two files built and, under each, the order its parts are put together - the order their scripts run |
| `build.py` | joins the parts into the kit and the page. No bundler, no package: the same parts always build the same bytes |
| `llm-bootstrap-kit.js` | the kit. Built, and committed |
| `index.html` | the bootstrap's own page: the kit, byte for byte, and one script of kit calls. Built, and committed: a change that should change nothing can be proven by its bytes |
| `example/index.html` | an application's own page, from another address: its own words and look, and two widgets placed with the kit |
| `test_client.js` | the rows, run on the built kit's and page's own scripts |
| `testing/` | what the rows run them in: a small DOM (`dom.js`), the page's sandbox and clock (`page.js`), and the contract's examples as the server (`contract.js`) |

## Placing a widget in your page

    <script>window.llmServer = 'http://127.0.0.1:7072';</script>
    <script src="llm-bootstrap-kit.js"></script>
    <script>
      llmKit.place('bench', document.getElementById('bench'));
      llmKit.container('bottom', { name: 'progress', slots: { middle: ['queue'] } });
    </script>

- **The server's address is said once,** before the kit. The server must
  allow your page's address (`server.py --allow-origin`), and it allows no
  other.
- **Your page serves the kit, from the llm-bootstrap your server runs
  from** - its file, `llm-bootstrap-client/llm-bootstrap-kit.js`, by its
  path, never a copy kept elsewhere. The kit carries the contract it checks
  every answer against: a copy older than the server checks the server's
  answers against a contract it no longer speaks, and a changed answer is no
  answer. The bootstrap server does not serve it - see *Why the server does
  not serve the kit*, below.
- **`llmKit.place(name, element)`** puts one widget at the end of an element
  of your page: `session` (the card your pipeline holds, and the models in
  it), `queue` (the model's progress - several answering at once on
  different models are counted, `2 answering`, each named on hover and in
  its panel, the fill the one furthest along - and how many requests wait for your
  check, in its words and its own dot, its panel saying where to check each:
  in a `check` placed on the page, behind the `tools` door, or that the page
  shows no Check), `log` (the log's light), `workspace`
  (the folder picker), `customize`, `bench`, `models`, `check` (the request
  check: each held request, scanned and tuned, then sent or cancelled), or
  `tools` (the instruments behind one door). It returns what takes it out.
- **`llmKit.requestView(name, {title, shows, view})`** is the request
  check's seam for your own data (PLAN-4 D16): a view of your part of a held
  request - a brief by section, a recording with a player - built in the
  Check widget for each held request `shows(held)` says yes to, handed the
  held request as the server says it. It is read-only, or gives back a
  changed request with `give(request)`, which is then what Send sends.
- **`llmKit.container(where, {name, slots, shed, axis})`** holds widgets
  that belong together, side by side in its slots - `start`, `middle`, `end`
  - docked to an edge of the window (`top`, `bottom`, `left`, `right`) or in
  an element of your page. Each widget sits in the box it declares; when the
  room runs short, widgets leave for the container's tray in the drop order
  (`shed`), and come back as it grows. Customize moves them between slots,
  or to the tray and back, every move undoable and kept in the bound folder
  under the container's `name`.
- **A call the kit cannot do as asked throws at once,** saying why, before
  anything is placed.
- **Loaded and not used, the kit costs your page nothing:** it asks nothing
  and puts nothing in the page until the first thing is placed. Its names are
  its own (`llmKit` and `llmClient` are all it gives the page), and its look
  reaches only what it builds. Every colour of that look is a `--llmk-*`
  name the page may set (GUIDE.md, *Build an application, start to
  finish*, step 8, lists them). The widgets are nodes of the page all the
  same: a bare rule or a page-wide query of the page's reaches inside them,
  so the page scopes its own to its own elements.

## Why the server does not serve the kit

Decided in S14b (S14k's register R4): the kit is your page's to
serve, and no route of the bootstrap server serves it.

- **Your page must load when the server is not up yet.** Served by your own
  server, the kit loads, its widgets say *no answer*, and they come alive
  when the bootstrap server answers. Loaded from the bootstrap server, a page
  opened a moment before it listens has no `llmKit` at all, and every call
  your page makes to it fails.
- **The contract declares JSON answers.** Every route is declared in it,
  with its shapes and examples, before the server answers it; a script is
  not a JSON answer, and the one page the server serves (`--client`, its own)
  is outside the routes on purpose. A second such answer would stretch the
  contract's language for a convenience.
- **The bootstrap gives the widgets, the API and the stack under them** -
  *"It is not a webpage bootstrapper, it is a LLM tooling bootstrapper"* -
  and your page, and what it serves, are yours.

The one risk of serving it yourself is a copy older than the server: serve
the bootstrap's own file, by its path, and the kit and the server are the
same contract. Health says the server's contract version
(`contract_version`); a kit that compared its own with it would say a stale
copy by name - a seam, not built.

## How the kit is made

**One store** (`store.js`) holds the whole state, and nothing else does.
**State is replaced, never changed in place**: it is frozen, and a part that
tries to change it is stopped by the language.

**Nothing calls a render.** A view is a watcher of the store:
- **its view model** (`select`) takes the state and gives back what the view
  shows: pure, and deciding everything;
- **its painter** takes that and builds the DOM, deciding nothing.

A view is repainted when what its view model gives back has changed, compared
by value, and only then. Changes are told once, at the end of the turn.

**One door to the server** (`api.js`).
- Routes are named by their contract id; their paths and every answer's shape
  are the contract's, built into the kit.
- An answer the contract does not declare is no answer at all.
- A body the route would refuse is a bug here, and is never sent.
- It asks the page's own address, or the one the page names
  (`window.llmServer`). An address that is not one is never guessed around:
  nothing is asked, and every ask says why.

**A hidden thing costs nothing.** What is not placed, or not shown, is
stopped: its watchers are gone, and nothing is fetched for it. A feed is
asked only while a view shown follows it and the browser shows the page.

**Every control has a name,** its `data-el`. The rows click it by that name,
and so will anything that walks the page.

## What a person sees on the bootstrap's own page

- **The bar,** a container docked to the top: the folder bound (and its
  picker), the model's progress (the card's state - idle, it names the model
  up, or says how many when several are side by side, every one on hover -
  the waiting jobs, the running job's progress), the log's light, and
  Customize. What does not fit
  is shed into the tray, in the drop order.
- **The instruments** sit behind one *Tools* menu: Bench (a raw request, as
  a queue job - for a whisper model, a transcription: a WAV file's path and
  the fields whisper-server reads, its answer shown whole), Models (the
  session the models are in, and each model's engine, where it stands -
  pinned or on demand - its file, its header as its engine reads it, its
  problems, load history, its statistics - load (from the asking to
  ready), first token, tokens a second, for a transcriber its real-time
  factor: the median, the 90th percentile and the last, each with its unit
  and how many samples - and its answers far slower than its normal, as the
  server flags them - and a disk measure), and the Log. The page opens on
  Bench, the first. An application's own screens have no place here:
  they are its own page.
- **The session** (`session`, a widget with a box) is an application's to
  place; the bootstrap's own bar keeps its four. There, the model's progress
  says a degraded card, or one taken back, in the server's words, and the
  Models tool says the session whole at its head. A model the session keeps
  whose load failed turns its dot red, and its words say so.
- **Scheduled work** (S18): the progress widget and the session say when a
  planned batch starts - `starts at 23:00`, this page's own hour - and that
  scheduled work waits for room, its numbers on hover in the server's words
  (health's `queue.scheduled`); the progress widget's panel says each queued
  job's. A batch planned for later is not work under way: the dot stays
  quiet for it.
- **A widget your page adds later** to a container whose layout a folder
  kept appears where your page puts it, never in the tray: only a widget
  the person put away stays there (the owner's ruling of 2026-09-27).
- **Customization Mode:** widgets dragged between slots and the tray, and
  every edit undoable; the layout is kept in the bound folder.

**How to add a widget, the client's rules and the traps it has paid for**
are in the guide: [`../GUIDE.md`](../GUIDE.md), *The client*.
`parts/registry.js` says what a part can register; `parts/kit.js` says the
kit's calls whole.

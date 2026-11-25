# llm-bootstrap-contract

Every route the server and the coordinator answer, the shape of every answer,
and real examples of each. The server and the client are both verified against
it, so they cannot drift apart without a test saying so.

    python validate.py          check the contract is well-formed
    python test_contract.py     the full self-test (needs `node` on the path)

## What is in it

    shapes/<name>.json              a shape used by more than one route - defined once -
                                    or one no route's shape reaches (conversation.json,
                                    audio_request.json)
    routes/<part>/<name>.json       one route, one example per line
    origins/<part>.json             what a part answers before any route: for the
                                    name a request was sent to (its Host), the page
                                    that sent it (its Origin) and its body's size
    machine/<name>.json             what the parts share on the machine, outside
                                    HTTP: the card lock - its file, the note its
                                    holder writes beside it, examples of each note
    validate.py                     the Python validator, and the contract's own checks
    validate.js                     the same validator for the browser
    ids.py                          the id grammar every part mints and reads
    ids.js                          the same grammar for the browser
    memory.py                       the ONE memory check: does a need fit on the card,
                                    beside a margin - run by the coordinator and by a
                                    server run on its own alike; and the amend's wait,
                                    held together on both sides
    test_contract.py                the contract testing itself

A route file:

```json
{
  "id": "server.jobs.claim",
  "method": "POST",
  "path": "/api/jobs/{id}/claim",
  "summary": "what it does, for a person",
  "request": null,
  "responses": {"200": <shape>, "409": <shape>, "404": <shape>},
  "examples": [
    {"name": "claimed", "status": 200, "path": "/api/jobs/j-0007/claim", "response": {...}}
  ]
}
```

The shape language is documented once, at the top of `validate.py`. It is
small on purpose: types, unions, enums, arrays, objects (closed unless
`"open": true`), and `{"ref": "name"}` for the named shapes.

## The ids

`ids.py` and `ids.js` mint and read every id any part uses, and must agree
character for character (`test_contract.py` runs both over one table). **An id
names, it never routes**: a reply always comes back on the connection that
asked. What an id does is name the thing asked about LATER - a job to poll, a
generation to ask about after a restart, a ticket to wait on, a request to
trace through every part. Two actors that could mint the same name could act
on each other's things, so every id that can reach a second actor is unique
beyond the one that minted it. A ticket's id names it and never proves who
holds it: a ticket is acted on only with its secret token.

| kind | looks like | what it names |
| --- | --- | --- |
| `actor` | `srv-k3x9m2p7` | a running thing, minted when it starts: a server (`srv`), a client session (`cli`), a coordinator (`crd`), one load of a model (`load`) |
| `correlation` | `c-20260923-140012-a3k9m2p7` | one request, minted by the client that starts it |
| `job` | `j-0007` | one job, numbered inside its folder |
| `handle` | `srv-k3x9m2p7:j-0007#01` | one generation, named by the server that ran it |
| `ticket` | `t-0042-k3x9` | one ticket for the card - an application's place in the coordinator's line, and its rental once claimed; the tail means a coordinator that lost its record cannot repeat an old ticket's id. The turns that tickets replaced had the same grammar, so an old record's turn ids read as ticket ids and one counter runs on |
| `pipeline` | `pipe-k3x9m2p7` | one pipeline of a server: the card held from its open to its close, over as many tickets as that takes; renewed and closed by this id. It is no actor, and holds nothing in its own name |

The random part is drawn from 32 letters no one misreads (no `l`, `o`, `0` or
`1`). Digits are written `[0-9]`, never `\d`, which in Python also matches
other scripts' digits and in JavaScript does not.

A shape declares an id as `{"id": "<kind>"}`, so every suite in every part
checks every id it answers with.

## The rules the contract keeps about itself

- **A refusal is never a success.** Every status of 400 and above answers with
  `error` - a sentence for a person - and `reason` - one value from a closed
  list, for a program. A program branches on `reason`; it never parses `error`.
- **No success carries an `error` field.** A request that did not do what was
  asked answers with a refusal status, not with `{"ok": false}` inside a 200.
- **Every status and every refusal reason has an example.** The examples are
  the client's fixtures: if a reason has no example, the client has no test
  for handling it.
- **Nothing is defined twice.** A shape used by two routes is a named shape.
  A refusal's shape is the exception: each route declares its own, because
  each route's reasons are a closed list of its own - even where two lists
  happen to match today.
- **Examples follow their route's path**, so `/api/jobs/j-0007/claim` is only
  an example of `/api/jobs/{id}/claim`.
- **A `bad_request` example shows a body being refused**, so its request must
  be one its route's shape refuses, and its `error` must be the first problem
  exactly as the validator words it - which is what a server answers.
  `bad_request` is declared only by a route that takes a body.
- **Any other refusal example may lack a field** - that can be the very reason
  it was refused - but what it does carry must be of the right kinds.

Each of these rules is also shown a route that breaks it (`META_TRAPS` in
`test_contract.py`): a rule nobody ever shows a broken route could be
deleted and every example would still pass.

What a part answers before any route (`origins/<part>.json`) keeps the same
rules, and two of its own: it declares only refusals - a request let through
is answered by its route - and each example says what of the request it
answers: the `origin` it came from, the `host` it was sent to, its body's
`length` - one of them at least, each of its own kind (a string, a string, a
whole number of bytes), and nothing else. Its file has no method, path or
request: the check comes before any of them is looked at. `ORIGIN_TRAPS`
shows it files that break its rules.

## How a side depends on it

**The server** validates every response it produces in its own tests:
`validate.check(body, route["responses"][str(status)], shapes)` must return
`[]`. Every answer a route declares - each status, each refusal reason - must
be provoked from the real server at least once, and so must every answer it
gives before any route (`origins/server.json`). The examples that refuse a
body are replayed and must be answered word for word, and so are the examples
of what it answers before any route - their Origin, Host and length sent as a
browser would send them. The server also READS its route table from here, and
will not start if a route is declared that it does not serve.

**The coordinator** is held to the same: `test_coordinator.py` checks every
answer against its route's shape, provokes every answer the coordinator's
routes declare at least once - and every answer it gives before any route
(`origins/coordinator.json`) - and replays their `bad_request` examples, and
the examples of what it answers before any route, over the wire, to be
answered word for word. The server's client of the coordinator
(`coordination.py`) checks every body before it goes and every answer as it
comes.

**The client** uses the examples **as its fixtures**: a test that needs "what
the server says when a job cannot be claimed because it is not done" reads
`routes/server/job-claim.json` and takes that example. There is no second,
hand-written server for the client's tests to trust.

**Both validators must agree.** `test_contract.py` runs every case - every
example, every mutation of it that must fail, and a table of known traps -
through `validate.py` and `validate.js`, and fails on any difference in the
problems they report. Two validators that disagree are two contracts.

## Protocol notes

- Times on the wire are seconds since the epoch, as numbers.
- A POST body that is not a JSON object is read as an empty one, so the
  route's own refusal for the missing field answers it.
- A body missing a field its route refuses by name (`kind`, `gen.model`,
  `path`) is answered by that refusal. Any other way a body does not match its
  route's request shape - a field of the wrong kind, a value outside an enum,
  a field the route does not take - is `400 bad_request`, naming the first
  problem.
- `"integer"` means a number with no fractional part: JSON cannot tell `3`
  from `3.0`, so neither does the contract.
- `GET /api/jobs?scope=<key>` narrows the list, and `?client=<actor id>`
  narrows it to one client's jobs; a query is not part of a route's path, so
  it is described in the route's summary. Each is given once or not at all,
  and a client is an actor id: a client that is not one, or one given twice,
  is `400 bad_client`, and a scope given twice `400 bad_scope` - refused before
  anything is asked of the queue, as a malformed question is malformed whether
  or not a folder is bound. A value left blank (`?client=`) counts as not
  given.
- `GET /api/jobs/{id}/result?wait_s=<seconds>` holds its answer until the job
  has ended or that many seconds have passed - a plain number from 0 to 60 -
  and then answers exactly as it would have without the wait: an application
  waits for its result without asking again and again. A job that has ended,
  or is not there, is answered at once, and so is every wait when the server
  stops. Any other `wait_s` is `400 bad_wait`, the route's own refusal: a
  query is not a body, so it is never `bad_request`.
- Health carries `contract_version`: `contract_version()` in `validate.py`,
  the first 12 hex digits of a sha256 over the contract's routes, shapes, id
  grammar and validators - each by its path in the contract and its bytes.
  Two sides that run the same contract agree on it wherever it lies; its
  README and its self-test are words about it, not in it.
- A job submitted with `check: true` is held once its request is built
  (PLAN-4 D16): status `held`, its `check` saying `marked`, `held`, then
  `sent` or `tuned` - a job not marked has no `check`. `server.jobs.held`
  answers the `held_request` shape, and `server.jobs.send` sends it as built
  or changed; its `gen` is the submit's less the queue's own three, and
  `test_contract.py` holds the two lists together.
- Every job route answers `409 no_workspace` with nothing bound and
  `503 store_unreadable` when the job store cannot be read - never an empty
  answer standing in for one it could not read.
- A running job's `progress` says how far it has got: this is `variant` of
  `of`, `tokens` of it so far, against `budget` - the most the whole job can
  take, its `max_tokens` for each variant; and its `stage` (`loading the
  model`, `waiting for the card` with `waiting_for`, `waiting for the card's
  memory` with `waiting_for` - the card full, in words a person acts on -
  and its numbers, `free_mib`, `need_mib`, `outside_mib` (what programs
  outside the bootstrap use), `waited_s` and `patience_left_s` (S5b: the job
  held until the card has room, and run again by itself), `thinking`,
  `writing` with `tok_s`, `answering` with `due_s`, `done`). A model that answers in
  one piece - a transcription (PLAN-4 D10) - says nothing between its start
  and its answer, and declares as it begins the deadline it is due by:
  `due_s`, seconds from `at`; before it the job is answering, never
  stalled - its `stalled_for` is null - and after it, it is judged as any
  quiet job is, `stalled_for` counted from its last movement, as the stall
  watch counts it. Every key the server writes is declared, in
  `shapes/progress.json`; the object is open, because an application's own
  backend may report more.
- A runner's candidate says, beside its text, the load it ran on (`load`, a
  `load-` actor id), its `reasoning` when the model thought aloud, and
  llama.cpp's own `usage` and `timings` - `timings.predicted_per_second` is
  how fast it wrote. A transcription's says whisper-server's own beside its
  text: `transcript`, its verbose answer whole (`shapes/transcript.json`),
  and `audio_s`, the audio's length; its text may be empty - silence heard is
  an answer. The transcript is whisper-server 1.9.1's as its source writes
  it: a segment's `start` and `end` are left out when the request asked for
  no timestamps, a word's times when it asked for no token timestamps, a
  segment's `tokens` and `words` when it has no text token, and an
  `avg_logprob` it could not reckon is null. Declared in
  `shapes/candidate.json`; open, as a backend of an application's own may
  say more.
- Each model names the engine it runs on (`engine` in `shapes/model.json`:
  `llamacpp` or `whisper`), and the runner's `backend` is `mock` (the
  stand-in, in each engine's shape), `real` (each model on its own engine)
  or `llamacpp` (every model on llama.cpp: `real`'s name from before
  engines were named).
- The card is handed out by ticket (`coordinator/card-*`): ask, wait, claim,
  heartbeat, release, cancel. The ask's answer is the only one that carries
  the ticket's secret `token`; every later call acts with the token, and the
  coordinator keeps only its sha256 - in memory and in its record. A wait is
  held open and answered the moment there is news. A ticket's answers are
  about the asker's own ticket and nothing else: no answer to an application
  names another application. `/state` and `/events` are the operator's, and
  show everything - but never a token.
- A rental's heartbeat names every process the rental put on the card
  (`processes`, `shapes/process.json`: each one's pid, its proof - the id of
  the load that started it, `load-...` - its model, engine, port and memory).
  The list is whole: it replaces the coordinator's, so a lost heartbeat is
  healed by the next; at most 8, and a proof that is not a load's id, or a
  pid named twice, is `400 bad_processes`. The coordinator keeps each with
  since when (`shapes/named_process.json`) and ends each, on its own proof,
  if the rental lapses - outside its lock, the rental `reclaiming` until
  every outcome is recorded (`process_ended`, `process_gone`,
  `process_left_alone`, `process_not_ended`). A release is checked: answered
  once the processes are gone and the card's memory is back, the rental
  `releasing` meanwhile. `/state`'s `card` is what the machine says of the
  card, shown on the page; a grant and an amend each read the card afresh
  for themselves (below). `/state` carries `contract_version`; `GET /up` answers only that the coordinator is
  up, and the version of the contract it runs (`contract_version`) - what a
  server asks, never the operator's `/state`. `/events` serves
  only lines in the event shape: an older coordinator's `turn_*` and
  `rental_process` lines stay in its file, and are counted on the start line.
- **The memory is verified** (PLAN-4 D2), by the one check in `memory.py`:
  what is needed, and a margin kept free beside it, against the card's free
  memory - `fits`, `no_room` (not now), `never_fits` (more than the whole
  card: it would not fit an empty one by the same rule), or `unknown` (the
  card could not be read), which decides nothing and refuses nothing. The
  parts share no code, and this is the one part they all import, so the rule
  is written once: the coordinator runs it, and so does a server run on its
  own. Where it is decided:
  - **at the ask**, an ask that could never fit is refused at once, whatever
    its class: `409 never_fits`, and no ticket;
  - **at the claim - the grant -** the coordinator reads the card afresh: a
    responsive ticket that does not fit now is refused at once (`409
    no_room`), a scheduled one waits for the room in line (`409
    not_offered`, offered again once a fresh read says it fits) - unless it
    asked to fail (`if_no_room: fail`, S18), then it is refused as a
    responsive one is - and one that could never fit is refused whatever its
    class; a ticket refused this way is over, and asked again answers
    `refused`;
  - **the words** (`memory.py`): `no_room_words` for a refusal - the numbers,
    what programs outside the bootstrap hold, what to do - and, given how
    long it waited (`waited_s`, S18), for scheduled work whose patience ran
    out while it waited for room; `room_words` while it waits - the same
    numbers, and that it starts by itself once it fits; `span`, how long, as
    a person reads it;
  - **before each model a rental adds** (`/card/amend`): approved, or
    refused `no_room` / `never_fits`; right after one of the rental's own
    processes went, the amend reads the card again until the model fits or
    `amend_settle_s` has passed since the process went, since its memory is
    still coming back. Approved, the rental's `models` follow what is on the
    card. **The two sides of the amend's wait are held together here:** a
    server waits `memory.AMEND_ANSWER_S` (30 s) for the answer - one it never
    hears decides nothing, and its model goes on unverified - so a
    coordinator's `amend_settle_s` is at most `memory.AMEND_SETTLE_MAX_S`
    (15 s), the rest being the card's last reading and the answer's way.

  Every refusal carries the numbers (`shapes/memory.json`: needed, free,
  margin, the whole card) and names nobody who holds the rest.
- **The machine's card lock** (`machine/card-lock.json`) says who holds the
  card at the top (PLAN-4 D5). A coordinator started from its command line
  takes it and holds it while it runs, its note giving its address; a server
  run on its own takes it for its session, its note naming it. A server
  opening a session reads it: free - it takes it; a coordinator's - it goes
  through that coordinator; anybody else's - it waits, and a patience of 0
  is `card_busy` at once. The note is read, never repeated to an
  application.
- A renewed rental's answer says whether the card is `wanted` - another
  ticket is in line for it - and nothing more: never whose, for what or how
  many. A server's own pipeline for its queue's work lets the card go at once
  when its work is done and the card is wanted; an application's pipeline
  keeps it until it is closed.
- **The coordinator watches every rental** (PLAN-4 D3). A heartbeat may say
  the work: `working` (a model answers a request), `progress` (tokens and
  answers since the grant - only real work moves it), `deadline_s` (an answer
  in one piece is due that long from now) and the memory errors its models'
  logs said (`memory_errors`, a count since the grant, and the newest line,
  `memory_error`); one below zero is `400 bad_work`. A rental that says it is
  working and whose progress has not moved for the coordinator's `hang_s` -
  never before a deadline it declared - is taken back as a lapsed one is,
  the record saying why (`rental_hung`) and its holder told `reclaimed` in
  words that say it. Free memory under `floor_mib`, read every `watch_s`
  during a rental, or a count of memory errors that rose, marks the rental
  `degraded` (`shapes/degraded.json`: `under_floor` or `memory_error`, why in
  words, the numbers, the line) - on every answer that carries the rental,
  null while the card has room - and it is never ended for it: an amend is
  refused `409 degraded`, and it clears once the card has room again
  (`rental_degraded`, `rental_restored` on the record). S5b: while degraded
  the card is read every `recover_watch_s`, `degraded` carries the newest
  read's numbers - the room it waits for (`need_mib`: its models' need, less
  what its processes hold, plus the floor) and what programs outside the
  bootstrap use (`outside_mib`) - and it clears on the first read with that
  room, `rental_restored` saying `free_mib`, `need_mib` and `outside_mib`. `/state`'s `watch` is
  the rental's work as its heartbeats said it - the count of answers far
  slower than their model's normal (`slow_answers`) among it, shown even
  where it marks nothing; `card.ours_mib` and
  `card.outside_mib` how much of the card is the rental's and how much is
  outside the bootstrap.
- **The pipeline** (`server/pipeline-*`): `open` holds the card from now until
  `close` - one ticket's rental with a coordinator, the machine's lock without
  one - so a model switch, or a pause between two requests, never lets
  another application in. `models` are loaded once the card is had - one load
  at a time, side by side - and kept (pinned); `keep_s` is how long it stays
  open without a `renew` (0: until it is closed). One per server, named by a
  `pipeline` id. The open answers at once, or - `?wait_s=N`, as a job's result
  takes it - once every model it names answers its health check or failed;
  health's `pipeline` says where it stands from then on: `closed`, `waiting`,
  `holding` (with nothing loaded too), `degraded` or `lost`, with why and since
  when, and `up`, the models on the card. Without an open pipeline the queue
  opens one of its own for its work.
- **Models side by side** (`server/model-load`, `server/model-unload`): a
  runner keeps several models on the card at once - never two loads of one
  model, loads one at a time, at most `max_loads` (8: a rental names at most 8
  processes). `load` pins a model in the application's open pipeline, and
  takes `?wait_s=N` the same way: the answer comes the moment the model
  answers its health check, or its load fails. `unload` takes one off,
  pinned or not, and the rest stay. A model a job needs that is not up is
  loaded for it on demand and idle-unloads; when it does not fit, on-demand
  models not answering a request come off first, the one used longest ago
  first. A pinned model never comes off for another. `shapes/model_state.json`
  is where a model stands: `off`, `waiting`, `loading`, `up` or `failed`.
- **Answers at once** (S17): each model answers one job at a time, and
  different models answer at the same time. Health's `queue.running` counts
  every running job, the runner's `in_use_models` names the models they
  hold; the jobs list has each `running` with its own progress
  (`server/jobs-list`, *two answers at once*); a rental's heartbeat is
  `working` while any answers, its `progress` counts all of them, and its
  `deadline_s` is the latest one-piece answer's (`coordinator/card-heartbeat`,
  *two models answer at once*).
- A job may say how long it waits for the card (`card_patience_s`; 0 is now
  or not at all), and a job that ends for a reason of its own says which in
  its `reason` - `card_patience` or `card_busy`, `judge_refused` when the
  application's judge refused its last attempt, `prompt_too_long` when its
  request does not fit its model's context, `no_room` or `never_fits` when
  the card's memory refused its model (not now, or not on this card at all),
  or `card_degraded` when the card went over-full while it ran or before it
  could - for a program to branch on, beside the `error` a person reads.
  Where the server can hold it for the card's memory (S5b), `card_degraded`
  is the end of a wait, not the first sign: the job was held within its
  patience and run again as the card had room, and it failed only when its
  patience ran out (saying how long, and what the card read), when it had
  none (0), or when the card filled again each time it ran.
- A text job's request is counted by its model before the model is asked:
  at the job's turn, with the model up, rendered with the model's own chat
  template and counted by its own tokenizer, against the context the model
  says it has. `progress.count` holds the three numbers on every job its
  model counted: `prompt_tokens`, `room` - what its answer needs, its
  `max_tokens` and at least one token (-1 names no limit; 0 still needs the
  position after the prompt) - and `context`. A request whose prompt and
  room are more than the context fails `prompt_too_long` before the model is
  asked, its `error` saying the three numbers: never cut to fit.
- A job's `gen` is closed and names every parameter a text request takes:
  the queue's own three (`model`, `n`, `retries`) and every parameter
  llama-server's chat route reads that is an application's to set, in
  llama-server's own spelling, each optional. A misspelt one is `400
  bad_request`, never ignored; a `max_tokens` llama-server refuses - below
  -1 or above 2147483647 - is `400 bad_max_tokens`, never sent; a value
  outside another of llama-server's hard limits (`n_keep` below -1, `n_indent`
  below 0, and the rest jobs-submit names) is `400 out_of_range`, never sent.
  The server's reading of llama-server's source
  is `llm-bootstrap-server/runner/llama_fields.py`, and a row there holds
  this list to it.
- A text request is a prompt - a string or `{"prompt": ...}` - or a
  conversation, `shapes/conversation.json`: a system message and the turns
  before this one, each a `role` and its `content`. It is what an
  application's build hook returns - with the default hooks, a job's own
  `input` - so no route's shape reaches it: its examples are jobs-submit's
  inputs that are conversations, and the self-test checks them against it,
  in both validators.
- A whisper model's request is audio, `shapes/audio_request.json`: `audio`,
  a WAV file on the server's machine, and `fields` - closed, and naming every
  field whisper-server 1.9.1 reads, each optional and each of the kind its
  request parser reads it as (a whole number, a number, true or false, or
  text), because it ignores a field it does not know: a misspelt one would
  read as one that took effect. `response_format` is not among them: the
  engine asks for the verbose answer itself. The server's reading of
  whisper-server's source is `llm-bootstrap-server/runner/whisper_fields.py`,
  and a row there holds this list to it. No route's shape reaches it either:
  its examples are jobs-submit's inputs that are audio requests, checked the
  same way - a refused one's input must be refused. With the default hooks a
  job for a whisper model is checked at submit, and an input that is not
  such a request is `400 bad_audio_request`, never sent.
- A model's `header` is its file's own: a llama.cpp model's GGUF, or a
  whisper model's ggml header (`shapes/ggml_header.json`) - its eleven
  numbers and its shape in words. Its `context` is what the settings ask of a
  llama.cpp model, and null for a whisper model, which has none to set.
- A server started with `--client <folder>` answers `GET /` with the client's
  page, as the coordinator answers `/` with its dashboard: a page, not a
  route, and outside the contract. Without it, `/` is `404 no_route`.
- **Before any route** (`origins/server.json`, `origins/coordinator.json`).
  On every request whatever its method - the page at `/` and a path no route
  has included - a part checks three things, in this order, and the first
  that refuses answers:
  - **The name it was sent to** (its `Host`): refused `403 host_not_allowed`
    unless it is one of the part's own names - `127.0.0.1`, `localhost`,
    `[::1]`, the address it listens on, and the host of each page it was
    started to allow - compared in lower case and without its port; a Host
    that is not a name and a port is refused. A website whose name is made to
    point at this machine (DNS rebinding) sends that name as its Host and as
    its Origin, and without this check would pass for the part's own page. So
    a request to a name the part does not have is refused whether or not it
    carries an Origin. A request that names no Host at all is answered as
    before: every browser names one.
  - **The page that sent it** (its `Origin`): refused `403
    origin_not_allowed` unless the Origin is the part's own, or a page's
    address it was started to allow:
    - **its own** is the address the request was sent to: `http://` and its
      `Host`, however that address is written (`127.0.0.1` or `localhost`) -
      the server's page, the coordinator's dashboard;
    - **allowed** are the addresses named by `--allow-origin` (repeatable),
      written as a browser writes a page's address in its Origin header:
      scheme, host and port, lower case, no default port, no path. The
      coordinator allows none unless it is told: applications never ask it
      from a browser.
  - **Its body**, whatever the method: a request that names a
    `Transfer-Encoding` - a body sent in chunks, its length never said - is
    refused `411 length_required`, whether or not it names a
    `Content-Length` too: a part reads a body by its `Content-Length` alone,
    and before this check the chunks were read as the next request on a
    connection kept open. A request whose `Content-Length` is more than the
    part reads (`--max-body`: 16 MiB for the server, 1 MiB for the
    coordinator) is refused `413 too_large` before a byte of its body is
    read. A `Content-Length` that is not a whole number of bytes reads as no
    body.

  So a web page from anywhere else can never act on either part, not even
  with a request a browser sends without asking first - a form's POST, or a
  text body: both read a body as JSON whatever its type.
  - **A body a part does not read** - one in chunks, one over the cap - is
    never read at all: its answer says `Connection: close`, and the
    connection is closed a moment after the answer is out, so an asker still
    sending hears the answer, not a reset, and nothing it sent is ever read
    as a request. A body within the cap that no route reads - a refused
    request's, a GET's - is read and set aside, so a connection the browser
    keeps open stays in step.
  - **A method no route takes** - `HEAD`, `PUT`, `DELETE`, any other - is
    checked the same way, and then answered `501`, as http.server answers a
    method it does not take. The checks run in one place, before any method
    of a part's handler, so a method added to it cannot skip them.
  - **Its time:** a request must arrive whole - its line, its headers and
    its body - within `--read-timeout` seconds (10 by default) of the part
    starting to wait for it; one that has not is never answered, and its
    connection is closed. No answer is declared for it: the asker that
    stopped sending is not listening either.
  - **Only an allowed page is ever told it may read an answer:**
    `Access-Control-Allow-Origin` naming it, and `Vary: Origin`, on every
    answer it is given - a refusal a route gives it, and one before any
    route, included. A refusal for any other page carries no
    `Access-Control-*` header, so the browser shows the page nothing.
  - **With no Origin, sent to one of the part's own names, and from its own
    page,** a request is answered exactly as if there were no check - an
    `OPTIONS` too: `501`, not a method the part takes.
  - **An example before any route** names what of the request it answers:
    its `origin`, its `host`, its body's `length`, or the
    `transfer_encoding` it was sent with - one at least, each of its own
    kind.
  - **An allowed page's preflight** - the `OPTIONS` its browser sends before
    a request with a JSON body - is answered `204`, with
    `Access-Control-Allow-Methods: GET, POST` and
    `Access-Control-Allow-Headers: Content-Type`.
  - **The coordinator says each refusal before any route on its console**,
    and never writes one to its record: a page must not be able to fill the
    record by asking.
- A server answers two things no route can declare, because they are about
  the request finding no route: `404 no_route` and `405 method_not_allowed`.
  A `500 internal` is the server failing - a bug, never an answer.

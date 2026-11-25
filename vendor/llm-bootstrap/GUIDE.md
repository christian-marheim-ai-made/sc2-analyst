# Working in llm-bootstrap

How to add to this repository without breaking what it promises. It is written
for an agent as much as for a person: every rule says why, and every "how to"
names the files. The client's half starts at *The client*.

**Building an application on the bootstrap, not changing it?** Start at
*Build an application, start to finish*. It is one path from an empty folder
to a working application, and it names the few documents you need.

## Before you change anything

    python gate.py                      every check and every suite; whole outputs kept
    python gate.py --no-suites          the tree checks alone, in a second
    python gate.py --only server,queue  the tree checks and the suites you name
    python mutate.py <mutants.py>       break rules on purpose, and see which rows notice

The gate is the definition of "done". It checks that:

- no file is CRLF;
- every Python file compiles with warnings as errors;
- every import is the standard library's or this repository's;
- every suite passes, prints its own tally - the gate reads that line, "P
  passed, F failed, T total", never unittest's "Ran N tests", and a tally
  that says a row failed fails it - and prints no `ResourceWarning`.

Run it before every commit. A change that is not finished until "the launch
runs it" is not finished when the gate has not.

| suite | what it proves | what it runs | about |
| --- | --- | --- | --- |
| `contract` | the contract against itself; both validators agree | Python and `node` | 1 s |
| `coordinator` | the coordinator's rules, and real coordinator and holder processes | processes | 6 s |
| `queue` | the queue's rows | nothing outside the process | 40 s |
| `server` | every route over real HTTP; every declared answer provoked | a server process | 10 s |
| `runner` | the runner on the stand-in model | nothing outside the process | 10 s |
| `stats` | the timing statistics store: per model and per measure, written whole | nothing outside the process | 10 s |
| `llamacpp` | llama.cpp against a stand-in llama-server | stand-in processes | 2 min |
| `whisper` | the stand-in whisper-server, as the real one was measured; a whisper model file's header; the fields whisper-server reads; the whisper engine against it - its command line, its proof, its start, its give-up and its held-off retry, its transcription, its deadline, a cancel, and the card's memory running out - and a real runner running each model on its own engine and answering through its one watched path | stand-in processes | 1 min |
| `adapter` | the runner behind the queue, end to end | stand-in processes | 30 s |
| `coordination` | the server as a coordinator's holder | stand-in processes | 1.5 min |
| `sessions` | the reference client - every answer checked against the contract, the contract's version first, every wait the server's - and three whole sessions through it over HTTP: a Dictaphone's, an analysis app's, and a host running a component's models with the swap and no gap | a coordinator and servers in its process, one server as a child | 20 s |
| `client` | the page, built from its parts, run in node against the contract's examples | `node` | 1 s |
| `real_card` | the runner on the real card | a real llama-server and model | skipped |

Every suite but `real_card` is safe on a machine whose card is in use: none of
them loads a model, starts a real llama-server or whisper-server, or asks
nvidia-smi.
`test_real_card.py` skips unless `LLM_BOOTSTRAP_REAL_CARD` names a settings
file. Never set that without the card's owner saying the card is free.

## The four parts

| part | owns | read first |
| --- | --- | --- |
| `llm-bootstrap-contract` | every route, its shapes, real examples; the id grammar; two validators (Python and JavaScript) that must agree character for character; the one memory check (`memory.py`) and the machine's card lock (`machine/`) - what the other parts share without sharing code | its `README.md`, then `validate.py`'s docstring (the shape language) |
| `llm-bootstrap-coordinator` | the card across applications: tickets, the rental and who holds it, liveness, reclaim, the ports registry, the dashboard | its `README.md`, then `coordinator.py`'s docstring |
| `llm-bootstrap-server` | one application's queue, its models (the runner), its HTTP server | its `README.md`; `jobqueue.py`'s docstring says what the queue owns and what the application owns |
| `llm-bootstrap-client` | the widgets: a kit an application's page includes (one script, one call per widget), and the bootstrap's own page, made with the same calls and served by the server - both built from parts | *The client*, below; then `parts/registry.js` and `parts/kit.js` |

Each part is usable alone. The server and the coordinator share no code:
they share the contract. Duplication across parts is by design (each has its
own `process_info`, its own port check); never import one part from another,
except the contract.

## The rules

1. **Standard library only.** No part installs a package to run or to test.
   The gate checks every import.
2. **A refusal is never a success.** Every refusal answers a non-2xx status,
   an `error` sentence for a person and a `reason` from a closed list for a
   program. A program branches on `reason`, never parses `error`.
3. **The contract comes first.** A route, a field, a refusal reason, an event
   kind: it is in the contract, with an example, before any code answers it.
   The server refuses to start if the contract declares a route it does not
   serve.
4. **An id names, it never routes.** A reply comes back on the connection
   that asked. Every id is minted by `ids.py`'s grammar (`srv-`, `load-`,
   `c-...`, `j-0007`, `t-0042-k3x9`).
5. **Nothing is remembered between launches** except what is written in the
   folder the server was pointed at. No settings in a home directory, no
   cache beside the code.
6. **The card is rented whole, to one application at a time.** The owner's
   ruling (2026-09-25): all or nothing is allocated to an application, for
   simplicity and stability. Across applications, the coordinator's rentals,
   handed out by ticket; between this project's servers on a machine with no
   coordinator, the machine's lock. Inside its rental an application decides how many models
   it runs; this server's runner runs them side by side - one load of each
   model, loaded one at a time, at most `max_loads` (8, the most a rental
   names) - under one lease, its server's; and its models answer at the same
   time, each one job at a time (S17, the owner's ruling of 2026-09-28). A mixed mode
   - applications sharing the card by memory - may come later, as a separate
   mode; the normal mode stays all or nothing.
7. **The card's truth is the card.** A record says what was done; only the
   hardware says what is there. Never decide from a record alone what the
   card holds.
8. **A holder never infers loss from silence.** It learns it lost the card
   from an answer (409 `reclaimed`), never from failing to reach the
   coordinator.
9. **A process is ended on proof, never by port, by pid alone, or by time.**
   A pid is reused constantly (measured on Windows: 91 of 300 processes in
   one run were handed a pid an earlier one had had). The proof is a string
   on the process's command line that only its starter could have put there:
   a load's id, carried by its engine's own proof (PLAN-4 D9) - `--alias
   <load id>` on llama-server, `--request-path /<load id>` on whisper-server
   (`runner/engines.py`, `is_ours`).
10. **Every wait is bounded, and says what it waits for.** A patience, a
    timeout, a join with a limit - and while it waits, a sentence naming who
    or what it is waiting on.
11. **A stop never waits out work nobody will use.** A load under way is given
    up; a wait for the card, or for another model's load, ends; every model
    comes off while its folder is still bound, so its record is written
    there.
12. **Applications know nothing of each other.** The owner's ruling
    (2026-09-25): every answer to an application is about its own ticket,
    rental or port alone - never who holds the card, what runs on it, or who
    else waits, its refusals included. The operator's pages (the
    coordinator's `/state`, `/events` and dashboard) show everything. A
    ticket is acted on with its secret token alone, kept only as its sha256.

## How a request travels

    client --HTTP--> server.py --submit--> jobqueue.py --(worker)--> hooks.build_request
                                                              \--> backend(request, params, run)
                                                                   = runner/adapter.py
                                                                   -> manager.acquire(model)
                                                                      -> the pipeline holding the card
                                                                         (card source: take, once)
                                                                      -> llama-server (a process)
                                                                   -> the model counts the request
                                                                      (prompt_too_long if it does not fit)
                                                                   -> manager.answer: stream tokens,
                                                                      each counted as the work's
                                                                      progress -> run.report
                                                              <--  hooks.land_result / judge / ended

- **The queue** knows jobs, never what they mean. Its only view of the
  application is the hooks; its only view of the model is the backend. Its
  worker starts each job on a thread of its own: jobs on different models
  travel this path at the same time, each model one job at a time (S17).
- **The runner** knows models, never jobs. It is handed a request, the job's
  parameters and a `run` (cancel event, progress reports, the request
  record).
- **The card source** is how the runner may use the card - and it decides,
  before each load, whether the model fits (the contract's one memory check):
  - the in-process lease (a stand-in model): nothing to decide;
  - the machine's lock (a real card): free, taken, and the card read here;
    held by a coordinator, gone through (below); held by anyone else, waited
    for;
  - a coordinator's rental, had by ticket: asked for once, waited on, claimed
    with the ticket's token; each model an amend.
- **The models** go side by side (PLAN-4 D7): one load of each, loaded one at
  a time under the load lock - never the manager's lock, so a generation, a
  health answer or a bind never waits for another model's load - and stopped
  together. A model an application's pipeline names, or loads with
  `POST /api/models/{key}/load`, is pinned: kept until it is unloaded
  (`/unload`), the pipeline closes or is lost, or the server stops. One a job
  needs is loaded for it on demand, and idle-unloads on its own clock. A load
  that does not fit - past `max_loads`, or refused by the card source's fit
  check - takes off the on-demand model used longest ago that is not
  answering a request, and asks again; a pinned model never comes off for
  another, and a stop already under way is room being made: waited out
  first, never a second model off for it.

## How to add a route

1. **The contract.** Add `llm-bootstrap-contract/routes/<part>/<name>.json`:
   method, path, request shape, a response shape per status, and at least
   one example per status and per refusal reason.
   - **Names follow the files already there.** A route about one job is
     `job-<verb>.json`, and one about the collection is `jobs-<verb>.json`.
     Either way its id is `server.jobs.<verb>`.
   - **Where a shape goes:** one used by two routes goes in `shapes/`. A
     refusal's shape is declared on its route, because each route's reasons
     are a closed list of its own.
   - **Check it:** run `python validate.py`, then `python test_contract.py`.
2. **The server.** Add a handler to `Server.handlers` in `server.py`, keyed
   by the route's `id`.
   - **Bodies are checked before your handler runs,** against the route's
     request shape (`Server.answer`, through the contract's validator). A
     missing field the route refuses by a reason of its own goes in
     `OWN_REFUSALS`.
   - **A job route answers through `Server._job(...)`.** It turns the queue's
     `Refused` and `StoreUnreadable` into the answers every job route
     declares: `409 no_workspace` with nothing bound, `503 store_unreadable`
     when the store cannot be read.
   - **The queue call behind it must raise for those cases, never answer
     None.** An empty answer standing in for one that could not be read is
     the one thing a job route must never give. A queue method made for
     internal callers may answer None (`next_up` does): give
     the route a way to ask it to raise.
3. **The proof.** In `test_server.py`, provoke every answer the route
   declares, over real HTTP, and check each against the contract's shape.
   - A job route joins the two sweeps every job route is in: the one with
     nothing bound (`routes()`, *"with nothing bound is no_workspace"*), and
     the one on an unreadable store (the store's `read_bytes` made to fail,
     *"on an unreadable store is store_unreadable"*).
   - `coverage()` fails the suite if any answer the contract declares was
     never provoked.
4. **Break it on purpose** (`mutate.py`, below): the route answering the
   wrong job, a refusal answered as a success. Each must turn a row red.

## Build an application, start to finish

For an application that runs its models on the bootstrap - a dictation app,
an analysis batch, anything else. You change nothing in this repository.

**What you read.** This section, and what it points to:
- this guide: this section, *Use the server from any language*, *The card,
  the ports and the coordinator*, *The records* and *An application's own
  page*;
- the contract, `llm-bootstrap-contract/`: `routes/server/*.json` is the
  whole API, one file a route - its fields, its answers, its refusals, and
  an example of each; `shapes/` the shapes they share; its `README.md`;
- the READMEs: `llm-bootstrap-server/README.md` (the settings file is its
  *The runner's settings*), `llm-bootstrap-client/README.md`,
  `llm-bootstrap-coordinator/README.md`;
- the reference client, `llm-bootstrap-server/reference_client.py`: every
  call, in Python's standard library. Import it, or copy it into your
  language;
- the kit, `llm-bootstrap-client/llm-bootstrap-kit.js`, and the example
  page, `llm-bootstrap-client/example/index.html`.

The rest - the server's and the coordinator's code, and the suites - is how
the bootstrap keeps its promises. An application needs none of it. Where
this section is not enough, the guide is at fault: say so.

### 1. Run on the stand-ins, safely

Build and try everything on the stand-ins: no card, no model, no lock. The
real card is its owner's to lend.

| you run | how | what it takes |
| --- | --- | --- |
| one application | `python llm-bootstrap-server/server.py --backend mock --runner <settings.toml> --workspace <folder>` | nothing: a lease in its own process. No lock, no card |
| several applications, the card handed out one at a time | a coordinator of your own: `python llm-bootstrap-coordinator/coordinator.py --data <folder> --port 7171 --ports 7400-7499 --hardware none --card-lock <folder>/card.lock`; then each server with `--backend mock --coordinator http://127.0.0.1:7171 --name <app>` | a lock file of your own, never the machine's; no card read (`--hardware none`) |

- **Never `--backend real` or `--backend llamacpp` on a machine whose card
  is in use.** Each loads a real model, reads the card (nvidia-smi) and
  takes the machine's card lock. Never set `LLM_BOOTSTRAP_REAL_CARD`, and
  never run `cardwatch.py`: it reads the card.
- **Give every coordinator you start a `--card-lock` of its own.** Without
  one it takes the machine's lock (`llm-bootstrap-card.lock` in the user's
  temp folder), which another application may hold. With `--hardware none`
  it reads no card and ends no process. Its start line says it holds its
  own lock, where, and that a server finds it only when that server's
  settings name the lock (`[runner] card_lock`) or it is started with
  `--coordinator`.
- **A server under a coordinator, with no `--port`,** takes its port from
  the coordinator's `--ports`. Its `listening on` line says which; with
  `--workspace`, a `bound to <folder>` line comes before it.
- **End only what you started:** by the handle that started it, or by a
  string on its command line only you put there (your folder's path). Never
  by its port or its pid alone.

What the stand-in model does (`[mock]` in the settings file):
- **A text model** answers made-up words, `tokens_per_second` of them, the
  same words for the same request. A load takes `load_seconds`. It ignores
  `json_schema` and `grammar`: code that parses a structured answer gets
  words, never JSON, from it.
- **A model's `file` need not exist.** Its `problems` (`GET /api/models`)
  say it is not there, and it loads all the same.
- **A whisper model** (`engine = "whisper"`) transcribes a PCM WAV file on
  the server's machine in `answer_seconds`, in whisper-server's own verbose
  shape: fixed words laid over the audio's length. Silence is no words, and
  an answer all the same.
- **Failures on purpose:** `fail_load_models = ["<key>"]` fails that model's
  every load - its state says `failed`, and why. `fail_models` fails every
  answer of a model. A prompt holding `[[MOCK_FAIL]]` fails; one holding
  `[[MOCK_THINK]]` thinks first.
- **It counts a request** as it answers it: each word a token - the number
  its `usage.prompt_tokens` says - against the `ctx` its model's settings
  give (8192 unless they say). A request and the room its answer needs past
  it fails `prompt_too_long` before the stand-in is asked, and a held
  request says its `count`. Give a model a small `ctx` to try the branch.
  A transcription is not counted: its length is its audio.
- **Its card, filled on purpose.** With no setting the stand-in has no card:
  nothing is read and no lock is taken. `card_mib = 24576` gives it a card
  of its own - read by the stand-in alone, never the real card - and
  `card_full_s = [20, 15]` makes that card read full (no MiB free) 20 s
  after the server starts, for 15 s, then free again. It is watched as a
  real card is, so the card full goes the real way: the session `degraded`,
  a job held `waiting for the card's memory`, and the job running again by
  itself once the card reads free. It is watched on a lock of its own,
  which `[runner] card_lock` must name (a file in your folder; the server
  refuses the machine's). Lower `[runner] card_watch_s` (1) to see it
  sooner, and keep a session open over the window (`session_models`, or a
  pipeline of yours): the card is watched while it is held. Under a
  coordinator the coordinator reads the card, and with `--hardware none` it
  reads none: the stand-in's card is for a server run on its own.
- **What the stand-in never does:** a model that does not fit - `no_room`,
  `never_fits` - since a stand-in model needs nothing on the card. Build
  that branch against the contract's examples (step 7).

### 2. The settings file

The models an application runs are named in one TOML file, `--runner
<file>`. A dictation app's, with two whisper models:

```toml
[runner]
backend = "mock"
session_models = ["large-v3", "nb-large"]

[mock]
answer_seconds = 0.3

[models.large-v3]
engine = "whisper"
file = "ggml-large-v3.bin"

[models.nb-large]
engine = "whisper"
file = "ggml-nb-large.bin"
```

- **`session_models`: the application starts ready.** The server opens its
  session as it starts, loads these one after the other, and keeps them
  side by side (pinned). Without it, no session opens until the application
  opens one.
- **A setting nothing reads is refused,** and the server does not start: it
  names the key. A model's settings are its engine's: a whisper model has no
  `ctx`, a text model no `threads`.
- **Every setting, its default and what it changes:** the server README,
  *The runner's settings*.

### 3. Start the server, and check its contract

    python llm-bootstrap-server/server.py --backend mock --runner <folder>/dictation.toml --port 7072 --workspace <folder> --allow-origin http://127.0.0.1:7867

- **It prints `listening on http://127.0.0.1:7072 as srv-<id>`** once it
  answers, or `cannot start: <why>` and ends with exit code 2.
  `reference_client.start_server` starts it and reads that line.
- **`--workspace` binds the application's folder** at the start, or `POST
  /api/workspace {"path"}` later. The folder must exist: the server does not
  make it. Bind it at the start when the server starts ready: a model's load
  is timed into the folder's statistics only when a folder is bound as it
  loads.
- **`--allow-origin`** is the address of the application's page, if it has
  one (step 8). A server with none answers no other page.
- **The first call is `GET /api/health`:** its `contract_version` must be
  your copy's (*Use the server from any language*, *The contract's
  version*). The reference client checks it as it is made.

### 4. The session: the card from its open to its close

- **Started ready, the session is open.** Health's `pipeline` says it: its
  `id`, `state`, the `models` it keeps and those `up`, and `opened_by:
  "start"`. Renew and close it by that id. An open of your own is refused
  `already_open` while it is open.
- **The `listening` line comes before those models are up.** Wait for each
  with `POST /api/models/{key}/load?wait_s=60`: a model the session already
  keeps is not loaded twice, and the answer is held until it is `up` or
  `failed`. On the stand-ins: `listening` at 0.3 s with `up` empty, then
  each load answered as its model came up, both up at 2.4 s
  (`load_seconds = 1.0`).
- **Otherwise, open one:** `POST /api/pipeline/open?wait_s=60 {"models",
  "class", "label", "keep_s"}`, answered once every model it names is up or
  has failed to load. A failed load does not fail the open: compare `up`
  with `models`, and read each model's `state` and `why` (`GET
  /api/models`).
- **`keep_s`:** 0 holds the session until it is closed. Above 0, renew it
  within that many seconds (`POST /api/pipeline/renew {"pipeline"}`), or it
  lapses and closes.
- **`class`:** `responsive` (the default: a person is waiting) or
  `scheduled` (batch work). A coordinator serves a waiting responsive ticket
  before a waiting scheduled one. A scheduled session's `if_no_room` -
  `wait` or `fail` - is what it does when its first model does not fit (step
  5b).
- **One session per piece of work that must not be broken** - a replay's
  chain of requests - closed after it. Between two, the card goes back, and
  another application's live work goes ahead. Run on the stand-ins under a
  coordinator, the card went: replay 1, the other application's dictation,
  replay 2. (The dictation's ticket was asked while replay 1 held the card,
  so it would have gone first by arrival too. A responsive ticket overtakes
  a scheduled one only when both are waiting: to see it, have the scheduled
  one asked first.)
- **A model more or one fewer, inside the session:** `POST
  /api/models/{key}/load?wait_s=60` and `POST /api/models/{key}/unload`. The
  load answers 200 however it went: its `model.state` says `up`, or
  `failed` and `why`.
- **Pinned or on demand.** A model the session names or loads is pinned:
  kept until it is unloaded or the session closes. A job for a model that is
  not up loads it on demand, and it comes off when it has been idle for
  `idle_unload_s`, or to make room.
- **Two pipelines at once - speech to text, then text to text - keep both
  models in the session.** Jobs on different models answer at the same time
  (S17), but a job whose model must first be loaded waits until the answers
  under way have ended: its load could need their room, and nothing
  answering comes off to make it. With both models pinned, a recording is
  transcribed while the one before it is translated, from the first. On the
  stand-ins (`test_sessions.py`, *talking and translating*): each chunk's
  transcription (1.2 s) overlapped the translation before it by over 0.8 s.
- **A job with no session open** has the queue open a session of its own
  for it. It holds the card until its work is done, then gives it back as
  soon as another application asks for it (under a coordinator, the next
  heartbeat), or else when its model has idled out. Measured on the
  stand-ins, with `idle_unload_s` 600: a job's answer came with no session
  open, and another server's open was answered, holding the card, 4.6 s
  later. An application that wants the card for a set piece of work still
  opens a session for it, and closes it after.
- **Close it:** `POST /api/pipeline/close {"pipeline"}`. Every model comes
  off, then the card goes back, and only then does the answer come.

### 5. Jobs: submit, wait, take

`POST /api/jobs`. `routes/server/jobs-submit.json` says every field, and has
an example of each use.
- **`kind`** is the application's own word for the job. **`input`** is the
  request itself (with the default hooks):
  - a text model's: `{"prompt": "..."}`, or `{"messages": [{"role":
    "system", "content": "..."}, {"role": "user", "content": "..."}]}`;
  - a whisper model's: `{"audio": "<a WAV file on the server's machine>",
    "fields": {...}}`, the fields whisper-server reads
    (`shapes/audio_request.json`). A field it does not read is refused
    `bad_audio_request` at submit, never sent.
- **`gen`:** `model`, which it must name, and any parameter llama-server's
  chat route reads - `max_tokens`, `temperature`, `json_schema`, `grammar`,
  `stop`, `reasoning_budget_tokens`, `cache_prompt` and the rest - each
  optional. A misspelt one is refused `bad_request`. Where a value comes
  from: the job's own; else the model's settings (its `temperature`,
  `top_p`, `min_p`, `repeat_penalty`, and no thinking for a model set
  `disable_thinking`); else the bootstrap's (`max_tokens` 600); else
  llama-server's. Where each value came from is said only for a held
  request (`params[].from`, step 6). Every job's record keeps what was sent:
  `samples/<NN>/params.json`, the values the job named and the bootstrap's,
  and `call.json`, the body its model was sent, the model's own values too
  (the stand-in's lacks the `chat_template_kwargs` of `disable_thinking`,
  which only llama.cpp adds).
- **Thinking, on a model set `disable_thinking`:** `reasoning_budget_tokens`
  alone does not turn it on. Name `chat_template_kwargs: {"enable_thinking":
  true}` as well.
- **`priority`:** `interactive`, the default (a person waits: its ticket is
  `responsive`), or `batch` (`scheduled`). **A job that does not say
  `batch` is live** - the ruled record, *"class default RESPONSIVE"*: it goes
  before batch work in the queue's order, asks the card as `responsive`,
  fails at once when its model does not fit, and can name no `not_before`
  still to come and no `if_no_room: wait`. Only `priority: "batch"`, said by
  your application, makes a job scheduled work (step 5b).
- **`card_patience_s`:** how long this job waits for the card. 0 is now or
  not at all. Unnamed, the settings' `card_patience_s` (an hour).
- **`after`:** the jobs this one waits for. With the default hooks it only
  orders them: the request is the `input` as it was sent. To build a request
  from an earlier answer, wait for that answer and put it in the next job's
  `input` - or run the queue with hooks of your own (*How to build an
  application on the server*).
- **`check: true`:** held for a person's check before it is sent (step 6).
- **`name`:** give every job one. Check lists a held request as `name ·
  model`, and a job's name is its `kind` unless you give one, so two held
  transcriptions would look the same.
- **`correlation_id`** names the request in every record (mint one, or the
  server does); **`client`** is your actor id; **`scope`** a key of your own.
- **Each model answers one job at a time; different models answer at the
  same time** (S17, the owner's ruling of 2026-09-28): a recording is
  transcribed while the text before it is translated, and a compare - one
  take on two models - takes about the time of the slower answer, not both.
  A job for a busy model waits and holds back no job for another model.
  Keep the models open in your session (step 4): a job whose model must
  first be loaded waits until the answers under way have ended. At most
  `answers_at_once` (8) answer at once in all. On the real card two answers
  share the card's compute: each may be slower than it is alone (M2
  measures it).

**Wait for it:** `GET /api/jobs/{id}/result?wait_s=30`, asked again until
`job.status` has ended: `done`, `failed`, `cancelled` or `claimed`. A job
marked `check` stops at `held`, which has not ended - branch on it, or you
wait for ever. A wait is answered the moment its job becomes held, as it
is when a job ends, and the reference client's `wait` returns it then,
`held`.

**The answer** is `candidates` (`shapes/candidate.json`): a text model's
`text`, `tokens`, `finish_reason`, `usage` and `timings`; a whisper model's
`text` and whisper-server's own `transcript` - its language, segments and
words (`shapes/transcript.json`). Two models on one recording are two jobs:
one audio, one `model` each.

**Take it:** `POST /api/jobs/{id}/claim {"client"}`, then `POST
/api/jobs/{id}/forget`. In a chain, the last one first.

### 5b. Plan a batch for tonight: scheduled work

The owner's case (the rulings of 2026-09-28): a batch that takes hours -
image generation, say - planned in the morning, run at night. *"I can plan
it earlier in the day and just let the program run, then when i go to sleep
it runs the task."* It starts no earlier than a time, and once its model
fits: both at once.

**The setting.** In the runner's settings, what scheduled work does when its
model does not fit on the card:

```toml
[runner]
scheduled_if_no_room = "wait"   # the default; "fail" fails it at once
card_patience_s = 3600          # how long it waits - from its time
```

- **`wait`:** the job waits for room, holding back nobody, and runs by itself
  the first moment its model fits.
- **`fail`:** it fails at once, `no_room`, in the loud words a live job does.
- **Live work always fails at once.** The setting never makes an
  `interactive` job wait - nor one that names no priority, which is
  `interactive`.

**The request.** A batch job - **`priority: "batch"` is what makes it
scheduled work; a job that names no priority is live** - with the time it
starts no earlier than, seconds since the epoch:

    POST /api/jobs {"kind": "page", "input": {...}, "gen": {"model": "painter"},
                    "priority": "batch", "not_before": 1790204400,
                    "if_no_room": "wait", "card_patience_s": 7200}

- **`not_before`:** the time. A time already past is now.
- **`if_no_room`:** `wait` or `fail`, for this job alone. Unnamed, the
  setting's.
- **`card_patience_s`:** counted from its time - from the first look at the
  queue after it - never from when you submitted it.
- **Refused `live_is_now`:** a job that does not say `priority: "batch"` -
  `interactive`, said or not - that names a time still to come, or
  `if_no_room: wait`. Live work is now.
- The reference client: `app.plan(kind, input, gen, not_before=...,
  if_no_room=..., card_patience_s=...)`, then `app.wait(job, within_s=...)`
  as for any job; `app.scheduled()` is what health says meanwhile.

**What you see, step by step:**
1. **Planned (the morning).** The job is `queued` and carries its
   `not_before`. Health's `queue.scheduled` says `planned`, and `next_at`.
   Under a coordinator, it keeps a place in the coordinator's line from now
   on, with its time, which the operator's `/state` and dashboard show. The
   card is not asked for: live work and other batch work go first, and no
   rental is told the card is wanted for it.
2. **Its time (tonight).** From the first look after its time - every
   `room_look_s`, 5 s - it runs the moment its model fits.
3. **No room.** It goes back to the queue, `queued`, holding no card:
   `progress.stage` is `waiting for room on the card`, `waiting_for` is the
   sentence - what is needed, what is free, what programs outside the
   bootstrap hold, and that it starts by itself - and `free_mib`,
   `need_mib`, `outside_mib`, `waited_s` and `patience_left_s` are the
   numbers. Health's `queue.scheduled` says `for_room` and `room`. Live
   work asked now still goes first.
4. **Room.** A person closes a program: the job runs by itself, and ends
   `done`.
5. **Patience out.** It fails `no_room`, in the loud words: the numbers,
   what programs outside hold, *"It waited 2 h 0 min for room, within its
   patience, and none came"*, and what to do.

**The widgets** say it too. The progress widget says `starts at 23:00` or
`waits for room`, the numbers on hover; its panel says each job's. The
session widget says the same while no session is open.

**The machine sleeps.** It compares its time with the wall clock at every
look. A job due at 23:00, while the machine slept through 23:00, starts
when it wakes, and its patience counts from then.

**A restart keeps it.** A planned or waiting job is in the folder's
`jobs.json`: after the server restarts, it waits again, its patience counted
from when it first waited. A place kept in the coordinator's line is asked
for again at the first look.

**What waits for room, and what does not:**
- A **job** waits in its queue, holding nothing.
- A **scheduled pipeline** (`class: scheduled` on the open, with its own
  `if_no_room`) waits in place, `waiting`, its `why` saying the numbers:
  - under a coordinator, its ticket is passed over;
  - on its own, it lets the machine's lock go and reads the card until the
    model fits.
- **Scheduled work that does not fit while your own models hold the card**
  fails at once, `no_room`, with *"Unload one it keeps"*. Waiting would
  wait on yourself.
- **A pipeline names no time.** It is the card held now; work for later is
  a batch job.

**On the stand-ins,** a model can need room on the stand-in's card:

```toml
[mock]
card_mib = 24576
need_mib = { painter = 9800 }   # painter needs 9800 MiB of the stand-in's card
```

With `card_full_s` it reads full for a while, so a batch waits for room,
and then has it. A model `need_mib` does not name needs nothing.

### 6. The request check: held, then sent

- **Submit it with `check: true`.** At its turn, once its request is built,
  the job waits: its `status` and its `check` are `held`.
- **`GET /api/jobs/{id}/held`** is the request as it would be sent: the
  model, the messages by role (or the audio and its fields), every parameter
  with its value and where it came `from`, the count against the context and
  whether it `fits`, the time the statistics expect, and `said` - why any of
  these is not known. A transcription's `params` and `gen` are empty:
  whisper reads only its fields.
- **`POST /api/jobs/{id}/send`:** no body sends it as built; `{"gen":
  {...}}` sets parameters over it; `{"unset": [...]}` leaves some unnamed;
  `{"request": ...}` replaces the request whole. The job goes back to its
  place in the queue, and its `check` says `tuned` (changed) or `sent`. The
  record keeps both (`held.json`, `sent.json`). **`POST
  /api/jobs/{id}/cancel`** drops it instead.
- **On a page,** the `check` widget shows every held request, with Send and
  Cancel; `llmKit.requestView` adds your own view of your part (step 8).

### 7. When a job fails: its reason

A failed job has a `reason`, for the program, and an `error`, for the
person. The reasons are in *Use the server from any language*; what an
application does with each:
- **`prompt_too_long`:** the request and the room its answer needs do not
  fit the model's context. `progress.count` has the three numbers
  (`prompt_tokens`, `room`, `context`). Shorten the input, or lower
  `max_tokens`, and submit a new job. Nothing is ever cut to fit.
- **`card_busy`** (patience 0, and the card was another's) and
  **`card_patience`** (it waited its patience): try later, or tell the
  person. Neither says who has the card.
- **`no_room`, `never_fits`:** its model does not fit on the card now, or
  never will. Live work never waits: the owner's ruling of 2026-09-28 is
  "Fail loudly. Memory almost never ever cleans itself up." A `no_room`'s
  `error` says what the card has free, what is needed, what programs
  outside the bootstrap hold, and what to do - close programs that use the
  card, or reboot - and the server's log says it too. Show it whole. A
  job that says `priority: "batch"` waits for room instead, unless it or
  the settings say `fail` (step 5b); its patience run out, it fails
  `no_room` in the same words, saying how long it waited.
- **The card full is not a failure.** The job stays `running`; its
  `progress.stage` is `waiting for the card's memory`, `waiting_for` is a
  sentence for the person, and `free_mib`, `need_mib`, `outside_mib`,
  `waited_s` and `patience_left_s` are the numbers. Health's `pipeline`
  says `degraded`, with `why`. When the person closes something, the job
  runs again by itself. Show the sentence; do nothing else. It fails
  `card_degraded` only with patience 0, when its patience runs out, or when
  the card fills again every time it runs (three times).
- **`judge_refused`:** only with hooks of your own.

The stand-ins provoke `prompt_too_long` (a small `ctx`), the card full
(`card_mib`, `card_full_s` - step 1), and `no_room` and waiting for room
(`need_mib` beside `card_mib` - step 5b); not `never_fits`. Test
every branch against the contract's own answers too:
`routes/server/job-result.json` has an example of each - *its request and
the room its answer needs do not fit its model's context*, *its model does
not fit on the card now*, *held for the card's memory*, and the three failed
`card_degraded`.

### 8. The page: the bootstrap's widgets in yours

- **Serve your page and the kit from your own server,** on an address of
  your own. The kit is `llm-bootstrap-client/llm-bootstrap-kit.js` of the
  llm-bootstrap your bootstrap server runs from, read by its path - never a
  copy (*An application's own page*). In the standard library:

```python
import http.server
from pathlib import Path

FILES = {"/": (Path("index.html"), "text/html; charset=utf-8"),
         "/llm-bootstrap-kit.js": (Path("<llm-bootstrap>/llm-bootstrap-client/llm-bootstrap-kit.js"),
                                   "text/javascript; charset=utf-8")}

class Page(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path, kind = FILES.get(self.path, (None, None))
        if path is None:
            return self.send_error(404)
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

http.server.ThreadingHTTPServer(("127.0.0.1", 7867), Page).serve_forever()
```

- **In the page,** the bootstrap server's address first, then the kit, then
  the calls:

```html
<script>window.llmServer = 'http://127.0.0.1:7072';</script>
<script src="/llm-bootstrap-kit.js"></script>
<script>
  llmKit.place('session', document.getElementById('session'));
  llmKit.place('check', document.getElementById('check'));
  llmKit.container('bottom', { name: 'dictation', slots: { middle: ['queue'] } });
  llmKit.requestView('recording', {
    title: 'The recording',
    shows: (held) => held.engine === 'whisper',
    view: (node, held, give) => {
      const line = document.createElement('div');
      line.textContent = held.audio.path;
      node.append(line);
      return () => {};
    },
  });
</script>
```

- **`node` already holds your view's title** (an `h4` the kit puts there):
  append to it, and never set its `textContent`.

- **The bootstrap server allows that page:** `--allow-origin
  http://127.0.0.1:7867`, its address as the browser shows it.
- **The widgets:** `session` (the card your session holds, and its models),
  `queue` (the model's progress; it also says when a request waits for
  your check, and lists it), `check` (the request check), `log`,
  `workspace`, `customize`, `bench`, `models`, `tools` - each placed in an
  element of your page or in a container's slots (*An application's own
  page*, *The kit's calls*).
- **Each widget sits in a box of fixed width,** at the kit's 14 px:
  `session` 18em, `queue` 20em, `workspace` 16em, `log` 4em. A container in
  an element needs its boxes, 1.2em of padding, 1em between each two of its
  three slots (start, middle, end), used or not, and 0.5em between two
  boxes in one slot; narrower, it sheds what does not fit into its tray,
  even its only widget. The model's progress alone in a container needs
  23.2em (325 px).
- **The widgets are nodes of your page.** A bare rule of yours (`section`,
  `button`, `h4`) or a page-wide `querySelectorAll` reaches inside them: a
  script of yours that disables every `button` disables Check's Send too.
  Scope your CSS and your scripts to your own elements.
- **Your look.** The kit's look reaches only what it builds (`.llmk`), and
  every colour in it is a name you may set: `--llmk-bg`, `--llmk-panel`,
  `--llmk-line`, `--llmk-text`, `--llmk-dim`, `--llmk-accent`, `--llmk-ok`,
  `--llmk-warn`, `--llmk-bad`, `--llmk-button` (every button's background:
  the session and the model's progress are buttons), `--llmk-banner`,
  `--llmk-unknown` (a dot whose state is not known) and `--llmk-shadow`.
  The kit puts its look in the page after yours, so name them more closely
  than `.llmk` alone: `body .llmk { --llmk-bg: #ffffff; --llmk-panel:
  #f4f4f4; --llmk-button: #eeeeee; --llmk-line: #cccccc; --llmk-text:
  #222222; color-scheme: light; }`.
- **The layout is kept in the bound folder,** under each container's
  `name`. A widget your page adds later goes where your page puts it
  (*How the page works*).
- **Run in headless Chrome** (the page above, its server, and a stand-in
  server started ready with a job held): the session said `holding the
  card`, Check showed the held transcription and the page's own view of it
  with Send and Cancel, the model's progress was in its container, the
  colours were the page's, and nothing was thrown.

### 9. The statistics

`GET /api/models`: each model's `state` - an object, `{key, state, why,
load}`, whose `state` says `off`, `waiting`, `loading`, `up` or `failed`
(`shapes/model_state.json`) - its `problems`, whether it is `pinned`, and its
`stats` - for each measure (`load_s`, `first_token_s`, `answer_s`,
`tokens_per_s`, `audio_s`, `real_time_factor`, `slow_answer` and the rest:
`routes/server/models-list.json` says each) its `count`, `mean`, `least`,
`most`, `last`, `median` and `p90`. They are kept in the bound folder, from
one launch to the next; `stats` is null with nothing bound. The `models`
widget shows them.

### 10. Stop

Close the session, then stop the server with `POST /api/stop` from your
program (no Origin: a page is refused `403 from_a_page`), or Ctrl+C where it
runs. It answers `202`, then does its last act - its models off, its folder
put down, its rental handed back - and its process ends with exit code 0;
wait for that end. The reference client's `StartedServer.stop` asks it, and
ends the process by its handle only when the server does not answer. A
server ended as a process, by its handle or otherwise, gives nothing back:
its card is taken back only when its heartbeats stop.

**The jobs are the folder's, and outlive the server.** Started again on the
same folder, a job left `held` is still held, and listed first in Check; a
job left `running` goes back to the queue and runs again; a finished one is
kept until it is forgotten. At stop, cancel what you left unfinished and
forget what has ended - or, at start, list the jobs (`GET /api/jobs`) and
tidy what an earlier launch left.

**A component handed its host's server** - a dictation app brought up inside
another application - opens nothing and starts nothing: *Use the server from
any language*, *A component handed its host's server*.

## How to build an application on the server

An application brings its hooks and a backend. Everything else - the
ordering, dependencies, fans, cancel, retry, recovery, the record - is the
queue's.

```python
import server
from jobqueue import Hooks

class MyHooks(Hooks):
    def build_request(self, job, after_results):
        # built when the job's turn comes, from what its `after` jobs produced
        return {"prompt": job["input"]["text"]}

    def land_result(self, job, result):
        # the application takes the finished result; return a dict to replace it
        return None

backend = server.build_backend("llamacpp", "runner.toml")   # or "mock", no card
httpd = server.serve(7072, hooks=MyHooks(), backend=backend)
```

The seven hooks, their order, and the one guarantee to design for - `ended`
is told **at least once**, so it must be idempotent by job id - are in
`jobqueue.py`'s docstring and the server README.

**Your hooks may run on two threads at once** (S17): jobs on different
models answer at the same time, so two jobs' `build_request`,
`sampling_for`, `manifest`, `judge` and `land_result` can be called
together. A hook that keeps state of its own - a counter, a cache, a file
it appends to - holds a lock of its own around it. The default hooks keep
none.

## Use the server from any language

An application that does not run inside the server makes five kinds of call
over HTTP (PLAN-4 D13). `llm-bootstrap-server/reference_client.py` makes each
of them, in the standard library, and `test_sessions.py` drives three whole
sessions through it - a Dictaphone's, an analysis app's, and a host running a
component's models. Copy it into your language; it is short.

| | the call | the route |
| --- | --- | --- |
| 1 | start the server, then bind the application's folder | `server.py --backend real --runner <settings> --name <app>` (on the stand-ins, `--backend mock`: *Build an application, start to finish*), then `POST /api/workspace {"path"}` |
| 2 | open the pipeline - the card held until it is closed, its models loaded and kept - and renew it | `POST /api/pipeline/open?wait_s=60 {"models", "keep_s", "class", "label"}`; `POST /api/pipeline/renew {"pipeline"}` |
| 3 | submit a job, then wait for its end - or plan a batch for later (`not_before`, `if_no_room`: *Build an application*, step 5b) | `POST /api/jobs`; `GET /api/jobs/{id}/result?wait_s=30`, asked again until the job has ended |
| 4 | take the result: claim it, then forget the job | `POST /api/jobs/{id}/claim {"client"}`; `POST /api/jobs/{id}/forget` |
| 5 | close the pipeline: every model off, and the card back | `POST /api/pipeline/close {"pipeline"}` |
| + | a host's swap, inside its open pipeline | `POST /api/models/{key}/load?wait_s=60`; `POST /api/models/{key}/unload` |
| + | stop the server it started: its last act done, its card handed back | `POST /api/stop` - a program's, sending no Origin; a page is refused `403 from_a_page` |

- **Start.** The server prints `listening on http://127.0.0.1:<port> as
  srv-<id>` once it answers, or `cannot start: <why>` and exits 2. Named a
  coordinator (`--coordinator <url>`) and no `--port`, it takes its port
  from the coordinator; otherwise it listens on `--port` (7072 unless
  named), and a real card's sessions find the machine's coordinator through
  the machine's lock (`--name` is what it calls itself there). Stop the
  server with `POST /api/stop` (H8): it answers `202`, then does its last
  act - its models off, its folder put down, its rental handed back - and
  its process ends with exit code 0; wait for that end. A server ended as a
  process gives nothing back: its card is taken back only when its
  heartbeats stop. End it by its handle only when it does not answer.
- **Nothing polls.** Every wait is the server's: `?wait_s=N` (0 to 60, digits
  and a point) holds the answer until the job ends, the models are up, or N
  seconds pass - give the HTTP request a timeout longer than N. A job that has
  not ended when the answer comes is asked again, with the same wait.
- **Open with `keep_s` 0** when the application owns its server's life: the
  pipeline is held until it is closed, with nothing to renew. With a
  `keep_s` above 0, renew within it, or the pipeline lapses and closes.
- **Take a chain from its end.** A job something still waits on (`after`)
  cannot be forgotten (`needed`): forget the one that waits on it first.

**The contract's version.** Check it before anything else. A client runs
against a copy of the contract; its version is `contract_version()` in that
copy's `validate.py` - the first 12 hex digits of a sha256 over its routes,
shapes, id grammar, validators and memory check - and `GET /api/health` says
the version the server serves (`contract_version`). When the two differ, stop
and say so, naming both: an answer checked against a contract the server does
not serve proves nothing. The coordinator says its own at `GET /up`, and the
server says, once, when its coordinator's is not its own.

**Check every answer against the contract**, as the client's JavaScript does
(`parts/api.js`): the shape declared for its route and status. A refusal the
contract declares is an answer - branch on its `reason`, show its `error` to
the person. Anything else - silence, an undeclared status, a field missing -
is no answer: ask health whether the server is there, and which contract it
serves.

**A component handed its host's server** (PLAN-4 D6, the Dictaphone's H-1).
A host - a trading app that brings up the Dictaphone - holds one pipeline for
everything it runs, and the component's models are in the host's runner
settings. The host hands the component two things: its server's address and
its pipeline's id. The component starts no server, binds no folder and opens
no pipeline - a second open beside the host's is refused `already_open`. It
sends its jobs to the host's server, and reads the host's session from health:
`pipeline.id` is the id it was handed, and `pipeline.state` says where it
stands (`closed` or `lost`: the host has stopped). The host does the swap -
unload, load, and `load ... ?wait_s=60` answering the moment the component's
models are ready - and the card is never given back in between: a pipeline
with nothing loaded still holds it.

**What to do on each refusal.** The `reason` is the program's; the `error`
says the same for a person.

| call | reason | what to do |
| --- | --- | --- |
| any body | `bad_request` and the route's own `bad_*`, `no_kind`, `no_model`, `out_of_range`, `unknown_after` | a bug on the application's side: the `error` names the field. Never sent again as it was |
| any job route | `no_workspace` | bind a folder first |
| any job route | `store_unreadable` | the folder's record cannot be read: tell the person; nothing is written over it |
| bind | `unusable_folder` | name a folder that exists and can be written |
| bind, unbind | `busy` | a job is unfinished (`job` names it): wait for it, or cancel it |
| bind | `folder_in_use` | another server has the folder (`holder`): stop it, or use another folder |
| open | `unknown_model`, `repeated_model`, `too_many_models` | name each model the runner's settings name, once, at most `max_loads` |
| open | `already_open` | one pipeline at a time: renew or close the open one. A component: the pipeline is the host's |
| open, load | `stopping` | the server is stopping: start it again |
| open, load, unload, close | `no_runner` | the server was started without a runner (`--backend`) |
| renew, close | `not_open` | it closed or lapsed: open another if the work goes on |
| close, unload | `unload_failed` | a model would not stop: it may still be on the card, and the pipeline keeps the card - close again |
| load | `no_pipeline` | open the pipeline first: a model is kept only in one |
| load, unload | `no_model` (404) | not a model of the runner's settings |
| unload | `in_use` | a job is answering on it: cancel it, or wait for it |
| submit, open | `live_is_now` (400) | live work is now: a job that does not say `priority: "batch"` - or a `responsive` pipeline - names no `not_before` still to come and no `if_no_room: wait`. Plan it as batch work, `priority: "batch"` (*Build an application*, step 5b) |
| submit | `correlation_taken` | mint a new correlation id: one id never names two requests |
| submit | `group_full` | the fan already has its members |
| result, open, load | `bad_wait` | `?wait_s` is a plain number of seconds, from 0 to 60 |
| held, send | `not_held` | it is not held - never marked `check`, not yet built, or sent already: `status` says which |
| cancel, give-up | `finished` | it has ended already: nothing to stop |
| retry | `not_retryable`, `already_retried` | only a failed or cancelled job is retried, and once: retry its latest attempt |
| any route (403) | `origin_not_allowed`, `host_not_allowed` | a page the server was not started to allow (`--allow-origin`), or a name that is not the server's own: name it `127.0.0.1` (the contract's `origins/server.json`) |
| result, claim, forget | `no_such_job` (404) | forgotten, or never asked here |
| claim | `not_done` | wait for it (`?wait_s`) |
| claim | `ended` | it failed or was cancelled: retry it, or tell the person |
| claim | `claimed_elsewhere` | another client took it: nothing to do |
| forget | `unfinished` | wait for it, or cancel it |
| forget | `needed` | something still waits on it: forget that first |

A job that ends `failed` says why in its own `reason`, and the application
decides: `card_busy` (it would not wait - `card_patience_s` 0 - and the card
was another's: try again, or tell the person), `card_patience` (it waited its
patience), `no_room` (its model does not fit now: something else holds the
card's memory - at once for live work, and for a batch job that asked to fail;
after its patience for one that waited for room), `never_fits` (not on this
card at all), `card_degraded` (the
card is over-full), `prompt_too_long` (the `error` says the three numbers),
`judge_refused` (the application's own judge). None of them names who has
the card.

## How to add a model backend

A backend is one loaded model: `runner/base.py`'s `Backend`. Each runs on an
engine - llama.cpp or whisper (`runner/engines.py`) - which a model names
(`[models.<key>] engine`, llama.cpp unless it says): `--backend real` runs
each model on its own, and the stand-in (`mock`) answers in each engine's
shape. There is no CPU engine (the owner's ruling): the settings refuse a
model set up to run on the CPU.

- `start()`: load it; raise `BackendError` in words a person can act on.
- `stop()`: take it off, and return only once it is gone. Wait on the
  process itself (`llamacpp.wait_exited`), never on `Popen`'s word.
- `stream(messages, params, cancel)`: `messages` is a conversation's list,
  or a prompt string - one user message (`base.as_messages`); `params` is a
  `GenParams`, whose `extra` is every other parameter the job named, sent as
  named. Yield `token` and `reasoning` events, then one `done`. Raise
  `Cancelled` when `cancel` is set, `BackendError` when the model goes away
  mid-answer - `PromptTooLong` when it refuses a request longer than its
  context. A cut-off answer is a failure, never a finish.
- ONE CALL, TWO KINDS (`answers`): `"stream"`, as above - or `"once"`, an
  answer in one piece (a transcription). Such a backend declares, before
  each answer, the deadline it is due by - `answer_deadline_s(messages,
  params)`, in seconds; None, the default, is a stream - and its stream takes
  its engine's own request (`{"audio", "fields"}`) and yields one `done`
  carrying the whole answer: `text`, `tokens`, `finish_reason`, and the
  engine's own fields beside them. Before the deadline a quiet model is
  answering, never hanging: the queue reads it from the job's progress
  (`due_s`), the coordinator's hang check from the heartbeat. A transcript of
  silence is an answer - "the model returned nothing" is the text engine's
  rule.
- `count(messages, params, cancel)`: the request as the model will read it,
  counted BY the model - the body `stream` would send, rendered with its own
  chat template and counted by its own tokenizer - and the context it
  answers in, as the model says it: `{"prompt_tokens", "context"}`. The
  runner asks it once the model is up and before it is asked, and a request
  whose prompt and answer's room do not fit fails `prompt_too_long`. `None`
  (the default): this backend cannot count, and nothing is checked.
- `answer_deadline_s(messages, params)`: an answer that comes in ONE piece -
  no tokens while it is made, as whisper's transcript - says how long it may
  take, in seconds from when it is asked, so a coordinator's watch does not
  call its rental hung before then (PLAN-4 D3). `None` (the default): the
  answer streams, and each token is progress.
- `memory_errors()`: the lines of this load's own log, since it was last
  asked, that say the card's memory ran out - each once, in the log's own
  words (`runner/memlog.py` names llama.cpp's, from its source). The runner
  reports them and a coordinator marks the rental degraded. `[]` (the
  default): no such log.
- `is_alive()`: cheap, and `True` when unsure. A wrong yes costs one failed
  generation; a wrong no costs a reload.
- `abandon(reason)`: give up a load under way, from another thread, at once:
  `start` ends within `base.GIVE_UP_S` - a pipeline's close waits at most
  `ModelManager.CLOSE_JOIN_S` for a load under way - and a row against its
  stand-in holds every engine to it.
- Set `pid`, `port` and `load_id`. Call `self.on_spawn(pid, proof)` when a
  process exists, so a coordinator can end it on proof if this server hangs.
  The proof is the load's id on its command line, carried by the engine's
  own flag, and the engine's `is_ours` reads it back.

Prove it against a **stand-in**, not the real thing:
- `fake_llama_server.py` is llama-server's command line and routes with no
  model, and switches of its own to misbehave on purpose.
  `fake_whisper_server.py` is whisper-server's, the same way; its docstring
  says which of its behaviours were measured and which were read from the
  source.
- Measure the real thing once, and make the stand-in behave as measured.

A new engine is one entry of `runner/engines.py`'s `ENGINES` - the
`[runner]` setting naming its program, its process names (the real card's
instrument, cardwatch.py, watches for them), the model settings it reads, how
it answers, its proof and its `is_ours` - a branch of `ModelManager._make`,
and its estimate in `runner/needs.py`.

## How to add a setting

`runner/config.py`'s `DEFAULTS` gets the key, its default, and a comment
saying what it changes and when you would change it. A setting that nothing
reads is refused, not ignored:
- `runner/config.py` refuses an unknown section or key, so a misspelt one
  fails the start instead of doing nothing;
- `server.parse_args` refuses a flag that nothing would read.

## How to write a row

A row is finished when it has failed. Break the rule it pins, on purpose,
and watch it go red. `mutate.py` does this for a list of mutants, each in its
own copy of the tree:

```python
# mutants.py - write it BEFORE the run, one mutant per rule, each checked
# against the rows as they stand: a mutant you cannot name a row for is a
# row you have not written
MUTANTS = [
    # (id, the rule it breaks, file, old text, new text, suites to run)
    ("M01", "the next job is the first in the queue's order",
     "llm-bootstrap-server/jobqueue.py",
     "            for r in self._order(doc):",
     "            for r in reversed(self._order(doc)):", ("queue",)),
]
```

    python mutate.py mutants.py --jobs 4 --controls 3

The ids `C1`, `C2`, ... (the controls) and `MX` (the self-check) are the
harness's own: it refuses a list that uses one. Name mutants anything else
(`M01`, `S01`, `D01`).

**The verdicts:**
- **KILLED:** the rule is pinned.
- **SURVIVED:** no row can tell. Write one.
- **TIMEOUT:** a row waits without a bound. Bound it.
- **NO VERDICT:** the suite crashed, or ran a different number of rows than
  the controls did.

The controls, copies with no mutant, must be green and agree. The run's
self-check must come back NO VERDICT.

- **Read the instrument, not the code.** Assert on what the system recorded
  or answered: the request record, `runner_events.jsonl`, the coordinator's
  events, an HTTP answer checked against the contract. Never assert that an
  internal field is set.
- **A row that cannot fail is worse than no row.** Pick a value that would
  change the answer. A needle must not be a substring of its own negation
  (`"by SPRITE"` is inside `"NONE by SPRITE"`). Look inside the window, not
  after the cleanup that closes it.
- **Bound every wait:** a join with a limit, a deadline, a patience. A
  mutant that removes a timeout must fail the row, not hang the suite.
- **Never wait by polling something that records a check.** The
  coordinator's suite checks every answer its `Probe` is given against the
  contract, and counts each check: `until(lambda: p.state() ...)` counts one
  per poll, so the suite's total moves with the machine's speed, and a
  sweep's controls disagree. Wait on the coordinator itself, then check once.
- **A row holds on a busy machine.** The suites run beside the owner's
  builds and games and other agents' sweeps, and each of these went red on
  untouched code there:
  - *What the operating system could not answer is asked again, never read
    as an answer* (`_suites.asked_again`, the coordinator suite's own copy).
    A query of the process list (PowerShell, `Win32_Process`) took 14 s, and
    one did not answer within its 30 s: read as an answer, a live child was
    dead and a stopped server nobody's. A listing that could not be given is
    None, never empty; a process whose pid a row knows is asked about through
    a handle to it (`test_llamacpp.gone`), never through the list.
  - *A kill that ends late is waited for* (`_suites.killed`): a plain
    sleeping Python process had not ended 15 s after its kill.
  - *A start is given `START_S`*: a holder did not say "holding" within 10 s.
    A child that ends without saying it is up is said at once, in its own
    words and exit code - never waited out to the bound in silence.
  - *Wait for what another thread says to have been heard.* The heartbeat
    that names a process goes on a thread of its own; a row that unloaded
    at once cleared the list before that thread had read it.
  - *Wait within the product's own bounds*: a reclaim of real processes
    takes its query's bound and its ends' - more than the 20 s a row gave it.

  Each bound is there for what did not work, never for a slow machine, and a
  row that provokes the fault on purpose proves it.
- **Count what happened, never what is new.** Launches are counted as they
  are made (`SPAWNS` in `test_llamacpp.py`). A pid nobody saw before does not
  mean a new process.
- **Test each line of defence alone.** Two checks that cover each other hide
  a mutant of either.
- **Never touch what is not the row's:**
  - the graphics card;
  - the machine's card lock (every stand-in suite gives each runner a lock
    file of its own, and each coordinator it serves or starts a `card_lock` /
    `--card-lock` of its own; each suite's `machine_lock` points at its own
    folder, or is a tripwire);
  - a port the operating system hands out;
  - the user's clipboard, settings or processes.

  A suite that needs a port takes it from a range of its own, below every
  common system's range for passing connections (32768 and up), and
  bind-checks it. Every copy of a suite on the machine - a sweep's copies,
  another agent's gate - takes from that same range, so each copy claims a
  block of it for as long as it runs and hands out only its own
  (`_suites.QuietPorts`, a lock on one byte of a file every copy shares; the
  coordinator's suite has its own copy of the claim). A load whose settings
  name no port takes one from the llama.cpp suite's block too, never one the
  operating system picks:

  | suite | its range |
  | --- | --- |
  | `whisper` | 28500-28999 |
  | `llamacpp` | 29000-29499 |
  | `adapter` | the `llamacpp` range, a block of its own copy's: its servers' and its stand-ins' |
  | `server` | 29500-29999 |
  | `coordinator` | 30000-30499 |
  | `coordination` | 30500-30999, plus 32000-32767 for its coordinators' registries |

  The coordinator's own default registry is 31000-31999.
- **Kill only what the row started,** and find it by its command line.
  Suites put a tripwire on the runner's kill before anything runs.

## The card, the ports and the coordinator

| how the server runs | the card | a load's port |
| --- | --- | --- |
| stand-in model (`--backend mock`) | the in-process lease; no lock, no card | none |
| real card (`--backend real`, or `llamacpp`: every model on llama.cpp), no coordinator named | the machine's lock, whatever each model's engine, looked at as each session opens: free - taken, held until the pipeline holding it closes; a coordinator's - its rental, through it; anyone else's - waited for | a free one of the runner's own range (27000-28499, bind-checked), never one the operating system picks |
| with `--coordinator <url>` | a rental, had by ticket and heartbeated; every process its loads put on the card named on it, the whole list on every heartbeat | a port from the registry, heartbeated |

**The machine's lock says who holds the card at the top** (`llm-bootstrap-card.lock`
in the user's temp folder; the contract's `machine/card-lock.json`). The
coordinator started from its command line takes it, and then its port,
before anything else - its record included - and holds it while it runs, its
note giving its address: a server finds it there,
nothing needs configuring, and a second coordinator does not start. A server
run on its own takes it for its session. A coordinator started while one runs
waits for it, and grants nothing meanwhile. `--coordinator <url>` stays, as
the explicit way (the stand-in with a coordinator; a coordinator whose lock
the user cannot see).

**The memory is verified wherever the card is decided,** by the contract's
one function (`memory.py`): the coordinator reads the card afresh before a
grant (an ask that could never fit is refused at once; responsive work with
no room now at once, in numbers; scheduled work waits for the room, or - its
`if_no_room: fail` - is refused at once too; and a ticket with a
`not_before` is not offered the card before its time) and
before each model a rental adds (`/card/amend`, which waits for the memory
right after the rental's own unload); a server run on its own reads it before
each load. The needs are the runner's estimate per engine (`runner/needs.py`).

The coordinator started from its command line asks the machine
(`--hardware this-machine`, the default). It reads the card's memory through
nvidia-smi. When a rental lapses it ends every process the rental named, each
on its own proof, all at once and never under its lock; the card goes to the
next ticket once each outcome is recorded. A release is answered once the
processes are gone and the card's memory is back. Built in code, as every
suite builds it, it asks nothing, ends nothing and takes no lock unless it is
handed a machine, or a lock, that does.

**The coordinator watches every rental** (PLAN-4 D3), and none of it under its
lock. Each heartbeat says the work - `working`, its `progress` (tokens and
answers, counted by the manager where they happen), a one-piece answer's
deadline - and a rental that says it is working and has not moved for the
hang limit (`--hang`, 300 s) is taken back as a lapsed one is, the record
saying why. The card is read during a rental every `--watch` (30 s): free
memory under `--floor` (256 MiB), or a memory error a model's log said, or
an answer far slower than its model's normal while the card last read had
under `--full` free (1024 MiB), marks the rental DEGRADED - its holder told
on its next heartbeat, the answer under way ended, nothing new going on the
card, the rental never ended for it - until the card has room again.

**The card full is recovered from by itself** (S5b; the owner, 2026-09-27:
he closes programs, and the request goes through by itself). DEGRADED MEANS
WAITING, NOT FAILING:
- **The job is held.** An attempt the card's memory broke - an answer ended
  by the mark, or one whose model's log said the memory ran out - is thrown
  away and run again from the start by the queue, with the same seed, never
  counted against the job's `retries` or its judge (`attempt_card_full` in
  its record). Its run again waits in `acquire` (`on_full`) until the card
  source says the card has room, within the job's `card_patience_s`,
  counted from the first break; its progress says `waiting for the card's
  memory`, in words and numbers - how much is free, how much is needed, how
  much programs outside the bootstrap use, how long it has waited and how
  much patience is left. Patience 0 fails `card_degraded` at once, as
  before; a patience run out fails it saying how long and what the card
  read; and a card that fills again each time fails it after the queue's
  `recover_attempts` (3), naming each attempt.
- **Room again is noticed quickly.** While degraded the card is read every
  `--recover-watch` (2 s), and the mark clears on the first read with room
  for what the rental needs - its models' need, less what its processes hold
  now, plus the floor - never merely over the floor.
- **The model comes back first.** A model the application's session keeps
  that came off - its process died, its load failed - is loaded again once
  the card has room, through the usual memory check; a held job's own
  acquire loads its model the same way before it runs.
- **Only a card source that says when the card has room holds work**
  (`recovers`): a coordinator's rental, and the machine's lock when the
  runner reads the card. A source that never says - the stand-in, a row's
  fake - fails the work at once, as before; so does a load or a pipeline's
  own model, which is not a job's.
- **The same on a server run on its own** (the owner: "same behaviour as
  coordinator"): the machine's lock watches the card itself while it holds
  it - `[runner] card_watch_s` (30), `card_recover_watch_s` (2),
  `card_floor_mib` (256), and its models' memory errors - with the runner's
  own card reader, and marks, refreshes and clears as a coordinator does.

**The runner times everything it does** (PLAN-4 D12): a load in its steps,
each answer, each unload, the queue's own share of each job - one `timed`
line each of the statistics' own record, `<folder>/runner/model_stats.jsonl`,
folded as it is written into the model's statistics in
`<folder>/runner/model_stats.json` (`runner/stats.py`), read through
`GET /api/models` and shown in the Models tool (each model's load, first
token, tokens a second and real-time factor - median, 90th percentile and
last, each with its unit and its count - and its slow answers, with the
thresholds the server judged them by: `/api/models`' `slow_answer`, never
typed into the widget). The lines are what the summary is summed from:
`stats.rebuild(stat_records(manager.stat_lines()))` equals the one kept. An
answer is judged against its model's own normal - far slower is at least
`slow_answer_factor` (3) times slower AND `slow_answer_floor_s` (0.25 s)
lost - and counted on the heartbeat; a model with three measured loads
needs the largest + 256 MiB (`runner/needs.py`).

**A model whose stop failed may still be on the card.** Its process stays
named on the rental - the models beside it named too, each until its own stop
returns - its stop is asked again before the next load and when a pipeline
closes, and nothing loads while that stop fails.

## The records

| where | what | read with |
| --- | --- | --- |
| `<folder>/jobs.json` | the queue's store | the queue; never by hand |
| `<folder>/requests/<correlation id>/` | every request: its events, manifest, the request as built, one folder per attempt (the exact call to the model and the model's count of it included), and a line for each attempt the card's memory broke (`attempt_card_full`: which, why, the card's numbers); a request checked before it was sent (D16), what was held (`held.json`) and what was sent (`sent.json`, `tuned` when changed) | `jobqueue.request_record(folder, correlation_id)` |
| `<folder>/runner/runner_events.jsonl` | every load and unload, each naming the request it served and what else was up beside it (`beside`); every take and give-back of a real card, named by its source (`machine lock`, `coordinator rental`) and its pipeline; every pipeline's open, close, lapse and loss; every line of a model's log that said the card's memory ran out (`memory_error`), and the card said degraded and room again (`card_degraded` with its numbers, `card_restored` with the session's models loaded `again`), and each wait of a job's acquire for the card's memory (`card_full_waited`) | `runner.read_events`; `max_concurrency` must never find two loads of one model, and `beside_faults` must find nothing |
| `<folder>/runner/model_stats.jsonl` | every thing the runner timed - a load, an answer, an unload, the card's memory, a job's share - one `timed` line each, and `<folder>/runner/model_stats.json`, the statistics summed from them (PLAN-4 D12) | `manager.stat_lines`, `stat_records`; `stats.rebuild` of them must equal the summary |
| `<coordinator data>/events.jsonl` | every ticket (with its token's sha256, never the token), rental, port, reclaim; a line whenever a rental's list of processes changes; and, when a rental ends, what became of each process it named; a rental found hung (`rental_hung`), marked degraded and room again (`rental_degraded`, `rental_restored` with the free memory and the room it needed) - never a line per heartbeat or per look at the card | the coordinator's `/events`, and its dashboard |

**What happens to old records is the application's call.** Request records
are never pruned by the queue. They are files, and no route reads them.

## Traps this code has already paid for

Each is a rule now. Where it is Windows-only, it says so.

- **Windows: `shutdown()` does not wake a thread blocked reading a socket.**
  Only a real `closesocket` does, and a response that still holds the
  socket defers `close()`. Detach and close: `llamacpp._shut`.
- **Windows: `Popen` reports a terminating process as exited**, and `wait()`
  returns at once. A killed CUDA process gives its memory back in about a
  third of a second but lives about two seconds more. Wait on the process
  handle (`WaitForSingleObject`) before starting the next load.
- **Windows: a connection to a closed localhost port takes about two seconds
  to be refused.** A heartbeat is bounded by its interval, or an outage
  stretches every beat to two seconds.
- **Windows: a pid is reused constantly.** See rule 9. A row asks whether a
  process it started is gone by its load id, read through a handle to it
  (`test_llamacpp.gone`), never by its pid alone.
- **Windows: SO_REUSEADDR lets a second socket share a port** a first one
  holds with it - and `http.server` sets it. Measured against the parts: one
  started on a port a program already listened on started, said it was
  listening, and heard none of 20 requests. Both parts (`Listener`) and the
  llama-server stand-in bind SO_EXCLUSIVEADDRUSE.
- **http.server reads no body its handler does not.** A body no route reads
  - a GET's, one sent in chunks with no Content-Length - was read as the next
  request on a connection kept open. Every body within the cap is read, by
  its route or set aside; one in chunks is refused 411, never read, and its
  connection closed.
- **http.server answers a method no `do_` method takes before any check.**
  Each part checks every request in `Handler.parse_request`, before any
  method of the handler runs: a method added to a handler cannot skip the
  checks.
- **http.server tells an asker that says `Expect: 100-continue` to send its
  body before any check** - inside its own reading of the headers. The server
  (HTTP/1.1) notes it there (`handle_expect_100`) and says `100 Continue` only
  once the checks let the request through; a refusal is sent without it, the
  body never read and the connection closed. The coordinator speaks HTTP/1.0,
  which never says it.
- **A suite's tripwire lives in its own process.** A coordinator a row starts
  from its command line takes the machine's lock before anything else unless
  it is given one of the row's own (`--card-lock`). Found after a merge: two
  rows written before the command line took the lock started coordinators
  with none named - and took the machine's own.
- **A socket timeout bounds one read, never a request.** A body dripping a
  byte a second passes any bound on one read, and held a handler thread for
  ever. Each part bounds the whole request - its line, its headers, its body
  - from the moment it starts waiting for it (`_Reads`, `--read-timeout`),
  and closes a connection whose request has not come whole.
- **The first timed wait of a fresh process can end early** (measured:
  `Event.wait(0.05)` under 45 ms in 14 of 200 processes). A stand-in's timed
  load waits on a monotonic deadline.
- **Windows: a client asking `localhost` tries `::1` first.** Against a part
  listening on `127.0.0.1` alone it waits out that refusal - measured: urllib
  took 2.0 - 2.1 s a request. Name a part by `127.0.0.1`.
- **A refusal that never read the body must not close at once.** Closed with
  the body still coming, the asker is sent a reset in place of the answer -
  measured here: a real client (urllib) sending 8 MiB or more over a small
  cap never heard the 413 (0 of 20), and one of 1 KiB sometimes did not.
  Kept a quarter of a second before it closes, every one did (20 of 20, up
  to 64 MiB). `UNREAD_GRACE_S`, in both parts.
- **llama-server holds its port and answers `/health` 503 from the first
  second of a load.** Its log says "listening" only once the weights are up;
  the log is not the port.
- **A port the operating system picks can be handed to any connection**
  between the pick and the bind. The coordinator's range and every suite's
  ports are below the passing-connection range (32768 on Linux, 49152 on
  Windows).
- **An interval read off a renewed rental grows** (renewal moves
  `expires_at`, keeps `granted_at`). Fix a lifetime at the grant.
- **A thread blocked in a held wait cannot hear a cancel.** A wait on a
  ticket blocks until the coordinator answers, up to its hold, so the server
  makes it on a thread of its own: a cancel or a stop gives the ticket up -
  which answers the wait at once - and never waits for the answer.
- **A wait that holds a lock holds up everyone who needs the lock.** The wait
  for the card holds none; look again under the lock after it.
- **A load that holds the manager's lock holds up every answer.** Found: a
  bind waited on it while the start session's model loaded, and a second
  model's generation would have waited out the first one's load. A load holds
  the load lock alone; a stop, none - and a stop begins, under the manager's
  lock, before a model could be handed to anyone else.
- **A barrier that asks only about the job it would start misses the job it
  just started.** Found on the real card (S17b): "a job whose model is not up
  waits while answers run" let a load start into an empty queue and, in the
  same pass, a job on the model that load had to take off - which then held
  it in use, and the load failed for room. A load under way holds back what
  is behind it (`_next_to_run`). A row that sees it is driven one pass at a
  time by hand, never by timing; a stand-in with one model's room refuses
  as the runner does (`test_queue.OneRoom`). It had shown once, as a red
  sweep control dismissed as a race: a rare red is a finding.
- **`return` inside `finally` swallows whatever was raised.** Python 3.14
  warns of it, and the gate fails on the warning.
- **`Path.write_text` writes CRLF on Windows.** Write bytes.
- **A file named like a standard module shadows it** (`queue.py` shadows
  `queue`; the queue is `jobqueue.py`).
- **An object nobody refers to is collected, and its file closed.** A lock
  file closed by the collector lets its lock go. Hold what holds a lock by
  name.
- **A reader with a fallback hides a broken writer.** A row about a written
  file reads the bytes.
- **A cleanup that looks for processes by something a mutant can change
  leaks them.** Find them by what no mutant touches: the row's own folder,
  the load id on the command line.
- **A stand-in that replaces a module must hold the real one.** A row swapped
  `subprocess` for a stand-in that looked names up in `subprocess` - itself:
  its own RecursionError was caught as the query's failure, and two rows
  passed down the path they were not about (a sweep's mutant survived them).
  Such a row now requires its stand-in to have gone the way it provoked.
- **A loop "until none is found" is a wait too.** A reap whose ends did not
  take found its processes for ever, and the suite timed out; it says so
  after a bounded number of rounds.
- **A tripwire in a suite's own process does not reach the processes it
  starts.** The coordinator suite's process could never take the machine's
  card lock, but two coordinators it started without `--card-lock` took the
  real one - where the owner's own application may hold it - and, three
  copies running at once, one found another's holding it and would not
  start. Every coordinator command line it starts is held to a lock of its
  own now (`of_its_own`), and a stand-in's port is given by the row, from
  its copy's block, never picked by the process that starts it.

## The client

Two files, built from the files in `parts/` in the order `parts/MANIFEST`
lists them under each file's name, with no toolchain, and both committed:

- **`llm-bootstrap-kit.js`, the kit:** one script an application's page
  includes, with every widget, its feeds, the containers, and the calls
  that place them - all inside one function, so its names are its own and
  never the page's. It gives the page `llmKit` (its calls), `llmClient`
  (its door, for rows and a console), and the contract's `llmIds` and
  `llmContract`.
- **`index.html`, the bootstrap's own page:** the kit, byte for byte, and one
  script (`page.js`) that places its widgets with the kit's own calls - the
  kit proven by its own use.

From the client's own folder (`gate.py` and `mutate.py` run from the root):

    cd llm-bootstrap-client
    python build.py            the kit and index.html from the parts (both committed)
    python build.py --check    is each what the parts build? (the gate asks)
    python test_client.py      the rows, run in node on a fresh build

The tally counts checks, not rows: one row usually makes several.

`server.py --client llm-bootstrap-client` serves the page at `/`, beside the
routes, and the page asks its own address. The kit embeds the contract - its
id grammar, its validator, every route and shape (`@contract/...` and
`@routes` in the MANIFEST) - so it asks by route id and checks every answer
the way the server's own suite does.

**What a person sees on the bootstrap's own page.** Its bar: a container
docked to the top of the window. Below it, a strip with one door - *Tools* -
to the instruments, and the tool shown; a panel for a widget that has one,
beside its container. The page opens on its first tool, Bench.

| part | what it is |
| --- | --- |
| `kit.css` | the kit's one look, scoped under `.llmk`: put in the page by the kit when the first thing is placed, so an application's page keeps its own |
| `dom.js` | `el(tag, props, ...kids)`, `setKids(node, kids)`, `named(name)`: all a view builds with |
| `store.js` | the one store: frozen state, `get`, `set(fn)`, `watch(select, fn, name)` |
| `api.js` | the one door to the server: `api(routeId, {params, query, body, quiet})`, at the page's own address or the one the page names |
| `registry.js` | what the kit is made of: `ITEMS`, `TOOLS`, `STARTS`, `SEEDS` |
| `log.js` | the log: its light, its tool; `logLine(level, area, text)` |
| `feeds.js` | what the kit keeps asking: `follow(id)`, `refresh(id)` |
| `workspace.js` | the folder picker, and `vmGate`: what needs a folder, and why it is dark |
| `queue.js` | the model's progress (the scheduler graphic) and the queue's panel |
| `session.js` | the session: health's `pipeline` - holding, waiting, the card full (degraded) or taken back, and why in the server's words - the card full, in numbers, and that its work runs again by itself - its panel the session whole and every model in it; and the words a model is said in (its engine, where it stands, pinned or on demand) |
| `models.js` | the Models tool: the session at its head, each model where it stands, its health and its header as its engine reads it, its statistics (`vmStats`: a line a measure, and a slow answer as the server flags it), and the disk measure |
| `bench.js` | Bench: a raw request, as a queue job - for a whisper model, a transcription, its answer shown whole |
| `check.js` | Check, the request check (D16): the held requests, each shown as the server holds it - the model, the count against its context, the time the statistics expect, the messages or the audio, every parameter and where it came from - tuned, sent or cancelled; and `llmKit.requestView`, the seam for an application's own view of its part |
| `tools.js` | the instruments behind one door: the Tools menu and the one shown; `region` |
| `container.js` | containers: slots, boxes, shedding, the tray, the docks, Customization Mode, the four layout edits |
| `journal.js` | every layout edit recorded, undone, redone, kept in the folder |
| `kit.js` | last of the kit: `llmKit.place` and `llmKit.container`, and what runs when the first thing is placed |
| `page-*.html`, `page.css`, `page.js` | the bootstrap's own page around the kit: its markup, its look, and its one script of kit calls |

### How the page works

- **One store.** `store.set(fn)` replaces the state with `fn`'s answer,
  frozen. `store.watch(select, fn, name)` calls `fn(select(state))` at once
  and again whenever the selection changes - compared structurally, told at
  the end of the turn. A view model is a `select`.
- **One door.** `api` answers `{status, answer}` for every status the route
  declares, and throws `NoAnswer` for anything else - a status or a shape the
  contract does not declare, or no answer at all - said in the log once
  (unless `quiet`: a feed says it itself, once per silence). A body for a
  route that takes none throws before anything is sent. It asks the page's
  own address, or the one the page names (*An application's own page*,
  below).
- **Nothing runs until something is placed.** The kit, loaded, asks the
  server nothing and puts nothing in the page. The first thing placed puts
  the kit's look in the page, hears the keys, and runs `STARTS` - once.
- **Feeds** are what the page keeps asking (`health`, `jobs`, `models`): a
  view `follow`s a feed for as long as it is built; the feed is asked only
  while one view follows it and the browser shows the page, one ask at a
  time. After an action that changes what a feed says, `refresh(id)`.
  - **The answer lands in `state.feeds.<id>`** = `{answer, status, at,
    ok}`: `ok` is null before the first answer, true for a 200, false for
    anything else.
  - **A feed that stops answering keeps its last answer**, with `ok: false`.
    A view model checks `ok` before it reads `answer`, or it shows stale
    words as current (`vmQueueItem` and `vmModels` do).
  - **A feed is shared.** One ask serves every view that follows it: the
    model's progress follows `health` and `jobs`, and Bench follows `models`
    and `jobs`. Following a feed another view already follows asks nothing
    more.
- **Whether a folder is bound** has two readings. `state.workspace` is what
  `vmGate` reads: the server's word from `server.workspace.get` and from
  each health answer - held while this page's own ask, bind or unload is
  under way, which will say it. `state.feeds.health.answer` is health's raw
  answer at its last ask. For "what needs a folder", ask `vmGate`.
- **Regions.** The pane shows one tool, a panel one widget's panel.
  When the choice changes, the old view is stopped before the new one is
  built. A view not shown has no watchers and follows no feeds: a hidden
  thing costs nothing.
- **Containers** (`container.js`): three slots - start, middle, end - of
  widgets side by side, each in the box it declares. A container's own
  observer keeps its length along its axis in the store
  (`containers.<name>`); widgets are shed into its tray, in its drop order,
  until what is left fits, and come back as it grows. Nothing in
  `container.js` knows which container is the bootstrap's bar.
- **The layout** changes only by the four edits - move, put away, restore,
  set priority - each naming its container, recorded by the page's one
  journal and kept in the bound folder (`server.config.*`), every container
  by its name: `{v: 2, layout: {<name>: {slots, shed, known}}}`. A container
  of another page, kept in the folder, is carried as it was; step 3's one bar
  (`v: 1`, left to right) is read as the bar's. Undo and redo move a
  pointer; the layouts are rebuilt.
- **A widget the page adds later is where the page puts it** (the owner's
  ruling of 2026-09-27 on S14k's R2, built in S14b): *"the point of the
  widget is to self-contain its feature so we can place it where we will
  and style it how we want."* A layout says the widgets it has seen
  (`known`); a widget its container holds and it has not seen goes into the
  slot the page gave it - before the first widget given after it there that
  is there now, else at the slot's end - and is seen from then on. Seeing a
  widget is not an edit: no journal records it and no undo takes it back.
  Only a widget the person put away is in no slot and seen, so only those
  (and the shed) are in the tray: the tray stays derived, never stored
  (R-F12). A layout kept before `known` was said has seen what it names -
  its slots and its drop order - so a widget put away there stays away when
  the drop order names it.

### An application's own page

The bootstrap gives the widgets, the API and the stack under them; an
application builds its whole page itself - the bootstrap's page has no place
for an application's own screens. The application's page includes the kit,
places the widgets it wants, and asks the bootstrap server from an address
of its own. `example/index.html` is one such page, and a row runs it:

    <script>window.llmServer = 'http://127.0.0.1:7072';</script>
    <script src="llm-bootstrap-kit.js"></script>
    <script>
      llmKit.container('bottom', { name: 'progress', slots: { middle: ['queue'] } });
      llmKit.place('bench', document.getElementById('bench'));
    </script>

- **The page names its server,** once, in a script that runs before the
  kit's: `window.llmServer = 'http://127.0.0.1:7072'`. Every request goes
  there. Named nowhere, every request goes to the page's own address, as on
  the page the server serves. An address that is not one -
  `http://<host>:<port>` and nothing after it - is never guessed around:
  nothing is asked of anybody, and every ask is no answer, saying why.
- **The server allows that page, and no other:** `server.py --allow-origin
  http://127.0.0.1:7867` (repeatable), the page's address as the browser
  shows it. A request from any other page is refused `403
  origin_not_allowed` before any route, so a website open in the same browser
  can never act on the server - not even with a request its browser sends
  without asking first. A request with no Origin, and one from the server's
  own page, are answered as before - when they are sent to one of the
  server's own names: `127.0.0.1`, `localhost`, `[::1]`, the address it
  listens on, the host of an allowed page. Any other name is refused `403
  host_not_allowed` first, with or without an Origin: a website whose name
  was made to point at this machine would otherwise pass for the server's
  own page. The page names its server by one of those names. The rule is the
  contract's (`origins/server.json`), and the coordinator keeps it too
  (`origins/coordinator.json`, no page allowed unless it is told).
- **Proven in a real browser** (headless Chrome 154): the server's own page
  opens on Bench and binds a folder; the same page on an allowed address is
  answered - its preflight too - with nothing wrong in its log; on any other
  address every request is refused in the server's log, and the page sees
  only silence.

**The kit's calls** (`kit.js` says them whole). A call the kit cannot do as
asked - a name it does not have, a place that is not an element, a setting
it would not read - throws at once, saying why, before anything is placed.

- `llmKit.place(name, element) -> stop()`: one widget, at the end of an
  element of the page. A widget with a box - `session` (the card the
  pipeline holds, and the models in it), `queue` (the model's progress),
  `log` (the log's light), `workspace` (the folder picker), `customize` -
  sits in the box it declares, its panel opening beside it;
  an instrument - `bench`, `models`, `check` - fills the element; `tools`
  is the instruments behind one door. "Next to one of your own things" is an
  element the page puts there. `stop()` takes it out, and stops everything
  it watched and asked.
- `llmKit.requestView(name, {title, shows(held), view(node, held, give)})
  -> remove()`: an application's own view of its part of a held request, in
  Check (D16). `shows(held)` says whether it is for this request; `view`
  builds into `node` from the held request as the server says it (`GET
  /api/jobs/{id}/held`) and returns its stop - read-only, or calling
  `give(request)` with a changed request, which is then what Send sends.
- `llmKit.container(where, {name, slots, shed, axis}) -> stop()`: widgets
  that belong together, side by side in slots (`start`, `middle`, `end`),
  each slot's in the order given - so "next to another widget" is the same
  slot. `where` is an edge of the window (`top`, `bottom`, `left`, `right`:
  fixed there, the body padded by its size, its axis the edge's) or an
  element of the page (in its flow; a row unless `axis: 'column'`). `name`
  is what the folder keeps its layout under; `shed` is its drop order. It
  holds only the widgets it was given, and Customize, if given, is never
  shed.
- **Docks that meet at a corner never cover each other:** each dock says
  its size to the page as `--llmk-dock-<edge>` on the body, and a column
  docked left or right stands between the rows docked top and bottom by it.
  A page's own fixed things can keep clear of the docks by the same values.
  A dock's size is its box as the browser measures it, rounded up to a whole
  pixel: nothing of the page is ever under it.
- **The page serves the kit, from the llm-bootstrap its server runs from**
  (its file, by its path; never a copy kept elsewhere): the kit carries the
  contract it checks answers against, and a copy older than the server
  reads a changed answer as no answer. No route of the server serves it -
  decided in S14b (S14k's register R4): the application's page must load
  when the bootstrap server is not up yet, and then say *no answer* until it
  is, where a kit asked of that server would leave the page without
  `llmKit`; the contract declares JSON answers, and the server's own page is
  outside the routes on purpose; and the bootstrap gives the widgets, never
  an application's page or what serves it.
- **Proven in a real browser** (headless Chrome 154, S14k): the bootstrap's
  own page, its bar docked to the top, sheds at the widths the sum says, to
  the pixel (at 14 px an em, all placed at 724 px, one shed at 723; the
  tray shown, the next at 555); the example page, served by
  `python -m http.server` from another address and allowed, docks the
  model's progress to the bottom and runs Bench end to end in its own div -
  nothing refused, nothing in the console, no icon asked of either server;
  a column docked left from the console sheds at the heights its sum says,
  and ends above the bottom dock, its Customize on top.

### How to add a surface

1. **A part.** `parts/<name>.js`, named in `parts/MANIFEST` under
   `[llm-bootstrap-kit.js]`, before `kit.js`. Registering only stores
   functions, and they are called when a page places the thing, so the
   order matters only for what a part reads as it loads. A part's text
   must not hold `</script`: every script part is inside one inline
   script, and the build refuses a part that would end it.
2. **Its state,** if it has any of its own: `SEEDS.<name> = {...}`, the
   part's own slice. A view that only shows a feed needs none.
3. **Its view models.** Pure functions of the state, returning exactly what
   the view paints - no more. A field nothing paints still changes the
   value, and the view is painted again for nothing: a container's size is
   not in `vmContainer`, so a resize that places the same widgets rebuilds
   nothing.
4. **Its view.** `view(node)` builds into `node` with `el` and `setKids`,
   watches its view models, follows the feeds it shows, and returns one
   function that stops them all.
5. **What it is.** A page places it by its id (`llmKit.place(id, ...)`, or
   in a container's slots), so an id is the name an application writes.
   - A widget with a box: `ITEMS.push({id, title, box: {w, h}, view,
     panel})` - `w` in em, `h` in lines; the box never grows, so what it
     shows fits or is clipped. `critical: true` is never shed and never put
     away.
   - An instrument: `TOOLS.push({id, title, rank, view})` - placed alone,
     filling its element, or behind the Tools door in the order of its rank.
     The ranks now: Bench 10, Models 20, Check 30, Log 90. The row *"the tools: one
     door, not tabs"* pins the menu's order, so a new tool is added to its
     expectation too; the door's own words are made from the tools.
   - An application's own screen has no place here: it is on the
     application's own page.
   - Something to learn from the server when the first thing is placed:
     `STARTS.push(fn)`.
   - The bootstrap's own page shows it only if `page.js` places it.
6. **Its actions** are functions that ask through `api` and set the store.
   Show a refusal in the server's own words, where it was asked; say it in
   the log with `logLine(level, area, text)` - the area is one of
   `LOG_AREAS` (add yours).
7. **`data-el` names everything a row reads or clicks** - `bench-run`,
   `model-speed-story-12b`. A row finds an element by its name, never by
   where it sits.
8. **Its rows**, in `test_client.js`:
   - `row(name, async () => { ... })`, and `check(what, ok, detail)`;
   - `started({ fetch })` builds the bootstrap's own page, and
     `started({ fetch, server })` one told the address to ask
     (`window.llmServer`); `loadKit({ fetch })` is the kit alone, as an
     application's page includes it, nothing placed (`page.kit` is its
     calls, `spot(page, id)` an element to place into); `loadHtml(file,
     { fetch, search })` runs a page of its own from its file, as a browser
     would;
     `contractFetch({[routeId]: (req) => ({status, body})})` answers from the
     contract's examples, except the routes you answer yourself. A handler
     may return a Promise: one never resolved holds the answer back, one
     rejected is no answer at all;
   - `fetch.asked` is every request in order, each with the `server` it was
     sent to (`''`: the page's own address) and the `path` after it;
     `asked(fetch, routeId)` counts one route's - find a row's requests by
     route, never by place;
   - `example(routeId, name)` is a copy of a contract example's answer;
     `health(over)` is the idle health example with `over` laid on it;
   - `page.click(name)`, `page.key(key, mods)`, `openTool(page, id)`,
     `page.named(name)`, `page.text(name)`; `page.settle()` lets the page
     finish what it started; `page.clock.pending()` counts the timers set;
     a name is unique within its container, so on a page with several
     `page.within(container)` finds, reads and clicks inside one;
   - `tick(page, ms)` moves the fake clock. A feed sets its next timer only
     once its answer is in, so one tick asks each feed at most once: count
     asks with one tick per period;
   - **a row about a shared feed makes it its own first:** it shows a tool
     that follows nothing (`openTool(page, 'log')`), and for `health` or
     `jobs` takes the model's progress out of the bar
     (`layout.bar.slots.middle = []`, as the row *"health is asked only
     while the graphic is shown"* does);
   - the DOM stub has no layout: a container's size is set in the store, as
     its observer would set it (`barAt` for the bar, `sized` for any);
     an offset a dock reads (`offsetHeight`, `offsetWidth`) is set on its
     node before a `resize` is fired.
9. **Its mutants**, with `("client",)` as their suites, in a file outside
   the tree - it is the run's input, not part of the product. The run's
   controls run every suite, whatever the mutants name: about five and a
   half minutes of stand-in processes, even for a list of client mutants.
   That is on purpose - a control is a detector too, and a red one is read,
   never run again until it goes green.

### The client's rules

- **The contract declares every key the page reads.** A key read and not
  declared breaks silently when the server renames it: a job's `progress`
  and a runner's candidate were declared for this reason.
- **The server's words, never the page's judgement.** A model's problems are
  the sentences a load would log; a refusal is shown as the server said it.
- **The page never rewrites what a person is typing.** Controls are built
  once; a value is set only when the store holds another.
- **The way back is never hidden.** Customize is never shed and never put
  away. The layout's keys (Ctrl+Z, Ctrl+Y) are its own only in
  Customization Mode, and never in a field someone types in.
- **One rule in one place.** A guard that repeats another hides the mutant
  of either: `measure` had a "no change" guard the store's own comparison
  already made, and no row could tell - it was removed.
- **The kit keeps its names to itself.** An application's page has names of
  its own - its own `store`, its own `api` - so every part of the kit is
  inside one function, and the kit gives the page only `llmKit` and
  `llmClient` (and the contract's `llmIds` and `llmContract`). Its look
  reaches only what it builds (`.llmk`). A part never reaches the page's
  names, and the page reaches the kit only by its calls.

### Traps this client has already paid for

- **A hidden page runs no rendering steps**, so it hears neither a resize nor
  a `ResizeObserver`. A bar measured while hidden kept 22 px for ever, and
  shed everything; it is measured again when the page is shown.
- **The stub has no layout, so a row cannot see an overflow.** The shedding's
  sum was proven in a real browser, at real widths, to the pixel: at 14 px an
  em everything needs 723.8 px; at 724 all are placed, at 723 one is shed.
- **The page asks things of its own.** A row that takes the last request for
  its own breaks the day the page starts asking something more: find a row's
  requests by route.
- **Answers merged by one-at-a-time hide a stale clock.** A refresh on the
  same tick as the feed's own timer made the old and the new one fire
  together; move the action inside the feed's period.
- **JavaScript typed through a shell and Python loses its escapes.** Write a
  file of rows with an editor, never through `bash -c "python -c ..."`.
- **Every script part is inside one inline `<script>`,** so the characters
  `</script>` anywhere in a part - a comment included - end the page's script
  there, in a browser as in the rows. Found by building: a comment showing an
  application's page how to name its server, and every row failed to load.
  The build now refuses such a part, naming it, in any case of the letters.
- **Two docks that meet at a corner covered each other.** Seen in a real
  browser: a column docked left ran the window's whole height, and a row
  docked to the bottom covered its end - Customize, the way back, with it.
  Each dock now says its size to the page (`--llmk-dock-<edge>`), and a
  column stands between the rows by it.
- **An offset is rounded; the box is not.** A bar 33.17 px high has an
  `offsetHeight` of 33, and the page padded by it had 0.17 px under the bar
  (S14k, in a real browser). A dock is padded by its measured box rounded
  up (`getBoundingClientRect`, then `Math.ceil`); the stub's elements answer
  it from a row's `rect`, else their offsets.

## Not here yet

- **One process's own memory, measured by the process.** A rental names
  every process it put on the card, with the memory it uses (`mib`): the
  runner fills it from the card's free memory before the load less the free
  memory at ready, then after its first answer (PLAN-4 D12, S11b) - a
  measure only when nothing else of this server came or went between the two
  reads, and only where the card is read. A program outside the bootstrap
  that takes or frees memory between the two reads still moves it; a
  per-process reading would need the card asked for each process.
- **The operator's page showing slow answers.** A slow answer on a card
  with room is counted by the coordinator and not shown: `/state`'s `watch`
  does not carry the count yet.
- **A mixed mode**, applications sharing the card by memory. Not the normal
  mode (rule 6); if it comes, it is a mode of its own.
- **Several named configurations.** The files are content-addressed already;
  more of them is more files and a name each (the seam is in `configs.py`).
- **A read past the cache on Linux and macOS.** The disk measure says it
  cannot tell there; `O_DIRECT` is the seam (`runner/diskread.py`).
- **A layout edit repaints its whole container.** Every widget's view in it
  is rebuilt - its feeds followed again, and asked at once. Cheap at five
  widgets; rebuilding only what moved is the change when there are many.
- **A kit that says it is older than its server.** An application serves
  the kit itself (S14b: no route of the server serves it); health says the
  server's `contract_version`, and the kit carries its own. Comparing the
  two when the kit starts would name a stale copy - the seam is `STARTS`
  in `parts/kit.js`.

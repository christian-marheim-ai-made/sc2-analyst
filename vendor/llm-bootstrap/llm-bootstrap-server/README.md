# llm-bootstrap-server

The server part: the job queue, on HTTP, bound to one folder at a time.

    python server.py [--port 7072] [--host 127.0.0.1] [--workspace <folder>]
                     [--backend none|echo|mock|real|llamacpp] [--runner <settings.toml>] [--reclaim]
                     [--coordinator <url>] [--name <name>]
                     [--client <folder>] [--allow-origin <page address> ...]
                     [--max-body <bytes>] [--read-timeout <seconds>]
    python test_queue.py      the queue's rows - no model, no GPU, no network
    python test_server.py     every route over real HTTP, and a real process killed
    python test_runner.py     the runner's rows - the stand-in model, no GPU, and it cannot kill
    python test_stats.py      the timing statistics store's rows - nothing outside the process
    python test_llamacpp.py   llama.cpp against a stand-in llama-server - no GPU, real processes
    python test_whisper.py    the stand-in whisper-server, a whisper model's header, and the
                              whisper engine's process against the stand-in - no GPU
    python test_adapter.py    the runner behind the queue, end to end - no GPU
    python test_coordination.py  the server as a coordinator holder - a real coordinator, no GPU
    python test_real_card.py  THE REAL CARD - a real llama-server and model; skips unless asked
    python cardwatch.py       the card now: its memory, and every model server on the machine

Try it with the stand-in model:

    python server.py --backend echo --workspace D:/some/folder
    curl -s -X POST 127.0.0.1:7072/api/jobs -d '{"kind": "generate", "input": {"prompt": "hello"}, "gen": {"model": "demo"}}'
    curl -s 127.0.0.1:7072/api/jobs/j-0001/result

`--backend mock` puts the runner behind the queue with the stand-in model, the
same way - and `curl -s 127.0.0.1:7072/api/health` then shows the runner: the
model loaded, its load id, and the lease held by this server's actor. To see
a load fail with no card - a model `failed`, and why, as the Models widget
shows it - name it in the stand-in's settings, `--runner <settings.toml>`
with `[mock] fail_load_models = ["<key>"]`: its every load fails once its
load time is waited out (S14b R3). A key the settings do not name is refused
at the start.
`--backend real --runner <settings.toml>` is the real thing, and loads each
model onto the card on its own engine - llama.cpp, or whisper for a model
that names it (`[models.<key>] engine`). `--backend llamacpp`, its name from
before engines were named, runs every model on llama.cpp.

## What is here

| file | what it is |
| --- | --- |
| `jobqueue.py` | the queue: one bound folder, one worker that starts each job on a thread of its own - different models answer at the same time, each model one at a time (S17). What the queue owns and what the application owns is written at the top of the file |
| `server.py` | the queue on HTTP. Every route and every answer is one the contract declares (`../llm-bootstrap-contract/routes/server`) |
| `configs.py` | a folder's kept configurations for the application: content-addressed (named by the sha256 of their canonical JSON), one in use, read back checked against its name |
| `runner/` | the models: side by side on the card, the lease on it, the record of every load. `base.py` what a backend is · `config.py` the runner's settings, one TOML file · `mock.py` the stand-in model, answering in each engine's shape · `manager.py` the manager · `engines.py` the engines a model runs on - llama.cpp and whisper - each one's program, process names, settings, way of answering and proof (`is_ours`) · `llamacpp.py` llama-server as a child process, one per load · `whisper.py` whisper-server as a child process, one per load - its command line, proof, start, give-up and stop, and its transcription in one piece · `gguf.py` the model file's header · `ggml.py` a whisper model file's header: the ggml magic, eleven numbers, its shape named · `whisper_fields.py` every field whisper-server 1.9.1 reads from a request, as its source was read · `jobobject.py` the kill-on-close job · `adapter.py` the runner behind the queue · `cardlock.py` the machine's card lock, when nothing coordinates the card · `modelhealth.py` what can be known of a model without loading it · `diskread.py` how fast a model's file comes off its disk · `stats.py` the timing statistics kept per model and per measure |
| `fake_llama_server.py` | a stand-in for llama-server: its command line and its routes, no model - with a chat template and a tokenizer of its own, simple enough to count a request by hand, and switches of its own to misbehave on purpose. Everything llama.cpp does here is proven against it |
| `fake_whisper_server.py` | a stand-in for whisper-server 1.9.1, as the real one was measured: its command line, its routes under `--request-path`, a fixed transcript in its verbose shape - with switches of its own to load slowly, exit at once, die or hang mid-answer. The whisper engine's process is proven against it |
| `_job_parent.py` | the parent the job rows terminate for real |
| `_suites.py` | what the suites share: the tally every unittest suite ends with, counted in rows (a class that could not be set up counts every row it has), ports of a copy's own - the block of a suite's range each copy claims for as long as it runs - and what a row does on a busy machine: a question the operating system could not answer asked again within a bound (`asked_again`), a process killed and waited for until it has ended (`killed`), a start given `START_S` |
| `coordination.py` | the coordinator's client half: the card's rental, had by ticket, and the ports, written against the contract's routes - it never imports the coordinator part |
| `reference_client.py` | the reference client (PLAN-4 D13): an application's whole use of its server, in the standard library - start it and bind a folder, open and renew the pipeline, submit a job and wait for its end (`?wait_s`, nothing polls), claim the result and forget the job, close; a host's load and unload; a component handed its host's address and pipeline. Every answer checked against the contract as the client's JavaScript checks it, and the contract's version first. What a client in any language does (GUIDE.md, *Use the server from any language*) |
| `cardwatch.py` | the card watched from outside: its memory, and every model server - every engine's: llama-server, whisper-server - with its parent, sampled all the time. It asks and never acts |
| `test_real_card.py` | the runner on the real card - skipped unless `LLM_BOOTSTRAP_REAL_CARD` names a settings file (see *On the real card*) |

**The route table is read from the contract** when the server is made, and a
route the contract declares that the server does not serve stops it from
starting. **Request bodies are checked with the contract's own validator**, so
a body of the wrong shape is refused `bad_request`, naming the first problem
in the contract's own words.

**The backend is the model.** `--backend none` (the default) has none, and
every generation fails saying so. `--backend echo` answers each request back:
a stand-in that runs the whole server with no model and no GPU, and nothing
more. The real model joins through the same seam - `backend(request, params,
run)`: the runner (`runner/`), with `--backend mock`, `--backend real` or
`--backend llamacpp`.

## The runner's settings

One TOML file, `--runner <file>`, in four sections: `[runner]`, how models
are run; `[mock]`, how the stand-in model behaves; `[defaults]`, a model
setting every model gets unless it names its own; `[models.<key>]`, one
model each. `runner/config.py` is the one source of these; the table says
each as it stands.
- **A section, a key or a model setting nothing reads is refused,** and the
  server does not start, naming it: a misspelt key never reads as one that
  took effect. A model setting its engine does not read is refused too - a
  whisper model has no `ctx`, a text model no `threads`.
- **No `[models.<key>]` at all:** one stand-in model, `demo`.
- **`--backend` on the command line wins** over the file's `backend`, and
  the server says so as it starts.
- **Nothing here says where anything is written:** the records follow the
  bound folder. A relative `models_dir` is read from the settings file's own
  folder.

| `[runner]` | default | what it changes |
| --- | --- | --- |
| `backend` | `"mock"` | `mock`, the stand-in; `real`, each model on its own engine; `llamacpp`, every model on llama.cpp |
| `llama_server`, `whisper_server` | `"llama-server"`, `"whisper-server"` | each engine's program: a path, or a list - an interpreter and a script |
| `models_dir` | `"models"` | where a model's `file` is |
| `session_models` | `[]` | the models the server's session opens with as it starts - an application that starts ready: held until it is closed or the server stops, its models loaded one after the other from the start and kept side by side, each once, at most `max_loads`. The server says `listening` before they are up: a `load?wait_s` of each waits for it |
| `max_loads` | `8` | how many models are on the card at once, side by side (1 to 8). A model a job needs past it takes an on-demand model off - the one used longest ago - never a pinned one |
| `pipeline_keep_s` | `120.0` | how long an application's pipeline stays open without a renewal, when its open names no `keep_s`. 0: until it is closed |
| `card_patience_s` | `3600.0` | how long a job waits for the card, when the job names no `card_patience_s`. 0 is now or not at all. A batch job planned for later (`not_before`) counts it from its time |
| `scheduled_if_no_room` | `"wait"` | what scheduled work whose model does not fit does (S18): `"wait"` for room, within its patience, holding back nobody - or `"fail"` at once, `no_room`, in the loud words. A job or a pipeline may say its own (`if_no_room`). Live work always fails at once |
| `idle_unload_s` | `600.0` | an on-demand model idle this long comes off |
| `warm_keep` | `true` | a model already up is handed on as it is; off, it is loaded again for each use |
| `port` | `0` | a load's port: 0 is a free one of the runner's own range. A port named here serves one model at a time |
| `startup_timeout_s`, `health_poll_s`, `shutdown_timeout_s`, `request_timeout_s` | `600.0`, `0.1`, `60.0`, `1800.0` | a load's bound, how often it asks whether its model is up, a stop's bound, an answer's bound |
| `flash_attn` | `"on"` | llama-server's `-fa` |
| `card_lock` | `""` | the machine's card lock file, when no coordinator is named: empty is the machine's own, which every server and coordinator on it shares |
| `card_margin_mib` | `256` | run on its own, the free memory kept beside a model's need when the card is read before a load |
| `slow_answer_factor`, `slow_answer_floor_s` | `3.0`, `0.25` | an answer far slower than its model's normal: both this many times slower and this many seconds lost (*The coordinator watches the work*) |
| `card_watch_s`, `card_recover_watch_s`, `card_floor_mib` | `30.0`, `2.0`, `256` | run on its own on the machine's lock: how often the card is read, how often while it is full, and the free memory under which it is full |

| `[mock]` | default | what the stand-in does |
| --- | --- | --- |
| `tokens_per_second` | `45.0` | how fast a text model's words come |
| `load_seconds`, `unload_seconds` | `1.2`, `0.4` | how long a load and an unload take |
| `answer_seconds`, `answer_due_s` | `0.5`, `60.0` | how long a whisper model's transcription takes, and the deadline it declares: below `answer_seconds`, it overruns it |
| `fail_load_models` | `[]` | these models' every load fails, once its time is waited out: a model's `failed` state, and why |
| `fail_models`, `fail_first_for` | `[]`, `""` | these models' every answer fails; this model's first answer in the process fails |
| `seed` | `7` | the words a stand-in answers are drawn from this, the request and its seed |
| `card_mib` | `0` | the stand-in's own card, in MiB (S16a R2): 0, none - nothing read, no lock. Above 0 it is read by the stand-in alone, never the real card, and watched as a real card is on the lock (S5b), on a lock of its own that `[runner] card_lock` must name - never the machine's |
| `card_full_s` | `[]` | `[after_s, for_s]`: from the server's start, the stand-in's card reads full - no MiB free - after `after_s`, for `for_s`, then free again: the session degraded, a job held for the card's memory, and run again by itself |
| `need_mib` | `{}` | what each stand-in model needs on the stand-in's card, a table - `{ painter = 9800 }` (S18). Only with `card_mib`. A model named here is judged by the memory check against the stand-in's card, as a real model against the real one, and the card reads that much less free while it is up: a model that does not fit fails at once, or waits for room. A model it does not name needs nothing |

| `[models.<key>]` | default | engine | what it is |
| --- | --- | --- | --- |
| `engine` | `"llamacpp"` | both | `llamacpp` or `whisper`. There is no CPU engine: a setting that puts a model on the CPU is refused |
| `file`, `label`, `note` | `""` | both | the model's file in `models_dir`; its name for a person; a note shown with it |
| `extra_args` | `[]` | both | more of its program's flags. A flag a setting says is refused, naming the setting |
| `ctx` | `8192` | llama.cpp | the context the model is loaded with |
| `n_gpu_layers`, `cache_type_k`, `cache_type_v` | `99`, `"f16"`, `"f16"` | llama.cpp | llama-server's own |
| `temperature`, `top_p`, `min_p`, `repeat_penalty` | `0.8`, `1.0`, `0.05`, `1.05` | llama.cpp | the model's own sampling, for a job that names none |
| `disable_thinking` | `false` | llama.cpp | no thinking, for a job that does not ask for it |
| `threads`, `vad` | `4`, `""` | whisper | its CPU threads (they prepare its sound); its voice detector's file |
| `startup_timeout_s`, `retry_after_s` | the runner's, `60.0` | whisper | its load's bound; how long a start that failed is not tried again |
| `due_base_s`, `due_per_audio_s` | `30.0`, `1.0` | whisper | a transcription is due within this, and this more for each second of audio |

## The decisions the code points to

### Seven hooks, not three

The plan was three: `build_request`, `land_result`, `sampling_for`. Carrying a
working application's queue rows onto this queue needed three more, and the
request record a fourth:

- **`merge(group, members)`** - a fan has settled. Called once every member
  has ended, failures included, because an application told only about
  successes can never learn that its fan stopped.
- **`judge(job, candidate)`** - the application's verdict on one attempt. A
  refusal makes the queue ask again with a new seed, at most three attempts.
  A refused last attempt fails the job, its error the judge's why and its
  `reason` `judge_refused`, for a program to branch on: an answer
  the application does not accept is never landed, never offered as a
  result and never built on. (It used to stand, whatever the judge said.)
  The request's record keeps every attempt, the refused ones too.
- **`ended(job, result)`** - a job has ended, however it ended: done, failed
  or cancelled, by its worker, a cancel, a give-up, a stall, a restart, or a
  job it waited on failing. It exists because `land_result` only ever hears
  about success, and an application that keeps a record of its requests has
  to be able to close the record of a failed one.
- **`manifest(job, after_results)`** - the inputs a request was built from,
  named, for the request's record (below). The default names none.

`ended` is told **at least once**: the debt is written in the same save as the
ending itself and paid only once the hook has returned, so a server that stops
in between tells it again when it starts. The hook must be idempotent by job
id. It is told after `land_result` for a success and before the fan's merge
for a fan member. A hook that raises is recorded on the row and not retried,
because a debt that can never be paid would be retried for ever. A row whose
ending has not been told is never trimmed by the cap; `forget` and `prune`
tell it first - and one another thread is telling at that moment is waited
for, at most `told_wait_s`, rather than refused as a debt about to be paid.

### Answers at once: each model one at a time, different models together (S17)

The owner's ruling of 2026-09-28: *"Yes, true parallellisation. If i am
talking and translating, i want the pipelines to go in parallell."* Until
then one job ran at a time, whatever models were up.
- **The worker starts; each job runs on a thread of its own.** It starts,
  in the queue's order, every job that can run: its `after` landed, and its
  model answering no other job. A job whose model is busy waits and holds
  back no job for another model - an interactive job for a free model is
  never held back by a batch job on a busy one. Priorities and `after` hold
  within what can run.
- **How many at once in all is a setting,** `answers_at_once`, 8: the most
  models a runner keeps up (`max_loads`, a rental's 8 processes), so by
  default the one limit is one answer per model. 1 is one at a time, as
  before.
- **A model that must be loaded waits for the answers under way.** Its load
  could need the room a running answer's model holds, and nothing in use
  comes off to make room: so, while any answer runs, a job whose model is not
  up (`backend.models_up()`) waits for them to end - never failed for room
  our own answers hold - and nothing behind it in the order starts
  meanwhile, so a load is never starved. A model already up answers at
  once; a session that keeps its models (the owner's case) answers in
  parallel from its first request. A backend that cannot say what is up
  runs nothing beside a load. Loads stay one at a time per server.
- **Nothing starts behind a load until it is done (S17b).** Once a job whose
  model is not up has started - its load under way, making its room - no job
  not yet started starts until that model is up or that job has ended; the
  answers already running go on. A job started beside it could take in use
  the very model its load must take off, and the load would then fail for
  room our own answers hold: found on the real card at `max_loads` 1, jobs
  a, b, a, b - the next a and the next b started in one pass, and a failed at
  once. "Behind" is every job not yet started, whatever its priority or its
  `after`: a load is never given up for a job that came later, so an
  interactive job waits one load at most. On every card, not only a full
  one. Once the model is up, the jobs behind it start beside it as above -
  at the queue's next wake (`tick_s`), or sooner when an answer ends.
- **What acts on an answer acts on each.** A cancel or a give-up ends the
  answer it names, and the others go on; a give-up's wedged call no longer
  retires the worker - the worker is never inside a job - its thread is left
  to return, and its late answer is refused. The stall watch judges each
  answer by its own movement, and asks whether ITS model is loading
  (`backend.loading_for(model)`). A submit takes back a quiet job of its own
  model only. The card taken back ends every answer, and the card full
  (S5b) ends every answer and runs each again. The server's stop - at the
  console or `POST /api/stop` - waits for every answer within one timeout,
  and the next start settles each (`recover`).
- **The record says what ran beside each answer.** A request's `started`
  line names the jobs running as it started, its `ended` line every job that
  ran beside it (`beside`, [{job, model}]); the runner's `timed` line of an
  answer names the models that answered beside it, and the statistics keep
  how many beside each sample (`beside`, in step with `samples`), so M2 can
  tell a lone answer from a shared one.
- **Health speaks of every one:** `queue.running` counts them, the runner's
  `in_use_models` names the models they hold, the jobs list has each
  `running`, and the rental's heartbeat is `working` while any answers, its
  `progress` counting all of them.
- **The application's hooks may run on two threads at once** - two jobs on
  two models build, sample, describe and land together. A hook that keeps
  state of its own must hold its own lock; the default hooks keep none.

### One live queue per folder

A queue holds an **operating-system** lock on `<folder>/jobs.lock` for as long
as it is bound. A second queue - in this process or another - is refused
`folder_in_use`, naming the holder's process and address, instead of being
left to settle the first one's running work as if it were a stopped server's.
The lock is the operating system's, not the file's existence: it is let go by
`close()`, and when the process dies, however it dies. Measured in
`test_server.py`: a server process killed, and its folder bound by another
server 0.01 s later.

`close()` detaches the store under the store's own lock before it lets the
folder go, so a generation that comes back after the close writes nothing.

### Who asked, and what names a request

- **The server has an actor id** (`srv-k3x9m2p7`), minted when it starts. It
  names the folder's lock note, and it starts every generation's handle:
  `srv-k3x9m2p7:j-0007#01`. Two servers' jobs are both numbered `j-0001`, and a
  model server they share could otherwise mistake one's generation for the
  other's. The queue stamps the handle and the sample number itself, whatever
  a backend sends; the seed stays the backend's, since it knows the one it used.
- **The correlation id names one request everywhere it goes.** The client that
  starts a request mints it. One that arrives without is minted by the server,
  and `correlation_source` says which, so a record never claims a client id it
  was not sent. A malformed one is refused `bad_request`, never replaced by a
  substitute; one this folder already has is refused `correlation_taken`. A
  retry is a new request with a new id, and `retry_of` names the old job.
- **`client`** is the asking client's actor id, recorded on the job so a
  client can list its own work (`?client=`). A claim records who claimed
  (`claimed_by`). Both are recorded, never enforced: a result may be taken by
  another client on purpose. The list's `?client=` is an actor id too, and it
  and `?scope=` are each given once: a malformed or repeated client is
  `bad_client`, a repeated scope `bad_scope` - once, a malformed client
  listed nothing, silently, and a repeated one the first.

### A page from another address

An application's own page shows the client's widgets by asking this server
from an address of its own. It is answered only when the server was started
to allow it - `--allow-origin http://127.0.0.1:7867`, repeatable - and a page
from any other address is refused before any route (the contract's
`origins/server.json`):

- **Why before any route.** The server reads a body as JSON whatever its
  type, so a web page open in the same browser could act on it with a request
  its browser sends without asking first - a form's POST, or a text body.
  Measured before the check: such a POST from another page bound a folder.
  Now every request whose Origin is neither the server's own nor named is
  `403 origin_not_allowed`, whatever it asks, and its browser is told nothing
  (no `Access-Control-*` header), so the page sees nothing either. A refused
  body is read and set aside, so a connection the browser keeps open stays in
  step.
- **The server's own page** is the address the request was sent to: `http://`
  and its `Host`, however it is written (`127.0.0.1` or `localhost`). Its
  requests, and those with no Origin at all, are answered exactly as before -
  an `OPTIONS` too: 501, a method the server does not take.
- **An allowed page** is answered as the server's own is, plus
  `Access-Control-Allow-Origin` naming it and `Vary: Origin`; its preflight is
  answered `204`, allowing `GET`, `POST` and the `Content-Type` header.
- **An address is read as its browser writes it** - scheme, host and port,
  lower case, no default port - and anything that is not one (a path, `null`,
  `*`) stops the start, saying why.
- **The name it was sent to is checked first.** A website whose name is made
  to point at this machine (DNS rebinding) sends that name both in its Origin
  and in the Host, and so looked like the server's own page - measured before
  the check: a text-body POST from such a page bound a folder. Now a Host
  that is not one of the server's own names - `127.0.0.1`, `localhost`,
  `[::1]`, the address it listens on (`--host`), and the host of each page it
  was started to allow - is `403 host_not_allowed`, whatever it asks and
  whether or not it carries an Origin: a server on this machine has no
  business answering a name it does not have. A name is compared in lower
  case and without its port. A request that names no Host at all is answered
  as before: every browser names one, so it is a program. A server listening
  on every address (`--host 0.0.0.0`) still answers to those names alone:
  asked by the machine's name or its address on the network, it refuses,
  unless a page at that name is allowed.
- **A body over the cap is never read.** A POST whose body is more than
  `--max-body` (16 MiB unless given) is `413 too_large` before any route, and
  not a byte of it is read - before, a body was read whole into memory,
  whatever its Content-Length said, from anyone. 16 MiB is some thirty times
  the longest text request a model here takes (a context of 128 k tokens is
  about half a MiB of text); audio never comes in a body. The answer says
  `Connection: close`, and the connection is closed a quarter of a second
  after the answer is out: measured here, closed at once, a real client
  sending 8 MiB or more never heard the answer - it met a reset. A refused
  page's body within the cap is still read and set aside; one over it is not
  read either. A `Content-Length` that is not a whole number of bytes reads
  as no body - one of `-1` once held the handler until the asker gave up.
- **A body in chunks is never read.** A request that names a
  `Transfer-Encoding` - its length never said - is `411 length_required`,
  whatever the method and whatever length it also names, and its connection
  is closed a moment after the answer: the server reads a body by its
  `Content-Length` alone, and before, on a connection kept open, the chunks
  were read as the next request. A GET's body within the cap is read and set
  aside for the same reason: before, it too was read as the next request.
- **Every method goes through the checks.** They run in one place
  (`Handler.parse_request`), before any method of the handler: a method no
  route takes - `HEAD`, `PUT`, `DELETE`, any other - is checked like every
  request and then answered `501`, as http.server answers one. Before,
  http.server answered such a method before any check.
- **An asker is told to continue only once the checks let it through.** One
  that says `Expect: 100-continue` sends its body only when it is told to,
  and http.server told it at once, before any check: a request refused for
  its name, its page or its size was invited to send a body its refusal
  would never read - and one within the cap was then waited for until the
  read timeout, its refusal unsent (H3's register R1). Now it hears the
  refusal first, the body never read and the connection closed; a request
  the checks let through is told `100 Continue`, then read as any other.
- **A request has a time to arrive whole** - its line, its headers and its
  body: `--read-timeout` seconds (10 unless given) from the server starting to
  wait for it, of the connection being made or of the answer before it on a
  connection kept open. One that has not is never answered, and its
  connection is closed; a request cut off part way is said in the log. Before,
  a body within the cap that never finished arriving held a handler thread
  for ever. The bound is the whole request's, never each read's: a byte a
  second would pass any bound on one read. Measured: the largest body the
  server reads, 16 MiB, arrived and was answered in 24 to 49 ms.
- **Its port is its alone.** http.server binds `SO_REUSEADDR`, and on Windows a
  second program's socket that sets it too could share the port - measured
  against the server before: a helper bound beside it, and a server started
  on a port a helper already listened on started, said it was listening, and
  heard none of 20 requests. It binds `SO_EXCLUSIVEADDRUSE` on Windows now
  (`Listener`): started on a port somebody holds, it says it cannot start -
  "cannot listen on <address>: ..." (`CannotListen`). A start stopped by
  anything else says what that was, never that it cannot listen, and lets
  the port it had go (H3's register R3: every error as the server began to
  serve was once said to be the port).
- **A program stops it; a page never can** (`POST /api/stop`, H8 - S13b's
  R2). A server ended as a process gives nothing back: its rental is taken
  back only when its heartbeats stop. So a program on this machine - one
  that sends no Origin - asks it to stop: it answers `202` first, then does
  what a stop at its console does (`Server.close`: its models off while the
  folder is bound, the folder put down, the rental handed back), and its
  process ends, exit code 0. A request that carries an Origin - the
  server's own page and an allowed one included - is refused `403
  from_a_page`, and the server goes on (`NOT_FROM_A_PAGE`): every check
  before any route holds for it as for any route, and nothing a page may
  do stops the server. The reference client's `StartedServer.stop` asks
  it, and ends the process by its handle only when it does not answer.

### Every job leaves a record

`<folder>/requests/<correlation id>/` - one folder per request, named by the
id that names the request everywhere else:

| file | what it holds |
| --- | --- |
| `events.jsonl` | the request's own story, a line per event: `submitted` (the job record as it was accepted), `started`, `attempt_started` and `attempt_ended` for every attempt (answered, refused by the judge, failed or cancelled), `attempt_card_full` for an attempt the card's memory broke and the job was held to run again (which attempt, when, why, and the card's numbers), `claimed`, `recovered` (what a start made of a run a stopped server left), `ended` (status, error, and what ended it) |
| `manifest.json` | the job as its run began, the jobs it waited on - with their correlation ids, so their records can be found - and the inputs the application's `manifest` hook names |
| `request.json` | the request as `build_request` built it |
| `samples/<NN>/` | one per attempt, numbered as its handle is: `params.json` (what it was sent with, its own seed), `candidate.json` (what came back), and whatever the backend keeps with `run.record(name, data)`. The runner keeps `call.json` there - the exact body llama-server received, and never the load's key, which travels in a header - and `count.json`, the request as its model counted it (`prompt_tokens`, `room`, `context`), whether it fit or not |

- **It is the queue's, not the runner's.** Every job has one, whatever the
  backend. A backend adds to its own attempt's folder and nothing else - a
  plain file name, never one of the queue's own. The runner keeps its call
  whether the answer came or not, and never a call that attempt did not make.
- **The inputs are the application's to name** - the seventh hook, with an
  empty default: the record's shape travels, what it names does not. A
  manifest that fails is written down as failed, and the job goes on.
- **The record never fails what it records.** A file that cannot be written
  is said in the log, and the job goes on.
- **It outlives the job**: `forget`, `prune` and the cap take a job out of the
  queue, never out of the record. A retry is a new request, so a new folder,
  and its first line names the request it runs again - by correlation id too,
  since the old job may be forgotten by then.
- **An ending is in the story once, dated when it ended.** It is written when
  the application is told, which is at least once. Lines are in the order
  they were written; `ts` is when each thing happened.
- **A stopped server writes nothing**, as a killed process writes nothing: a
  generation that comes back after its queue let go is not kept.

`jobqueue.request_record(folder, correlation_id)` reads one back.

### A job that names no temperature runs at its model's own

The queue stamps no temperature. One a job names is sent as it is - 0
included - and a job that names none reaches the backend with none, so the
runner uses the model's own: its `temperature`, else the settings'
`[defaults]`, else the runner's 0.8. An existing application's queue stamped
0.7 on every job, so a temperature set in a model's settings never took
effect, and nothing said so. A model whose settings name no temperature now
runs at 0.8, not 0.7; `[defaults] temperature = 0.7` keeps the old number.

### Every text parameter, none mandatory; a conversation, not one message

The owner's word: the bootstrap handles all its own responsibilities and
makes the low-level parameters available, not mandatory.

- **What a job's `gen` may name is the contract's list** (jobs-submit's
  `gen`): the queue's own three - `model`, `n`, `retries` - and every
  parameter llama-server b10724's chat route reads that is an application's
  to set, spelled as llama-server spells them. Each is optional.
- **The list is a reading of llama-server's source**, kept in
  `runner/llama_fields.py` (tag b10724: the chat layer, the completion's
  schema, the route's documented options). Of the 84 names the route reads,
  51 are offered; 12 are the bootstrap's own (`model`, `messages`, `stream`,
  `n`, and other names for a parameter already offered); 15 are not offered
  yet, because what they give back - probabilities, tool calls, token ids -
  has no place in a candidate; 6 the chat layer sets itself. A row holds the
  contract to the reading, both ways.
- **A parameter the list does not name is refused `bad_request`** - at the
  server's door, and at `Queue.submit` for an application in the same
  process - never dropped without a word: a misspelt one never reaches the
  model. So is one of the wrong kind. What an application's `sampling_for`
  names is held to the same list: one it does not name fails the job before
  the backend is asked - and so does one of the queue's own three (`model`,
  `n`, `retries`), which the job's record names as it was submitted: a
  sampling that named another model ran the job on a model its record did
  not name.
- **Each is sent exactly as it was named** - False and 0 included, which are
  choices, not absences - and one named None is unset. `max_tokens` 0 is
  sent as 0 (llama-server reads the prompt into its cache and writes
  nothing), and the empty answer it asked for does not fail its job, as an
  empty answer to any other length does. `max_tokens` -1 names no limit: the
  job's progress has no `budget` then, never a negative one.
- **A length llama-server refuses is refused at the submit** - `max_tokens`
  below -1 or above 2147483647 (b10724: n_predict's hard limits), 400
  `bad_max_tokens`, never sent (S10b's register RG3). Before, such a job
  waited for the card and its model, and failed in the model's words - or,
  too long for its context, the count's words said it named no limit. A
  sampling the application's hook chooses is held to the same range: its job
  fails before the model is asked.
- **So is a value outside another of llama-server's hard limits** (H5's
  register 4; b10724's `set_hard_limits`): `n_keep` and
  `reasoning_budget_tokens` from -1; `n_indent`, `n_discard`,
  `n_cache_reuse`, `repeat_last_n`, `dry_allowed_length`,
  `dry_penalty_last_n` and `min_keep` from 0 - each up to 2147483647, the
  most llama-server reads into 32 bits; `t_max_predict_ms` from -1, read into
  64; `adaptive_decay` from 0 to 0.99, compared as llama-server compares it,
  in single precision. 400 `out_of_range`, its `error` naming the parameter,
  its range and the value, never sent (`jobqueue.HARD_LIMITS`); a sampling
  hook's choice is held to them too.
- **Where the application names nothing,** the model's settings apply (its
  `temperature`, `top_p`, `min_p`, `repeat_penalty`; for a model told not to
  think, `enable_thinking` false - a default a job's own
  `chat_template_kwargs` replaces), then the bootstrap's (`max_tokens` 600),
  then llama-server's own.
- **A JSON schema or a grammar** (`json_schema`, `grammar`,
  `response_format`), **the prompt cache** (`cache_prompt`, `n_cache_reuse`,
  `id_slot`) and **a limit on thinking** (`reasoning_budget_tokens`, with
  `reasoning_budget_message`; llama-server's `--reasoning-budget` is the same
  limit for the whole server) are parameters like the rest.
- **A request is a prompt or a conversation.** A prompt - a string, or
  `{"prompt": ...}` - is sent as one user message, as it always was. A
  conversation - `{"messages": [{"role": ..., "content": ...}, ...]}`, the
  contract's `conversation` shape - is a system message and the turns before
  this one, sent as they were built, in their order. It is what the
  application's `build_request` returns - with the default hooks, the job's
  own `input` - because a conversation built from earlier answers exists only
  when the job's turn comes, and the queue never reads what it carries.
- **Not measured, and owed to the real card (M1, with its owner's go):** what
  llama-server does with a field it does not read (its source reads what it
  knows and nothing else); that a schema or a grammar really forces the
  answer's shape; that the prompt cache is really reused; that a thinking
  budget really ends the thinking; and whether a job's `enable_thinking`
  reaches a model its settings started with `--reasoning off`.

### The request is counted by its model before the model is asked

A request longer than its model's context was found only by llama-server,
and only for the prompt: one whose prompt fit and whose answer did not was
answered, and the answer cut where the context ended. Now:

- **At the job's turn, with its model up and before it is asked, the model
  counts the request** (`Backend.count`): the exact body its answer will be
  asked with - one builder for both - rendered by the model's own chat
  template (`POST /apply-template`, which parses a body as the chat route
  does) and counted by its own tokenizer (`POST /tokenize`, with the first
  token and the special tokens, as the chat route tokenizes a prompt). The
  context is the model's own word too (`GET /props`,
  `default_generation_settings.n_ctx`, read once a load): llama-server caps
  its slot's context at the model's training context, so it is not always
  the `ctx` the settings name. All three read from llama-server b10724's
  source, without the card.
- **The prompt and the room its answer needs must fit the context**, or the
  job fails with its own reason, `prompt_too_long`, saying the three numbers
  and what would make it fit - and the model is never asked. Never cut to
  fit.
- **The room is the job's `max_tokens`, and never less than one token**
  (`runner/base.py`, `answer_room`). An answer gets at most what its prompt
  leaves of the context (llama-server stops it there, without
  `--context-shift`), so a length that does not fit is refused. **-1 names no
  limit, and needs room to begin:** the answer may run to the end of the
  context, where llama-server stops it with `length`; anything larger would
  be a limit the application declined to set, refusing a request that could
  have been answered in full - and the count on the record says how much the
  context left it. **0 writes nothing**, but llama-server refuses a prompt of
  the whole context (`n_prompt >= n_ctx`), so it still needs one position.
- **The count is on the job and in its record:** `progress.count`
  (`prompt_tokens`, `room`, `context`) on every job its model counted, and
  `count.json` in the attempt's folder, whether it fit or not.
- **llama-server's own refusal is the last line of defence** behind the
  count: a request it refuses as longer than its context
  (`exceed_context_size_error`) ends its job `prompt_too_long` too.
- **The stand-in model counts as it answers** (S16a R1): each word it is
  asked a token - the number its `usage.prompt_tokens` says - against the
  `ctx` its model's settings give; a transcription is not counted. So a
  request past it fails `prompt_too_long` on the stand-in too. The
  stand-in llama-server counts with a
  template and a tokenizer of its own, simple enough that a row counts a
  request by hand (`test_llamacpp.stand_in_tokens`), and answers the three
  routes in b10724's shapes.
- **The request check (S15) reaches the count the same way** (below):
  its model acquired as a job's is, then the same `_count` - never a count
  of its own. Nothing here serves a count to an application before it
  submits.
- **Owed to the real card (M1):** that the count equals the answer's own
  `usage.prompt_tokens` on a real model and template; that `/props`' context
  is the one a refusal names; what `max_tokens` 0 really writes (the README
  says nothing; b10724's code, as read, sends its first token before its
  limit stops it); and how long a count takes on the longest prompt.
  llama-server b10724 also counts a chat body in one call
  (`POST /v1/chat/completions/input_tokens`: the same parse, the same
  tokenizer call); the README's documented pair was chosen, and both count
  the same.

### A request checked before it is sent (PLAN-4 D16, S15)

A job submitted with `check: true` is held once its request is built - at
its turn, by the application's build hook or the default one - so a request
made from an earlier job's answer is seen exactly as it will be sent. The
owner's words: *"strong UX seams ... allow operators to scan easily and tune
data for a single request during development or debugging or very specific
requests."*

- **The job says so:** `check` is `marked`, then `held`, then `sent` or
  `tuned`; a job not marked has no `check`, and never waits for anyone.
- **Held** is a status of its own. The worker builds the request, merges
  the sampling hook, holds both to the contract as a turn does, and then
  asks the backend to describe it (`hold(request, params, run, named)`,
  `runner/adapter.py`) - and goes on to the next job. What a turn would fail
  on fails the job here, in the same words and reason. A backend with no
  `hold` gets the queue's own description: each parameter, the
  application's or the bootstrap's, and nothing more.
- **`GET /api/jobs/{id}/held`** answers the contract's `held_request`: the
  request as built, its `gen` - only what its model's engine reads, none for
  a whisper model (R-B1: the backend's `hold` says which, `reads`) - the
  model and engine, the messages by role
  (or a whisper model's audio, its length and whisper-server's fields),
  every parameter with where it came from (`application`, `model`,
  `bootstrap`), the count and whether it fits, what the statistics expect,
  and in words why any of these is not known.
- **The count is made when the job is held**, by the same `_count` the
  turn's check uses, its model taken as the turn takes it. Only a loaded
  model can count, and the turn would load it next anyway; counting later,
  on demand, would load a model while a person reads - or never. A request
  that does not fit is held all the same (`fits` false), to be changed;
  sent unchanged, its turn counts it again and fails it `prompt_too_long`.
- **A held job holds no card.** It is not running: the stall watch never
  takes it back, and the heartbeat's work never counts it, so a coordinator
  never calls a rental hung while a person reads. The queue's own pipeline
  does not keep the card for it either (`unfinished(held=False)`, the
  server's `_work_pending`): a person may read for an hour, and the card is
  the whole machine's. The model loaded for the count is on demand, and
  idles off on its own clock; sent, the job asks for the card as any job
  does. A folder with a held job is still busy: it is owed.
- **Health counts it** (`queue.held`, H8 - S15's RS2), apart from the
  queued and running jobs: it waits on a person, not on the card. So a page
  that shows only the progress widget says that a request waits for its
  check, and where it can be checked.
- **The expected time is read through `ModelManager.kept_stats()`** - the
  manager's public way to its kept statistics (S15's RS3): None, said as
  "the model's statistics cannot be read", when the summary cannot be read.
- **`POST /api/jobs/{id}/send`** sends it as built (no body), or changed
  for this one request: `request` replaces it whole, `gen` sets parameters
  (never the queue's own three), `unset` leaves some unnamed. Held to what
  a submit is, and refused the same way (`bad_request`, `bad_max_tokens`,
  `out_of_range`, and the runner's `check_sent` - `bad_request` for a text
  model's request that is not one, `bad_audio_request` for a whisper
  model's); never sent. The job goes back in the queue in its place, and
  its turn runs what was sent - never built again, across a restart too.
- **The record keeps both:** `request.json` and `held.json`, what was
  built; `sent.json`, what was sent, `tuned` when it differs, naming each
  thing `changed`; the story says `held`, then `sent`. The store keeps the
  held and sent request only until the job ends.
- **A held job is cancelled** (`cancel`, `give-up`) at once, `was: held`;
  what waits on it, and its fan, are stopped as for a queued one. A retry of
  a marked job is marked again.

### A job whose `after` already failed is accepted, then stopped

It is not refused. The submitter's picture is a moment old, and the same job
submitted a moment earlier would have been accepted and then stopped when its
dependency failed. So both cases give one answer: the job is accepted and
ended `cancelled` at once, its error naming the job it waited on. The record
is the answer.

### A result waits for its job, and health says which contract it speaks

- **`GET /api/jobs/{id}/result?wait_s=N`**, N a plain number of seconds from
  0 to 60, holds the answer until the job has ended, or is held for a
  person's check (R-B3: nothing more comes of it until it is sent), or N
  seconds have passed, then answers exactly as without it: an application
  submits a job and waits for its result without asking again and again.
  Every way a job ends wakes the wait - `_end` is the one door to an ending
  - and so does a job becoming held (`_hold`). The reference client's
  `wait` returns a held job too. A job that has
  ended, or is not there, is answered at once, and a server that stops
  answers every wait at once. Any other `wait_s` is `400 bad_wait`, refused
  before the queue is asked anything. The 60 is the contract's word, not a
  setting: an application relies on it.
- **Health's `contract_version`** is the contract's own `contract_version()`
  over the folder the server reads its route table from, so a client - or
  the coordinator - can tell whether it speaks the same contract.

### A store that cannot be read is said so

Every job route answers `503 store_unreadable` - never an empty answer
standing in for one the queue could not read, because every writer reads,
edits and writes back. A folder whose store cannot be read can still be let
go: it cannot prove it owes anything, and a server stuck in a broken folder
could never leave it.

### The runner: models side by side, and a record of every load

`runner/manager.py` keeps models side by side on the card (PLAN-4 D7). What
it decides:

- **One load of each model, loaded one at a time.** A load holds the LOAD
  LOCK for the whole of it - its making room, its weights going up - and
  never the manager's lock, so nothing that asks the manager waits for a
  load: a generation on a model already up, health, a bind, a stop. Stops
  take no load lock and run together. At most `max_loads` models (8, the
  most a coordinator's rental names; one when the settings name one port for
  every model).
- **Pinned and on demand.** A model an application's pipeline names, or loads
  with `POST /api/models/{key}/load`, is pinned: kept until it is unloaded
  (`POST /api/models/{key}/unload`), the pipeline closes or is lost, or the
  server stops - the idle sweep never takes it. A model a job needs that is
  not up is loaded for it on demand, and idle-unloads on its own clock.
- **Making room.** A load that does not fit - past `max_loads`, or refused by
  the card source's fit check (`_check_vram`) - takes off an on-demand model
  not answering a request, the one used longest ago first, and asks again,
  until it fits or nothing more may come off: then it is refused in the fit
  check's own words. A pinned model never comes off for another. A stop
  already under way - an unload, the idle sweep - is room being made: the
  load waits it out and asks again, never taking a second model off for that
  room, nor refused for it; a cancel and the server stopping end the wait,
  and a stop that failed meanwhile refuses the load, naming the model that
  may still be on the card.
- **Ready without polling.** `load` and a pipeline's `open` take `?wait_s`
  (0 to 60, as a job's result does): the answer comes the moment the model is
  up - its health check answered - or its load failed, within that bound.
- **A lease on the card, asked BEFORE anything moves.** A second holder is
  refused before it can unload the first holder's model - a refusal placed
  only where the lease is granted would let it evict the model first and
  take the card cleanly. The lease is taken before the weights go up (a cold
  load takes minutes, and every one of them is time the card is spoken for),
  and released once the last model has come off - a failed load, beside
  others, keeps it. **A holder is an actor id**, the coordinator's rule: a
  name can be shared, an actor cannot.
- **A warm model is handed out only while its process is alive.** One killed
  underneath is loaded again, and the record says so.
- **Every load has an identity** - a `load-` actor id on every line it
  writes. An unload closes ITS load in the record, never merely the latest.
- **The loads are one table** (`LoadTable`), keyed by that id. The load whose
  weights are going up and the loads on the card are each one entry, from
  the moment the id is minted until the model comes off. It holds ONE LOAD
  OF EACH MODEL - under way, up or being stopped - and refuses a second,
  whoever asks. Each load keeps its own idle clock and its own use count;
  what no one load owns stays beside the entries:
  - the live count, and the loads this session;
  - the card's idle clock - health's `last_used` outlives the load it last
    timed;
  - the in-use count over every model;
  - the model a load waits for the card for - a load has no id until the card
    is had.
- **The record is `<folder>/runner/runner_events.jsonl`**, and it is the
  instrument: `max_concurrency` must never find two loads of ONE model open
  at once in one process, every `load_ok` names what else was up (`beside`)
  and `beside_faults` must find none that names it wrong, `side_by_side` says
  how many models were up together, and `orphaned_loads` finds a load a
  stopped server never unloaded. Beside it, `model_loads.json` keeps how long
  loads of each model normally take here - cold (the first by a process) and
  warm apart - folded in under one lock, through a temporary file of its own;
  a load far above that says so: *"Model loading is slow, check computer"*.
- **Each model's health, without loading it** (`GET /api/models`,
  `runner/modelhealth.py`): its file - there or not, its size, its disk; its
  header, as its engine reads it - a GGUF's against the settings, in the
  sentences a load would log, or a whisper model's ggml header, its eleven
  numbers and its shape in words (`runner/ggml.py`), a file that is not one
  said in that reader's own words; its load history from that record, the
  last load judged against the normal BEFORE it - and with nothing bound, no
  history, never an empty one. A header is read once, by its engine's
  reader, for as long as its file is unchanged.
- **Where each model stands** (`GET /api/models`, the manager's own answer
  beside the health): its `state` - the word a load or an unload of it
  answers (`model_state`: up with its load, loading, waiting for the card,
  failed and why, off) - and whether the open pipeline keeps it (`pinned`,
  health's `pipeline.models`). A model up and not pinned was loaded on
  demand, for a job. Read under the pipeline's own lock, never the load
  lock: the list never waits for a load.
- **How fast a model's file comes off its disk, on demand**
  (`POST /api/models/{key}/read-speed`, `runner/diskread.py`): one stretch of
  at most 64 MB, a third of the way in, for at most 3 s. On Windows the read
  goes past the operating system's cache, and says so; elsewhere it says it
  cannot tell, and a repeat is most likely the cache - O_DIRECT is the seam
  there. Refused while a model loads (the measure would measure the load as
  well) and while another measure runs.
- **Where it writes is handed in, never remembered.** Nothing bound writes
  nothing, and keeps the last events in memory. A rebind follows the folder
  WITHOUT reloading a model, and every load's record goes with it - one whose
  stop failed included, since it may still be on the card: the old folder is
  told each load left, the new one that each arrived - so neither calls one
  an orphan while it lives, and the folder it dies in does. Letting the
  folder go, with none after it, leaves a load whose stop failed open in it:
  the next start there finds it.
- **A port per load, from a port source** - anything with
  `take(model_key, load)` and `give_back(port)`. The default gives the
  configured port (one model at a time, then), or a free one of the runner's
  own range for each load - 27000-28499, below every system's range for
  passing connections, bind-checked - never one the operating system picks,
  which it may hand to any connection before llama-server binds it. The
  coordinator's port registry fits the same two calls, and holds each load's
  port under its own name (`model-server <load id>`), so two loads side by
  side are never handed one port. A port is taken once per load, never per
  reuse, and given back when the load comes off the card or fails - but not
  when stopping threw, since what would not stop may still be listening on
  it. The stand-in opens none.
- **What a stopped server left running is found from the record, never by
  port.** `startup_check()` reports; `startup_check(reclaim=True)` kills only
  a process whose command line PROVES it is one of this runner's loads - the
  runner's executable, the model file, the port as a whole number, the load's
  own id. A process that is gone is skipped; one it could not ask about is
  never "ours". Building a manager asks the operating system nothing.
- **Settings are one TOML file** (`tomllib`). An unknown section, setting or
  backend is refused by name - a misspelt key that loads quietly reads as a
  setting that took effect - and no data root can be written in it.
- **Timing statistics, per model and per measure** (`runner/stats.py` - the
  store; what is measured, and when, below). Kept in
  `<folder>/runner/model_stats.json`: over all time the count, the mean, the
  least, the most and the last; the last 200 samples, for the median and the
  90th percentile - the nearest rank, always a sample that happened. Written
  whole: one write at a time, to a temporary name of its own, swapped in,
  bytes with LF; a refused swap is tried again for a moment, and a file it
  cannot read is never written over. The records it is summed from stay:
  `rebuild(records)` folds them into the whole summary again by the same
  fold, and a row proves it equals the one kept.
- **What the runner times** (PLAN-4 D12, S11b). Each thing measured is ONE
  `timed` line of the statistics' own record, `<folder>/runner/model_stats.jsonl`
  - `{"model_key", "what", "measures"}` - folded as it is written, under one
  lock, into the model's statistics: the records are those lines
  (`manager.stat_lines`, `stat_records`). Their own file: a line per answer
  in `runner_events.jsonl` would bury the loads it is read for. With nothing bound
  the line is kept in memory and nothing is folded; a summary that cannot be
  read is never written over - its lines stay, and `stats_unreadable` says
  so once. The measures are the contract's (`shapes/model_stats.json`):
  - a LOAD: the card's wait and its claim when this load took it
    (`load_wait_s`, `load_claim_s` - the claim as the coordinator's source
    timed it; the lock's take is all wait), each step its engine marks from
    the one before it (`load_spawn_s`, `load_port_s`, `load_healthy_s`,
    `load_identity_s`; whisper-server listens only once its model is up, on
    this load's own path, so it marks spawn and healthy), and its total from
    its asking to ready (`load_s`). The health poll is 0.1 s
    (`health_poll_s`), so a start is timed to a tenth;
  - the card's MEMORY it took: the free memory its fit check read, less the
    free memory at ready (`ready_mib`) and after its first answer
    (`first_answer_mib`, read on a thread of its own) - a measure only when
    nothing else of this server came or went between the reads, and only
    where the card is read (a real backend, or a suite's stand-in `vram`);
    never on the stand-in. Each is said on the heartbeat as its process's
    `mib`;
  - an ANSWER: to its first token (in one piece, the whole answer), whole,
    its tokens, tokens a second once it began, prompt tokens a second, and
    for audio its seconds and the real-time factor - and `slow_answer`, how
    many times its normal it took, when it was far slower;
  - an UNLOAD: from its asking to its process gone (`unload_s`), and for a
    measured load on to its memory back (`unload_memory_s`: the card looked
    at every 0.1 s, for at most 5 s);
  - a JOB: the queue's own share - from its pick to its end written, less
    its time in the model (`queue_s`, told through `RunnerBackend.job_share`).
    Measured by `test_queue`: about 30 ms on an empty store, about 180 ms
    on a store of 1 000 jobs (this machine, loaded, 2026-09-27).
- **A slow answer** (D3, D12) is at least `slow_answer_factor` times slower
  than its model's normal (3) AND at least `slow_answer_floor_s` lost over
  what its normal would have taken (0.25 s) - its normal the median of the
  model's kept samples, three at least, before it: tokens a second for a
  stream, the real-time factor for an answer in one piece. Three: a memory
  spill to the machine's memory makes a model several times slower, while a
  long context or a grammar slows it less than that. A quarter of a second:
  jitter on a busy machine is less, and a person at a live command notices
  more. It is on the record (`answer_slow`), in the statistics, and counted
  on each heartbeat with the newest in words (`slow_answers`,
  `slow_answer`), said at once. `GET /api/models` says the two thresholds
  it judges by (`slow_answer`: `factor`, `floor_s`), and the Models widget
  shows them beside a flagged slow answer - read from the server, never
  typed into the widget (H8, S14c's R3).

### llama.cpp: a child process per load, and it must be the one we started

`runner/llamacpp.py` runs llama-server as a child, one per load:

- **A load is named, and holds a key.** llama-server starts with `--alias
  <load id>` and a key of its own (`--api-key`). Once `/health` says it is
  ready, `/v1/models` must name THIS load - a port answered by anything else
  is refused before a request is sent - and every request carries the key,
  so a runner that ever reaches the wrong port is refused 401 instead of
  being answered by the wrong model. The key is never written to the log,
  the record or an error. It guards against a misdirected request; it is not
  a secret from people at this machine.
- **The child dies with its parent** (Windows): it is put in a kill-on-close
  job the moment it exists - before the health wait, which can take minutes
  - so a server killed by any means takes its llama-server along. A job that
  cannot be built is said, and the model still loads.
- **A refused argument is retried once, without the optional flags** - and
  only those: an argument from the settings file that the build refuses
  fails saying which, instead of the model quietly running without it.
- **A cancel is honoured at once** - also while the server is still silent
  before its first token. The backend owns the socket and ends it both ways
  there are: `shutdown`, which wakes a reader on POSIX, and a real close,
  the only thing that wakes one on Windows (measured: the first version
  waited out an eight-second silence).
- **A stream that ends with no finish reason and no `[DONE]` is a failure**:
  the server went away mid-answer, and half an answer is never handed on as
  a whole one.
- **Stop means gone**: terminated, waited for, and its port free before the
  next load starts. A real model needs a bound folder - its launch is logged
  there, and a launch nobody can read cannot be diagnosed.

### An engine per model (PLAN-4 D9, D10)

A model names the engine it runs on - `engine = "llamacpp"` or `"whisper"`
in its `[models.<key>]` - and one that names none is llama.cpp's: every
settings file written before engines were named ran its models there, and
runs as it did. `runner/engines.py` holds what each engine is.

- **`--backend real` runs each model on its own engine**; `llamacpp`, the real
  backend's name from before, runs every model on llama.cpp and refuses a
  whisper model, naming `real` - named in its settings, or on the command
  line over settings that name `real`. `whisper_server` in `[runner]` names
  whisper-server, as `llama_server` names llama-server. Every real backend
  takes the card through the machine's lock, whatever its models' engines,
  and names each model's own engine to the card source: its fit, its ticket,
  each process it puts on the card.
- **A setting a model's engine never reads is refused**, as any setting
  nothing reads: a whisper model's own `ctx` would read as one that took
  effect. A default is every model's that reads it.
- **No CPU models** (the owner's ruling): an application that wants a CPU
  path keeps it outside the bootstrap. The settings refuse a model set up to
  run on the CPU - llama.cpp's `n_gpu_layers = 0`, or `-ngl 0` or `-dev none`
  in its extra arguments; whisper-server's `-ng` / `--no-gpu`.
- **A proof on every process, whatever the engine** (D9). llama-server
  carries `--alias <load id>` and a key of its own; whisper-server carries
  `--request-path /<load id>` - every route of that load under it, so a
  stranger on its port answers 404 there. `is_ours` belongs to the engine:
  its program, the model file, the port and the load's id, read by that
  engine's proof. A load's line in the record names its engine and its file
  (`file`, whatever the engine; a llama.cpp line says it as `gguf` too, the
  name from before engines were named, and a whisper line never does - its
  file is not a GGUF), and a startup check holds each orphan to its own
  engine's proof - a line naming none is llama.cpp's, and a line from
  before `file` is read by its `gguf`.
- **One call, two kinds.** llama.cpp streams tokens. An engine that answers
  once - a transcription - declares its deadline before it is asked
  (`Backend.answer_deadline_s`, the seam the coordinator's hang check reads
  too), said on the job's progress (`due_s`), and the queue's stall watch and
  a new submission leave it until then; after it, it is judged as any quiet
  job is. Its answer comes whole: `text`, `tokens`, `finish_reason`, and the
  engine's own fields beside them (`transcript`, whisper-server's verbose
  answer, and `audio_s`). A transcript of silence is an answer: "the model
  returned nothing" is the text engine's rule.
- **A whisper model is asked audio**: `{"audio": <a WAV file on this
  machine>, "fields": {...}}` (the contract's `audio_request`), every field one
  whisper-server reads, `response_format` the engine's own. A text model is
  asked a prompt or a conversation; a request of the other kind fails before
  any model is loaded.
- **The stand-in answers in each engine's shape**: a whisper model's
  transcription in one piece, in the verbose shape the whisper stand-in
  server answers in (`answer_seconds`, `answer_due_s` in `[mock]`).
- **A give-up ends a start within `base.GIVE_UP_S`**, whatever the engine: a
  pipeline's close waits at most 15 s for a load under way (S7 R3). Each
  engine's suite holds it to that against its stand-in.
- **The whisper engine** (PLAN-4 D11, `runner/whisper.py`): `-m <file>
  --host 127.0.0.1 --port <p> --request-path /<load id> -t <threads>
  [--vad -vm <vad>]`, then the model's `extra_args` - which may not repeat
  one of those flags (the settings refuse it, naming the setting to use).
  Its file's ggml header is read before the launch; it is ready when
  `/<load id>/health` answers within its own `startup_timeout_s` (the
  runner's unless it names one); any exit before that is a failed start, a
  clean one included, and a start that launched it and failed is not tried
  again for its `retry_after_s` (60 s) - refused at once, saying what the
  failed one said.
- **A transcription** is `{"audio": <a WAV file on this machine>, "fields":
  {...}}`: every field whisper-server 1.9.1 reads, of its kind, none
  mandatory, `response_format` the engine's own. With the default hooks it
  is checked AT SUBMIT (`backend.check_request`): one it could never send is
  refused `bad_audio_request`, and never stored - whisper-server ignores a
  field it does not read, so a misspelt one would read as one that took
  effect. Its answer, in one piece: `text`, `tokens`, `finish_reason`, the
  `transcript` whole and `audio_s`, the WAV's own length. It is due by
  `due_base_s` + `due_per_audio_s` for each second of audio (30 s and real
  time, by default; `runner/whisper.py` says why), and one waiting for
  another on the load is due after that one's time left too; at its
  deadline it is given up - its socket closed, which whisper-server hears
  as its client gone - and fails saying so.

### The runner behind the queue

`runner/adapter.py` is the queue's backend when the server is started with
`--backend mock`, `--backend real` or `--backend llamacpp`:

- **The server's actor holds the card.** The server hands the runner its id
  (`attach`); a generation takes the model its job names under that id - the
  one `acquire` in the server. One server, one holder.
- **The record follows the folder.** Binding a folder points the runner's
  record at `<folder>/runner/`; binding another moves it there without
  reloading the model; letting the folder go stops the record there.
- **What a stopped server of a folder left running is looked for at bind.**
  It is reported in the log; `--reclaim` takes it back, and only on proof that
  it is this runner's own.
- **Health says what the runner holds** - its own status, verbatim, under
  `runner` (null when there is no runner behind the queue).
- **The model leaves the card by itself** after its idle window - on a small
  timer beside the manager, since the queue's worker goes with the folder. A
  model in use is never taken: it is held from before it is loaded until its
  stream has ended, and its window starts again after.
- **The answer is the text alone**; what a model deliberated goes beside it,
  in `reasoning`. Thinking counts as progress, so a model deliberating at a
  healthy rate is not a stalled one. Progress is reported at the first word,
  then at most every half second - each report is a write of the store.
- **A request is a prompt or a conversation** - a string, `{"prompt": ...}`,
  or `{"messages": [...]}` held to the contract's `conversation` shape - and
  anything else fails the job before a model is loaded, never turned into a
  prompt by guessing.
- **The server stopping takes the model off the card.**

### With a coordinator: the card by ticket, and the ports

`--coordinator <url>` makes the server one of the coordinator's holders - and
so does a coordinator found through the machine's lock, for a session with a
real card (*The machine's card lock*, below). The client half of its protocol
is `coordination.py`: every body it sends is checked against its route before
it goes, every answer against the shape the route declares - and an answer
the contract does not declare is no answer.

- **The memory is verified by the coordinator** (PLAN-4 D2; *The memory,
  before each load*, below). The ticket declares what the runner estimates
  its model needs; at the claim, work needed now that the card has no room
  for is refused at once - the job fails in the coordinator's words and
  numbers, and asks no more - while scheduled work waits for the room,
  within its patience. Before each load, an amend.
- **A pipeline's label is its ticket's** (R-B4): the take a pipeline makes
  asks with its `label`, so the operator's page tells one session from
  another (`Terms.label`).
- **A connection its asker resets mid-request** is said in one line of the
  server's log, never a traceback (R-B5) - as the coordinator does.

- **The card is a rental, had by ticket.** Before a model's weights go up the
  runner asks for a ticket ONCE, waits on it - each wait held open by the
  coordinator and answered the moment there is news - and claims the card
  with the ticket's token the moment it is offered. Nothing polls: a waiting
  server makes one request per hold (30 s), where it once asked every second.
  The rental names the request that put the model up.
- **The card is held by a pipeline, from its open to its close** (PLAN-4 D6).
  A model switch inside it NEVER gives the card back - the rule that a switch
  gave it back and asked again is reversed - and a pipeline with nothing
  loaded still holds it. It goes back only when the pipeline closes, once
  every model of it has come off. See *The pipeline*, below.
- **Each renewal says whether the card is `wanted`** - another ticket is in
  line - and nothing more. The queue's own pipeline lets the card go at once
  when its work is done and the card is wanted; an application's keeps it.
- **A job's terms.** An interactive job asks as `responsive`, a batch job as
  `scheduled`: responsive work is offered the card first. A job that names
  no priority is interactive (S18b), so it asks as `responsive`. A job may say how
  long it waits (`card_patience_s` on submit); without it, the runner's
  settings say (`card_patience_s`, an hour). When the patience runs out the
  job fails with a `reason` of its own, `card_patience`; a patience of 0 is
  now or not at all, and a card not free at once fails the job `card_busy`.
  Neither names who has the card.
- **Waiting is visible, bounded - and names no one.** While the card is not
  free, the job's progress says *waiting for the card, since 10:02*, health
  says `waiting_since`, and the stall watch treats the wait as the load it
  is. The coordinator tells no application who holds the card, and the
  server does not guess. A cancel ends the wait at once and gives the ticket
  up; so does the server stopping. The wait holds no lock: stopping the
  server, or binding a folder, never queues behind it. **Never a load without
  a claim.**
- **One place in line.** A server holds at most one ticket: a second load
  waiting for the card waits for the first's take, and a take while the
  server holds a rental is handed that rental.
- **The token is the ticket's only key.** The server keeps it in memory and
  sends it to the coordinator alone - never into its log, its folder or an
  answer.
- **Loss is learnt only from an answer.** A rental the coordinator took back
  (`reclaimed`, `unknown_ticket`) takes the model off at once - another
  application may be loading onto the card now - and a generation it cuts
  off says so. Nothing loads until a new claim: an application's pipeline
  asks for a new ticket by itself, and loads its models again once it has
  one. A coordinator that does not answer takes nothing away: health says
  `unreachable, holding`, and the model keeps working.
- **Ports come from its registry**: a load's (purpose `model-server <load
  id>`, one per load, so models side by side never share one) and, with no
  `--port`, the server's own (purpose `server`), each kept alive by
  the one heartbeat and given back with what used it. An explicit `--port` is
  the application's own, and is not registered. A start the coordinator
  cannot give a port stops, saying so; it never guesses.
- **Heartbeats wait at most one interval.** On Windows a connection to a
  port nobody listens on takes about two seconds to be refused; unbounded,
  an outage would slow every heartbeat that much, and a rental could lapse
  after the coordinator came back. The interval is fixed at the claim: read
  off a renewed rental, it grew until a live holder lost the card - found by
  building.
- **One actor.** The server's id is minted before anything is asked for,
  and it holds the folder, the rental and the ports.
- **Health's `card`** is the contract's three fields: `coordinator` (`none`,
  or the last contact's `reachable` / `unreachable`), `holding`,
  `waiting_since`.

### A cancel during a load

A job cancelled while its model's weights go up gives the load up at once:
- **the process is ended;**
- **the card, the lease and the port go back;**
- **the job ends cancelled.**

The exception: a job the queue can run next names the same model. Then
the load goes on for that job. The question is asked again every half second
while the weights go up, and once more when they are up. A next job cancelled
meanwhile gives the load up too, and a model up with nobody left to want it
comes off.

**A cancelled job is never handed the model.** The queue answers whether a
job that can run wants the model (`Queue.wants(model)`: a queued job, its
`after` all landed, that names it). Since S17 jobs on different models run
at once, so the question is any such job, never which one job is next. The
server hands the runner that one question (`wants`), so the runner never
reads the queue's store.

On the real card this was the other way: the job ended only once the weights
were up, 7.0-7.4 s after the cancel for a load of about 8. The model then sat
on the card for whoever came next, or for nobody until the idle sweep.

### The machine's card lock: who holds the card at the top

A real card with no coordinator named is had through a lock the operating
system holds (`runner/cardlock.py`; the contract's `machine/card-lock.json`
names it and the note its holder writes beside it): `llm-bootstrap-card.lock`
in the user's temporary folder. EACH TIME A SESSION OPENS - a pipeline, the
queue's own included - the server looks at it (PLAN-4 D5), and nothing needs
configuring:
- **free:** no coordinator runs, and the server takes the lock itself, held
  until the pipeline holding it closes (see *The pipeline*);
- **held, its note naming a coordinator:** the coordinator took it as it
  started. The session goes through it, at the address the note gives - a
  rental, had by ticket, under this server's actor and `--name` - and is let
  go with the session: the next one looks again. Two takes at once - a
  pipeline's thread and a load - go through one card source, so one ticket:
  the second waits for the first's answer (found building S4: the second
  closed the first's under it, and asked a ticket of its own);
- **held otherwise:** an application run on its own has the card. The server
  waits, its request's own patience applying (`card_patience_s` on a job;
  the settings' otherwise): 0 is `card_busy` at once, and a patience that
  runs out `card_patience` - each naming no one. It was once read as "none
  given", and waited ten minutes; and a refusal once named the other
  server's pid and actor (S6's register: D4 forbids it);
- **held, with no note yet:** a holder writes its note just after it takes
  the lock. A request that would not wait gives it a moment (`NOTE_WAIT_S`,
  0.2 s) and looks again, once - so a coordinator that has only just started
  is gone through; before, in that instant, the request was refused
  `card_busy` as if an application held the card (S4's register R5). A
  holder that never writes a note is `card_busy` after the moment.
- **held, beside a stale note:** a holder that dies lets go of the lock and
  leaves its note. A note is trusted only while the process it names (`pid`)
  lives and started before the note was written (`since`); one whose writer
  has gone - or whose pid was handed on - names nobody: never a coordinator
  to go through, it is read as no note (the moment above is given), and a
  refusal adds that the note beside the lock is stale, never whose it was
  (H5's register 6). A pid that cannot be asked about is trusted.

And:
- **a holder killed outright lets go:** the kernel releases a dead process's
  locks, and nothing has to run in it;
- **a waiting server says only that it waits,** and since when - *waiting for
  the card, since 10:02*, as with a coordinator - never who holds the card or
  for what (D4);
- **the wait ends** on a cancel, on its patience, or when the server stops -
  through a coordinator the lock named too;
- **the record says** how long a load waited, and what it was told while it
  waited (`card_waited`'s `waiting_for`), and every take and give-back of the
  card, named by its source (`card_granted`, `card_given_back`: `machine
  lock` or `coordinator rental`) -
  a take nothing waited for used to leave no line, so no record could show
  the lock was ever taken;
- **after an unload that failed,** the card is kept, since the model may
  still be on it, and goes back when the server stops; no model loads until
  that stop, asked again, works (below).

**`--coordinator <url>` stays, as the explicit way:** named, the server goes
to that coordinator for every session and takes no lock - its loads' ports
and its own port from its registry too - which is what a stand-in model with
a coordinator (the suites), and a coordinator whose lock this user cannot
see, need. Found through the lock, a session's loads take their ports from
the operating system, as a server run on its own does.

A stand-in model takes no lock, and finds no coordinator. `[runner]
card_lock` names another file; the stand-in suites give every runner one of
its own, so no suite ever takes the machine's.

**What the lock does not do, and the coordinator does:** take the card back
from a holder that hangs, and order the applications waiting.

### The memory, before each load

The runner's own check before a load is gone: THE CARD SOURCE DECIDES
(PLAN-4 D2), by the contract's one memory check (`memory.py`), on the
runner's estimate of what the model needs (`runner/needs.py`):
- **llama.cpp:** its weights, its cache at its context, and 1536 MiB of slack;
  **whisper:** w + w/4 + 600 MiB, w its file;
  **the stand-in:** nothing. A model whose file is not there has no estimate,
  and fails at its start. Every ticket declares the estimate
  (`models[].need_mib`).
- **Through a coordinator:** each load is an amend (`/card/amend`) naming the
  rental's whole list - so a process that has just gone is known to be gone -
  and the coordinator reads the card afresh and approves, or refuses in its
  words and numbers. A coordinator that does not answer decides nothing.
- **Run on its own, on the lock:** the card is read here and judged by the
  same function, keeping `[runner] card_margin_mib` (256) free.
- **Refused,** the load is refused before it is paid for, in words that say
  the numbers and which: a model no card this size could hold says so, and
  what to change. The record's `vram_check` says the verdict, the source and
  the numbers either way; `vram_unknown` that the card could not be read,
  which never gates.
- **A job refused for the memory ends with a reason of its own** (S4's
  register R2): `no_room` - not now, something else holds the rest - or
  `never_fits` - not on this card at all - whether the amend refused its
  model, the lock's own check did, or the coordinator refused its ticket at
  the ask or the claim. Its `error` keeps the words and numbers of whoever
  decided.
- **No room now fails at once, LOUDLY** (H9; the owner, 2026-09-28: "Fail
  loudly. Memory almost never ever cleans itself up and if it does it will
  be after hours when a program is shut down"). On a card that is not
  over-full, a model that does not fit is never waited for - a job's model
  on demand, a `/load`, a pipeline's open and the models a session starts
  ready with alike, on the lock and under a coordinator. Its words are the
  contract's (`memory.no_room_words`): what the card has free of its whole,
  what is needed with the margin, what programs outside the bootstrap hold
  (the card's use less the session's or rental's own), and what to do -
  close programs that use the card, or reboot. The job's `error` and the
  pipeline's `why` say them whole, and the server's log says them once, in
  a `runner:` line (`ModelManager.on_say`, which the adapter points at the
  log). SCHEDULED WORK may wait for room instead (S18, below).

### Scheduled work: wait or fail, and not before a time (S18)

The owner's second ruling of 2026-09-28: scheduled work that does not fit
waits for room or fails at once, *"both, with a config"*; and a batch starts
as soon as there is room, not before a time, or both - *"Lets support both
combined."* His case is a batch of many hours, planned in the day and run at
night.
- **A job that does not say it is batch is live (S18b).** The ruled record:
  PLAN-4 D0, *"class default RESPONSIVE"*; the owner, *"none of my agents
  run scheduled batches, it is always ran right now and response asap"*.
  Without `priority`, a job is `interactive` (`jobqueue.Queue.submit`, the
  server's door, and a stored row that says none): it goes before batch work
  in the queue's order, asks as `responsive`, never waits for room, and is
  refused `live_is_now` for a time still to come or `if_no_room: wait`. Only
  `priority: "batch"`, said by the application, makes a job scheduled work.
  Decided: the default priority itself became `interactive`, rather than a
  job's class being responsive while its place in the order stayed batch's -
  a job's priority is ONE thing, its place in the order and its class at the
  card, so the queue's own pipeline, which takes its most urgent job's class,
  never has a live job riding a scheduled ticket behind a batch job asked
  before it.
- **Wait or fail, by a setting.** `[runner] scheduled_if_no_room` (`wait`) -
  and a job's or a pipeline's own `if_no_room` over it. `ModelManager.if_no_room`
  decides: responsive work always `fail`s. A coordinator's ticket carries it:
  at its claim, a scheduled ticket that asked to fail is refused `no_room`
  as a responsive one is.
- **A job waits for room in its QUEUE, never on a thread holding the card.**
  Its terms `park` (`runner.Terms.parks`, set by the adapter for a batch
  job). A load that finds no room for it, with nothing of the application's
  own left on the card, is refused `WaitsForRoom` - its `room`: the numbers,
  what programs outside the bootstrap hold, the words - and nothing is said
  loudly: waiting is no failure. The queue puts it back, `queued`
  (`jobqueue.Queue._park`), its `progress` the room it waits on. Its own
  models holding the room (pinned, or answering) is no wait anybody else can
  end: that fails at once, as ever.
- **The queue's look** (`jobqueue.Queue._look`, every `room_look_s`, 5 s):
  every planned or waiting job, on the queue's WALL clock - so a machine
  that slept through a job's time starts it at its first look after it
  wakes. The soonest is asked about (`backend.room_look`):
  - **under a coordinator** (`CoordinatorCard.look`): ONE PLACE IN LINE is
    kept for it - a ticket asked with its `not_before` from the moment it is
    named, so the operator sees it planned, and it holds back nobody until
    its time - kept alive by each look (a wait of 0 s), and due once the
    coordinator offers it; the job's next take claims it
    (`CoordinatorCard.parked`). Through a coordinator found by the machine's
    lock, the same card source is kept for it (`MachineCard.look`).
  - **on its own** (`ModelManager.room_look`): at its time at once; after
    waiting for room, the card is read and the model's need judged by the
    contract's one memory check, with `card_margin_mib`.
- **Its patience counts from its time** - from the first look after it, on
  the wall clock, kept in the folder (`waits.since` on its row) - so a
  restart does not start it again. Run out while it waited for room, it
  fails `no_room` in the loud words, saying how long it waited
  (`memory.no_room_words(..., waited_s=)`); run out while the card was
  another application's, `card_patience`.
- **A scheduled pipeline waits in place:** a coordinator passes its ticket
  over (its take waits, as before S18, saying the room it waits on as the
  pipeline's `why`); on its own, `MachineCard.take` judges its first model
  BEFORE it takes the lock (`_judges_room`), and while it does not fit it
  waits without the lock, reading the card every `card_recover_watch_s` -
  holding back nobody - within the server's patience, then fails loudly.
- **Live work is now:** a job that does not say it is batch naming a
  `not_before` still to come or `if_no_room: wait`, and a responsive
  pipeline naming `wait`, are refused `live_is_now`.
- **What a person sees:** a job's `not_before` and `progress`; health's
  `queue.scheduled` (`planned`, `next_at`, `for_room`, `room`); the progress
  and session widgets' "starts at 23:00" and "waits for room".
- **Not kept across a restart:** a place in a coordinator's line (it is
  asked again at the first look - the old one is dropped by the coordinator
  once nobody waits on it); a pipeline, as ever.

**The measured need** (D2, D12): once a model's statistics hold three
measured loads - `first_answer_mib`, what it held once it had answered - its
need is the largest of them + 256 MiB, in place of the estimate: in the
fit check, and on every ticket (`ModelManager.need`, `runner/needs.py`). A
process's own memory (`mib`) is the card's memory its load took, as
measured (above) - null until then.

### A rental names every process it put on the card

With a coordinator, the moment a load's process exists (before its weights
are up) the runner names it to its card source, which names it on the
rental:
- **its pid;**
- **its proof:** the load's id, which its command line carries (llama-server
  as `--alias`);
- **its model, its engine and its port** (`mib`, its memory, is not measured
  yet, and goes as null).

The card source keeps the list, and every heartbeat carries the whole of it
(`processes`), `[]` included: it replaces the coordinator's, so a beat lost
on the way is healed by the next. One goes at once after every spawn and
every stop (`beat_now`), not an interval later. A process leaves the list
only once its stop has returned; `give_back` forgets the list with the
rental. With models side by side the list names each of them - at most 8.

**A process whose stop failed stays named, and nothing loads while it will not
go.** It may still be on the card; the processes beside it stay named too,
each until its own stop returns. Its load leaves the table (its model is
never used again), but the runner keeps it aside, and asks its stop again
before the next load and when a pipeline closes - the record says `again`.
While it still fails, a load is refused - at once, too, when it failed
while the load made room or began - and a close answers `unload_failed`,
each saying so; once it goes, the record closes its load,
its port goes back, and the load or the close goes on, on the rental still
held. The runner used to load beside it, and its record then read two models
at once.

If this server hangs, alive and silent, with its models on the card, the
coordinator ends every process the rental names when the rental lapses, each
only on its own proof. The next holder is then handed a card with nothing
left on it. The kernel's kill-on-close covers a server that dies; this covers
one that hangs.

**Whether the coordinator is up** is asked once, at start, at its own route
(`/up`), which answers that and the version of the contract the coordinator
runs, and nothing more: the operator's `/state` names every application, and
no answer to one names another. The server keeps that version
(`CoordinatorClient.coordinator_version`) and, when it is not its own, says
so once, naming both: an answer the two contracts do not share is then no
answer.

### The coordinator watches the work

PLAN-4 D3. With a coordinator, every heartbeat also says what the rental's
work does - the manager counts it where it really happens, and its card
source sends it on each beat (`ModelManager.work`, `CoordinatorCard.said`):
- **`working`:** a model is answering a request - from the moment its answer
  is asked until its stream ends (`ModelManager.answer`, which every
  generation goes through). A load, a count, a wait for the card is not
  answering: a cold load of minutes is never a hang.
- **`progress`:** each token streamed, each piece of reasoning and each
  answer's end - since the rental was granted. Nothing else moves it.
- **`deadline_s`:** an answer that comes in one piece - whisper's transcript
  - declares how long it may take, through its backend's seam,
  `Backend.answer_deadline_s(messages, params)` (None, the default: its
  answer streams). The latest of the answers under way, from now. The
  engine that answers in one piece fills it (S8, S12b).
- **`memory_errors`, `memory_error`:** the lines of its models' logs that
  said the card's memory ran out (`Backend.memory_errors`; llama.cpp's reads
  the log the runner keeps for each load, from the load's own launch -
  `runner/memlog.py` names b10724's lines, and where each is written): how
  many since the grant, and the newest. Each is on the record
  (`memory_error`) and said at once, a heartbeat now; read after every
  answer, and after a load that failed.

A rental that says it is working and does not move for the coordinator's
hang limit is taken back as a crashed one is, and heard as a loss. **A
degraded card** - the coordinator's word on a heartbeat's answer - ends the
answer under way at once (its stream is cancelled; the job fails
`card_degraded`, never "cancelled"), gives up a load under way, refuses
every next model work (`card_degraded`, before the card source is asked),
and the pipeline says `degraded`, with why; the card is still held. It
clears when the answer says so, and work goes on. An answer that broke while
its model's log said the card's memory ran out fails `card_degraded` too,
with the line. The coordinator's own refusal of an amend (`degraded`) is the
second line: a server not yet told puts nothing on a degraded card either.
The record says `card_degraded` and `card_restored`.

**The card full, recovered by itself** (S5b; the owner, 2026-09-27: he
closes programs, and the request goes through by itself). On a card source
that says when the card has room again (`recovers`: a coordinator's rental,
or the machine's lock when the runner reads the card), degraded means
WAITING, not failing:
- an attempt the card's memory broke fails `card_degraded` MARKED for the
  queue (`GenerationFailed.card_full`, the card's numbers), which throws it
  away and runs it again from the start, the same seed, as a sample of its
  own - never counted against the job's `retries` or its judge - at most
  the queue's `recover_attempts` (3) times, then the job fails
  `card_degraded`, naming each attempt; the request's record has an
  `attempt_card_full` line for each;
- the run again is HELD in `acquire` (`on_full`) until the source says the
  card has room, within the job's patience counted from the first break
  (`Run.card_waited_s`): its progress says `waiting for the card's memory`,
  in words a person acts on - *"The card is full: 1.2 GB free, 6.4 GB
  needed. 9.8 GB is used by programs outside the bootstrap. Close
  something, and this runs again by itself."* - and in numbers (`free_mib`,
  `need_mib`, `outside_mib`, `waited_s`, `patience_left_s`); the session's
  `why` says the same. Patience 0 fails at once, as before; a patience run
  out fails saying how long and what the card read;
- a memory error the source has not yet said holds the job a moment
  (`UNHEARD_S`, 5 s) for its word;
- room again: the models the application's session keeps that came off are
  loaded again first, through the usual memory check, and every held job
  goes on - its own model loaded again if it came off.

A server run on its own does the same (the owner: "same behaviour as
coordinator"): the machine's lock reads the card itself while it holds it -
every `[runner] card_watch_s` (30), every `card_recover_watch_s` (2) while
degraded - marks it under `card_floor_mib` (256) or on a memory error its
models' logs said, refreshes the numbers, and clears on room for what its
session needs, as a coordinator does. A source that never says the card has
room - the stand-in's, a row's fake - fails the work at once, as before.

### The pipeline: the card from its open to its close

PLAN-4 D6, and D7: models side by side. A pipeline is the card held from its
open to its close: one ticket's rental with a coordinator, the machine's lock
without one, nothing to take for the stand-in. One per server, named by a
`pipe-` id (the contract's id grammar).
- **An application's:** `POST /api/pipeline/open {models?, class?, label?,
  keep_s?}` answers at once - `waiting`, or `holding` - or, with `?wait_s=N`,
  once every model it names is up or failed to load; the pipeline's own
  thread takes the card and loads its `models`, one at a time and side by
  side, and keeps them (pinned): the idle sweep leaves them alone, and no
  other load takes them off. Each is named once (`repeated_model`), at most
  `max_loads` (`too_many_models`). `POST /api/models/{key}/load` pins one
  more, and `POST /api/models/{key}/unload` takes one off - pinned or not -
  and leaves the rest, and the card. `POST /api/pipeline/renew {pipeline}`
  keeps it open;
  `POST /api/pipeline/close {pipeline}` gives up a wait or a load, takes every
  model off, and only then gives the card back - the answer waits for that.
  Another open while one is open is refused `already_open`.
- **Unrenewed, it lapses** after `keep_s` (the open's, else the runner's
  `pipeline_keep_s`, 120 s): a lapse is a close, with a record line of its
  own (`pipeline_lapsed`). `keep_s = 0` holds it until it is closed or the
  server stops.
- **An application starts ready:** the runner's `session_models` (none by
  default) are the session the server opens as it starts - held until it is
  closed or the server stops - and loads them one after the other from the
  start, kept side by side: each named once, at most `max_loads`, and one
  on a configured `port` - a session it could never open is refused as the
  settings load.
- **Without an open pipeline, the queue opens its own** with its first load,
  in the class of the job that opens it: the queue runs its most urgent job
  first, so that is its most urgent job's (interactive: responsive). The
  class is the ticket's from the moment it asks - an interactive job queued
  behind a scheduled job's wait for the card rides that scheduled ticket, its
  place in the line being the one the ticket asked for (S6's register R4,
  decided in S7: kept). An application whose interactive work must not wait
  behind its own batch work opens a pipeline of its own, `responsive`. It
  closes when nothing is loaded and no job is queued: at once when a renewal
  says the card is wanted (its models come off first), otherwise once the
  idle sweep has taken its models off. An open that finds it takes it over,
  card and all.
- **A switch never gives the card back, and zero loads is not a release.**
  A host's swap has a moment with nothing on the card; the pipeline holds
  it through that moment, and its heartbeat names no process.
- **Lost** - the card taken back, said by an answer: every model comes off,
  nothing loads until a new claim, and an application's pipeline asks for a
  new ticket by itself. The queue's own is simply over; its next work opens
  another.
- **Health's `pipeline`** says where it stands - `closed`, `waiting`,
  `holding`, `degraded` or `lost` - with `why`, `since`, its `label`, its
  `models`, `up` (every model on the card and answering), its class, its
  `keep_s` and who opened it (`application`, `start`, `queue`). Health's
  `card.holding` is the pipeline's too, with no coordinator: a model up
  while the queue is idle still holds the card. `degraded` is the
  coordinator's word, heard on a heartbeat's answer (below): the card is
  still held, and `why` says the coordinator's words.
- **The record** says `pipeline_opened` (with `took_over` when it took the
  queue's own), `pipeline_closed` or `pipeline_lapsed` (with why),
  `pipeline_lost`, `pipeline_load_failed`, and every `card_granted` and
  `card_given_back` names its pipeline.

### On the real card

`test_real_card.py` runs the runner against a REAL llama-server, a real model
and the real card - and only when asked: every row skips unless
`LLM_BOOTSTRAP_REAL_CARD` names a runner settings file (llama-server, the
models folder, a model). It is not one of the part's gates, and no sweep runs
it. Rows run one at a time; each keeps its evidence - its settings, the
server's log, the runner's record, llama-server's log and a timeline - under
`LLM_BOOTSTRAP_REAL_OUT`.

**Every row is watched from outside** by `cardwatch.py`: the card's memory,
read five times a second from one long-lived `nvidia-smi`, and every model
server on the machine - every engine's - with its PARENT, read from the kernel's process
snapshot ten times a second. The parent tells this suite's llama-servers from
anybody else's - and still does after the parent was killed, since Windows
never re-parents an orphan. Whatever a row provokes, it ends with the same
four checks: never two of its llama-servers alive at once, read off the
machine and not the record; none of them outlives the row; the card's memory
comes back to what it was; and the runner's own record never has two models
loaded at once. **A llama-server the suite did not start stops the suite** -
before a row or during one - and nothing of it is touched.
`python cardwatch.py` prints the card and every model server on its own:
llama-server and whisper-server, each named by its engine.

The rows: one job end to end; six jobs in one instant sharing one load; a
cancel mid-answer, while the prompt is read, and while the weights go up; a
storm of cancels; a stop mid-answer and mid-load; the server killed
mid-answer and mid-load; llama-server killed mid-answer and mid-load; a job
arriving as the idle sweep takes the model off; two models switched back to
back; two servers taking turns through a coordinator, a holder killed with its
model up, a waiting server stopped, and the card taken back mid-load; two
servers with NO coordinator taking turns through the machine's lock, each take
and give-back on their records (measured: the lock changed hands in 50 ms, the
second model's process started 0.7 s later); a
context no card could hold, a prompt longer than the context, an answer that
fills it; five starts and stops; and the port while the weights go up.

**M1's rows** (PLAN-4, run on the card 2026-09-29, 32 of 32 green) need two
more variables. `LLM_BOOTSTRAP_REAL_WHISPER` is JSON - `{"server":
<whisper-server>, "model": <a whisper model file>, "audio": <a WAV>}` - and
runs two engines side by side: whisper beside the text model, answering at
once; every process named to the coordinator within one beat; a lost rental
ended whole; a HUNG holder's rental ended whole by the coordinator on proof
(the server suspended, alive and silent); and never two rentals' processes on
the card at once. These rows allow as many model servers at once as they put
there on purpose. `LLM_BOOTSTRAP_REAL_FILLER` names a program OUTSIDE the
bootstrap that fills the card and holds it until it is ended: the card full,
a request waiting in words, the program closed, the request running by
itself - on the machine's lock and through a coordinator. **Run ONE filler,
never two:** on Windows' WDDM driver GPU memory asked for beyond the card is
taken from system RAM, and two fillers ran the machine out of memory. The
driver also keeps a reserve - a filler alone stopped at 634 MiB free, beside
the 24B once at 1.8 GB - so the card-full rows name a floor of 2 560 MiB.

**M2, the measurement** (`measure_card.py`, not a gate; the same variables,
its whisper JSON naming `"models": {key: file}`): one big model, the whisper
models side by side, one alone, the whisper models started at once outside
the runner, the host's swap timed whole, and two answers at once against the
same two one after the other - each arm once thrown away, then `--runs`
times, the table and every number kept under `--out`.

**What the first runs measured**, with Mistral Small 3.2 24B (Q5_K_M, 15.6 GB)
at context 4096 on a 24 GB card: a load takes 8-9 s (14 s the first time
from disk); the card rises 16.3 GB, where the runner's check asks for 17.7;
about 40 tokens a second, 1,200 a second reading a prompt. A cancel ends the
job in 0.03 s and llama-server lets go of the slot in 0.1 s - of a prompt
being read, in about one second. A stopped llama-server gives the card's
memory back in a fifth of a second, and takes about two more seconds to
finish exiting. Two things about llama-server itself, both measured after I
had them wrong: it holds its port, and answers `/health` 503, from the first
second of a load; and killed, it answers nothing at once and gives its memory
back within a third of a second - yet its process lingers about two seconds
more, one thread finishing the exit, while Python's Popen already reports it
gone.

Two rows were rewritten for the cancel rule above (*a cancel during a
load*), and have not run on the real card since:
- a cancel while the model loads gives the load up;
- a cancel while the model loads leaves the load to the next job that wants
  it.

The storm of cancels now counts a load given up for a cancel as the rule,
not a failure.

**What the real card found, and what changed** - each with a stand-in row
that failed first:

- **A stop waited out a load** (10.2 s, for a load of 8.2 - and a load that
  never came up would have held it for the whole startup timeout). A load can
  now be given up (`ModelManager.abandon`): its process is ended at once, and
  the load fails in the words it was given up with. A stopping server gives
  up its load first, refuses any other (`stopping`), and asks the
  coordinator for nothing more - a wait for the card ends too.
- **A card taken back during a load stayed loaded** until the load finished
  (9.4 s after the answer said so). The loss now gives the load up at once,
  on the heartbeat's thread - it takes no lock - and a loss heard between
  the grant and the load means the weights never go up.
- **A dying llama-server was reused.** Killed underneath, its process lingers
  about two seconds while it answers nothing; the reuse check asked only the
  process, and the next job was handed the corpse. It now asks the port too.
- **A stop wrote the unload nowhere.** The folder was let go first, so its
  record said the load had left, and the unload itself went to memory: an
  unload that failed would have been invisible to the next start's search
  for what was left running. The model now comes off while the folder is
  still bound.
- **A stop mid-answer waited ten seconds for a timeout of five**: the queue
  waited for its worker twice. One timeout now covers every thread.
- **An empty card was blamed.** A context no card this size could hold was
  refused with "Something else holds it". The check now reads the card's
  total too, and says which: cannot fit at all (and what to change), or
  something else holds the rest.
- **The next load started beside a dying llama-server** - found by the card
  watch itself, on the second run: two llama-servers alive at once for 0.4 s
  (the first's memory already given back). Killed underneath, a llama-server
  takes about two seconds to exit, and all that time Popen reports its exit
  code - `terminate` finds it terminating and records the code, and `wait`
  returns at once. A stop now waits on the process itself.

## Proof

`python gate.py` (at the repository's root) runs every suite below. It keeps
each one's whole output, and fails on a red - its exit code, or its own tally
saying a row failed - a missing tally, or a ResourceWarning. The tally it
reads is the suite's own last line, "P passed, F failed, T total", never
unittest's "Ran N tests", which leaves out every row of a class that could
not be set up.

- `test_sessions.py` - 18 rows. The reference client: an answer the contract
  does not declare, and silence, are no answer; a declared refusal is raised
  with its reason, its sentence and its status; a body the contract refuses
  is never sent; the contract's version is asked first, and another is
  refused naming both; a wait is the server's - one ask, held until the job
  ends, even past the client's own timeout - and one that runs out names the
  job; a component opens nothing and is told when its host has no session; a
  start says where the server listens, or why it did not. Then three whole
  sessions over HTTP, against a real coordinator in the suite's process: a
  Dictaphone's (its server started as a child, two whisper models pinned,
  live dictations, a compare, a close - one ticket, one load of each model);
  an analysis app's (a pipeline per replay, another application's live work
  going ahead between two); and a host running a component's models in its
  own session - whisper, the deep thinker, whisper again, with the card never
  given back, never two loads of one model, a live ask from outside refused
  `card_busy` naming no one while nothing is loaded, and "answer done ->
  whisper ready" printed (the Dictaphone's H-1, H-2, H-4 to H-7; H-3 needs a
  process named on the rental, and `test_coordination.py` proves it).
- `test_queue.py` - 425 rows. No model, no GPU, no network, no subprocess.
  Among them (S8): an answer in one piece never stalled, nor taken back by a
  new submission, before the deadline it declared, and judged as any quiet
  one after it; a transcript of silence an answer, an empty text still
  nothing.
- `test_server.py` - 843 checks: every route over real HTTP against a model
  the test steers; every response validated against the contract; **every
  answer the contract declares for the server - each status, each refusal
  reason, and its answers before any route - provoked from the real server at
  least once**; a page from another address refused, one allowed answered
  with what its browser needs and its preflight, and a request with no Origin
  or from the server's own page answered header for header as before; a
  request to a name the server does not have refused whatever it asks, and
  one to each of its own names answered; a body over the cap or in chunks
  refused unread, and a real client sending one hearing the answer; a method
  no route takes checked like every request; an asker that expects 100
  Continue told so only once the checks let it through, and a refused one
  answered without its body read; a request that never arrives whole
  closed at the read timeout, and a helper's socket refused the server's
  port each way round; a start stopped by anything but its port saying
  what it was, and letting the port go; the contract's
  `bad_request` examples, and its examples before any route, answered word
  for word; a route added to the contract and not served stops the server
  starting; a real server process killed while it holds a folder; the models
  routes against a stand-in runner whose settings name real GGUF headers - a
  folder's load record read back, a measure refused while a model loads, and
  a file locked by another handle said in the system's own words; and the
  kept configuration - its
  file's bytes hashed to its name, the same kept twice one file, a file
  changed after it was kept never handed out, and a current.json naming
  something that is not a hash refused, never followed out of its folder;
  and the pipeline's three routes - every answer they declare, health's
  `pipeline` as it moves, and a server whose settings name a session opening
  it as it starts; models side by side - a pipeline opened with two and
  answered once both are up (`?wait_s`), a model loaded and kept, and one
  unloaded while the rest and the card stay, with every answer the two model
  routes declare; and a job naming every text parameter the contract names,
  its model handed each exactly as named, and a misspelt one refused before
  any job is made.
- `test_runner.py` - 440 rows, on the stand-in model and fake backends. **It
  cannot kill**: the module's kill is replaced by a tripwire before any
  manager is built, and the startup-check rows hand the manager a fake that
  says what it would have killed. It builds real managers, so what an
  existing application's suite had to assert on the SOURCE is asserted here
  on the BEHAVIOUR: a load record made unwritable while the load still
  succeeds, cold and warm read off real loads, the slow-load warning printed
  by a real load. The clock is the row's: the idle and lease rows cross their
  windows exactly, and nothing sleeps. The read-speed measure runs on a real
  file - bounded, past the cache on Windows, stopped at its limit by the
  row's own clock - and model health's history is checked where the server's
  rows cannot reach: the last load judged against those before it, and a
  record somebody broke. **One run through every door a load passes** - a
  cold load, a use, a reuse, a swap under a generation, a dead backend loaded
  again, a rebind, the idle sweep, a load that fails, a stop that throws -
  is compared, answer for answer, with what the runner answered before its
  loads were one table: `status()`, `models()`, both folders' records. **The
  pipeline:** the card held from its open to its close through the machine's
  lock, across a switch and with nothing loaded; the queue's own opened and
  closed by its work - warm while nobody wants the card, gone at once when it
  is wanted, kept while work is owed, and never closed under a generation
  that began while it decided; keep_s, the lapse and its line; a close that
  ends a wait, gives up a load and gives the card back only once its models
  are off; a loss; the settings' session; and wave 1's three registers. A
  process the startup check could not ask about is left, and taken back at
  the next start: late, never lost. And what the gate reads of a suite's end,
  run through the gate's own `run_suite`: the suite's own last tally, never
  unittest's count; a tally saying a row failed, or none at all, fails it.
  **The memory and the lock:** each engine's estimate, rounded up to whole
  MiB; the card source asked before each load, its refusal refusing it
  before it is paid for, an unread card never gating, a source with no check
  deciding nothing, the margin a setting; and the machine's lock - its name
  and note the contract's, a patience of 0 `card_busy` at once and one that
  runs out `card_patience`, each naming no one, a coordinator's lock gone
  through (and the manager's ears handed on), two takes at once through it
  sharing one card source, a session lost or refused through it let go and
  the lock looked at again, and the card read and judged here on the lock.
  **Models side by side** (S7): two up at once, each load naming what else
  was up; loads one at a time under the load lock, and a generation, health,
  a bind and a stop each answered while another model loads; stops that run
  together; room made by taking off the on-demand model used longest ago -
  never a pinned one, never one answering a request - past `max_loads`, one
  model at a time on a configured port; a stop already under way waited out
  as room being made - never a second model off for it, never the load
  refused, the fit check asked again once and not in a spin, a cancel and the
  server stopping ending the wait, and one that failed refusing the load, as
  one failing as a load begins does; each model swept on its own idle clock;
  a load and an open answered the moment their models are up, or failed; an
  unload that leaves the rest and the card, gives up a load under way and
  ends a pinned load still waiting its turn; a rebind, a loss and a shutdown
  that take every load - a rebind carrying a load whose stop failed to a new
  folder, and leaving it open in a folder let go; a close that asks a failed
  stop again, waits out a stop another caller began and a load that cannot
  be given up, and is refused while that stop has failed; the load history
  folded under one lock through temporary files of its own; health's
  `holding` from the pipeline; the settings' session of several models,
  refused past `max_loads` and on one configured port; and the runner's own
  port range, bind-checked. **The watch** (PLAN-4 D3): each token, piece of
  reasoning and answer's end counted as the work's progress, and nothing
  else; working while an answer streams, and only then; a one-piece answer's
  deadline through its backend's seam; the work handed to a card source that
  says it; a memory error from a model's log counted once, said at once and
  recorded - after an answer and after a load that failed - and an answer
  that broke with one failing as the card degraded; the card degraded ending
  the answer under way, giving up a load under way, refusing the next model
  work - a load whose fit check was asked as the word came included - the
  pipeline saying so, and clearing - the queue's own, degraded, still letting
  a wanted card go; a new rental not the degraded one; and a load refused for
  the memory carrying its reason.
  An engine per model (S8): each model's engine and
  its settings - a setting its engine never reads refused, a default reaching
  only the models that read it, a CPU model refused - `real` and `llamacpp`;
  each engine's program, process names and way of answering, and cardwatch
  seeing them; whisper's proof on the request path and a startup check
  holding each orphan to its own engine's; a real load's line naming its
  engine and file, and the card source hearing each model's own engine; the
  stand-in's transcription in one piece, its deadline declared before it
  (`answer_deadline_s`), in the whisper stand-in server's shape, silence an
  answer, a cancel heard; and
  the give-up rule, in each engine's shape.
- `test_llamacpp.py` - 159 rows, against `fake_llama_server.py`: real
  processes, no model, and never the real card: its tripwire
  (`_suites.forbid_the_card`) is set as it starts, and the one row about the
  real reader's parsing runs the reader kept before it, with `subprocess`
  stood in for (H7). **It starts nothing but its own stand-in and kills
  nothing it did not start** - both guarded before anything runs, and the
  second by the load id on a process's command line, never by its pid alone:
  Windows hands pids out again all the time (measured: 91 of 300 processes
  in one run were given a pid an earlier one had had). For the same reason
  it counts launches as they are made, never as pids it had not seen. Among
  them: a runner pointed at the wrong port is refused 401 and never answered;
  a cancel lands before the first token; a server killed mid-answer, or one
  that closes early without a finish, is a failure; what a stopped server
  left running is found from the record and reclaimed only on proof, with a
  real kill; and a real parent holding a real backend MID-LOAD is terminated
  and its llama-server dies with it - and, the control, without the job it
  does not. The text request is read off what the stand-in RECEIVED: a
  conversation in its order, every parameter a job named as it named it,
  nothing llama-server does not read - and the reading of its source held to
  the contract. The count: the stand-in's `/apply-template`, `/tokenize` and
  `/props` in b10724's shapes and defaults, its key and its context; the
  backend's count equal to the stand-in's own `usage.prompt_tokens` for the
  same request, rendered from exactly the body the answer is asked with, the
  model's thinking default included; the context the model's word, not its
  settings'; a count refused in llama-server's words, and a cancelled one
  never made; `max_tokens` -1 and 0 read as llama-server's README reads
  them; and before any route, b10724's checks in b10724's order - the
  loading state, then the key, `/v1/models` keyed too, and saying its
  slot's context as `/props` does.
  The memory's lines (PLAN-4 D3): the lines looked for are b10724's
  own, and no others; a load's log is read from its own launch - an earlier
  load's line is not its own - whole lines only, each once; the stand-in
  runs out of memory mid-answer as ggml does (`--fake-oom-after`); and the
  manager fails that answer as the card degraded, saying the line, counted
  on the work and on the record.
- `test_whisper.py` - 81 rows, against `fake_whisper_server.py` and model
  files of a few bytes: the stand-in behaving as whisper-server 1.9.1 was
  measured - an unknown argument exiting 0, nothing listening while the model
  loads, every route under the load's path and a stranger's path 404, health
  answered beside a transcription, a death mid-answer that answers nothing,
  a hang that holds the answer while health goes on - and a whisper model's
  header, and the fields whisper-server reads. And the whisper engine's
  process (S8), started through the runner's one spawn, which the suite
  opens to its stand-ins alone: its command line and the load's own path on
  it, ready when health answers there and proven ours by it, any exit before
  that a failed start (a clean one included) with its log's last lines, a
  stranger on its port refused by its 404, a give-up within the rule, a
  stop, and a real runner running a llama.cpp model and a whisper model side
  by side on the machine's lock (the suite's own lock file). **Every process
  it starts carries the run's token on its command line**: a row ends what
  it started through the handle it started it with, and the last row reads
  every command line on the machine for the token.
- `test_adapter.py` - 110 rows: the runner behind the queue, run end to end
  over real HTTP - on the stand-in model, and on llama.cpp against the
  stand-in llama-server - with health checked against the contract, a
  stopped server's orphan reported at the next bind and reclaimed only with
  `--reclaim`, a model in use never taken by the idle sweep, and the
  request record's `call.json` compared with the body the stand-in
  llama-server actually received - two generations on one model at once
  each keeping the call it made; a conversation and every text parameter,
  submitted over HTTP, read off what the stand-in received, and a misspelt
  parameter that never leaves the bootstrap. The count: a request that fits
  its model's context to the token asked, one token more failed
  `prompt_too_long` before the model is asked - with no limit and with 0 as
  well as with a length - its count on its progress and in its record either
  way; the model's own refusal ending the same way; a count that fails,
  is cancelled or is cut short by the card taken back; and end to end over
  HTTP, against a stand-in whose slot's context is not its settings'. The
  watch (PLAN-4 D3): a generation's tokens and its end moving the progress;
  a job refused for the card's memory ending `no_room` or `never_fits`; one
  on a degraded card, one whose load was given up for it, and one whose
  answer was under way ending `card_degraded` - never cancelled, never left
  to crawl.
  One call, two kinds: a transcription's deadline on its progress before its
  answer, its answer whole with whisper-server's own fields, silence a job
  done end to end, a request of the wrong kind for its model's engine
  refused before any load, and an audio request held to the contract and to
  the fields whisper-server reads; `--backend real` on the command line, and
  `--backend llamacpp` there refusing settings that name a whisper model.
- `test_coordination.py` - 134 rows, against a REAL coordinator served in the
  suite's own process: the client half (every answer checked against the
  contract; silence never a loss; heartbeats bounded while the coordinator is
  away), the card and port sources in the runner, and the server end to end -
  its own port from the registry, health's `card` in all three states, two
  servers on one coordinator (the second waits, told only that it waits, and
  loads when the first lets go), and **two holders and a kill**: the first a
  real server process killed while it holds the card, the second proceeding
  once the coordinator takes the card back by liveness. The ticket rows: one
  ask and a wait per hold, never a poll (counted); a cancel, a stop and a
  patience that runs out each give the ticket up; a job's class and its own
  patience, `card_patience` and `card_busy` at once, naming no one; one
  ticket per server; and a token that never leaves the server's memory. The
  named processes: the whole list on every heartbeat, one sent at once after
  a spawn and after a stop, a lost beat healed by the next; every process of
  models side by side named, and one whose stop failed still named while the
  others come off; each load's port held under its own name; a process whose
  stop failed still named, and nothing loaded beside it; a hung holder's
  llama-server ended on proof, one that does not carry it left alone; and the
  probe asks `/up`, never the operator's `/state`. The
  pipeline rows: one rental across a switch; a session with nothing loaded
  keeping the card while another application asks; the queue's own letting
  the card go at once when it is wanted, and keeping it between its own jobs
  - a job taken and not yet generating included; a lost pipeline asking again
  by itself; a pipeline's class on its ticket; a lapse giving the rental back.
  The memory rows: a ticket declaring the runner's estimate; each load an
  amend on a fresh read, refused in the coordinator's words and numbers while
  the session keeps the card; a switch leaving the rental naming what is on
  the card; the amend naming the rental's list, so a process just gone is
  waited for; responsive work refused at once and scheduled work waiting for
  the room, one ask each; an amend nobody answers deciding nothing; and a
  server finding its coordinator through the machine's lock - and, the
  coordinator gone, taking the lock itself - built so by `build_backend`
  with nothing to configure. The watch rows (PLAN-4 D3): every heartbeat
  saying the work, counted from the rental's grant; a rental whose answer
  never moves taken back as for a crash, its model's server handed over to
  be ended on proof, the server hearing the loss; a session that is not
  working never hung; a card over-full degrading the rental - its work
  failing `card_degraded`, nothing new loaded, the rental held - and room
  again clearing it; a memory error in a stand-in llama-server's log doing
  the same; the coordinator's refusal of an amend, before a heartbeat said
  so, refusing the load; the work and the degraded card reaching a
  coordinator found through the lock; and a job refused for the memory
  ending `no_room` or `never_fits`.

**Every rule was broken on purpose.** The queue's first 51 mutants: 49 red,
and the two that survived defended two live servers on one folder - a state
the folder lock now refuses at the door. Then 31 more over the `ended` hook,
the folder lock, scope, the claim's `ended` and the server itself: **31 red**,
each in its own copy of the folder, both suites run, with unmutated controls
in the same pool (all green) and a mutant that cannot even import as the
sweep's own check - it must come back NO VERDICT, never a survivor, and did.

Two of the 31 first came back NO VERDICT: removing the `busy` check made a
request hang, and the suite died before its tally. The suite now records a
section that dies as a failure and a request with no answer as a failed
check, so it always prints its tally.

**A later sweep's CONTROL found a race no row had:** a carried row failed once
in 268 runs because its `forget` landed while the worker was inside the
`ended` hook - the forget found the debt unpaid and refused it. Three rows now
hold the worker inside the hook on purpose (the forget waits and succeeds; a
hook that never returns is waited for a bounded time and the answer is the
true one; prune waits too), a fourth has the hook forget its own job (refused
at once - it never waits for itself), and the five mutants of the fix are red.

**The runner: 95 of 95 red**, one mutant per rule above - the lease's order,
liveness, load identity, the record's pairing, the rebind's hand-over, every
part of "ours", the load timing, the settings' refusals, the stand-in - with
three unmutated controls running every part's suite, green and agreeing. Two
rows first let their mutant through, and both were rows that COULD NOT FAIL:
two models' replies always differed in their first line, which names the
model; and a malformed pid the operating system answers "gone" either way.
Both now use a value that tells.

**llama.cpp: 49 of 50 red, and the fiftieth is equivalent - measured.** A
wider field in the job's struct lands in padding it already had: every offset
the kernel reads is unchanged. Three rows first let their mutant through. A
failed launch's server was being killed by the JOB, not by the hard stop - so
a launch that fails with no job to fall back on now has its own row. A carried
row about the GGUF read window could never fail: the header it read was
smaller than the smallest window. And my own first cancel waited out the
server's silence on Windows until measured.

**The runner behind the queue: 33 of 33 red** - the adapter, the manager's
hold on a model in use, the server's wiring and the contract's `runner` -
with the controls green across all seven suites. The first run's controls
were all red, and rightly: the contract now requires `runner` in health, and
a queue row that composes the server's answer had not been given it.

**The real card's seven: 33 of 33 red, in two sweeps** - every line the
hardenings added (the queue's one timeout; the stop's order; giving a load up
- in the manager, llama.cpp and the stand-in; the card taken back mid-load;
the reuse that asks the port; the card's total; the stop that waits on the
process itself), each in its own copy, three controls running all eight
suites green and agreeing each time. The second sweep ran llama.cpp's seven
again, against the stand-in corrected to what the real card measured. Writing
the mutants before each run found five lines no row could fail on: two checks
that covered each other (one went, and the process is looked at right after
it starts instead), a refusal only a model already up can show, a probe row
that never looked at what it asked the card, and a process that cannot be
opened at all.

**Closing every row registered against this part: 51 of 51 mutants red**
(with `mutate.py`'s forerunner, the same rules):
- **What was covered:** every rule the closing added.
  - the small ones: llama-server's words, the load lines' request, the record's
    guards, the two hooks' interrupts;
  - a cancel during a load: given up, kept for the next job, asked again, never
    handed out;
  - the machine's lock: excludes, goes back, ends its waits, says who;
  - the process a turn names, and the coordinator's hardware.
- **How it ran:** each mutant in its own copy. Three controls ran every suite,
  green and agreeing. The self-check came back NO VERDICT, and the tree was
  untouched.
- **What the first run found:**
  - **one row that could not fail:** a turn changed from an earlier answer
    than the one it checked;
  - **two waits a broken lock turned into a hung suite:** now bounded;
  - **a guard that let a kill through by a recycled pid.**

  All four were run again, with the new guard, through `mutate.py` itself:
  5 of 5 red, the controls agreeing.

**The loads made one table: 22 of 22 mutants red**, one per rule of the
table's bookkeeping:
- **an entry leaves:** when its load ends, even when the stop throws, and
  when it never came up;
- **one entry:** a second is refused by the table itself, and acquire's own
  guard refuses first, in its own words;
- **each read as what it is:** the load on the card, the load under way, and
  what status reads where nothing is up;
- **the counts and clocks:** the live count, the loads this session, the idle
  clock outliving the load it last timed, one in-use count for every model;
- **the rest:** the card's wait on the table, the rebind carrying its load,
  the entry keyed by its id, and the three names older rows read.

One was red on the one-run row alone: a load that ends resetting the in-use
count. In both runs the self-check came back NO VERDICT and the tree was
untouched.

**The first run's controls were not all green.** Two of three were red on one
row, the one that times the stand-in's 50 ms load
(`test_the_load_time_is_answered_through_status`: 0.04 s). It is red on the
tree before the table too: 10 of 150 fresh runs there, 12 of 150 here, side by
side. The cause is the first timed wait of a fresh process, which can end
early on this machine: under 45 ms in 55 of 200 processes, and the second wait
never. No kill rests on that row. The second run's three controls were green,
and every verdict came back the same.

**The whisper stand-in, its header and its fields: 75 of 75 red**, in two
sweeps, each with three controls running every suite, green and agreeing,
the self-check NO VERDICT and the tree untouched. One mutant first came
through: a stand-in that answered a POST outside its path without reading the
body. Measured on this machine, a 64 KB body still came back as a whole 404
nine times in ten; an 8 MB one never did. The row now sends 8 MB.

**The pipeline and the session (PLAN-4 S6): 56 of 56 red**, with three
controls running every suite, green and agreeing, the self-check NO VERDICT
and the tree untouched - the coordinator's `wanted`, the server hearing it and
the queue's own pipeline acting on it, a request's terms by hand, the
pipeline's open, renew, lapse, close and loss, the card held across a switch
and with nothing loaded, the settings' session, and wave 1's three registers.
Writing the mutants took out two lines no row could tell apart: a refusal's
reason written on the terms by both the card source and the runner (the
runner's alone is left), and the idle sweep asking the queue's pipeline
whether its work was done, which every end of work already asks. **The
controls of three earlier runs found three things no row had:** a queue row
that composes health had not been given `pipeline`; a wait that polled
health through the contract's probe, so the count of checks moved with the
machine's speed; and a race - the queue's own pipeline found its work done, a
job's generation took the warm model in that same moment, and the close took
it off under the job, which failed. The close now looks again under the
manager's lock and gives nothing up - two rows, each seen red before the fix -
and a job the worker has taken, its request still being built, is work owed
as a queued one is: its row is red on the code before it (the sweep's M16).
The waits now ask around the probe. **Then 4 of 4 more**, written once the
verdicts were read row by row, for the rows no targeted mutant had turned
red: the reversed rule put back (a model coming off gives the card back),
both lines that give a pipeline's own load up set aside at once, and the
pipeline's open and close taken out of the record - three controls green,
the self-check NO VERDICT, the tree untouched.

**Every text parameter and a conversation: 28 of 28 red**, in two sweeps -
the contract's closed `gen`, its conversation and `judge_refused`; the
queue's keep-as-named, its refusals and the sampling hook's; the body, the
order of a conversation and the copy a call keeps; a model's thinking setting
as a default; both stand-ins answering the whole conversation; the adapter's
checks and defaults; and the reading of llama-server's source against the
contract. One came through first: a stand-in that added the role to every
prompt passed every row, because no row read what a prompt was answered
from. The row now does. The first sweep's third control was red on the one
row that times the stand-in's 50 ms load (0.04 s, above); the second sweep's
three controls were green, the self-check NO VERDICT both times, and the tree
untouched.

**The token count and wave 2's register R1-R5: 45 of 45 red**, in two
sweeps - the contract's `prompt_too_long` and `progress.count`; the queue's
empty answer to 0, its budget for -1, for n and for an unset length, and the
sampling hook's own three; GenParams' length and its fields; the room's
floor; the adapter's check - the room counted, no off-by-one, its reason,
its progress and its record, before the model is asked - the model's own
refusal, a count that fails, is cancelled or is cut short by the card taken
back, a model that cannot count and the words for no limit; the count's
body, its first token, its special tokens, its key, its cancel and its
context the model's word; and the stand-in's template, tokenizer, context,
routes, key and `max_tokens`. The first sweep ran across the machine's sleep:
two mutants were cut off by their bound with their rows already red (each
suite's own tally: 30,595 s), and ran again at once, red in 152 s each; in
the one that spanned the sleep, a reclaim row unrelated to its mutant was red
too - the orphan found, its command line not read as ours - and was green
when it ran again and in every control. Controls green in both sweeps, the
self-check NO VERDICT, the tree untouched.

**Models side by side (PLAN-4 S7): 61 of 62 red, then 19 of 19 on the
final tree**, with three controls running every suite, the self-check NO
VERDICT and the tree untouched - the table's one load of each model and the
guard before a port or a backend, the load lock, a load that never holds the
manager's lock, stops that run together, making room (the limit, a configured
port, the fit check, the model used longest ago, never a pinned one, never
one answering a request, a stop under way waited out as room being made),
each model's own idle clock, `load` and `open` answered the moment their
models are up or failed, `unload` and its refusals, the record's `beside` and
its checks, a port per load under its own name, the call kept per
generation, the load history under one lock through names of its own, a
rebind, a loss and a close taking every load, health's `holding`, a close
asking a failed stop again, the settings, the runner's own port range, and
the server's `?wait_s`. **A first sweep was stopped on purpose** after nine
mutants, all red: reading the close again found a load that had looked
before a close began going up after it, on a card given back, and a stop
that failed in a close asked twice in it. A load now takes its port outside
the manager's lock and looks again as it begins; a close waits out every
load under way, asks the old failed stops first, and is never cut short by
the server stopping. **The second was cut off by a session limit** after two,
both red; the next ran the other sixty on a tree byte for byte the same, and
one came through as its author had worked out it would: a close waiting out
a load that cannot be given up had no row yet. It has one, and is red in the
last list. **Reading the code again then found more**, each fixed with its
row seen red first: making room counted a stop under way as a model on the
card - a load that did not fit took a second model off, or was refused "no
room", while another caller's stop was freeing that room - and a load went
up beside a model whose stop failed while it made room, or as it began; a
rebind left behind a load whose stop had failed, though it may still be on
the card; a session one configured port could never open was said only in
the start's log; and two lines of a rebind row could never fail - a record
read for orphans before a later `session_start` has none. **The last list's
first run had red controls, and rightly:** the rebind's first fix also
carried such a load away from a folder being let go - the last thing a server
does as it stops - and an adapter row's folder lost the load its next start
must find. The rebind carries it to a new folder only, a row says so, and the
list ran again with a mutant for it. One control of that first run was red
on a coordination row too, which waits 20 s for a reclaim that first asks
the operating system for its processes' command lines: on a loaded machine
the question took longer (green in the other control, and in every run
since). It is not this slice's code, and is left as it is; the slice's
report names it, with a row of the runner's suite that failed the same way -
a process's question to the operating system unanswered in its bound - once
in a gate and once in the last list's run of S09, whose rows went red too.

**Wave 3's small registers (H5): 44 of 44 red**, in two sweeps, every row
seen red on the old code first - one mutant per rule the slice added or
moved: `100 Continue` said only once the checks let a request through, and a
refused one answered without its body read; the coordinator's port had
before its record; "cannot listen" for the port alone, and the port let go by
a start that stops after it; the contract suite's broken status a red check;
a holder's patience on every way back to asking or claiming; a lock with no
note yet given a moment - bounded, and ended by a stop or a cancel - and one
with a note looked at again at once; the amend's wait held together (the
contract's two numbers, a settle refused above its most, the server waiting
the contract's bound); the stand-in model's -1 and 0; a length llama-server
refuses refused at the submit, by its own reason and in the contract's words,
a hook's held to it; a recovered empty answer to `max_tokens` 0 adopted, and
to any other length not; the llama.cpp stand-in's loading state before its
key, its `/v1/models` keyed and saying its context. **The first sweep's key
mutant was killed by the wrong rows:** making `/v1/models` public sent it to
`/health`'s answer, so every load's identity check failed first; a second
broke the key rule alone, and its own row went red. **Found before any row
ran:** two rows of the coordinator suite, written before the command line
took the machine's lock first, started `coordinator.py` with no
`--card-lock`, and took the machine's own; each now takes one of its own.

**An engine per model (PLAN-4 S8): 76 of 76 red, in five sweeps**, with three
controls running every suite, the self-check NO VERDICT and the tree
untouched - a model's engine and the defaults', the closed list, `llamacpp`
refusing a whisper model, a setting its engine never reads, a default only
for the models that read it, every CPU switch, `whisper_server`, what runs a
model under the stand-in; whisper's proof - its exact path, its load id, its
own and no other engine's - an unknown engine, process names in any case,
each engine's names; a real load's line (its engine, its file), the startup
check by each line's engine, the machine's lock for every real backend, each
model's own engine to the card source's fit and its named processes, the
factory's engine per model, the stand-in's shape per model; the deadline an
answer in one piece declares, and none for a stream; the give-up rule and
its fit in a close, the language table, silence, a cancel, the stand-in's way
of answering and its give-up; whisper-server's command line, its readiness at its own path, a
stranger's 404, an exit before ready (0 included) and its log's last lines,
its give-up, a missing file, its stop; the adapter's deadline on the
progress, the engine's fields beside the text, each engine's kind of
request, whisper-server's own fields and `response_format`, the audio
request's shape; the queue's stall watch, a new submission, the deadline's
end, silence an answer and an empty text still nothing; `--backend real`
and `--name` with it, a real server through the lock, a ticket's need and
engine per model, both ways a coordinator is found; cardwatch; the models
list's engine and a whisper file never read as a GGUF, with its own words;
the Models head; the contract's audio request, transcript and engine; and
llama.cpp's give-up held to the rule. **The first sweep found one gap**: an
engine the defaults name reached the model through the defaults, while its
settings were held to llama.cpp's - no row could tell (S8M01). A row holds a
whisper-by-default model naming `ctx` to whisper's settings now, and the
second sweep killed it. **Reading the code again found a hole**: the
command line's backend is what runs, and `--backend llamacpp` over settings
that name `real` would have run their whisper model anyway - the settings'
own check had passed it under `real`. The start refuses it now, naming
`--backend real`, with its row seen red first, and its mutant (S8M71) was
red in two runs - each with one or two controls red in suites it does not
run: the coordinator suite's 10 s for a coordinator process to say it
listens, on a loaded machine, and the coordination suite's amend row, which
reads a heartbeat race (both registered); its own suite was green in all six
controls. **Then S5 landed its seam** for an answer in one piece -
`Backend.answer_deadline_s`, asked before each answer - and this slice took
it for its own: the deadline the stand-in declares, none for a stream, said
on the job's progress before the wait and not after it, and a cancel heard,
were pinned again in a fifth sweep (5 of 5 red, its controls green). Three
mutants of the first sweep (S8M26, S8M27, S8M29) broke the seam it replaced,
and are counted as they fell.

**The request check (PLAN-4 S15): 73 of 73 red, in one sweep**, its three
controls green and its self-check no verdict: the held state and the worker
moving on, what the application named and the sampling hook chose, the send
- as built, tuned only when it differs, the changes named, the queue's own
three never, every limit a submit is held to, the backend's own check - the
record keeping both, cancel and give-up of a held job, its fan and its
retry, a cancel the check did not hear; the runner's view - messages,
provenance, the count and whether it fits, the model never asked, the
expected time, a transcription's audio; the server's wiring and the card not
kept for a held job; the widget and its seam; the contract's two lists held
together. Four rows were written for mutants that could not be named a row
before the sweep (a cancel the backend raises, the model kept on a send, two
answers not enough, a send with nothing changed over HTTP), and two guards no
row could reach were found redundant and removed.

## Registered against this part

Nothing. A row filed against it blocks the next ship until it is closed.

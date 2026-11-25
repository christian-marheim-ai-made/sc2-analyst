# llm-bootstrap-coordinator

One owner of the card, many applications taking tickets for it - and the
ports they run on.

    python coordinator.py --data <folder> [--port 7071] [--ttl 15] [--claim 30]
                          [--drop 120] [--hold 30] [--settle 10] [--margin 256]
                          [--amend-settle 5] [--recheck 5] [--hang 300] [--watch 30]
                          [--floor 256] [--full 1024] [--recover-watch 2]
                          [--card-lock <path>]
                          [--ports 31000-31999] [--hardware this-machine|none]
                          [--allow-origin <page address> ...] [--max-body <bytes>]
                          [--read-timeout <seconds>]
    python test_coordinator.py

Open `http://127.0.0.1:7071/` for the dashboard, the operator's page:
- whether the coordinator holds the machine's card lock, or waits for it -
  and who holds it then;
- who holds the card, for what, and whether the rental is held, being given
  back or being taken back - and whether it is degraded, since when and why;
- every process their rental put on the card: its pid, model, engine,
  memory, port, proof and since when;
- the rental's work: whether it is working, its progress, when that last
  moved, and when the card was last read for it;
- how much of the card's memory is in use, as the machine sees it, and how
  much of it is outside the bootstrap;
- the tickets in line, each with its class and since when;
- the limits in force;
- which ports are held, and by whom;
- the record.

## What it is

The card is handed out **by ticket**, to one application at a time. The
coordinator grants the card; it never loads a model. Each holder loads its
own while it holds the card.

1. **Ask.** `POST /card/ask` with the application's actor and name, the
   models it wants to run (`[{key, engine, need_mib}]`), its class and,
   optionally, a label and the request it serves. The answer is a ticket and
   its secret token - in that answer, and in no other.
2. **Wait, without asking again.** `POST /card/wait` is held open - at most
   30 s - and answered the moment there is news: the ticket was offered the
   card, or it can no longer be (refused, with the reason). When the hold
   ends with no news, the answer is: still waiting. A waiting application
   makes one request per hold, never one a second.
3. **Claim.** Offered the card, the asker claims it with the token within the
   claim window (30 s). The claim is the **rental**. Claiming is the proof
   that the asker is alive and ready the moment it gets the card.
4. **Hold.** A rental is kept by `POST /card/heartbeat` with the token, before
   it expires (15 s), and ended by `POST /card/release`. Each renewal says
   whether the card is `wanted` - another ticket is in line for it - and
   nothing more: never whose, for what, or how many. A server's own pipeline
   for its queue's work lets the card go at once when its work is done and the
   card is wanted; an application's pipeline keeps it until it is closed.
5. **Give up.** A ticket its asker stops waiting on - patience out, work
   cancelled, server stopping - is cancelled: `POST /card/cancel`.

**The order.** Waiting tickets are offered the card responsive first, then
scheduled; first come, first served within a class; one offer at a time.
Nothing is taken from a holder: a responsive ticket waits for the rental to
end.

**Offers and drops.**
- An offer nobody claims within the claim window passes to the next ticket.
  The ticket ends `unclaimed`.
- A waiting ticket nobody waits on for 120 s is dropped: its asker has gone.
  A ticket its asker is waiting on is never dropped, however long.

**Every limit is a setting,** with the starting default above:
- `--ttl`: a rental's lifetime between heartbeats (how soon a crash is seen);
- `--claim`: the claim window;
- `--drop`: how long a waiting ticket nobody waits on is kept;
- `--hold`: the longest a wait is held open;
- `--settle`: how long the end of a rental - its release, or its reclaim -
  waits for its processes to go and the card's memory to come back, before
  it ends what is left on proof;
- `--margin`: the free memory, in MiB, kept beside what a ticket or an amend
  needs - 256, the floor under which a rental is degraded, so a grant never
  lands a rental there;
- `--amend-settle`: how long an amend right after one of the rental's own
  processes went waits for its memory to come back before it refuses - at
  most 15 s, the contract's `memory.AMEND_SETTLE_MAX_S`: a server waits the
  contract's 30 s for an amend's answer (`AMEND_ANSWER_S`), and one it never
  hears decides nothing, so a longer settle would have had its amends
  approved unverified (S4's register R9). The card's last reading after it
  (10 s at most) fits in the rest;
- `--recheck`: how often the card is read again for scheduled work waiting
  for room on a free card;
- `--hang`: how long a rental that says it is working may go without its
  progress moving before it is taken back (300 s, D0's 5 min);
- `--watch`: how often the card is read during a rental (30 s);
- `--floor`: the free memory, in MiB, under which a rental is degraded (256);
- `--full`: the free memory, in MiB, under which the card counts as full -
  an answer far slower than its model's normal then marks a rental degraded
  too (1024);
- `--recover-watch`: how often the card is read while a rental is degraded,
  so room again is noticed within seconds of the person closing something
  (2 s);
- `--card-lock`: the machine's card lock it takes - the machine's own, in the
  user's temporary folder, unless given (below). Its start line says which
  (S16a R4): the machine's, where every server on the machine finds it; or
  its own, where a server finds it only when its settings name that lock
  (`[runner] card_lock`) or it is started with `--coordinator`.
- A connection its asker resets mid-request - a server under it ended while
  it asked - is said in one line of its log, never a traceback (R-B5).

## The memory, verified

The coordinator reads the card itself - afresh, never an answer it kept -
wherever the card is decided, and judges by the contract's ONE memory check
(`memory.py`): what is needed, and the margin kept free beside it, against
the card's free memory. A server run on its own judges by the same function;
the parts share no code, and the contract is the one part both import.
- **An ask that could never fit** - more than the whole card, with the
  margin; what its models need together - is refused at once, whatever its
  class: `409 never_fits`, with the numbers, and no ticket is made.
- **The claim is the grant, and the card is read first.** Not fitting now:
  responsive work is refused at once (`409 no_room`) - the person waiting can
  act - and its ticket is over; scheduled work waits for the room, within its
  asker's patience: answered `not_offered`, it stays in line, passed over for
  memory while the card goes to the next ticket whose needs fit. It is
  offered again - first among those passed over, in the order of the line -
  when a rental ends, or when a fresh read, every `--recheck` while the card
  is free, says it fits. A ticket that could never fit is refused at its
  claim whatever its class. Asked again, a refused ticket answers `refused`.
- **Before each model a rental adds, the server asks (`/card/amend`),** naming
  the model, its need, and the rental's whole list of processes. Approved,
  the rental's `models` follow what is on the card - the models its
  processes run, and this one - so a switch no longer leaves the rental
  naming its first model. Refused: `no_room` or `never_fits`, in numbers.
- **No room now is said LOUDLY** (H9; the owner, 2026-09-28: "Fail loudly.
  Memory almost never ever cleans itself up"): a `no_room` refusal - at a
  responsive claim, and at an amend - says, in the contract's words
  (`memory.no_room_words`), what the card has free, what is needed, what
  programs outside the bootstrap hold (the card's use less the rental's
  own; at a claim no rental holds it), and what to do: close programs that
  use the card, or reboot. The record's `ticket_refused` and
  `amend_refused` say the same.
- **Scheduled work: wait or fail, and not before a time** (S18; the owner's
  second ruling of 2026-09-28). A ticket may say `if_no_room`: `wait`, a
  scheduled ticket's default, is passed over at its claim as above; `fail`
  is refused at once, `no_room`, in the same loud words as responsive work -
  whose tickets are always `fail`. A ticket may name `not_before`, seconds
  since the epoch: until then it is in line and holds back NOBODY - never
  offered, and never counted when a rental is told the card is `wanted` -
  and from its time it is offered the card like any other. The time is
  looked at on the coordinator's clock at every beat (`_due`, from
  `_offer` and the heartbeat's answer), never counted down: a machine that
  slept through it is offered it at the first beat after it wakes - though
  a ticket nobody could wait on while the machine slept is dropped as any
  is (`--drop`), and its asker asks again for the same time, now past. A
  ticket passed over for room keeps what it waits on (`room`: the numbers,
  what programs outside the bootstrap hold - at a claim, the card's whole
  use - the words a person reads while it waits, `memory.room_words`, and
  when the card was read), refreshed at every `--recheck` read; its waits,
  `/state` and the dashboard say it, and `ticket_passed_over` says what
  programs outside hold. Responsive work is now: an ask naming `if_no_room:
  wait`, or a `not_before` still to come, is refused `400 live_is_now`. The
  record's `ticket_asked` carries the time and whether it waits, and a
  restart keeps both.
  **Right after the rental's own unload** its memory is still coming back
  (the Dictaphone's measure: a whisper-server gone in 476 ms, its memory back
  half a second later): an amend that finds no room reads the card again
  until the model fits, or `--amend-settle` has passed since the process
  went. An amend renews nothing.
- **A card that cannot be read decides nothing:** granted, approved, and
  said - a missing instrument must not become an outage.

Every refusal says the numbers - needed, free, margin, the whole card - and
names nobody who holds the rest. The record says each decision with its
numbers: `ticket_refused`, `ticket_passed_over` (once each time), `rental_granted`,
`rental_amended`, `amend_refused`.

**A process's own memory (`mib`)** is what its load took of the card, as the
runner measured it - the free memory before it less the free memory at
ready, then after its first answer - or null until it is measured. The need
a ticket declares is the runner's estimate for its engine until the model
has three measured loads; then the largest of them + 256 MiB (the server's
`runner/needs.py`).

## The machine's card lock

The machine's lock says who holds the card at the top (the contract's
`machine/card-lock.json`). A coordinator started from its command line takes
it as it starts, and then its port - both before it writes a line of its
record, or resumes a reclaim or a release its record left: one started on a
port somebody holds acts on nothing, and says only that it cannot listen
(H3's register R2) - and holds it while it runs; its note beside the lock
gives its address and its actor. So:
- **a server finds it there,** each time it opens a session - nothing needs
  configuring;
- **a machine never runs two:** a second coordinator started while one holds
  the lock does not start, saying where the first is;
- **started while an application runs on its own**, it waits for the lock:
  it answers its routes and takes tickets, offers the card to none until the
  lock is its own, and its page - and `/state`'s `lock` - says whom it waits
  for. Once it has the lock, the next ticket is offered the card on the
  reaper's next beat. An offer its record's rebuild made before it tried the
  lock is taken back.
- **a note is its holder's only while its writer lives:** a holder that dies
  lets go of the lock and leaves its note. One whose `pid` has gone, or was
  handed on to a process started after its `since`, is stale: it names
  nobody - never "another coordinator runs" - and the page says the note is
  stale, why, and what it claimed (H5's register 6). The command line writes
  its note the moment it has its port, before its record is read, and a
  coordinator that finds the lock held with no live note gives its holder a
  moment (`NOTE_WAIT_S`) to write one - so one started in that instant is
  refused as the second coordinator it is (H5's register 3).
- **a start that stops says why:** another coordinator, a port somebody
  holds, or a setting the coordinator refuses (`--ttl 0`) - "cannot start:
  ...", exit 2, never a traceback - and lets the lock go, its note first
  (H5's register 2).

Built in code, as every suite builds it, it takes no lock unless `serve` is
handed one (`card_lock`); every coordinator a suite starts as a process is
given a lock of the suite's own (`--card-lock`), and the suite's own
`machine_lock` is a tripwire - no suite ever takes the machine's.

`/state` shows the ones in force (`limits`), and the version of the contract
the coordinator runs (`contract_version`, as a server's health carries its
own).

**What it keeps of the tickets that ended is bounded:** why each ended, and
its token's hash, for the newest 1000 (`keep_closed` in code). An older
one's token is simply unknown (`unknown_ticket`), in memory and after a
restart alike; its lines stay in the record. **So is what it keeps of the
ports it closed:** whether each was given back or taken back, for the newest
1000 - a port closed again counting from its last close. An older one's
holder is told it holds no such port (`unknown_port`).

**Whether it is up** is asked at `GET /up`, which answers `{"up": true}` and
the version of the contract it runs (`contract_version`, as a server's health
carries its own), and nothing more: an application's server asks it as it starts, and the
operator's `/state` - which names every application - is never an
application's to read.

## The four rules

1. **The card's truth is the card.** The coordinator is a record and a
   decider, never the source of truth about the hardware. On start it rebuilds
   from its record, and the record's first line says what the machine says of
   the card. A rental it finds in its record gets a fresh lifetime - a holder
   is not blamed for heartbeats that failed while the coordinator itself was
   down - and the tickets in line keep their place, each waited for afresh.
2. **A holder never infers loss from silence.** It learns it lost the card
   from an answer - `409 reclaimed` - never from failing to reach the
   coordinator. The card does not know the coordinator went away, and neither
   should the work running on it.
3. **Reclaim is by liveness, not trust.** A holder is alive because it said so
   before its rental expired.
4. **Applications know nothing of each other.** Every answer to an application
   is about its own ticket alone: never who holds the card, what runs on it,
   or who else waits - its refusals included. A ticket is acted on with its
   token alone:
   - the token is in the ask's answer and nowhere else;
   - the coordinator keeps only its sha256, in memory and in its record, so
     neither can give it away;
   - a restart recognises it again, from the record;
   - a token that is not a ticket's own is answered as if the ticket did not
     exist.

   The operator's `/state`, `/events` and dashboard show everything - never a
   token. A holder is its actor and its token, never its name: two
   applications that call themselves the same are two askers with two
   tickets.

## What it answers before any route

Its routes are asked by programs - an application's server, a holder - and
never from a browser; its one page is the dashboard, the operator's, served
from its own address. So a web page open in the same browser must never reach
it. Before any route, on every request whatever its method - the dashboard
and a path no route has included - three things are checked, in this order,
and the first that refuses answers (the contract's `origins/coordinator.json`):

1. **The name it was sent to.** A Host that is not one of its own names -
   `127.0.0.1`, `localhost`, `[::1]`, the address it listens on, and the host
   of each page it was started to allow - is `403 host_not_allowed`, with or
   without an Origin. A website whose name was made to point at this machine
   (DNS rebinding) sends that name as its Host and as its Origin; without
   this check it would pass for the dashboard, and could read `/state`.
2. **The page that sent it.** An Origin that is neither the coordinator's
   own - `http://` and its Host: the dashboard - nor a page it was started to
   allow (`--allow-origin`, repeatable, none by default) is
   `403 origin_not_allowed`. An allowed page is told it may read every answer
   it is given, and its preflight is answered `204`.
3. **Its body.** One sent in chunks - a `Transfer-Encoding`, its length never
   said - is `411 length_required`, whatever the method and whatever length
   it also names: a body is read by its `Content-Length` alone. One over
   `--max-body` (1 MiB unless given) is `413 too_large`. Neither is read at
   all: the answer says `Connection: close`, and the connection is closed a
   moment after it. Its bodies are a ticket's ask, a wait, a claim, a
   heartbeat, a port's request: under a KiB each.

Then a body within the cap that no route reads - a GET's - is read and set
aside before the answer, so the connection is never closed with the body
still coming; and a method no route takes (`HEAD`, `PUT`, `DELETE`, any other)
is answered `501`, as http.server answers one - after the checks, which run
in one place before any method of the handler: before, http.server answered
such a method before any of them. A request must arrive whole - its line, its
headers and its body - within `--read-timeout` seconds (10 unless given) of
its connection being made, or it is never answered and its connection is
closed: before, a body that never finished arriving held a handler thread for
ever. A request cut off part way is said on the console.

**Its port is its alone.** http.server binds `SO_REUSEADDR`, and on Windows a
second program's socket that sets it too could share the port - measured
against the coordinator before: a helper bound beside it, and a coordinator
started on a port a helper already listened on started, said it was
listening, and heard none of 20 requests. It binds `SO_EXCLUSIVEADDRUSE` on
Windows now: started on a port somebody holds, it says it cannot start -
"cannot listen on <address>: ..." (`CannotListen`). A start stopped by
anything else - a record it cannot keep, a lock it cannot open - says what
that was, never that it cannot listen (H3's register R3).

Measured on the code before these checks: a text-body `POST /card/ask` from
`http://example.com` took a ticket, a rebound page read `/state`, a
`Content-Length` of `abc` made the handler fail with no answer at all, and one
of `-1` held it until the asker gave up. Now a `Content-Length` that is not a
whole number of bytes reads as no body.

A refusal is said on the console, naming the page, the name or the length -
and never written to the record: a page must not be able to fill the record
by asking. A request with no Origin, sent to one of its own names, and one
from the dashboard are answered exactly as before: the holder and the
server's client name the host they were sent to, and never an Origin.

## Ports, with the same discipline

The thing that knows which models are up also knows which ports are taken,
so two applications on one machine never pick the same one - for their own
servers or for their model servers.

- `POST /port/request {actor, purpose}` hands out a port from the range: one
  per actor per purpose, and the same actor asking again renews its own. A
  port anything else on the machine is using is never handed out - the
  operating system is asked, by binding it for a moment. A full range
  answers `exhausted`, saying how many ports are held and how many are in
  use by something else.
- A port is kept by `POST /port/heartbeat {port, actor}` before it expires,
  and given back by `POST /port/release {port, actor}`. The actor is named
  because a port number is easy to guess: another actor's port answers
  `not_yours`, naming no one. A holder that goes quiet loses its port, and
  learns it from an answer - `reclaimed`.
- Every grant, release and reclaim is a line of the same record as the
  tickets; a restart carries held ports over with a fresh lifetime.
- **The range defaults to 31000-31999: below every common system's range for
  passing connections** (Windows from 49152, Linux from 32768), where any
  outgoing connection could be given a granted port before its holder had
  bound it. The first default was inside that range; building found it, when
  the operating system handed the coordinator's own port out of the middle of
  it.

## A rental names every process it put on the card

A holder names every process its rental put on the card, on the rental's
heartbeat (`processes`), each with:
- **its pid;**
- **a proof:** the id of the load that started it (`load-...`, the id
  grammar's load actor), which its command line carries and only the holder
  put there - llama-server carries it as `--alias`. Anything else is refused;
- **its model, engine and port, and its memory** (`mib`, null when not
  measured).

The list is the whole list: it REPLACES the one the coordinator has, so a
heartbeat lost on the way is healed by the next, and `[]` says nothing of
the rental is on the card any more (the rental is still held). A heartbeat
without `processes` leaves the list as it was. A rental names at most 8; a
heartbeat naming more, a proof that is not a load's id, or one pid twice is
refused `400 bad_processes`, and renews nothing.

The server's runner names a process the moment it exists, before its weights
are up, and one heartbeat goes at once whenever its list changes. The
coordinator keeps the list on the rental, each process with since when it
has known of it; `/state` and the dashboard show it. The record gets one
line when the list changes (`rental_processes`, the whole list after the
change, and what was added or taken off), never one per heartbeat - a
process's memory, measured anew, writes nothing. A restart carries the list.

## What a rental left on the card is ended - on proof, never under the lock

**When a rental lapses** it goes to `reclaiming`, and its holder is answered
`reclaimed` from that moment. Then, on a thread of its own and **outside the
coordinator's lock**, so every other application is answered meanwhile:
- **every process it named is handed to the machine at once;**
- **each is held open first** (its handle on Windows, a pidfd on Linux), so
  its pid cannot change hands between the reading and the ending;
- **their command lines are read in ONE query**, not one each;
- **each that carries its proof is ended in parallel,** each waited for with
  its own bound, so the whole takes about the slowest one, not the sum;
- **the card's memory is given the settle time to come back** to what it
  read when the rental was granted.

Each outcome is a line of the record:
- `process_ended`: it carried its proof, was ended, and has gone;
- `process_gone`: it was already gone;
- `process_left_alone`: it is alive without its proof - its pid belongs to
  something else now - or its command line could not be read: never ended
  on a guess, and **never** a process merely found on a port;
- `process_not_ended`: it could not be ended, or had not gone within its
  bound.

Only then is the card free (`rental_reclaimed`, naming each outcome) and
offered to the next ticket. A machine that fails while it ends them does not
keep the card from the next ticket: each process is `process_not_ended`,
saying why. A coordinator that stops in the middle of a reclaim ends them
again, on proof, when it comes back (`rental_reclaiming` is in the record).

**A release is checked the same way.** `/card/release` is answered once every
process the rental still names is gone and - where the machine can read the
card - its memory is back to what it read when the rental was granted, give
or take 256 MiB (the desktop's own use moves meanwhile). Where the card
cannot be read, the processes gone suffice. What is still there after the
settle time is ended on its proof, and the record names each. While the
check runs the rental is `releasing`, and the card goes to no one else; the
answer is the rental as it was released. A coordinator with no machine
(`--hardware none`) asks nothing, answers at once, and says in the record
what it could not ask. The release is on the record the moment it is asked
(`rental_releasing`, with what the rental names): a coordinator that stops in
the middle of the check rebuilds the rental as being given back - its holder
told `released` - checks it again, and gives the card to the next ticket
once it is done, never after the rental's lifetime as if it had been held.

This covers what the kernel's kill-on-close cannot:
- **a holder that hangs,** alive and silent, with its models still on the
  card;
- **on POSIX,** a holder that dies.

Without it, the next holder would load beside the hung one's models. Before
the list, a reclaim ended one process, under the lock, for up to about 45 s.

## It watches every rental (PLAN-4 D3)

None of it holds the coordinator's lock: a hang's processes are ended as a
lapse's are, on a thread of their own, and the card is read with the lock
let go.

**A hang: the heartbeat goes on, and the work does not.** A heartbeat says
what the rental's work does:
- `working` - a model of the rental is answering a request now;
- `progress` - the tokens streamed and the answers ended since the rental was
  granted: a count only real work moves;
- `deadline_s` - an answer under way comes in ONE piece (a transcript), and
  is due within this many seconds of the beat.

A rental that says it is working, and whose progress has not moved for the
hang limit (`--hang`, 300 s) - counted from its last move, or from when it
began saying it is working, and never before a deadline it declared - is
taken back as a lapsed one is: a line of the record says why
(`rental_hung`), every process it names is ended on its proof, and its holder
is answered `reclaimed`, in words that say it was a hang - after a restart
too. A heartbeat that does not say `working` says it is not working: a session
with nothing to answer holds the card as long as it likes, and is never hung.
A count or a deadline below zero is `400 bad_work`, and renews nothing.

**The card over-full: DEGRADED, never ended for it.** During a rental the
card is read every `--watch` seconds (30), on the coordinator's clock. Free
memory under the floor (`--floor`, 256 MiB), or a memory error its holder
reports from a model's log (`memory_errors`, a count since the grant that
rose, with the newest line, `memory_error`), marks the rental degraded:
- the holder is told on its next heartbeat: the rental's `degraded` says
  why, in words, and the numbers of the card's last read (`under_floor` or
  `memory_error`, with the line);
- nothing new goes on the card: an amend is refused `409 degraded`, saying
  why, without reading the card for it - an amend whose read was under way
  when the mark came is refused too;
- the rental is NOT ended: the operator closes something, or reboots;
- while it is degraded the card is read every `--recover-watch` seconds (2),
  not every `--watch` - a memory error brings the next read that close too -
  and the numbers the holder is told are the newest read's, with the room it
  waits for (`need_mib`) and what programs outside the bootstrap use
  (`outside_mib`): its server holds its work for the card's memory, and
  tells a person so (S5b);
- it clears when a look at the card begun after the last mark finds room for
  what the rental needs - its models' need (the ticket's, and each amend's),
  less what its processes hold on the card now, plus the floor - never
  merely over the floor: a card with 300 MiB free does not clear a rental
  whose 6 GB model came off it (`rental_restored`, with the free memory and
  the room it needed). A card that cannot be read marks nothing and clears
  nothing.

One line of the record when it is marked (`rental_degraded`, with what the
holder was told) and one when it clears - never a line per look. A restart
keeps a degraded rental degraded. A memory-error line keeps the count its
holder had said (`memory_errors`), and a restart takes that count - 0 for a
rental with no such line since its grant - as what was said already: an
error said first on the heartbeat after a restart (the coordinator was down
between the error and its heartbeat) is news, and marked (H8, S5b R3). On a
record with no count - an older coordinator's line - the count its holder
says first is taken as known, not as news.

**How much of the card is outside the bootstrap** is on `/state`'s `card`
and the page: `ours_mib` - what the rental's processes use, each one's
measured `mib` or else the need its model was approved with (an estimate) -
and `outside_mib`, the card's use less that. `/state`'s `watch` is the
rental's work as the heartbeats said it, when the card was last read for
it, and how many of its answers were far slower than their model's normal
(`slow_answers`) - the page shows that count too.

**A slow answer on a full card** (D3, D12): the holder's runner judges each
answer against its model's own normal and counts the far slower ones on
every heartbeat (`slow_answers`, since the grant, with the newest in words,
`slow_answer`; below zero is `400 bad_work`). A count that rose while the
card, as last read, had less free than it counts as full (`--full`, 1024
MiB) marks the rental degraded (`slow_answer`, with the words); on a card
with room - or one not read since the grant or a restart - a slow answer is
not the card's doing, and is only counted - on `/state`'s `watch` and the
page (S11b R2). It clears as every mark does.
Why a GiB: a model's buffers grow after its first answer by 350-550 MiB (the
Dictaphone's measure), so a card with less free than that beside it is one
where the next answer may not fit, and the driver spills to the machine's
memory. "Under the floor" would never add anything: under the floor the
rental is degraded already.

## Using it from an application

```python
from holder import Holder

with Holder("http://127.0.0.1:7071", "writer-app", "demo-7b",
            for_="c-20260923-140012-a3k9m2p7") as rental:
    ...load the model, run the work...
```

`Holder` mints its own actor id (or takes one as `actor=`), asks for a
ticket once, waits on it, and claims the card the moment it is offered. It
heartbeats in the background and releases on every way out of the `with`
block.
- **`klass`**: `responsive` (the default) or `scheduled`.
- **`patience_s`**: how long it waits. None is for as long as it takes; when
  it runs out, `Refused` with the reason `card_patience`. A patience of 0 is
  now or not at all: a ticket not offered the card at once is given up,
  `card_busy`. Either way the ticket is cancelled, and nothing names who has
  the card. Past its patience it asks nothing more: a claim or a wait
  answered for a ticket that ended, or a claim the coordinator never
  answered, sends it back to asking or claiming only within its patience -
  before, a holder whose offers kept ending asked for ever, and one whose
  wait was answered as its patience ran out asked once more (S4's register
  R1). Refused for the memory - `never_fits` at its ask, `no_room` or
  `never_fits` at its claim - it is `Refused` at once, with that reason and
  the numbers in its words, and asks no more.
- **`for_`**: the correlation id of the request the rental serves. The rental
  and the record carry it, so the dashboard can say which request holds the
  card.
- **`work`**: the application's own function -> `{working, progress,
  deadline_s, memory_errors, memory_error}` (a key it leaves out is
  nothing: not working, no progress, no deadline, no errors), so the
  coordinator watches it for a hang as it watches a server's rental. Every
  heartbeat carries it, its counts counted from the grant (`holder.said()`
  is what it sends). Given none, the holder says nothing of its work, and is
  never found hung - before, no holder could say it (S5's register R5).

If the coordinator is unreachable at release time, the rental simply expires
and the card comes back on its own. Every process the application puts on
the card, `holder.running(pid, proof, model=..., engine=..., port=...,
mib=...)` names (above) - the model and engine are by default the first it
asked for - and `holder.stopped(pid)` takes off once it has gone. Each sends
a heartbeat at once, carrying the whole list. `holder.asks` and
`holder.waits` count what it asked, as it asked it.

## The record

`events.jsonl` in the data folder, one JSON object per line - each ended by LF
alone, written as bytes (a record an older coordinator wrote with CRLF is
read as well: H5's register 1) - append-only,
sequence numbers unbroken across restarts. A ticket is one line when it is
asked - with its token's sha256, never the token - and one line for each
thing that happens to it: offered, claimed (`rental_granted`), given up,
dropped, passed on, released, taken back - found hung (`rental_hung`),
degraded (`rental_degraded`), room again (`rental_restored`). There is never
a line per wait, per heartbeat or per look at the card, so a ticket waiting
an hour does not bury the record. The dashboard reads the
record through `/events` and nothing else.

A rental's processes are a line whenever its list changes
(`rental_processes`); a reclaim is `rental_reclaiming`, a line per process
(`process_ended`, `process_gone`, `process_left_alone`,
`process_not_ended`), then `rental_reclaimed`; a release is
`rental_releasing`, a straggler named the same way, then `rental_released`.

The memory's decisions are lines with their numbers (`memory`): a ticket
refused - at its ask, or at its claim (`ticket_refused`) - one passed over
for memory (`ticket_passed_over`), a grant (`rental_granted`), and each
amend, approved (`rental_amended`, the rental's models after it) or refused
(`amend_refused`). The machine's lock: waited for (`lock_waiting`, a line
when whoever holds it changes, never one a look) and taken (`lock_taken`).

A record written before tickets is read too. Its turn ids share the ticket
grammar, so the numbers run on and no old id is handed out again. A turn it
says is held is not carried over: no token can prove its holder.

**`/events` serves only lines in the event shape.** A line an older
coordinator wrote in a shape since replaced - a turn (`turn_*`), a rental's
one process (`rental_process`) - stays in the file, is read by the rebuild,
and is left out of `/events`; the start line says how many there are, and
of which kinds. They are not translated: a turn has no ticket and no token
to put in the new shape, and a translation would show the dashboard lines
nobody wrote. A rental's one process, from a record written before the
list, is carried into the list on a restart - its model and engine the
rental's, its port and memory unknown.

## A platform note that shaped the holder

On Windows, a connection to a port nobody is listening on takes about two
seconds to be refused (measured: 2.04 - 2.07 s). Unbounded, every heartbeat
during a coordinator outage would block that long, and a holder with a short
lifetime could lose its rental after the coordinator came back. So every
heartbeat's timeout is its interval: an outage costs one interval per attempt,
and the cadence holds. `test_coordinator.py` measures it.

## What it asks of the machine

`Coordinator(hardware=...)` takes an object with up to three calls:
- **`probe(fresh=False)`:** the card as the machine sees it,
  `{known, used_mib, total_mib}`; `fresh` asks now rather than keeping the
  last answer;
- **`alive(processes)`:** which of a rental's processes are still alive, one
  answer each, in order - a pid it cannot ask about reads as alive;
- **`end(processes)`:** end them, each on its own proof, all at once:
  `[{outcome, detail}]`, one each, in order.

A machine without `alive` and `end` is asked neither: nothing is ended, and
the record says so.

`machine.py` has two of them:

- **`ThisMachine`,** what a coordinator started from its command line uses
  (`--hardware this-machine`, the default).
  - **The probe** asks nvidia-smi how much of the card is in use. It keeps the
    answer about two seconds, since the dashboard asks every second - unless
    a fresh one is asked for: when a rental is claimed (what the card holds
    before it, and what it is granted on), before each amend, while a
    scheduled ticket waits for room, and while a rental's end waits for the
    memory to come back.
  - **An answer that cannot be read is `known: false`,** never an empty card
    - and it decides nothing.
  - **What it is used for:** the memory, verified (above) - an ask's whole
    card, a grant, an amend; the record at start says what the card holds,
    beside what the record says; `/state` carries it, and the dashboard shows
    it; and the end of a rental waits for it to come back.
  - **It never takes a rental back.** Rentals are taken back by liveness.
  - **`alive` and `end`** hold each process open, read the command lines in
    one query, and end what carries its proof in parallel, as above.
- **`NoHardware`,** what a coordinator built in code starts with. It asks
  nothing and ends nothing: the record is updated, and the card is not
  touched. So no suite asks the card unless it hands in a machine that does.

`test_coordinator.py` never asks the card:
- **nvidia-smi** is replaced by a tripwire before anything runs;
- **every coordinator it starts as a process** gets `--hardware none`;
- **the only processes it ends on proof** are children it started itself.
  One carries the proof; one does not, and must be left alone.

It provokes every answer the coordinator's routes declare, checks each against
the contract, and runs the dashboard in node against the contract's own
examples. What it answers before any route is asked as a browser asks,
headers and all - a page from elsewhere, a rebound name, a body over the cap
or in chunks, a method no route takes, an allowed page and its preflight, the
dashboard's own address - every answer `origins/coordinator.json` declares is
provoked, and its examples are answered word for word. A request that never
arrives whole is sent as a slow asker sends it - a body stopped short, headers
never finished, a byte every tenth of a second - and a helper's socket, bound
as http.server binds, is refused the coordinator's port each way round.
The watch is run on the fake clock, to the second: a rental working and still
held until the hang limit and taken back at it - its processes ended outside
the lock, the record and its holder told why, after a restart too - and never
while its progress moves, while it says nothing of work, or before a
deadline it declared; the card read every `watch_s` and not before, a rental
degraded under the floor or by a memory error and never ended for it, an
amend refused while it is, and room again clearing it - never a look begun
before the mark; how much of the card is outside the bootstrap.

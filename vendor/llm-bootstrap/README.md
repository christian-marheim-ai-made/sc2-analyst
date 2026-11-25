# llm-bootstrap

A starting point for any application that runs part of its work on a local
GPU it does not own — on a machine a person is also using.

    python gate.py      every check and every suite - the definition of done

**Working here? Read [GUIDE.md](GUIDE.md) first**: the rules, how to add a route,
an application, a model backend, a setting, a row or a surface of the page,
and the traps this code has already paid for. **Building an application on
it?** The guide's *Build an application, start to finish*.

Four parts. Each is usable alone.

| part | owns | depends on |
| --- | --- | --- |
| `llm-bootstrap-coordinator` | the card: who holds it now, whether they are alive, taking it back when they are not - and the ports applications run on, handed out so that no two collide. One per machine, with a dashboard across every application using it. | the contract's ids and validator |
| `llm-bootstrap-server` | an application's models and its job queue - each model answering one job at a time, different models at the same time. Asks the coordinator for a turn on the card and gives it back when the work is done. | the contract |
| `llm-bootstrap-client` | the widgets: a kit an application's page includes - one script, one call per widget, placed in an element of the page or in a container docked to an edge - and the bootstrap's own page, made with the same calls and served by the server: a bar (the folder, the card and its queue, the log's light) that sheds what does not fit; the tools behind one door (Bench, a raw request as a queue job; Models, each model's health and statistics; Check, each held request scanned and tuned before it is sent; the Log); layouts kept in the folder, every edit undoable. One store, and every answer checked against the contract. | the contract |
| `llm-bootstrap-contract` | every route, its shapes, and real examples. Both server and client are verified against it. | nothing |

The rules the whole project keeps:

- **Standard library only.** No part installs a package to run or to test.
- **A refusal is never a success.** Every refused request answers with a
  non-2xx status, an `error` sentence for a person, and a `reason` from a
  closed list for a program.
- **The card's truth is the card.** The coordinator is a record and a
  decider, never the source of truth about the hardware.
- **An id names, it never routes.** A reply comes back on the connection that
  asked. Every id that can reach a second actor is unique beyond the one that
  minted it - one grammar for every part, in `llm-bootstrap-contract/ids.py`.
- **Nothing is remembered between launches** except what is written in the
  workspace it was pointed at.

# Vendored: llm-bootstrap

**PROVENANCE: a copy · source: the llm-bootstrap repository, master `b5d7c16` (2026-09-29 16:40 +0200,
"measure_card: the card watch is stopped whatever fails after it starts") · copied 2026-10-26 with
`git archive b5d7c16` · ordered by the owner, `../../docs/canon/rulings-2026-10-26.md`**

Never edited here. Everything below this folder is the bootstrap's own, byte for byte; this file is
the only addition. An upgrade is a fresh `git archive` of a newer commit over this folder and a
diff read before committing.

What the analyser uses: `llm-bootstrap-server/server.py` (started as a child, `--backend llamacpp`)
and `llm-bootstrap-server/reference_client.py` (every call). The settings are the analyser's own,
`../../config/runner.toml`.

Known at the copy, from the bootstrap's own history:
- the hardening slice H10 ("no child outlives its starter") was never committed; its worktree is
  gone. `reference_client.start_server` does not bind the server to its starter: a starter that
  dies without `stop()` leaves the server (and its model) up. The analyser stops its server in a
  `finally`, and the bootstrap's llama-server children are bound to the server by a kill-on-close
  job object.
- the bootstrap's own gate, run on this copy 2026-10-26 with the analyser's `.venv` (Python 3.12.10),
  twice (once beside other work, once quiet): every check of the tree and 13 of 16 suites pass;
  5 rows of ~30,000 fail the same way both times, all timing rows - a claim timed at exactly 0.0 s
  (`test_coordination` NativeServerRows), two slow-answer flags and one `prompt_tokens_per_s`
  absent when a stand-in's duration rounds to zero (`test_runner`), one process-holder line
  (`test_server`). Not fixed here (never edited); the real-card proof (`tools/llm_poc.py`) passed
  end to end. A register row for the bootstrap's own builder.

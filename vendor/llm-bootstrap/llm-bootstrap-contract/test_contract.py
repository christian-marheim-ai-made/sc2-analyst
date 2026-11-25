"""The contract tests itself: its examples, its shapes, and both validators.

    python test_contract.py

Three things must hold before either side may lean on this contract.

1. IT IS WELL-FORMED. Every shape, every route, every example, and every
   refusal carries `error` and a `reason` enum - validate.py's own checks.

2. ITS VALIDATORS ARE NOT VACUOUS. A validator that accepted everything would
   pass (1). So every example is turned into values its shape MUST refuse -
   a required field dropped, an unknown field added, a field given a kind its
   shape does not accept, a string outside an enum - and each must fail. The
   wrong values are chosen from the SHAPE, never by asking the validator what
   it rejects, which would only prove the validator agrees with itself. A
   table of hand-written cases pins the traps that are easy to get wrong
   twice: 3.0 is an integer, true is not a number, 1 is not true.

3. THE TWO VALIDATORS AGREE. Every case above - valid and invalid - is run
   through validate.py and validate.js, and their problem lists must be
   identical, character for character.

And its version, `contract_version()`: the same wherever the contract lies,
and changed by every byte of what it is - never by words about it.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ids as I  # noqa: E402
import memory as M  # noqa: E402
import validate as V  # noqa: E402

SAMPLE = {"string": "__probe__", "number": 12345, "boolean": True,
          "null": None, "array": [], "object": {}}
MISSING_ONLY = object()      # a refusal example's request: wrong only by what it lacks
failures: list = []
counts: dict = {}


def record(group: str, ok: bool, name: str, detail: str = "") -> None:
    counts[group] = counts.get(group, 0) + 1
    if not ok:
        failures.append(f"FAIL  {group} - {name}" + (f"\n      {detail}" if detail else ""))


def accepted_kinds(shape, shapes) -> set:
    """Which JSON kinds a shape could accept, read off its structure alone."""
    s = V.deref(shape, shapes)
    if isinstance(s, str):
        return set(V.KINDS) if s == "any" else {"number" if s == "integer" else s}
    if isinstance(s, list):
        return set().union(*(accepted_kinds(a, shapes) for a in s))
    if "id" in s:
        return {"string"}
    if "enum" in s:
        return {V.kind_of(x) for x in s["enum"]}
    return {"array"} if "array" in s else {"object"}


def mutations(v, shape, shapes, depth=0, kind_level=True):
    """(label, value) pairs derived from *v* that *shape* must refuse."""
    s = V.deref(shape, shapes)
    if kind_level:
        wrong = [k for k in V.KINDS if k not in accepted_kinds(shape, shapes)]
        if wrong:
            yield f"a {wrong[0]} where {V.label(shape)} belongs", SAMPLE[wrong[0]]
        if isinstance(s, dict) and "enum" in s and any(isinstance(x, str) for x in s["enum"]):
            yield "a string outside the enum", "__not_in_enum__"
        if isinstance(s, dict) and "id" in s:
            yield f"a string that is not {I.SHOWN[s['id']]}", "__not_an_id__"
    if depth >= 4:
        return
    if isinstance(s, list):
        # Recurse into the one alternative that is the value's kind, keeping
        # only STRUCTURAL changes: a kind-level change might be another
        # alternative's valid value.
        same = [a for a in s if V.top_kind(a, shapes) == V.kind_of(v)]
        if len(same) == 1:
            yield from mutations(v, same[0], shapes, depth, kind_level=False)
        return
    if isinstance(s, dict) and "object" in s and isinstance(v, dict):
        fields, optional = s["object"], set(s.get("optional", ()))
        for n in sorted(fields):
            if n not in v:
                continue
            if n not in optional:
                yield f'without "{n}"', {k: x for k, x in v.items() if k != n}
            for lab, inner in mutations(v[n], fields[n], shapes, depth + 1):
                yield f'"{n}": {lab}', {**v, n: inner}
        if not s.get("open", False):
            yield "an unknown field", {**v, "__probe__": 1}
    if isinstance(s, dict) and "array" in s and isinstance(v, list) and v:
        for lab, inner in mutations(v[0], s["array"], shapes, depth + 1):
            yield f"[0]: {lab}", [inner] + v[1:]


# (name, value, shape, the problems it must produce - [] when it must pass)
TRAPS = [
    ("an integer accepts 3", 3, "integer", []),
    ("an integer accepts 3.0: JSON cannot tell them apart", 3.0, "integer", []),
    ("an integer refuses 3.5", 3.5, "integer", ["$: expected integer, got number"]),
    ("an integer refuses true, though Python's True is an int", True, "integer",
     ["$: expected integer, got boolean"]),
    ("a number refuses true", True, "number", ["$: expected number, got boolean"]),
    ("a boolean refuses 1", 1, "boolean", ["$: expected boolean, got number"]),
    ("a string refuses null", None, "string", ["$: expected string, got null"]),
    ("an enum refuses a near miss", "Queued", {"enum": ["queued"]},
     ['$: expected one of ["queued"], got "Queued"']),
    ("an enum of true refuses 1", 1, {"enum": [True]},
     ["$: expected one of [true], got 1"]),
    ("an enum of 1 refuses true", True, {"enum": [1]},
     ["$: expected one of [1], got true"]),
    ("a small number prints as JavaScript prints it", 1e-07, {"enum": [2]},
     ["$: expected one of [2], got 1e-7"]),
    ("a string outside ASCII prints unescaped in both", "café", {"enum": ["cafe"]},
     ['$: expected one of ["cafe"], got "café"']),
    ("a union names every side", 5, ["string", "null"],
     ["$: expected string | null, got number"]),
    ("a union reports the one alternative of the value's kind", {"x": 1},
     [{"object": {"x": "string"}}, "null"], ["$.x: expected string, got number"]),
    ("a closed object refuses an unknown field", {"a": 1, "b": 2},
     {"object": {"a": "integer"}}, ['$: unexpected field "b"']),
    ("an open object allows one", {"a": 1, "b": 2},
     {"object": {"a": "integer"}, "open": True}, []),
    ("an optional field may be absent", {}, {"object": {"a": "integer"}, "optional": ["a"]}, []),
    ("an optional field that is present must still match", {"a": "x"},
     {"object": {"a": "integer"}, "optional": ["a"]}, ["$.a: expected integer, got string"]),
    ("missing fields are named in order", {},
     {"object": {"b": "string", "a": "string"}},
     ['$: missing required field "a"', '$: missing required field "b"']),
    ("a field called constructor is missing when it is missing", {},
     {"object": {"constructor": "string"}}, ['$: missing required field "constructor"']),
    ("an array names the failing index", [1, "x", 3], {"array": "integer"},
     ["$[1]: expected integer, got string"]),
    ("a nested path is written out", {"a": {"b": [{"c": 1}]}},
     {"object": {"a": {"object": {"b": {"array": {"object": {"c": "string"}}}}}}},
     ["$.a.b[0].c: expected string, got number"]),
    ("any accepts anything", {"x": [1, None]}, "any", []),
]


A_SRV, A_CLI = "srv-k3x9m2p7", "cli-w5r8t2h6"
_ID_VALUES = [
    # one of each, well-formed
    A_SRV, A_CLI, "crd-a3k9m2p7", "load-b2x7q4n8",
    "c-20260923-140012-a3k9m2p7", "j-0007", "j-123456",
    f"{A_SRV}:j-0007#01", f"{A_SRV}:j-0007#100", "t-0042-k3x9", "t-12345-k3x9",
    "pipe-k3x9m2p7",
    # near misses: each is one rule broken
    "pipe-k3x9m2p", "pipe-k3x9m2pl", "pip-k3x9m2p7", "PIPE-k3x9m2p7",
    "srv-k3x9m2p", "srv-k3x9m2p7a", "SRV-k3x9m2p7", "srv-k3x9m2pl", "srv-k3x9m2p0",
    "bot-k3x9m2p7", "c-20260923-140012-a3k9m2p1", "c-2026092-140012-a3k9m2p7",
    "c-20260923-140012-A3K9M2P7", "j-007", "j-00a7", "j-0007\n", " j-0007",
    "j-٠٠٠٧",            # Arabic-Indic digits: \d in Python takes them
    f"{A_CLI}:j-0007#01", f"{A_SRV}:j-0007#1", f"{A_SRV}j-0007#01",
    "t-0042", "t-042-k3x9", "t-0042-k3x", "",
    # not strings at all
    7, None, True, [], {},
]
_KNOWN_DRAWS = list(range(32)) + list(range(31, -1, -1))


def _id_cases():
    """(name, op, args, draws) - every case both implementations answer."""
    out = []
    for v in _ID_VALUES:
        out.append((f"kindOf {v!r}", "kindOf", [v], None))
        for k in I.KINDS:
            out.append((f"matches {k} {v!r}", "matches", [k, v], None))
        out.append((f"parseHandle {v!r}", "parseHandle", [v], None))
        out.append((f"ticketSeq {v!r}", "ticketSeq", [v], None))
    out += [
        ("matches an unknown kind refuses", "matches", ["jobid", "j-0007"], None),
        ("handle names a generation", "handle", [A_SRV, "j-0007", 3], None),
        ("handle pads the sample to two digits", "handle", [A_SRV, "j-0012", 7], None),
        ("handle refuses a client as the server", "handle", [A_CLI, "j-0007", 1], None),
        ("handle refuses a job that is not one", "handle", [A_SRV, "7", 1], None),
    ]
    for role in I.ROLES + ("bot",):
        out.append((f"mintActor {role}", "mintActor", [role], _KNOWN_DRAWS[:8]))
    for now in (1790172012.5, 1790172012.0, 1700000000.999, 0):
        out.append((f"mintCorrelation at {now}", "mintCorrelation", [now], _KNOWN_DRAWS[8:16]))
    for seq in (1, 42, 12345):
        out.append((f"mintTicket {seq}", "mintTicket", [seq], _KNOWN_DRAWS[16:20]))
    for i, at in enumerate((20, 28)):
        out.append((f"mintPipeline {i}", "mintPipeline", [], _KNOWN_DRAWS[at:at + 8]))
    return out


def py_ids(op, args, draws):
    queue = list(draws or [])
    draw = lambda: queue.pop(0)                                 # noqa: E731
    fn = {"matches": I.matches, "kindOf": I.kind_of, "parseHandle": I.parse_handle,
          "ticketSeq": I.ticket_seq, "handle": I.handle,
          "mintActor": lambda r: I.mint_actor(r, draw),
          "mintCorrelation": lambda n: I.mint_correlation(n, draw),
          "mintTicket": lambda s: I.mint_ticket(s, draw),
          "mintPipeline": lambda: I.mint_pipeline(draw)}[op]
    try:
        return {"ok": fn(*args)}
    except (ValueError, TypeError):
        return {"error": True}


# What some of those cases MUST answer, so two implementations that agree
# because they are wrong the same way still fail.
ID_TRUTHS = [
    ("the Arabic-Indic digits are not a job id", "matches",
     ["job", "j-٠٠٠٧"], None, {"ok": False}),
    ("a trailing newline is not a job id", "matches", ["job", "j-0007\n"], None,
     {"ok": False}),
    ("l is not one of the letters", "matches", ["actor", "srv-k3x9m2pl"], None,
     {"ok": False}),
    ("1 is not one of the letters", "matches",
     ["correlation", "c-20260923-140012-a3k9m2p1"], None, {"ok": False}),
    ("a handle is a server's, never a client's", "matches",
     ["handle", f"{A_CLI}:j-0007#01"], None, {"ok": False}),
    ("a ticket id carries its tail", "matches", ["ticket", "t-0042"], None, {"ok": False}),
    ("a handle is taken apart", "parseHandle", [f"{A_SRV}:j-0012#07"], None,
     {"ok": {"server": A_SRV, "job": "j-0012", "sample": 7}}),
    ("a ticket id's number is read", "ticketSeq", ["t-0042-k3x9"], None, {"ok": 42}),
    ("a turn id an old record holds reads as a ticket id: one grammar, one counter",
     "ticketSeq", ["t-0041-k3x9"], None, {"ok": 41}),
    ("there is no turn kind any more: tickets replaced turns", "matches",
     ["turn", "t-0042-k3x9"], None, {"error": True}),
    ("the kinds cannot overlap", "kindOf", [f"{A_SRV}:j-0007#01"], None, {"ok": "handle"}),
    ("a scripted mint is the letters it drew", "mintActor", ["srv"], list(range(8)),
     {"ok": "srv-abcdefgh"}),
    ("an actor of no known role is refused", "mintActor", ["bot"], list(range(8)),
     {"error": True}),
    ("a pipeline id is a pipeline's", "kindOf", ["pipe-k3x9m2p7"], None, {"ok": "pipeline"}),
    ("a pipeline is no actor: it holds nothing in its own name", "matches",
     ["actor", "pipe-k3x9m2p7"], None, {"ok": False}),
    ("a pipeline is no actor role either", "mintActor", ["pipe"], list(range(8)),
     {"error": True}),
    ("a scripted pipeline mint is the letters it drew", "mintPipeline", [], list(range(8)),
     {"ok": "pipe-abcdefgh"}),
]


def _break(route_key, change):
    """A copy of a real route with one thing broken, for META_TRAPS."""
    def make(routes):
        r = json.loads(json.dumps(routes[route_key]))
        change(r)
        return r
    return make


def _set(path, value):
    def change(r):
        *head, last = path
        for k in head:
            r = r[k]
        r[last] = value
    return change


# The contract's rules about ITSELF, each shown a route that breaks it. A
# route-level rule nobody ever shows a broken route could be deleted and
# every example would still pass.
META_TRAPS = [
    ("a refusal without a reason list is refused",
     _break("server/jobs-list", _set(["responses", "409"],
                                     {"object": {"error": "string", "reason": "string"}})),
     "a refusal carries"),
    ("a success carrying an error field is refused",
     _break("server/workspace-get", _set(["responses", "200", "object", "error"],
                                         ["string", "null"])),
     "a success must not carry"),
    ("a status with no example is refused",
     _break("server/job-result", lambda r: r["examples"].__setitem__(
         slice(None), [e for e in r["examples"] if e["status"] != 404])),
     "status 404 has no example"),
    ("a refusal reason with no example is refused",
     _break("server/job-claim", lambda r: r["examples"].__setitem__(
         slice(None), [e for e in r["examples"]
                       if e["response"].get("reason") != "ended"])),
     "'ended' has no example"),
    ("an example on another route's path is refused",
     _break("server/job-claim", lambda r: r["examples"][0].__setitem__(
         "path", "/api/jobs/j-0007/cancel")),
     "is not '/api/jobs/{id}/claim'"),
    ("a bad_request example whose request matches its shape is refused",
     _break("server/jobs-prune", lambda r: next(
         e for e in r["examples"] if e["response"].get("reason") == "bad_request"
     ).__setitem__("request", {"statuses": ["failed"]})),
     "needs a request its route's shape refuses"),
    ("a bad_request example that does not name the first problem is refused",
     _break("server/workspace-bind", lambda r: next(
         e for e in r["examples"] if e["response"].get("reason") == "bad_request"
     )["response"].__setitem__("error", "the path is wrong")),
     "its error must be the first problem"),
    ("bad_request on a route that takes no body is refused",
     _break("server/job-result", _set(["responses", "404", "object", "reason", "enum"],
                                      ["no_such_job", "bad_request"])),
     "refuses a body, and this route takes none"),
    ("a refusal example may lack a field, but what it carries must be the right kind",
     _break("server/workspace-bind", lambda r: next(
         e for e in r["examples"] if e["response"].get("reason") == "no_path"
     )["request"].__setitem__("path", 7)),
     "request $.path: expected string, got number"),
    ("an id shape of a kind that does not exist is refused, not crashed on",
     _break("server/jobs-list", _set(["responses", "200", "object", "jobs"],
                                     {"array": {"id": "jobid"}})),
     "no id kind 'jobid'"),
]


def version_rows() -> list:
    """(name, ok, detail) for `contract_version`: a short hash of what the
    contract IS - its routes, shapes, answers before any route, id grammar and
    validators. Each change
    is made to a fresh copy of the contract, somewhere else, so each row sees
    one change alone."""
    here = V.contract_version()
    rows = [("a short hash: 12 lowercase hex digits",
             re.fullmatch(r"[0-9a-f]{12}", here) is not None, here),
            ("the same contract twice is the same version", V.contract_version() == here, "")]

    def append(rel, data=b" "):
        return lambda c: (c / rel).write_bytes((c / rel).read_bytes() + data)

    def nothing(c):
        return None

    def words_about_it(c):
        append("README.md", b"\nOne more sentence about the contract.\n")(c)
        append("test_contract.py", b"\n# one more comment\n")(c)
        (c / "__pycache__").mkdir()
        (c / "__pycache__" / "validate.cpython-314.pyc").write_bytes(b"\0a cache")

    def add(c):
        route = c / "routes" / "server" / "health.json"
        route.with_name("health-again.json").write_bytes(route.read_bytes())

    def rename(c):
        route = c / "routes" / "server" / "health.json"
        route.rename(route.with_name("health-renamed.json"))

    def remove(c):
        (c / "shapes" / "fault.json").unlink()

    def origins(c):
        append("origins/server.json")(c)

    same = [("a copy elsewhere is the same version: where it lies is not what it is", nothing),
            ("its README, its self-test and a cache beside it are not in it", words_about_it)]
    changed = [("one byte of a route changes it", append("routes/server/job-result.json")),
               ("one byte of a shape changes it", append("shapes/job.json")),
               ("one byte of the id grammar changes it: ids.py", append("ids.py")),
               ("one byte of the id grammar changes it: ids.js", append("ids.js")),
               ("one byte of a validator changes it: validate.py", append("validate.py")),
               ("one byte of a validator changes it: validate.js", append("validate.js")),
               ("a route added changes it", add),
               ("a route renamed, its bytes the same, changes it", rename),
               ("a shape removed changes it", remove),
               ("one byte of what a part answers before any route changes it: origins",
                origins),
               ("one byte of what the parts share on the machine changes it: the card lock",
                append("machine/card-lock.json")),
               ("one byte of the one memory check changes it: memory.py", append("memory.py"))]
    cases = [(n, c, True) for n, c in same] + [(n, c, False) for n, c in changed]
    with tempfile.TemporaryDirectory() as tmp:
        for i, (name, change, stays) in enumerate(cases):
            copy = Path(tmp) / f"copy-{i}" / "llm-bootstrap-contract"
            shutil.copytree(HERE, copy, ignore=shutil.ignore_patterns("__pycache__"))
            change(copy)
            got = V.contract_version(copy)
            rows.append((name, (got == here) is stays,
                         f"{got}, not {here}" if stays else f"still {got}"))
    return rows


def _break_origins(part, change):
    """A copy of a part's answer before any route with one thing broken, for
    ORIGIN_TRAPS."""
    def make(origins):
        d = json.loads(json.dumps(origins[part]))
        change(d)
        return d
    return make


def _example_naming(key, change):
    """The first example that names *key* of its request, changed."""
    return lambda d: change(next(e for e in d["examples"] if key in e))


# The rules about what a part answers BEFORE any route (origins/<part>.json),
# each shown a file that breaks it - the ones of its own, and two it keeps as
# a route does, which prove it is held to a route's rules at all.
ORIGIN_TRAPS = [
    ("an answer before any route that is not a refusal is refused",
     _break_origins("server", _set(["responses", "200"], {"object": {"ok": "boolean"}})),
     "before any route there are only refusals"),
    ("an example before any route that names no Origin is refused",
     _break_origins("server", lambda d: d["examples"][0].pop("origin")),
     "the Origin it answers"),
    ("a refusal before any route without a reason list is refused",
     _break_origins("server", _set(["responses", "403"],
                                   {"object": {"error": "string", "reason": "string"}})),
     "a refusal carries"),
    ("a refusal before any route with no example is refused",
     _break_origins("server", lambda d: d["examples"].clear()),
     "status 403 has no example"),
    # what an example says of the request it answers: its Origin, its Host,
    # its body's length - each of its own kind, and nothing else
    ("an example before any route whose Origin is not a string is refused",
     _break_origins("server", _example_naming("origin", lambda e: e.__setitem__("origin", 7))),
     "the Origin it answers is a string"),
    ("an example before any route whose Host is not a string is refused",
     _break_origins("server", _example_naming("host", lambda e: e.__setitem__("host", 7))),
     "the Host it was sent to is a string"),
    ("an example before any route whose body's length is below 0 is refused",
     _break_origins("server", _example_naming("length", lambda e: e.__setitem__("length", -1))),
     "its body's length is a whole number of bytes"),
    ("...and one whose length is a fraction of a byte",
     _break_origins("server", _example_naming("length", lambda e: e.__setitem__("length", 1.5))),
     "its body's length is a whole number of bytes"),
    ("...and one whose length is true, though Python's True is an int",
     _break_origins("server", _example_naming("length", lambda e: e.__setitem__("length", True))),
     "its body's length is a whole number of bytes"),
    ("an example before any route whose Transfer-Encoding is not a string is refused",
     _break_origins("server", _example_naming("transfer_encoding",
                                              lambda e: e.__setitem__("transfer_encoding", 7))),
     "the Transfer-Encoding it was sent with is a string"),
    ("...and so is the coordinator's",
     _break_origins("coordinator", _example_naming(
         "transfer_encoding", lambda e: e.__setitem__("transfer_encoding", ["chunked"]))),
     "the Transfer-Encoding it was sent with is a string"),
    ("an example before any route that says what no rule reads of its request is refused",
     _break_origins("server", lambda d: d["examples"][0].__setitem__("path", "/api/health")),
     "and nothing else"),
]


def origins_are_checked() -> list:
    """What `check_contract` says of a copy of the contract whose answer
    before any route is broken - a file the contract never read would be a
    rule nobody keeps."""
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "contract"
        shutil.copytree(HERE, copy, ignore=shutil.ignore_patterns("__pycache__"))
        f = copy / "origins" / "server.json"
        doc = json.loads(f.read_bytes().decode("utf-8"))
        doc["responses"]["403"] = {"object": {"error": "string", "reason": "string"}}
        f.write_bytes(json.dumps(doc).encode("utf-8"))
        problems, _, _ = V.check_contract(copy)
    return problems


def _break_machine(change):
    """A copy of the card lock's file with one thing broken, for MACHINE_TRAPS."""
    def make(machine):
        d = json.loads(json.dumps(machine["card-lock"]))
        change(d)
        return d
    return make


# The rules about a file the parts share on the machine (machine/<name>.json),
# each shown a file that breaks it.
MACHINE_TRAPS = [
    ("a lock whose name carries a folder is refused",
     _break_machine(_set(["file"], "..\\llm-bootstrap-card.lock")), "a plain file name"),
    ("a note that is not written beside its lock is refused",
     _break_machine(_set(["note_file"], "holder.json")), "beside the file it is about"),
    ("a note example its shape refuses is refused",
     _break_machine(lambda d: d["examples"][0]["note"].pop("coordinator")), "note $"),
    ("a note example that says more than its name and its note is refused",
     _break_machine(lambda d: d["examples"][0].__setitem__("status", 200)),
     "a name and a note, and nothing else"),
    ("a note with no example is refused",
     _break_machine(lambda d: d["examples"].clear()), "at least one example"),
    ("a note shape that is no shape is refused",
     _break_machine(_set(["note"], {"object": {"pid": "int"}})), "unknown type"),
]


def machine_is_checked() -> list:
    """What `check_contract` says of a copy of the contract whose card lock is
    broken - a file the contract never read would be a rule nobody keeps."""
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "contract"
        shutil.copytree(HERE, copy, ignore=shutil.ignore_patterns("__pycache__"))
        f = copy / "machine" / "card-lock.json"
        doc = json.loads(f.read_bytes().decode("utf-8"))
        doc["note_file"] = "holder.json"
        f.write_bytes(json.dumps(doc).encode("utf-8"))
        problems, _, _ = V.check_contract(copy)
    return problems


def _fit(need, free, total, margin=256):
    return M.fit(need, free, total, margin)["verdict"]


def _loud(verdict, outside) -> str:
    """The contract's words for no room now (H9) - "" where it has none."""
    say = getattr(M, "no_room_words", None)
    return "" if say is None else say(verdict, outside)


def _waited(verdict, outside, waited_s) -> str:
    """The contract's words for no room after a wait for it (S18) - "" where
    it has none."""
    say = getattr(M, "no_room_words", None)
    try:
        return "" if say is None else say(verdict, outside, waited_s=waited_s)
    except TypeError:
        return ""


def _waiting(verdict, outside) -> str:
    """The contract's words while scheduled work waits for room (S18) - ""
    where it has none."""
    say = getattr(M, "room_words", None)
    return "" if say is None else say(verdict, outside)


def _refuses(*args) -> bool:
    try:
        M.fit(*args)
    except ValueError:
        return True
    return False


#: THE ONE MEMORY CHECK (memory.py): what it must answer, whichever part runs
#: it - each rule at its edge, where a mistake by one would show
MEMORY_TRUTHS = [
    ("need and margin exactly the free memory: it fits", _fit(9000, 9256, 24576) == "fits"),
    ("one MiB more than the free memory: no room now", _fit(9001, 9256, 24576) == "no_room"),
    ("more than the whole card, with the margin: it never fits, even on an empty card",
     _fit(24321, 24576, 24576) == "never_fits"),
    ("...and the whole card with the margin exactly: an empty card holds it",
     _fit(24320, 24576, 24576) == "fits"),
    ("never_fits is judged before no_room: waiting cannot help it",
     _fit(30000, 100, 24576) == "never_fits"),
    ("a card whose free memory is not known decides nothing", _fit(99999, None, 24576) == "unknown"),
    ("a card whose total is not known can still have no room",
     _fit(9001, 9256, None) == "no_room"),
    ("a need nobody declared adds nothing: the margin alone is checked",
     (_fit(None, 256, 24576), _fit(None, 255, 24576)) == ("fits", "no_room")),
    ("the margin is the caller's: a margin of 0 keeps nothing free",
     _fit(9256, 9256, 24576, 0) == "fits"),
    ("the default margin is D0's floor, 256 MiB", M.MARGIN_MIB == 256),
    ("the verdict carries the numbers it was made on",
     M.fit(9000, 9256, 24576, 300) == {"verdict": "no_room", "need_mib": 9000, "free_mib": 9256,
                                       "margin_mib": 300, "total_mib": 24576}),
    ("the numbers alone are the contract's memory shape",
     not V.check(M.numbers(M.fit(9000, None, None)), {"ref": "memory"}, V.load(HERE)[0])),
    ("its words say the numbers",
     all(s in M.words(M.fit(9001, 9256, 24576)) for s in ("9001 MiB", "256 MiB", "9256 MiB free",
                                                           "of 24576"))),
    ("its words for a card that can never hold it say the whole card",
     "the whole card is 24576 MiB" in M.words(M.fit(30000, 100, 24576))),
    ("its words for a card it could not read say so",
     "could not be read" in M.words(M.fit(10, None, None))),
    # H9, the owner's ruling of 2026-09-28: a model that does not fit on a
    # card that is not over-full fails at once, loudly - in numbers, and what
    # to do
    ("no room now, in words: what is needed, what is free of the whole, what programs outside "
     "the bootstrap hold, and what to do - close programs, or reboot - never a wait",
     all(s in _loud(M.fit(9001, 9256, 24576), 12000)
         for s in ("9001 MiB", "256 MiB", "9256 MiB free of 24576",
                   "programs outside the bootstrap hold 12000 MiB", "does not wait for room",
                   "Close programs that use the card, or reboot"))),
    ("...what programs outside hold, not known, is said to be not known - the advice all the same",
     all(s in _loud(M.fit(9001, 9256, None), None)
         for s in ("9256 MiB free", "programs outside the bootstrap hold is not known",
                   "or reboot"))),
    ("...and when programs outside hold none of it: the application's own models hold it, and "
     "one it keeps is what comes off - not a program closed",
     all(s in _loud(M.fit(9001, 9256, 24576), 0)
         for s in ("hold none of it", "own models hold the rest", "Unload one it keeps"))
     and "reboot" not in _loud(M.fit(9001, 9256, 24576), 0)),
    # S18, the owner's second ruling of 2026-09-28: scheduled work may wait for
    # room - and a patience that runs out while it waits fails in the same
    # loud words, saying how long it waited, never only that its patience ran
    # out
    ("no room after a wait for it, in words: the numbers, what programs outside hold, how long "
     "it waited, and what to do - never that it does not wait",
     all(s in _waited(M.fit(9001, 9256, 24576), 12000, 9000)
         for s in ("9001 MiB", "256 MiB", "9256 MiB free of 24576",
                   "programs outside the bootstrap hold 12000 MiB", "It waited 2 h 30 min for "
                   "room", "none came", "Close programs that use the card, or reboot"))
     and "does not wait for room" not in _waited(M.fit(9001, 9256, 24576), 12000, 9000)),
    ("...a wait under two minutes is said in seconds, and one under two hours in minutes",
     "It waited 45 s" in _waited(M.fit(9001, 9256, 24576), 12000, 45.4)
     and "It waited 90 min" in _waited(M.fit(9001, 9256, 24576), 12000, 5399)),
    ("...and when programs outside hold none of it, after a wait: unload one it keeps",
     all(s in _waited(M.fit(9001, 9256, 24576), 0, 600)
         for s in ("hold none of it", "It waited 10 min", "Unload one it keeps"))
     and "reboot" not in _waited(M.fit(9001, 9256, 24576), 0, 600)),
    ("while scheduled work waits for room, in words: the numbers, what programs outside hold, "
     "and that it starts by itself once it fits - never that it does not wait",
     all(s in _waiting(M.fit(9001, 9256, 24576), 12000)
         for s in ("9001 MiB", "9256 MiB free of 24576",
                   "programs outside the bootstrap hold 12000 MiB",
                   "starts by itself once it fits", "close programs that use the card"))
     and "does not wait" not in _waiting(M.fit(9001, 9256, 24576), 12000)),
    ("...what programs outside hold, not known, is said to be not known",
     "is not known" in _waiting(M.fit(9001, 9256, None), None)),
    ("...and when they hold none of it: the application's own models hold the rest",
     "own models hold the rest" in _waiting(M.fit(9001, 9256, 24576), 0)),
    ("a need below 0, a fraction of a MiB, or true, is refused",
     _refuses(-1, 10, 10) and _refuses(1.5, 10, 10) and _refuses(True, 10, 10)),
    ("a margin that is not given as a number is refused", _refuses(1, 10, 10, None)),
    # S4 R9: a server waits AMEND_ANSWER_S for an amend's answer; a coordinator
    # waits in it for a memory just given back at most AMEND_SETTLE_MAX_S, and
    # the rest is its last reading of the card and the answer's way
    ("an amend's answer is waited for longer than a coordinator may wait in it for the memory",
     0 < getattr(M, "AMEND_SETTLE_MAX_S", 0) < getattr(M, "AMEND_ANSWER_S", 0)),
]

#: set in the environment of the one run of this suite that runs on a broken
#: copy of the contract (`a_broken_status_is_a_red_check`): that run runs no
#: copy of its own
NESTED = "LLM_BOOTSTRAP_CONTRACT_COPY"


def a_broken_status_is_a_red_check():
    """H3 R8: this suite, run on a contract one of whose examples - a route's,
    and an answer's before any route - names a status with no response
    declared, crashed with a KeyError before it said anything, where the
    contract's own check had already found the problem. -> (exit code, what
    it printed), run on such a copy; None in that run itself."""
    if NESTED in os.environ:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "contract"
        shutil.copytree(HERE, copy, ignore=shutil.ignore_patterns("__pycache__"))
        for f in (copy / "origins" / "server.json", copy / "routes" / "server" / "health.json"):
            doc = json.loads(f.read_bytes().decode("utf-8"))
            doc["examples"][0]["status"] = 499
            f.write_bytes(json.dumps(doc).encode("utf-8"))
        r = subprocess.run([sys.executable, str(copy / "test_contract.py")], cwd=copy,
                           env={**os.environ, NESTED: "1", "PYTHONDONTWRITEBYTECODE": "1"},
                           capture_output=True, encoding="utf-8", errors="replace", timeout=300)
    return r.returncode, r.stdout + r.stderr


def main() -> int:
    # A case name may carry any character (one trap is named for "café").
    # On Windows a PIPED stdout defaults to the locale's code page, so a
    # caller reading this output as UTF-8 would crash on the first such name
    # - which is how the first mutation sweep of this suite died, leaving a
    # mutant in place. The output is UTF-8 whoever is reading it.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    problems, shapes, routes = V.check_contract()
    origins = V.load_origins()
    record("well-formed", not problems, "the contract", "\n      ".join(problems))
    for name, make, needle in META_TRAPS:
        found = V.check_route(make(routes), shapes, "broken")
        record("meta", any(needle in p for p in found), name,
               f"wanted a problem containing {needle!r}, got {found}")
    for name, ok, detail in version_rows():
        record("version", ok, name, detail)
    for name, make, needle in ORIGIN_TRAPS:
        try:
            found = V.check_origins(make(origins), shapes, "broken")
        except StopIteration:
            found = ["(no example names that part of its request: there was nothing to break)"]
        record("meta", any(needle in p for p in found), name,
               f"wanted a problem containing {needle!r}, got {found}")
    found = origins_are_checked()
    record("meta", any(p.startswith("origins/server") and "a refusal carries" in p for p in found),
           "the contract's own answers before any route are checked with it",
           f"a broken origins/server.json in a copy of the contract gave {found}")
    ran = a_broken_status_is_a_red_check()
    if ran is not None:
        code, said = ran
        tally = re.findall(r"^(\d+) passed, (\d+) failed, (\d+) total$", said, re.M)
        record("meta", code == 1 and bool(tally) and int(tally[-1][1]) >= 1
               and "Traceback" not in said, "this suite, run on a contract whose examples name a "
               "status with no response declared, says so in a red check - never a crash",
               f"exit {code}: {said[-600:]}")
        record("meta", all(f"{where}.examples[0]" in said and "status 499 is not declared" in said
                           for where in ("server/health", "origins/server")),
               "...naming each: a route's, and an answer's before any route", said[-600:])
    machine = V.load_machine()
    record("machine", set(machine) == {"card-lock"}, "the card lock is the one file the parts "
           "share on the machine", f"found {sorted(machine)}")
    for name, make, needle in MACHINE_TRAPS:
        found = V.check_machine(make(machine), shapes, "broken")
        record("meta", any(needle in p for p in found), name,
               f"wanted a problem containing {needle!r}, got {found}")
    found = machine_is_checked()
    record("meta", any(p.startswith("machine/card-lock") and "beside the file" in p for p in found),
           "the contract's own card lock is checked with it",
           f"a broken machine/card-lock.json in a copy of the contract gave {found}")
    lock = machine["card-lock"]
    record("machine", [("coordinator" in ex["note"]) for ex in lock["examples"]].count(True) >= 1
           and [("holder" in ex["note"]) for ex in lock["examples"]].count(True) >= 1,
           "the card lock's examples show both holders: a coordinator and a server run on its own",
           str([ex["name"] for ex in lock["examples"]]))
    for name, ok in MEMORY_TRUTHS:
        record("memory", bool(ok), name)

    # AN EXAMPLE WHOSE STATUS DECLARES NO RESPONSE has no shape to check it
    # against: the contract's own check said so (well-formed, above), and it is
    # left out here - never a KeyError that ends the suite before its tally
    cases = []   # (group, name, value, shape, expected problems or None=any non-empty)
    for key, route in routes.items():
        for ex in route["examples"]:
            shape = route["responses"].get(str(ex["status"]))
            if shape is None:
                continue
            base = f"{key} / {ex['name']}"
            cases.append(("positive", f"{base}: response", ex["response"], shape, []))
            for lab, v in mutations(ex["response"], shape, shapes):
                cases.append(("negative", f"{base}: response with {lab}", v, shape, None))
            if route["request"] is not None and "request" in ex:
                if V.refuses_the_request(ex):
                    # the one request an example carries in order to be refused
                    cases.append(("negative", f"{base}: the refused request",
                                  ex["request"], route["request"], None))
                    continue
                if int(ex["status"]) >= 400 and V.check(ex["request"], route["request"],
                                                         shapes):
                    # a refusal missing the field it was refused for: only
                    # missing-field problems allowed, and both sides must agree
                    cases.append(("missing only", f"{base}: request", ex["request"],
                                  route["request"], MISSING_ONLY))
                    continue
                cases.append(("positive", f"{base}: request", ex["request"],
                              route["request"], []))
                for lab, v in mutations(ex["request"], route["request"], shapes):
                    cases.append(("negative", f"{base}: request with {lab}", v,
                                  route["request"], None))
    # what a part answers before any route: its examples are answers like any
    # route's, and both validators must read them alike
    for part, doc in origins.items():
        for ex in doc["examples"]:
            shape = doc["responses"].get(str(ex["status"]))
            if shape is None:
                continue
            base = f"origins/{part} / {ex['name']}"
            cases.append(("positive", f"{base}: response", ex["response"], shape, []))
            for lab, v in mutations(ex["response"], shape, shapes):
                cases.append(("negative", f"{base}: response with {lab}", v, shape, None))
    # what the parts share on the machine: each note example is a value like
    # any answer, and both validators must read it alike
    for part, doc in machine.items():
        for ex in doc["examples"]:
            base = f"machine/{part} / {ex['name']}: note"
            cases.append(("positive", base, ex["note"], doc["note"], []))
            for lab, v in mutations(ex["note"], doc["note"], shapes):
                cases.append(("negative", f"{base} with {lab}", v, doc["note"], None))
    # A SHAPE NO ROUTE'S SHAPE REACHES: a conversation is the request a text
    # model is asked - what an application's build hook returns, and with the
    # default hooks a job's own `input`, which the server never reads. So a
    # submit example whose input is a conversation is one: both validators
    # must take it, and must refuse it broken, alike.
    talks = [ex for ex in routes["server/jobs-submit"]["examples"]
             if isinstance((ex.get("request") or {}).get("input"), dict)
             and "messages" in ex["request"]["input"]]
    record("conversation", bool(talks), "a submit example's input is a conversation",
           "no example of server/jobs-submit carries one, so nothing checks the shape")
    for ex in talks:
        base = f"server/jobs-submit / {ex['name']}: input as a conversation"
        talk, shape = ex["request"]["input"], {"ref": "conversation"}
        cases.append(("positive", base, talk, shape, []))
        for lab, v in mutations(talk, shape, shapes):
            cases.append(("negative", f"{base} with {lab}", v, shape, None))
    # AND AN AUDIO REQUEST: what a whisper model is asked (PLAN-4 D10) - a WAV
    # file on the server's machine, and whisper-server's own fields - held to
    # its shape the same way, by both validators alike. A submit REFUSED for
    # its audio request (`bad_audio_request`, checked at submit: S12b) exists
    # to show one being refused, so its input must be one the shape refuses
    hears = [ex for ex in routes["server/jobs-submit"]["examples"]
             if isinstance((ex.get("request") or {}).get("input"), dict)
             and "audio" in ex["request"]["input"]]
    refused = [ex for ex in hears
               if (ex.get("response") or {}).get("reason") == "bad_audio_request"]
    record("audio request", bool(hears), "a submit example's input is an audio request",
           "no example of server/jobs-submit carries one, so nothing checks the shape")
    record("audio request", bool(refused) and len(refused) < len(hears),
           "a submit example is refused for its audio request, and another is taken",
           f"{len(refused)} refused of {len(hears)}: both kinds are needed for the shape to be "
           "checked both ways")
    for ex in hears:
        base = f"server/jobs-submit / {ex['name']}: input as an audio request"
        heard, shape = ex["request"]["input"], {"ref": "audio_request"}
        if ex in refused:
            cases.append(("negative", f"{base}: the refused audio request", heard, shape, None))
            continue
        cases.append(("positive", base, heard, shape, []))
        for lab, v in mutations(heard, shape, shapes):
            cases.append(("negative", f"{base} with {lab}", v, shape, None))
    # THE REQUEST CHECK'S SEND (PLAN-4 D16) CHANGES WHAT A SUBMIT MAY NAME, AND
    # NOTHING ELSE: its `gen` is the submit's, field for field and kind for
    # kind, less the queue's own three - which the job's record names as it
    # was submitted - and every one optional. Two lists kept by hand drift.
    sub_gen = routes["server/jobs-submit"]["request"]["object"]["gen"]["object"]
    send_gen = routes["server/job-send"]["request"]["object"]["gen"]
    want = {k: v for k, v in sub_gen.items() if k not in ("model", "n", "retries")}
    record("request check", send_gen["object"] == want,
           "a send's gen is a submit's, less the queue's own three",
           f"differs in {sorted(set(want) ^ set(send_gen['object']))} or a kind")
    record("request check", sorted(send_gen.get("optional") or []) == sorted(want),
           "every parameter a send names is optional",
           f"required: {sorted(set(want) - set(send_gen.get('optional') or []))}")
    held = routes["server/job-held"]["examples"]
    record("request check", {e["response"]["held"]["engine"] for e in held
                             if e["status"] == 200} == {"llamacpp", "whisper", None},
           "a held request is shown for a text model, a whisper model and a backend that "
           "says nothing more")
    cases += [("trap", n, v, s, want) for n, v, s, want in TRAPS]

    py = [V.check(v, s, shapes) for _, _, v, s, _ in cases]
    for (group, name, _, _, want), got in zip(cases, py):
        if want is None:
            record(group, bool(got), name, "the validator accepted a value it must refuse")
        elif want is MISSING_ONLY:
            record(group, bool(got) and all(V.missing_field(p) for p in got), name,
                   f"only missing fields may be wrong, got {got}")
        else:
            record(group, got == want, name, f"wanted {want}, got {got}")

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as f:
        json.dump({"shapes": shapes,
                   "cases": [{"value": v, "shape": s} for _, _, v, s, _ in cases]},
                  f, ensure_ascii=False)
        batch = f.name
    r = subprocess.run(["node", str(HERE / "validate.js"), "--batch", batch],
                       capture_output=True, encoding="utf-8")
    Path(batch).unlink()
    if r.returncode != 0:
        record("agreement", False, "validate.js ran", r.stderr.strip()[:400])
    else:
        js = json.loads(r.stdout)
        for (_, name, _, _, _), p, j in zip(cases, py, js):
            record("agreement", p == j, name, f"python {p}\n      node   {j}")

    # THE IDS: what each implementation answers for one table, compared; and
    # what some of those answers must BE, so a shared mistake still fails.
    id_cases = _id_cases()
    py_answers = [py_ids(op, args, draws) for _, op, args, draws in id_cases]
    for name, op, args, draws, want in ID_TRUTHS:
        got = py_ids(op, args, draws)
        record("ids", got == want, name, f"wanted {want}, got {got}")
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as f:
        json.dump({"cases": [{"op": op, "args": args, "draws": draws}
                             for _, op, args, draws in id_cases]}, f, ensure_ascii=False)
        batch = f.name
    r = subprocess.run(["node", str(HERE / "ids.js"), "--batch", batch],
                       capture_output=True, encoding="utf-8")
    Path(batch).unlink()
    if r.returncode != 0:
        record("ids agree", False, "ids.js ran", r.stderr.strip()[:400])
    else:
        for (name, _, _, _), p, j in zip(id_cases, py_answers, json.loads(r.stdout)):
            record("ids agree", p == j, name, f"python {p}\n      node   {j}")

    for f in failures:
        print(f)
    total = sum(counts.values())
    print("-" * 60)
    print("  ".join(f"{g} {counts[g] - sum(1 for f in failures if f.startswith(f'FAIL  {g} '))}/{counts[g]}"
                    for g in counts))
    print(f"{total - len(failures)} passed, {len(failures)} failed, {total} total")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

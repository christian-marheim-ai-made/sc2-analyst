"""Check JSON values against the contract's shapes. Standard library only.

    python validate.py        check the whole contract; name every problem, exit 1

THE SHAPE LANGUAGE is plain JSON, and deliberately small:

    "string" "number" "integer" "boolean" "null" "any"
    [shape, shape, ...]                  one of these
    {"enum": [literal, ...]}             exactly one of these values
    {"array": shape}                     a list whose every item matches
    {"object": {name: shape, ...},       a dict with these fields,
     "optional": [name, ...],            ...any of which may be absent,
     "open": true}                       ...and other keys allowed (default: refused)
    {"ref": "name"}                      the shape in shapes/<name>.json
    {"id": "kind"}                       a string that is an id of that kind:
                                         actor, correlation, job, handle,
                                         ticket or pipeline - the grammar is
                                         ids.py's

`check()` returns a list of problems, one line each, each naming the path of
the value that is wrong. An empty list means the value matches.
`validate.js` is the same algorithm for the browser and must produce the same
list, character for character: `test_contract.py` runs both over every case
and compares them, because two validators that disagree are two contracts.

"integer" means a number with no fractional part. JSON cannot tell 3 from
3.0, so neither may this: the JavaScript side reads the same bytes as one
number, and the two sides must reach one verdict.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

import ids

HERE = Path(__file__).resolve().parent
SCALARS = ("string", "number", "integer", "boolean", "null", "any")
KINDS = ("string", "number", "boolean", "null", "array", "object")
ROUTE_KEYS = {"id", "method", "path", "summary", "request", "responses", "examples"}
EXAMPLE_KEYS = {"name", "status", "path", "request", "response"}
ORIGIN_KEYS = {"id", "summary", "responses", "examples"}
ORIGIN_EXAMPLE_KEYS = {"name", "status", "response"}
MACHINE_KEYS = {"id", "summary", "file", "note_file", "note", "examples"}
MACHINE_EXAMPLE_KEYS = {"name", "note"}
#: what an example of an answer before any route says of the request it
#: answers - one of them at least: the page that sent it (its Origin), the
#: name it was sent to (its Host), its body's length (its Content-Length),
#: how its body was sent (its Transfer-Encoding)
BEFORE_ANY_ROUTE = ("origin", "host", "length", "transfer_encoding")


# --------------------------------------------------------------- the check

def kind_of(v) -> str:
    """The JSON kind of a decoded value. `bool` first: Python's True is an int."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return type(v).__name__


def render(v) -> str:
    """A scalar the way both validators print it: strings as JSON, whole
    numbers without `.0`, exponents without a padding zero (`1e-7`, as
    JavaScript writes it, not Python's `1e-07`)."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "null"
    if isinstance(v, (int, float)):
        if float(v).is_integer() and abs(v) < 1e21:
            return str(int(v))
        return re.sub(r"e([+-])0*(\d)", r"e\1\2", repr(float(v)))
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    return kind_of(v)


def deref(shape, shapes):
    while isinstance(shape, dict) and "ref" in shape:
        shape = shapes[shape["ref"]]
    return shape


def label(shape) -> str:
    """How a union names one of its alternatives in a problem."""
    if isinstance(shape, str):
        return shape
    if isinstance(shape, list):
        return " | ".join(label(s) for s in shape)
    if "ref" in shape:
        return shape["ref"]
    if "id" in shape:
        return f"{shape['id']} id"
    for key in ("enum", "array", "object"):
        if key in shape:
            return key
    return "?"


def top_kind(shape, shapes):
    """The single JSON kind a shape accepts at its top, or None if not one."""
    shape = deref(shape, shapes)
    if isinstance(shape, str):
        return {"integer": "number", "any": None}.get(shape, shape)
    if isinstance(shape, dict):
        if "array" in shape:
            return "array"
        if "object" in shape:
            return "object"
        if "id" in shape:
            return "string"
    return None


def same_literal(v, lit) -> bool:
    """JavaScript's `===` on JSON scalars. Python's `==` says 1 == True."""
    if isinstance(v, bool) or isinstance(lit, bool):
        return isinstance(v, bool) and isinstance(lit, bool) and v == lit
    return kind_of(v) == kind_of(lit) and v == lit


def check(v, shape, shapes=None, at="$") -> list:
    """Every problem with *v* against *shape*; [] when it matches."""
    shapes = shapes or {}
    shape = deref(shape, shapes)
    if isinstance(shape, str):
        if shape == "any":
            return []
        got = kind_of(v)
        if shape == "integer":
            ok = got == "number" and float(v).is_integer()
        else:
            ok = got == shape
        return [] if ok else [f"{at}: expected {shape}, got {got}"]
    if isinstance(shape, list):
        if any(not check(v, alt, shapes, at) for alt in shape):
            return []
        got = kind_of(v)
        # When exactly one alternative is the value's kind, its problems are
        # the useful ones: "expected turn | null, got object" names nothing.
        same = [alt for alt in shape if top_kind(alt, shapes) == got]
        if len(same) == 1:
            return check(v, same[0], shapes, at)
        return [f"{at}: expected {' | '.join(label(a) for a in shape)}, got {got}"]
    if "id" in shape:
        if shape["id"] not in ids.KINDS:
            return [f"{at}: malformed shape"]        # check_shape names the kind
        if ids.matches(shape["id"], v):
            return []
        got = render(v) if isinstance(v, str) else kind_of(v)
        return [f"{at}: expected {ids.SHOWN[shape['id']]}, got {got}"]
    if "enum" in shape:
        if any(same_literal(v, lit) for lit in shape["enum"]):
            return []
        wanted = ", ".join(render(x) for x in shape["enum"])
        return [f"{at}: expected one of [{wanted}], got {render(v)}"]
    if "array" in shape:
        if not isinstance(v, list):
            return [f"{at}: expected array, got {kind_of(v)}"]
        out = []
        for i, item in enumerate(v):
            out += check(item, shape["array"], shapes, f"{at}[{i}]")
        return out
    if "object" in shape:
        if not isinstance(v, dict):
            return [f"{at}: expected object, got {kind_of(v)}"]
        fields = shape["object"]
        optional = set(shape.get("optional", ()))
        out = [f'{at}: missing required field "{n}"'
               for n in sorted(fields) if n not in v and n not in optional]
        if not shape.get("open", False):
            out += [f'{at}: unexpected field "{n}"'
                    for n in sorted(v) if n not in fields]
        for n in sorted(fields):
            if n in v:
                out += check(v[n], fields[n], shapes, f"{at}.{n}")
        return out
    return [f"{at}: malformed shape"]


# ------------------------------------------- the contract checks itself

def check_shape(shape, shapes, at) -> list:
    """Is *shape* itself a well-formed shape?"""
    if isinstance(shape, str):
        return [] if shape in SCALARS else [f"{at}: unknown type {shape!r}"]
    if isinstance(shape, list):
        if len(shape) < 2:
            return [f"{at}: a union needs at least two alternatives"]
        return [p for i, s in enumerate(shape)
                for p in check_shape(s, shapes, f"{at}[{i}]")]
    if not isinstance(shape, dict):
        return [f"{at}: a shape is a type name, a list, or an object"]
    keys = set(shape) & {"enum", "array", "object", "ref", "id"}
    if len(keys) != 1:
        return [f"{at}: needs exactly one of enum/array/object/ref/id"]
    (key,) = keys
    allowed = {key} | ({"optional", "open"} if key == "object" else set())
    if set(shape) - allowed:
        return [f"{at}: unknown keys {sorted(set(shape) - allowed)}"]
    if key == "ref":
        return [] if shape["ref"] in shapes else [f"{at}: no shape named {shape['ref']!r}"]
    if key == "id":
        return [] if shape["id"] in ids.KINDS else \
            [f"{at}: no id kind {shape['id']!r} - one of {', '.join(ids.KINDS)}"]
    if key == "enum":
        lits = shape["enum"]
        if not isinstance(lits, list) or not lits:
            return [f"{at}: an enum needs at least one literal"]
        if any(kind_of(x) in ("array", "object") for x in lits):
            return [f"{at}: enum literals must be scalars"]
        return []
    if key == "array":
        return check_shape(shape["array"], shapes, f"{at}.array")
    fields = shape["object"]
    if not isinstance(fields, dict):
        return [f"{at}: 'object' maps field names to shapes"]
    out = [p for n, s in fields.items() for p in check_shape(s, shapes, f"{at}.{n}")]
    out += [f"{at}: optional names {n!r}, which is not a field"
            for n in shape.get("optional", ()) if n not in fields]
    if not isinstance(shape.get("open", False), bool):
        out.append(f"{at}: 'open' is true or false")
    return out


def fields_of(shape, shapes) -> dict:
    s = deref(shape, shapes)
    return s["object"] if isinstance(s, dict) and "object" in s else {}


BAD_REQUEST = "bad_request"


def missing_field(problem: str) -> bool:
    """Is *problem* a required field that is absent - as `check` words it?"""
    return ": missing required field " in problem


def refuse_body(route, body, shapes, own=()):
    """The first way *body* does not match *route*'s request shape, or None.
    A problem listed in *own* - a missing field the route refuses by a reason
    of its own - is left to that reason, so it is not reported here."""
    problems = [p for p in check(body, route["request"], shapes) if p not in own]
    return problems[0] if problems else None


def refuses_the_request(ex) -> bool:
    """Is *ex* an example of the refusal for a body that does not match its
    route's request shape? Such an example exists to show a request being
    refused, so its request must be one the shape refuses."""
    r = ex.get("response")
    try:
        status = int(ex.get("status", 0))
    except (TypeError, ValueError):
        return False
    return status >= 400 and isinstance(r, dict) and r.get("reason") == BAD_REQUEST


def path_matches(template: str, path: str) -> bool:
    """`/api/jobs/{id}/result` matches `/api/jobs/j-7/result`; a query is ignored."""
    pattern = re.sub(r"\\\{[a-z_]+\\\}", "[^/]+", re.escape(template))
    return re.fullmatch(pattern, path.split("?")[0]) is not None


def check_route(route, shapes, name) -> list:
    """A route is well-formed, keeps the refusal rule, and exemplifies itself."""
    if set(route) != ROUTE_KEYS:
        return [f"{name}: keys must be exactly {sorted(ROUTE_KEYS)}"]
    out = []
    if route["method"] not in ("GET", "POST"):
        out.append(f"{name}: method must be GET or POST")
    if not str(route["path"]).startswith("/"):
        out.append(f"{name}: path must start with /")
    if route["request"] is not None:
        out += check_shape(route["request"], shapes, f"{name}.request")
    responses = route["responses"]
    for status, shape in responses.items():
        where = f"{name}.responses.{status}"
        if not re.fullmatch(r"[1-5][0-9][0-9]", status):
            out.append(f"{where}: a status is three digits")
            continue
        out += check_shape(shape, shapes, where)
        fields = fields_of(shape, shapes)
        # THE REFUSAL RULE. A program must be able to tell WHY it was refused
        # without reading an English sentence, and a refusal must never be
        # disguised as a success.
        if int(status) >= 400:
            reason = fields.get("reason")
            if fields.get("error") != "string" or not (
                    isinstance(reason, dict) and "enum" in reason):
                out.append(f"{where}: a refusal carries `error: string` and "
                           f"`reason: {{enum}}`")
            elif BAD_REQUEST in reason["enum"] and route["request"] is None:
                out.append(f"{where}: `{BAD_REQUEST}` refuses a body, and this "
                           f"route takes none")
        elif "error" in fields:
            out.append(f"{where}: a success must not carry an `error` field")

    seen = {}
    for i, ex in enumerate(route["examples"]):
        where = f"{name}.examples[{i}] ({ex.get('name', '?')})"
        if not {"name", "status", "response"} <= set(ex) <= EXAMPLE_KEYS:
            out.append(f"{where}: an example has name, status, response, and "
                       f"optionally path and request")
            continue
        status = str(ex["status"])
        if status not in responses:
            out.append(f"{where}: status {status} is not declared")
            continue
        if "path" in ex and not path_matches(route["path"], ex["path"]):
            out.append(f"{where}: path {ex['path']!r} is not {route['path']!r}")
        out += [f"{where} response {p}" for p in check(ex["response"], responses[status], shapes)]
        if route["request"] is None and "request" in ex:
            out.append(f"{where}: this route takes no request body")
        if route["request"] is not None:
            if "request" not in ex:
                out.append(f"{where}: this route takes a request body")
            elif refuses_the_request(ex):
                # The one example whose request must NOT match: it shows a
                # body being refused for its shape, and its `error` is the
                # sentence a server answers with - the first problem, exactly
                # as this validator words it.
                problems = check(ex["request"], route["request"], shapes)
                if not problems:
                    out.append(f"{where}: a {BAD_REQUEST} example needs a request "
                               f"its route's shape refuses")
                elif ex["response"].get("error") != problems[0]:
                    out.append(f"{where}: its error must be the first problem, "
                               f"{problems[0]!r}")
            else:
                problems = check(ex["request"], route["request"], shapes)
                if int(status) >= 400:
                    # A refusal may be missing a field - that can be the very
                    # reason it was refused - but what it carries must be of
                    # the right kinds.
                    problems = [p for p in problems if not missing_field(p)]
                out += [f"{where} request {p}" for p in problems]
        if int(status) >= 400 and isinstance(ex["response"], dict):
            seen.setdefault(status, set()).add(ex["response"].get("reason"))

    # COVERAGE: the examples are the client's fixtures, so every answer the
    # client must handle - every status, every reason - has one.
    for status, shape in responses.items():
        if not any(str(ex.get("status")) == status for ex in route["examples"]):
            out.append(f"{name}: status {status} has no example")
        if re.fullmatch(r"[4-5][0-9][0-9]", status):
            # a `reason` that is not an enum was reported above; it lists nothing
            reason = fields_of(shape, shapes).get("reason")
            for r in (reason.get("enum", []) if isinstance(reason, dict) else []):
                if r not in seen.get(status, set()):
                    out.append(f"{name}: refusal {status} {r!r} has no example")
    return out


def load(root=HERE):
    def read(p):
        return json.loads(p.read_text(encoding="utf-8"))
    shapes = {p.stem: read(p) for p in sorted((root / "shapes").glob("*.json"))}
    routes = {f"{p.parent.name}/{p.stem}": read(p)
              for p in sorted((root / "routes").glob("*/*.json"))}
    return shapes, routes


# ---------------------------------------------------- the contract's version

#: What the contract IS, for its version: every route and shape, what a part
#: answers before any route (origins), what the parts share on the machine
#: outside HTTP (machine: the card lock), the id grammar, the two validators
#: and the one memory check - what a server, a client and a coordinator each
#: run against. Not its README nor its self-test: words about the contract,
#: not the contract.
VERSIONED = ("routes/*/*.json", "shapes/*.json", "origins/*.json", "machine/*.json", "ids.py",
             "ids.js", "validate.py", "validate.js", "memory.py")


def contract_version(root=HERE) -> str:
    """The contract's version: a short hash of its files (VERSIONED) - the
    first 12 hex digits of a sha256 over one line per file, in path order:
    its path in the contract, with `/`, and the sha256 of its bytes. Where
    the contract lies changes nothing; a byte of any of those files, or one
    of them added, renamed or removed, changes it. A server's health carries
    it, so a client - or the coordinator - can tell whether both run the same
    contract."""
    root = Path(root)
    files = {p.relative_to(root).as_posix(): p for pattern in VERSIONED
             for p in root.glob(pattern) if p.is_file()}
    h = hashlib.sha256()
    for rel in sorted(files):
        h.update(f"{rel}\0{hashlib.sha256(files[rel].read_bytes()).hexdigest()}\n"
                 .encode("utf-8"))
    return h.hexdigest()[:12]


def load_origins(root=HERE) -> dict:
    """What each part answers before any route, by part: origins/<part>.json."""
    return {p.stem: json.loads(p.read_text(encoding="utf-8"))
            for p in sorted((root / "origins").glob("*.json"))}


def check_origins(doc, shapes, name) -> list:
    """What a part answers BEFORE any route - for the name the request was
    sent to (its Host), the page that sent it (its Origin), its body's size
    and how its body was sent: only refusals - a request let through is
    answered by its route - and each example says what of the request it
    answers: its `origin`, its `host`, its body's `length`, its
    `transfer_encoding`, one of them at least, each of its own kind.
    Everything else is what a route that takes no body keeps: the
    refusal rule, and an example for every status and every reason. The
    method and the path it is checked under are placeholders: the check
    comes before either is looked at."""
    if not isinstance(doc, dict) or set(doc) != ORIGIN_KEYS:
        return [f"{name}: keys must be exactly {sorted(ORIGIN_KEYS)}"]
    out = [f"{name}.responses.{status}: before any route there are only refusals - "
           f"a request let through is answered by its route"
           for status in doc["responses"] if not re.fullmatch(r"[4-5][0-9][0-9]", status)]
    examples = []
    for i, ex in enumerate(doc["examples"]):
        where = f"{name}.examples[{i}] ({ex.get('name', '?') if isinstance(ex, dict) else '?'})"
        if not (isinstance(ex, dict)
                and ORIGIN_EXAMPLE_KEYS <= set(ex) <= ORIGIN_EXAMPLE_KEYS | set(BEFORE_ANY_ROUTE)):
            out.append(f"{where}: an example has name, status and response, what of the request "
                       f"it answers - its origin, host, length or transfer_encoding - and nothing "
                       f"else")
            continue
        wrong = []
        if not any(k in ex for k in BEFORE_ANY_ROUTE):
            wrong.append(f"{where}: an example names what of the request it answers: the "
                         f"Origin it answers, the Host it was sent to, its body's length, or the "
                         f"Transfer-Encoding it was sent with")
        if "origin" in ex and not isinstance(ex["origin"], str):
            wrong.append(f"{where}: the Origin it answers is a string")
        if "host" in ex and not isinstance(ex["host"], str):
            wrong.append(f"{where}: the Host it was sent to is a string")
        if "length" in ex and (check(ex["length"], "integer") or ex["length"] < 0):
            wrong.append(f"{where}: its body's length is a whole number of bytes")
        if "transfer_encoding" in ex and not isinstance(ex["transfer_encoding"], str):
            wrong.append(f"{where}: the Transfer-Encoding it was sent with is a string")
        if wrong:
            out += wrong
            continue
        examples.append({k: v for k, v in ex.items() if k not in BEFORE_ANY_ROUTE})
    return out + check_route({"id": doc["id"], "method": "GET", "path": "/",
                              "summary": doc["summary"], "request": None,
                              "responses": doc["responses"], "examples": examples},
                             shapes, name)


def load_machine(root=HERE) -> dict:
    """What the parts share on the machine, outside HTTP, by name:
    machine/<name>.json - the card lock (`card-lock`)."""
    return {p.stem: json.loads(p.read_text(encoding="utf-8"))
            for p in sorted((root / "machine").glob("*.json"))}


def check_machine(doc, shapes, name) -> list:
    """A file the parts share on the machine: its `file` and its `note_file`
    - plain names, the note's beside the lock's - the shape of the note its
    holder writes, and examples, each a note the shape takes."""
    if not isinstance(doc, dict) or set(doc) != MACHINE_KEYS:
        return [f"{name}: keys must be exactly {sorted(MACHINE_KEYS)}"]
    out = []
    for key in ("file", "note_file"):
        if not (isinstance(doc[key], str) and doc[key]
                and re.fullmatch(r"[A-Za-z0-9._-]+", doc[key])):
            out.append(f"{name}.{key}: a plain file name, with no folder in it")
    if not out and not doc["note_file"].startswith(doc["file"] + "."):
        out.append(f"{name}.note_file: the note is written beside the file it is about, "
                   f"named after it")
    out += check_shape(doc["note"], shapes, f"{name}.note")
    examples = doc["examples"] if isinstance(doc["examples"], list) else []
    if not examples:
        out.append(f"{name}: a note needs at least one example")
    for i, ex in enumerate(examples):
        where = f"{name}.examples[{i}] ({ex.get('name', '?') if isinstance(ex, dict) else '?'})"
        if not isinstance(ex, dict) or set(ex) != MACHINE_EXAMPLE_KEYS:
            out.append(f"{where}: an example has a name and a note, and nothing else")
            continue
        out += [f"{where} note {p}" for p in check(ex["note"], doc["note"], shapes)]
    return out


def check_contract(root=HERE):
    """-> (problems, shapes, routes). Loads everything, checks everything."""
    shapes, routes = load(root)
    out = []
    for n, s in shapes.items():
        out += check_shape(s, shapes, f"shapes/{n}")
    ids, doors = {}, {}
    for key, route in routes.items():
        out += check_route(route, shapes, key)
        rid, door = route.get("id"), (route.get("method"), route.get("path"))
        if rid in ids:
            out.append(f"{key}: id {rid!r} is also {ids[rid]}")
        if door in doors:
            out.append(f"{key}: {door[0]} {door[1]} is also {doors[door]}")
        ids[rid], doors[door] = key, key
    for part, doc in load_origins(root).items():
        out += check_origins(doc, shapes, f"origins/{part}")
    for part, doc in load_machine(root).items():
        out += check_machine(doc, shapes, f"machine/{part}")
    return out, shapes, routes


def main() -> int:
    problems, shapes, routes = check_contract()
    for p in problems:
        print(p)
    if problems:
        return 1
    n = sum(len(r["examples"]) for r in routes.values())
    before, shared = load_origins(), load_machine()
    print(f"contract: {len(routes)} routes, {n} examples, "
          f"{len(shapes)} named shapes, what {len(before)} part(s) answer before any route, "
          f"{len(shared)} file(s) the parts share on the machine - well-formed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

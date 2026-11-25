"""The ids every part of llm-bootstrap mints and reads. Standard library only.

    python ids.py          mint one of each kind

AN ID NAMES, IT NEVER ROUTES. A reply always comes back on the connection that
asked for it. What an id does is name the thing that is asked about LATER: a
job to poll, a generation to ask about after a restart, a ticket to wait on, a
request to trace through every part. Two actors that can mint the same name
can act on each other's things - so every id that can reach a second actor is
unique beyond the one that minted it. (A ticket is acted on only with its
secret token: its id names it, and never proves who holds it.)

    actor        srv-k3x9m2p7               a running thing, minted when it starts:
                                            a server (srv), a client session (cli),
                                            a coordinator (crd), one load of a model
                                            (load)
    correlation  c-20260923-140012-a3k9m2p7  one request, minted where it starts - by
                                            the client. A server mints one only for a
                                            request that came without, and says so.
    job          j-0007                     one job, numbered inside its folder
    handle       srv-k3x9m2p7:j-0007#01     one generation, named by the server that
                                            ran it, so a model server two servers
                                            share cannot mistake one's for the other's
    ticket       t-0042-k3x9                one ticket for the card: an application's
                                            place in the coordinator's line, and its
                                            rental once claimed. The tail means a
                                            coordinator that lost its record cannot
                                            hand out an old ticket's id again. (The
                                            turns tickets replaced had this grammar,
                                            so an old record's turn ids are read as
                                            ticket ids, and one counter runs on.)
    pipeline     pipe-k3x9m2p7              one pipeline of a server: the card held
                                            from its open to its close, over as many
                                            tickets as it takes. Renewed and closed
                                            by this id; it is no actor, so it holds
                                            nothing in its own name.

The random part is drawn from 32 letters no one misreads for another (no l, o,
0 or 1); eight of them are 40 bits. Digits are written [0-9], never \\d, which
in Python also matches digits of other scripts and in JavaScript does not.

`ids.js` is the same rules for the browser and must agree character for
character: `test_contract.py` runs both over one table. The shape language
reads these kinds as `{"id": "<kind>"}` (see validate.py), so every part's
suite checks every id it answers with.
"""
from __future__ import annotations

import re
import secrets
import sys
import time

ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"
ROLES = ("srv", "cli", "crd", "load")
_L = "[a-km-np-z2-9]"
PATTERNS = {
    "actor": f"(?:srv|cli|crd|load)-{_L}{{8}}",
    "correlation": f"c-[0-9]{{8}}-[0-9]{{6}}-{_L}{{8}}",
    "job": "j-[0-9]{4,}",
    "handle": f"(srv-{_L}{{8}}):(j-[0-9]{{4,}})#([0-9]{{2,}})",
    "ticket": f"t-([0-9]{{4,}})-{_L}{{4}}",
    "pipeline": f"pipe-{_L}{{8}}",
}
KINDS = tuple(PATTERNS)
#: how a problem names what was expected
SHOWN = {
    "actor": "an actor id",
    "correlation": "a correlation id",
    "job": "a job id",
    "handle": "a handle",
    "ticket": "a ticket id",
    "pipeline": "a pipeline id",
}
_RE = {k: re.compile(p) for k, p in PATTERNS.items()}


def _draw() -> int:
    return secrets.randbelow(len(ALPHABET))


def _letters(n: int, draw=None) -> str:
    draw = draw or _draw
    return "".join(ALPHABET[draw() % len(ALPHABET)] for _ in range(n))


def matches(kind: str, value) -> bool:
    """Is *value* an id of *kind*? A kind that does not exist is a mistake
    in the caller, and says so."""
    if kind not in _RE:
        raise ValueError(f"no id kind {kind!r}")
    return isinstance(value, str) and _RE[kind].fullmatch(value) is not None


def kind_of(value):
    """The one kind *value* is an id of, or None. The kinds cannot overlap:
    each begins differently."""
    return next((k for k in KINDS if matches(k, value)), None)


def mint_actor(role: str, draw=None) -> str:
    if role not in ROLES:
        raise ValueError(f"no actor role {role!r}: one of {', '.join(ROLES)}")
    return f"{role}-{_letters(8, draw)}"


def mint_correlation(now=None, draw=None) -> str:
    """One request's id. The time is local, as a person reading a log reads
    a clock."""
    now = time.time() if now is None else now
    return time.strftime("c-%Y%m%d-%H%M%S-", time.localtime(now)) + _letters(8, draw)


def mint_ticket(seq: int, draw=None) -> str:
    return f"t-{int(seq):04d}-{_letters(4, draw)}"


def mint_pipeline(draw=None) -> str:
    """One pipeline's id, minted when it opens."""
    return f"pipe-{_letters(8, draw)}"


def handle(server: str, job: str, sample: int) -> str:
    """One generation's name: the server that ran it, its job, its sample."""
    h = f"{server}:{job}#{int(sample):02d}"
    if not matches("handle", h):
        raise ValueError(f"not a handle: {h!r} - the server must be a srv- actor id "
                         f"and the job a job id")
    return h


def parse_handle(value):
    """-> {"server", "job", "sample"} for a handle, or None."""
    m = _RE["handle"].fullmatch(value) if isinstance(value, str) else None
    return None if m is None else {"server": m.group(1), "job": m.group(2),
                                   "sample": int(m.group(3))}


def ticket_seq(value):
    """The sequence number inside a ticket id, or None."""
    m = _RE["ticket"].fullmatch(value) if isinstance(value, str) else None
    return None if m is None else int(m.group(1))


if __name__ == "__main__":
    for role in ROLES:
        print(mint_actor(role))
    srv = mint_actor("srv")
    print(mint_correlation(), handle(srv, "j-0007", 1), mint_ticket(42), mint_pipeline())
    sys.exit(0)

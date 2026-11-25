"""THE ONE MEMORY CHECK: does what is asked for fit on the card, beside the
margin kept free? (PLAN-4 D2, D5.) Standard library only.

    import memory
    memory.fit(9000, free_mib=20000, total_mib=24576, margin_mib=256)
      -> {"verdict": "fits", "need_mib": 9000, "free_mib": 20000,
          "margin_mib": 256, "total_mib": 24576}

WHERE IT RUNS: wherever the card is decided. In the coordinator, before it
grants a rental - the ticket's declared needs - and before it approves a
model a rental adds (`/card/amend`); in a server run on its own, holding the
machine's lock, before each load. The parts share no code: they share this
contract, the one part every other part imports. So the rule lives here,
once, and a coordinator and a server run on its own never judge one card by
two rules.

THE RULE, in MiB:
  fits        need + margin <= free: it goes on.
  no_room     need + margin > free: not now - something on the card holds
              the rest, and waiting may help.
  never_fits  need + margin > total: it would not fit an EMPTY card by the
              same rule, so no wait can help, whatever the request's class.
  unknown     the card's free memory could not be read: nothing is decided
              and nothing is refused - a missing instrument must never
              become an outage. Said, never hidden.
A card whose total is not known can still say `no_room`; only `never_fits`
needs the total. A need nobody declared (None) adds nothing: the margin alone
is checked.

THE MARGIN is a setting of whoever decides (PLAN-4 D0). MARGIN_MIB is its
starting default: the free memory under which a rental is degraded (D0's
floor, 256 MiB), so a grant never puts a rental in that state from its first
second.

Every refusal says its numbers (`words`): what was needed, the margin, what
the card had free, the whole card. The numbers are the card's; the words
name nobody who holds the rest (D4). A `no_room` - a card that is not
over-full, and a model that does not fit on it now - FAILS AT ONCE, LOUDLY
(the owner, 2026-09-28): it never waits for room, and its words
(`no_room_words`) add what programs outside the bootstrap hold, and what to
do - close programs, or reboot. SCHEDULED WORK may wait for room instead,
by a setting (S18, the owner's second ruling of 2026-09-28): while it waits
it says the same numbers (`room_words`), and a patience that runs out while
it waits fails in the same loud words, saying how long it waited
(`no_room_words(..., waited_s=)`).

THE AMEND'S ANSWER is waited for AMEND_ANSWER_S by the server that asked,
and one it never hears decides nothing: the model goes on, unverified. Right
after one of the rental's own processes went, a coordinator waits in the
amend for the memory to come back - its `amend_settle_s` - and reads the card
once more. So the two are held together here: a coordinator's settle is at
most AMEND_SETTLE_MAX_S, and the rest of AMEND_ANSWER_S is that last reading
of the card and the answer's way back.
"""
from __future__ import annotations

#: the free memory kept beside a need, unless whoever decides says otherwise
MARGIN_MIB = 256
#: how long a server waits for an amend's answer, in seconds
AMEND_ANSWER_S = 30.0
#: the longest a coordinator's amend may wait for a memory just given back
AMEND_SETTLE_MAX_S = 15.0
#: what `fit` answers, in its `verdict`
VERDICTS = ("fits", "no_room", "never_fits", "unknown")


def _mib(value, name: str, may_be_none: bool):
    """A number of MiB: a whole number, 0 or more - or None where allowed."""
    if value is None and may_be_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value \
            or value < 0 or value == float("inf") or not float(value).is_integer():
        raise ValueError(f"{name} is a whole number of MiB, 0 or more, not {value!r}")
    return int(value)


def fit(need_mib, free_mib, total_mib, margin_mib=MARGIN_MIB) -> dict:
    """The verdict on *need_mib* - None: nothing declared - against a card
    with *free_mib* free of *total_mib* (either None: not read), keeping
    *margin_mib* free beside it. -> {"verdict", "need_mib", "free_mib",
    "margin_mib", "total_mib"}: the verdict, and the numbers it was made on."""
    need = _mib(need_mib, "need_mib", True) or 0
    free = _mib(free_mib, "free_mib", True)
    total = _mib(total_mib, "total_mib", True)
    margin = _mib(margin_mib, "margin_mib", False)
    if free is None:
        verdict = "unknown"
    elif total is not None and need + margin > total:
        verdict = "never_fits"
    elif need + margin > free:
        verdict = "no_room"
    else:
        verdict = "fits"
    return {"verdict": verdict, "need_mib": need, "free_mib": free, "margin_mib": margin,
            "total_mib": total}


def numbers(verdict: dict) -> dict:
    """The numbers alone, as an answer carries them (the contract's `memory`)."""
    return {k: verdict[k] for k in ("need_mib", "free_mib", "margin_mib", "total_mib")}


def words(verdict: dict) -> str:
    """The numbers in words - what was needed, the margin, what the card had
    - naming nobody."""
    said = f"it needs {verdict['need_mib']} MiB and {verdict['margin_mib']} MiB kept free beside it"
    if verdict["free_mib"] is None:
        return f"{said}; the card's memory could not be read"
    if verdict["verdict"] == "never_fits":
        return f"{said}, and the whole card is {verdict['total_mib']} MiB"
    whole = f" of {verdict['total_mib']}" if verdict["total_mib"] is not None else ""
    return f"{said}; the card has {verdict['free_mib']} MiB free{whole}"


#: what a person does when a model does not fit on a card that is not
#: over-full: nothing waits for the room (the owner, 2026-09-28)
NO_ROOM_ADVICE = ("It does not wait for room: memory a program holds is almost never given back "
                  "while it runs. Close programs that use the card, or reboot, then ask again")
#: ...and when nothing outside the bootstrap holds any of it
NO_ROOM_OURS = ("It does not wait for room: this application's own models hold the rest. "
                "Unload one it keeps, then ask again")


#: ...after scheduled work waited for room within its patience, and none came
#: (S18): the same advice, saying how long it waited
NO_ROOM_WAITED = ("It waited {waited} for room, within its patience, and none came: memory a "
                  "program holds is almost never given back while it runs. Close programs that "
                  "use the card, or reboot, then ask again")
NO_ROOM_WAITED_OURS = ("It waited {waited} for room, within its patience, and none came: this "
                       "application's own models hold the rest. Unload one it keeps, then ask "
                       "again")


def span(seconds) -> str:
    """How long, as a person reads it: seconds under two minutes, minutes
    under two hours, then hours and minutes."""
    s = max(0.0, float(seconds))
    if s < 120:
        return f"{int(round(s))} s"
    if s < 7200:
        return f"{int(round(s / 60))} min"
    m = int(round(s / 60))
    return f"{m // 60} h {m % 60} min"


def _outside_words(outside_mib) -> tuple:
    """-> (what programs outside the bootstrap hold, in words; how much - None
    not known)."""
    if outside_mib is None:
        return "how much of it programs outside the bootstrap hold is not known", None
    outside = _mib(outside_mib, "outside_mib", False)
    if outside == 0:
        return "programs outside the bootstrap hold none of it", 0
    return f"programs outside the bootstrap hold {outside} MiB of it", outside


def no_room_words(verdict: dict, outside_mib=None, waited_s=None) -> str:
    """A MODEL THAT DOES NOT FIT ON A CARD THAT IS NOT OVER-FULL FAILS AT ONCE,
    LOUDLY - the owner's ruling of 2026-09-28: "Fail loudly. Memory almost
    never ever cleans itself up and if it does it will be after hours when a
    program is shut down." One sentence for every part that refuses one, so
    a person reads the same words wherever they look: the numbers - what is
    needed with the margin, what the card has free of its whole - what
    programs outside the bootstrap hold (*outside_mib*, None: not known), and
    what to do. It names nobody (D4).

    SCHEDULED WORK THAT WAITED FOR ROOM (S18; the owner's second ruling of
    2026-09-28) and whose patience ran out fails in the same words -
    *waited_s*, how long it waited, said in place of "it does not wait":
    never only that its patience ran out."""
    said = words(verdict)
    outside_said, outside = _outside_words(outside_mib)
    if waited_s is not None:
        advice = (NO_ROOM_WAITED_OURS if outside == 0 else NO_ROOM_WAITED).format(
            waited=span(waited_s))
    else:
        advice = NO_ROOM_OURS if outside == 0 else NO_ROOM_ADVICE
    return f"{said}; {outside_said}. {advice}"


def room_words(verdict: dict, outside_mib=None) -> str:
    """WHILE SCHEDULED WORK WAITS FOR ROOM (S18): the same numbers - what is
    needed, what the card has free of its whole, what programs outside the
    bootstrap hold - and that it starts by itself once it fits, and what makes
    the room. It names nobody (D4)."""
    said = words(verdict)
    outside_said, outside = _outside_words(outside_mib)
    if outside == 0:
        return (f"{said}; {outside_said}: this application's own models hold the rest. It "
                f"waits for room, and starts by itself once it fits - once one of them comes off")
    return (f"{said}; {outside_said}. It waits for room, and starts by itself once it fits: "
            f"close programs that use the card to make room")

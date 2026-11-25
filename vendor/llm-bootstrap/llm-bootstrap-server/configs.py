"""A folder's kept configurations: content-addressed, and one in use.

    <folder>/client/configs/<sha256>.json   a configuration, as its canonical
                                            JSON - keys sorted, no spaces,
                                            UTF-8 - named by the hash of
                                            exactly those bytes
    <folder>/client/configs/current.json    {"sha256": ...}: the one in use

A configuration is the application's own object: the server never reads into
it. It is kept whole or not at all - written under a temporary name and
swapped in - and the same configuration kept twice is one file. Reading it
back checks the bytes against the name: a file changed after it was kept is
said so, never handed out as the one that was kept.

Several named configurations are a seam, not built: more files, and a name
each.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

LIMIT = 64 * 1024
WHERE = ("client", "configs")
CURRENT = "current.json"


class ConfigUnreadable(Exception):
    """The folder names a configuration it cannot give back whole."""


class ConfigTooLarge(ValueError):
    """Over LIMIT bytes as canonical JSON."""


def canonical(config) -> bytes:
    return json.dumps(config, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _dir(folder) -> Path:
    return Path(folder).joinpath(*WHERE)


def _write_whole(path: Path, data: bytes) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def keep(folder, config) -> dict:
    """Keep *config* and use it. -> {"sha256", "new"}. ConfigTooLarge over
    the limit; OSError when the folder cannot be written."""
    data = canonical(config)
    if len(data) > LIMIT:
        raise ConfigTooLarge(f"a configuration is at most {LIMIT} bytes as JSON; "
                             f"this one is {len(data)}")
    sha = hashlib.sha256(data).hexdigest()
    where = _dir(folder)
    where.mkdir(parents=True, exist_ok=True)
    path = where / f"{sha}.json"
    new = not path.is_file()
    if new:
        _write_whole(path, data)
    _write_whole(where / CURRENT, json.dumps({"sha256": sha}).encode("utf-8"))
    return {"sha256": sha, "new": new}


def current(folder) -> dict:
    """The configuration in use. -> {"config", "sha256"}, both None when
    nothing has been kept. ConfigUnreadable when what is named cannot be
    given back whole."""
    where = _dir(folder)
    try:
        raw = (where / CURRENT).read_bytes()
    except FileNotFoundError:
        return {"config": None, "sha256": None}
    except OSError as exc:
        raise ConfigUnreadable(f"configs/{CURRENT} cannot be read: {exc}") from None
    try:
        sha = json.loads(raw.decode("utf-8"))["sha256"]
        if not (isinstance(sha, str) and len(sha) == 64 and all(c in "0123456789abcdef" for c in sha)):
            raise ValueError(sha)
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        raise ConfigUnreadable(f"configs/{CURRENT} does not name a configuration") from None
    try:
        data = (where / f"{sha}.json").read_bytes()
    except OSError:
        raise ConfigUnreadable(f"configs/{sha}.json, the one in use, is not there") from None
    if hashlib.sha256(data).hexdigest() != sha:
        raise ConfigUnreadable(f"configs/{sha}.json does not hash to its name: it was changed "
                               "after it was kept")
    try:
        config = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ConfigUnreadable(f"configs/{sha}.json is not JSON") from None
    return {"config": config, "sha256": sha}

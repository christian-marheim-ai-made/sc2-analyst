"""Build the client from its parts: the kit, and the bootstrap's own page.

    python build.py              write llm-bootstrap-kit.js and index.html
    python build.py --check      exit 1 if either is not what the parts build
    python build.py --print      the page the parts build, to stdout - what the rows run
    python build.py --print-kit  the kit the parts build, to stdout

`parts/MANIFEST` names each file built - `[its name]` - and under it the
parts it is put together from, in order, which is the order their scripts
run. A part is:

    <name>.html            put in the page as it is
    <name>.css             in the page: inside one <style> with the css parts
                           around it. In the kit: its look, which the kit puts
                           in the page when the first thing is placed
    <name>.js              inside one <script> with the js parts around it
    @contract/<file>.js    a file of the contract, wrapped in a function of its
                           own so its names stay its own; it publishes itself
                           on `globalThis` (`llmIds`, `llmContract`)
    @routes                generated: every server route of the contract, by
                           id, and the named shapes - a widget names a route by
                           its contract id, never by a path written by hand
    @kit                   the kit, byte for byte, as one script of the page

THE KIT is one script an application's page includes: every part of it inside
one function, so its names are its own and never the page's - an
application's page has names of its own. It gives the page `llmKit` (its
calls) and `llmClient` (its door, for rows and a console), and the contract
gives `llmIds` and `llmContract`. The bootstrap's own page runs the kit byte
for byte, then places its widgets with the kit's own calls.

A part that goes inside a script is refused if it holds `</script`: the
browser would end the script there and cut the page, silently.

Nothing in the build reads the clock or the machine: the same parts always
build the same bytes, and a change that should change nothing can be proven
by them.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PARTS = HERE / "parts"
CONTRACT = HERE.parent / "llm-bootstrap-contract"
KIT = "llm-bootstrap-kit.js"
PAGE = "index.html"

#: the kit's first words: what it is, and how a page takes it
KIT_HEAD = """/* llm-bootstrap-kit.js - the widgets of llm-bootstrap, for a page of your own.
   Built by build.py from parts/ (MANIFEST): never edited by hand.

   A page names its bootstrap server once, in a script before this one
   (window.llmServer = 'http://127.0.0.1:7072'; named nowhere, the page's own
   address is asked), includes this file as a script, and then places what it
   wants shown:

     llmKit.place('bench', document.getElementById('bench'));
     llmKit.container('bottom', { name: 'progress', slots: { middle: ['queue'] } });

   The server must allow the page's address (server.py --allow-origin). The
   calls are described in kit.js, the last part. */
"""


def manifest() -> dict:
    """-> {file built: [its parts, in order]}"""
    out, into = {}, None
    for line in (PARTS / "MANIFEST").read_bytes().decode("utf-8").splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1]
            if name not in (KIT, PAGE):
                raise SystemExit(f"parts/MANIFEST: [{name}] is not a file this builds ({KIT}, {PAGE})")
            into = out.setdefault(name, [])
        elif into is None:
            raise SystemExit(f"parts/MANIFEST: {line} is under no [file]")
        else:
            into.append(line)
    for name in (KIT, PAGE):
        if name not in out:
            raise SystemExit(f"parts/MANIFEST: it names no [{name}]")
    return out


def routes_part() -> str:
    """Every server route the contract declares, by id, and the named shapes -
    written in a fixed order, so the bytes never depend on the file system's."""
    routes = {}
    for f in sorted((CONTRACT / "routes" / "server").glob("*.json")):
        r = json.loads(f.read_bytes().decode("utf-8"))
        routes[r["id"]] = {"method": r["method"], "path": r["path"],
                           "request": r["request"], "responses": r["responses"]}
    shapes = {f.stem: json.loads(f.read_bytes().decode("utf-8"))
              for f in sorted((CONTRACT / "shapes").glob("*.json"))}
    dump = lambda v: json.dumps(v, sort_keys=True, ensure_ascii=False)   # noqa: E731
    return (f"/* generated from the contract - never edited by hand */\n"
            f"const ROUTES = {dump(routes)};\n"
            f"const SHAPES = {dump(shapes)};\n")


def part_text(name: str) -> tuple:
    """-> (kind, text) for one line of the manifest."""
    if name == "@routes":
        return "js", routes_part()
    if name.startswith("@contract/"):
        src = (CONTRACT / name[len("@contract/"):]).read_bytes().decode("utf-8")
        return "js", f"(function () {{\n{src}\n}}).call(globalThis);\n"
    text = (PARTS / name).read_bytes().decode("utf-8")
    kind = Path(name).suffix.lstrip(".")
    if kind not in ("html", "css", "js"):
        raise SystemExit(f"parts/MANIFEST: {name} is not .html, .css or .js")
    return kind, text


def lined(text: str) -> str:
    return text if text.endswith("\n") else text + "\n"


#: what ends an inline script, as a browser reads it: `</script` in any case
SCRIPT_END = re.compile(r"</script", re.IGNORECASE)


def scripted(name: str, text: str) -> str:
    """A part that goes inside a <script>, refused if it holds what ends one:
    the page would be cut there, silently, in a browser as in the rows."""
    if SCRIPT_END.search(text):
        where = name if name.startswith("@") else f"parts/{name}"
        raise SystemExit(f"{where} holds </script>, which ends the page's script there and cuts "
                         "the page - even in a comment. In a string, write it <\\/script>")
    return text


def build_kit(parts: list) -> str:
    """The kit: its look as one string, and every script part inside one
    function - its names its own, never the page's."""
    css, js = [], []
    for name in parts:
        if name == "@kit":
            raise SystemExit("parts/MANIFEST: the kit cannot hold itself (@kit)")
        kind, text = part_text(name)
        if kind == "html":
            raise SystemExit(f"parts/MANIFEST: {name} is html, and the kit is a script")
        if kind == "css":
            css.append(lined(scripted(name, text)))
        else:
            js.append(f"/* ---- {name} ---- */\n{lined(scripted(name, text))}")
    look = json.dumps("".join(css), ensure_ascii=False)
    return (f"{KIT_HEAD}(function () {{\n'use strict';\n"
            f"/* the kit's look: the css parts, put in the page by kit.js */\nconst KIT_CSS = {look};\n"
            f"{''.join(js)}}}());\n")


def build_page(parts: list, kit: str) -> str:
    """The bootstrap's own page: its markup and look, the kit byte for byte,
    and its own script."""
    out, block = [], None                       # block: the kind of the open <style>/<script>
    for name in parts:
        kind, text = ("kit", kit) if name == "@kit" else part_text(name)
        if kind != block and block is not None:
            out.append("</style>\n" if block == "css" else "</script>\n")
            block = None
        if kind == "kit":
            out.append(f"<script>\n{text}</script>\n")
            continue
        if kind in ("css", "js") and block is None:
            out.append("<style>\n" if kind == "css" else "<script>\n'use strict';\n")
            block = kind
        if kind == "js":
            out.append(f"/* ---- {name} ---- */\n")
            scripted(name, text)
        out.append(lined(text))
    if block is not None:
        out.append("</style>\n" if block == "css" else "</script>\n")
    return "".join(out)


def build() -> dict:
    """-> {file name: its text}"""
    parts = manifest()
    kit = build_kit(parts[KIT])
    built = {KIT: kit, PAGE: build_page(parts[PAGE], kit)}
    for name, text in built.items():
        if "\r" in text:
            raise SystemExit(f"{name}: a part has CRLF line ends - every file here is LF")
    return built


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="build the client's kit and page from its parts")
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if the kit or the page is not what the parts build")
    ap.add_argument("--print", action="store_true", dest="print_",
                    help="write the page the parts build to stdout, and nothing else")
    ap.add_argument("--print-kit", action="store_true",
                    help="write the kit the parts build to stdout, and nothing else")
    a = ap.parse_args(argv)
    built = {name: text.encode("utf-8") for name, text in build().items()}
    if a.print_ or a.print_kit:
        sys.stdout.buffer.write(built[PAGE if a.print_ else KIT])
        return 0
    if a.check:
        stale = [name for name, raw in built.items()
                 if not (HERE / name).exists() or (HERE / name).read_bytes() != raw]
        for name in stale:
            print(f"{name} is not what the parts build: run `python build.py`")
        if not stale:
            print(f"{KIT} and {PAGE} are what the parts build")
        return 1 if stale else 0
    for name, raw in built.items():
        (HERE / name).write_bytes(raw)
        print(f"{name}: {len(raw):,} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())

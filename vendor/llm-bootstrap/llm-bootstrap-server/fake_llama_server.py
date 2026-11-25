"""A stand-in for llama.cpp's llama-server: its command line, its routes, no model.

    python fake_llama_server.py -m <gguf> --host 127.0.0.1 --port <n> --alias <load id>
                                [--api-key <key>] [llama-server's other flags] [--fake-...]

It exists so the llama.cpp backend - the launch, the health wait, the
identity check, the count, the stream, a cancel, the stop, the kill-on-close
job, a startup check's reclaim - can be proven with no GPU, no weights and no
llama.cpp build. It speaks

    GET  /health                503 while "loading", then 200 {"status": "ok"} (and
                                /v1/health)
    GET  /v1/models             {"data": [{"id": <alias>, "meta": {"n_ctx": <its
                                slot's context>, ...}}]}
    GET  /props                 {"default_generation_settings": {"n_ctx": <its
                                slot's context>}, ...}
    POST /apply-template        {"prompt": <the conversation, rendered with its
                                chat template>}
    POST /tokenize              {"tokens": [<ids>]} - or [{"id", "piece"}] with
                                "with_pieces"
    POST /v1/chat/completions   server-sent events: deterministic tokens, then
                                usage and timings, then [DONE]

and it behaves like llama-server where the backend depends on it: an argument
it does not know ends it with "error: invalid argument: <arg>"; a port it
cannot bind ends it; before any route - a path no route has included - it
checks what b10724 checks, in b10724's order: while it loads, every request
is answered 503 "Loading model"; then, with --api-key, every one but /health
and /v1/health that does not carry the key is refused 401; and a request as
long as its context or
longer is refused 400, in the JSON the real llama-server answered on the real
card. The request is the whole conversation - a system message and every
turn before the last count against the context, and are part of what it
answers.

ITS TEMPLATE AND ITS TOKENIZER are its own, deterministic and simple, so a
row can count a request by hand - make one too long on purpose, or exactly
as long as its context. The routes' shapes and defaults are llama-server
b10724's, read from its source (tools/server/server-context.cpp):

    the template   each message is "<|role|>\n" + its content + "<|end|>\n";
                   then, unless add_generation_prompt is false, "<|assistant|>\n"
                   - followed, when chat_template_kwargs' enable_thinking is
                   false, by an empty thinking block, "<think>\n\n</think>\n\n",
                   as a thinking model's template writes one
    the tokenizer  a special token ("<|user|>", "<|end|>" ...) is one token -
                   unless parse_special is false (default true), when it is the
                   three pieces it is spelled with, "<|", "user", "|>"; every
                   other word, split at whitespace, is one; add_special
                   (default false) puts "<s>" first
    the chat route counts its request as llama-server does: rendered with the
                   template, tokenized with add_special and parse_special - so
                   /apply-template, then /tokenize with both, is its own count
    the context    its slot's, answered by /props: the -c it was started with,
                   or --fake-n-ctx (a model's training context, or parallel
                   slots, give llama-server's slot a context other than -c).
                   A prompt of the whole context or more is refused (b10724:
                   n_prompt >= n_ctx), and an answer stops where the context
                   ends, finish "length"
    max_tokens     -1, or none, is no limit; 0 writes nothing and finishes
                   "length" (the README: "When 0, no tokens will be generated
                   but the prompt is evaluated into the cache" - b10724's code,
                   as read, sends the first token before its limit check: which
                   is true is owed to the real card); a length cuts the answer
                   there, "length"

A count's own calls are recorded (`--fake-record`) as "props",
"apply-template" and "tokenize" - never as "request", which is a request for
an answer.

Every other field of a request - a sampler, a JSON schema, a grammar, the
prompt cache's, the thinking's budget - it takes and records (`--fake-record`)
exactly as it came, and answers as it would without it: rows read what it
RECEIVED to prove a parameter reached the model unchanged, or never left the
bootstrap. What the real llama-server does with a field it does not read is
not measured (it is owed to the real card), so the stand-in claims nothing
about it.

Its own switches - never llama-server's - make it misbehave on purpose:

    --fake-load-s S          answer /health 503 for S seconds first
    --fake-reject FLAG       refuse FLAG as an invalid argument (an older build)
    --fake-exit-code N       exit with N at once, before answering anything
    --fake-health-status N   answer /health with N and nothing else
    --fake-alias NAME        name itself NAME in /v1/models (another load)
    --fake-first-token-s S   think S seconds before the first token
    --fake-cut-after N       stop after N tokens and close the stream cleanly,
                             with no finish reason and no [DONE]
    --fake-oom-after N       run out of the card's memory after N tokens of an
                             answer, as b10724's ggml does - its log's lines
                             (ggml-cuda.cu, ggml_cuda_error: "CUDA error: out
                             of memory", where it was, the call) and then the
                             process ends (GGML_ABORT: abort()'s exit code).
                             Read from the source, not measured
    --fake-oom-once PATH     ...only while PATH does not exist, which the run
                             out of memory then creates: the first load that
                             answers breaks, and every load after it - once
                             the programs that filled the card are closed -
                             answers whole (S5b)
    --fake-record PATH       append every request, and every client that went
                             away mid-answer, as a JSON line
    --fake-n-ctx N           its slot's context is N, not the -c it was given
    --fake-template-refuses M   /apply-template refuses every body 400, saying
                             M - as llama-server's chat layer refuses a body
                             its template cannot take

and one route of its own:

    POST /fake/deaf          stop listening and live on, answering nothing -
                             what a llama-server killed underneath does for
                             the two seconds or so it takes to finish exiting
                             (measured on the real card)
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

#: llama-server's flags that take a value - the ones the backend sends
VALUED = {"-m", "--model", "--host", "--port", "-ngl", "--n-gpu-layers", "-c", "--ctx-size",
          "-a", "--alias", "--api-key", "-ctk", "--cache-type-k", "-ctv", "--cache-type-v",
          "-fa", "--flash-attn", "--reasoning"}
FAKE = {"--fake-load-s", "--fake-reject", "--fake-exit-code", "--fake-health-status",
        "--fake-alias", "--fake-first-token-s", "--fake-cut-after", "--fake-record",
        "--fake-n-ctx", "--fake-template-refuses", "--fake-oom-after", "--fake-oom-once"}
#: abort()'s exit code: 3 on Windows, where the stand-in runs; 134 elsewhere
ABORTED = 3 if sys.platform == "win32" else 134
WORDS = ("rain", "on", "the", "tin", "roof", "again", "slowly", "a", "letter", "came",
         "through", "the", "open", "window", "and", "nobody", "read", "it")
#: the context of a model started with -c 0: llama-server's is then the
#: model's training context; the stand-in's model says this one
TRAIN_CTX = 131072
#: a special token of the stand-in's template
SPECIAL = re.compile(r"<\|[a-z_]+\|>")
BOS = "<s>"
#: the paths b10724 answers without the key (server-http.cpp's public
#: endpoints, the web page's files aside)
PUBLIC = ("/health", "/v1/health")


def render(body: dict) -> str:
    """The stand-in's chat template - what /apply-template answers, and what
    the chat route reads a request as (see the module's docstring)."""
    out = [f"<|{m.get('role')}|>\n{m.get('content', '')}<|end|>\n"
           for m in body.get("messages") or [] if isinstance(m, dict)]
    if body.get("add_generation_prompt", True) is not False:
        out.append("<|assistant|>\n")
        kwargs = body.get("chat_template_kwargs")
        if isinstance(kwargs, dict) and kwargs.get("enable_thinking") is False:
            out.append("<think>\n\n</think>\n\n")
    return "".join(out)


def pieces(text: str, add_special: bool = False, parse_special: bool = True) -> list:
    """The stand-in's tokenizer: a text as its pieces (see the module's
    docstring). llama-server b10724's /tokenize defaults: no first token,
    special tokens parsed."""
    out = [BOS] if add_special else []
    at = 0
    for m in SPECIAL.finditer(text):
        out += text[at:m.start()].split()
        out += [m.group()] if parse_special else ["<|", m.group()[2:-2], "|>"]
        at = m.end()
    return out + text[at:].split()


def token_id(piece: str) -> int:
    """A piece's id: the same piece, the same id, every time."""
    if piece == BOS:
        return 1
    return 3 + int(hashlib.sha256(piece.encode("utf-8")).hexdigest()[:8], 16) % 32000


def refuse(arg: str) -> None:
    print(f"error: invalid argument: {arg}", file=sys.stderr, flush=True)
    sys.exit(1)


def parse(argv: list) -> dict:
    rejected = {argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "--fake-reject"}
    opts: dict = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in rejected:
            refuse(a)
        if a in VALUED or a in FAKE:
            if i + 1 >= len(argv):
                refuse(a)
            opts[a] = argv[i + 1]
            i += 2
            continue
        refuse(a)
    return opts


class Fake:
    def __init__(self, opts: dict):
        self.alias = opts.get("--alias") or opts.get("-a") or opts.get("-m", "model")
        self.named = opts.get("--fake-alias", self.alias)
        self.key = opts.get("--api-key")
        self.load_s = float(opts.get("--fake-load-s", 0))
        self.health = int(opts["--fake-health-status"]) if "--fake-health-status" in opts else None
        self.first_token_s = float(opts.get("--fake-first-token-s", 0))
        self.cut_after = int(opts["--fake-cut-after"]) if "--fake-cut-after" in opts else None
        self.oom_after = int(opts["--fake-oom-after"]) if "--fake-oom-after" in opts else None
        self.oom_once = opts.get("--fake-oom-once")
        self.record_path = opts.get("--fake-record")
        self.model_path = opts.get("-m") or opts.get("--model") or ""
        #: its slot's context: -c, or the model's training context for -c 0 -
        #: unless it is told its slot has another
        self.ctx = int(opts.get("-c") or opts.get("--ctx-size") or 0) or TRAIN_CTX
        if "--fake-n-ctx" in opts:
            self.ctx = int(opts["--fake-n-ctx"])
        self.template_refuses = opts.get("--fake-template-refuses")
        self.started = time.monotonic()
        self.lock = threading.Lock()
        self.deaf = threading.Event()

    def loading(self) -> bool:
        return time.monotonic() - self.started < self.load_s

    def record(self, entry: dict) -> None:
        if not self.record_path:
            return
        with self.lock, open(self.record_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

    def text(self, prompt: str, seed) -> list:
        digest = hashlib.sha256(f"{self.alias}|{seed}|{prompt}".encode("utf-8")).hexdigest()
        rng = random.Random(int(digest[:16], 16))
        words = [rng.choice(WORDS) for _ in range(rng.randint(12, 24))]
        return [w + " " for w in words[:-1]] + [words[-1] + "."]


def handler_for(fake: Fake):
    class Handler(BaseHTTPRequestHandler):
        def log_request(self, code="-", size="-"):          # one short line per request
            print(f"request: {self.command} {self.path} {getattr(code, 'value', code)}",
                  file=sys.stderr, flush=True)

        def log_message(self, fmt, *args):
            print("fake: " + (fmt % args), file=sys.stderr, flush=True)

        def answer(self, status: int, doc: dict) -> None:
            raw = json.dumps(doc).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def keyed(self) -> bool:
            if fake.key is None or self.headers.get("Authorization") == f"Bearer {fake.key}":
                return True
            self.answer(401, {"error": {"code": 401, "message": "Invalid API Key",
                                        "type": "authentication_error"}})
            return False

        def ready(self) -> bool:
            """b10724's order before EVERY route - its pre-routing handler, run
            before a path is looked at, a path no route has included: the
            loading state first (503 "Loading model"), then the key (401),
            which every path but /health and /v1/health needs."""
            if fake.loading():
                self.answer(503, {"error": {"code": 503, "message": "Loading model",
                                            "type": "unavailable_error"}})
                return False
            return self.path in PUBLIC or self.keyed()

        def body(self):
            """The request's JSON, or None once it is answered: llama-server
            parses a body with nlohmann::json, whose error is a 500."""
            try:
                doc = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            except ValueError as exc:
                self.answer(500, {"error": {"code": 500, "message": f"parse error: {exc}",
                                            "type": "server_error"}})
                return None
            return doc if isinstance(doc, dict) else {}

        def do_GET(self):
            if self.path == "/health" and fake.health is not None:
                return self.answer(fake.health, {"error": {"code": fake.health}})
            if not self.ready():
                return
            if self.path in PUBLIC:
                return self.answer(200, {"status": "ok"})
            if self.path == "/v1/models":
                # b10724's get_res_models, in part: the slot's context is
                # `meta.n_ctx`, as /props says it
                return self.answer(200, {"object": "list", "data": [
                    {"id": fake.named, "object": "model", "owned_by": "llamacpp",
                     "meta": {"n_ctx": fake.ctx, "n_ctx_train": TRAIN_CTX}}]})
            if self.path == "/props":
                fake.record({"event": "props"})
                # b10724's get_res_props, in part: the slot's context is
                # `default_generation_settings.n_ctx`
                return self.answer(200, {
                    "default_generation_settings": {"n_ctx": fake.ctx, "params": {"n_predict": -1}},
                    "total_slots": 1, "model_alias": fake.named, "model_path": fake.model_path,
                    "chat_template": "the stand-in's own (fake_llama_server.render)",
                    "bos_token": BOS, "eos_token": "<|end|>", "build_info": "fake llama-server",
                    "is_sleeping": False})
            self.answer(404, {"error": {"code": 404, "message": "File Not Found"}})

        def do_POST(self):
            if not self.ready():
                return
            if self.path == "/fake/deaf":
                self.answer(200, {"deaf": True})
                fake.deaf.set()
                return
            if self.path == "/apply-template":
                return self.apply_template()
            if self.path == "/tokenize":
                return self.tokenize()
            if self.path != "/v1/chat/completions":
                return self.answer(404, {"error": {"code": 404, "message": "File Not Found"}})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            except ValueError:
                return self.answer(400, {"error": {"code": 400, "message": "bad json"}})
            fake.record({"event": "request", "body": body})
            # THE WHOLE CONVERSATION is the request, as llama-server reads it:
            # rendered with its template, tokenized with its first token and
            # its special tokens (b10724: tokenize_input_prompts(..., true,
            # true)) - so every message counts against the context
            n = len(pieces(render(body), add_special=True))
            if n >= fake.ctx:
                # b10724 refuses a prompt that leaves no position (n_prompt >=
                # n_ctx), in the words and fields it answered on the real card
                # for a prompt of 8,259 tokens at a context of 4,096
                return self.answer(400, {"error": {
                    "code": 400, "message": f"request ({n} tokens) exceeds the available "
                                            f"context size ({fake.ctx} tokens), try increasing it",
                    "type": "exceed_context_size_error", "n_prompt_tokens": n,
                    "n_ctx": fake.ctx}})
            # every message is part of what is answered: the answer is drawn
            # from all their words
            prompt = " ".join(str(m.get("content", "")) for m in body.get("messages") or []
                              if isinstance(m, dict))
            words = fake.text(prompt, body.get("seed"))
            # -1 (or none) is no limit, 0 writes nothing; and an answer stops
            # where the context ends (b10724, without --context-shift)
            most = body.get("max_tokens")
            most = -1 if most is None else int(most)
            limit = fake.ctx - n if most < 0 else min(most, fake.ctx - n)
            finish = "length" if len(words) > limit else "stop"
            words = words[:limit]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                if fake.first_token_s:
                    time.sleep(fake.first_token_s)
                if "[[THINK]]" in prompt:
                    for piece in ("weighing ", "the ", "request"):
                        self.event({"choices": [{"index": 0, "delta": {"reasoning_content": piece},
                                                 "finish_reason": None}]})
                for i, w in enumerate(words):
                    if fake.cut_after is not None and i == fake.cut_after:
                        return                              # a clean close, mid-answer
                    broke = fake.oom_once and os.path.exists(fake.oom_once)
                    if fake.oom_after is not None and i == fake.oom_after and not broke:
                        if fake.oom_once:
                            with open(fake.oom_once, "wb"):
                                pass                        # the next load answers whole
                        # b10724's ggml_cuda_error, then GGML_ABORT: the card's
                        # memory ran out in the middle of an answer
                        print("CUDA error: out of memory\n  current device: 0, in function "
                              "ggml_cuda_pool_vmm::alloc at ggml/src/ggml-cuda/ggml-cuda.cu:410\n"
                              "  cuMemCreate(&handle, reserve_size, &prop, 0)\n"
                              "ggml/src/ggml-cuda/ggml-cuda.cu:88: CUDA error",
                              file=sys.stderr, flush=True)
                        os._exit(ABORTED)
                    self.event({"choices": [{"index": 0, "delta": {"content": w},
                                             "finish_reason": None}]})
                    time.sleep(0.01)
                self.event({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                            "usage": {"prompt_tokens": n, "completion_tokens": len(words),
                                      "total_tokens": n + len(words)},
                            "timings": {"predicted_n": len(words), "predicted_per_second": 100.0}})
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                fake.record({"event": "client_gone"})

        def event(self, doc: dict) -> None:
            self.wfile.write(b"data: " + json.dumps(doc).encode("utf-8") + b"\n\n")
            self.wfile.flush()

        def apply_template(self) -> None:
            """b10724's post_apply_template: the body read as the chat route
            reads it, answered {"prompt"} - its chat layer's refusal, 400."""
            body = self.body()
            if body is None:
                return
            fake.record({"event": "apply-template", "body": body})
            refused = fake.template_refuses or (
                None if "messages" in body else "'messages' is required")
            if refused:
                return self.answer(400, {"error": {"code": 400, "message": refused,
                                                   "type": "invalid_request_error"}})
            self.answer(200, {"prompt": render(body)})

        def tokenize(self) -> None:
            """b10724's post_tokenize: `content` (a text, or a list of texts and
            token ids) as {"tokens": [ids]}, or [{"id", "piece"}] with
            `with_pieces`; its defaults, add_special false and parse_special
            true; no content, no tokens. A value of the wrong kind is its
            default, as llama-server's json_value reads it."""
            body = self.body()
            if body is None:
                return
            fake.record({"event": "tokenize", "body": body})
            content = body.get("content")
            if content is None:
                return self.answer(200, {"tokens": []})
            parse_special = body.get("parse_special") is not False
            got = [(1, BOS)] if body.get("add_special") is True else []
            for part in content if isinstance(content, list) else [content]:
                if isinstance(part, int) and not isinstance(part, bool):
                    got.append((part, ""))
                else:
                    got += [(token_id(p), p) for p in pieces(str(part), False, parse_special)]
            if body.get("with_pieces") is True:
                return self.answer(200, {"tokens": [{"id": i, "piece": p} for i, p in got]})
            self.answer(200, {"tokens": [i for i, _ in got]})

    return Handler


class _Listening(ThreadingHTTPServer):
    """A backlog of 64, not the standard library's 5. A row that asks
    `is_alive` twenty times in a row opens twenty connections, and the port
    probe counts a refused one as a dead server. With every suite running at
    once (mutate.py's controls: four copies), the one accepting thread can
    fall behind five - measured 2026-09-25 with 24 CPU burners: 15 of 15 runs
    red at 5, 0 of 15 at 64. The real llama-server is asked once a reuse.

    And its port is its own. The standard library's http.server sets
    SO_REUSEADDR, and on Windows a second socket that sets it too is let bind
    a port a first one holds (measured 2026-09-25: a listener with it, then a
    second bind with it, of the same address - bound): two stand-ins on one
    port, and a row's requests answered by another copy's. On Windows the
    socket is bound SO_EXCLUSIVEADDRUSE instead: its bind fails on a port
    somebody holds, and nobody's bind shares it after."""
    request_queue_size = 64
    allow_reuse_address = sys.platform != "win32"

    def server_bind(self):
        if sys.platform == "win32":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def main(argv: list) -> int:
    opts = parse(argv)
    if "--fake-exit-code" in opts:
        print(f"fake llama-server: exiting with {opts['--fake-exit-code']} as asked",
              file=sys.stderr, flush=True)
        return int(opts["--fake-exit-code"])
    host, port = opts.get("--host", "127.0.0.1"), int(opts.get("--port", 8080))
    fake = Fake(opts)
    try:
        server = _Listening((host, port), handler_for(fake))
    except OSError as exc:
        print(f"error: couldn't bind HTTP server socket, hostname: {host}, port: {port}: {exc}",
              file=sys.stderr, flush=True)
        return 1
    server.daemon_threads = True
    print(f"fake llama-server listening on {host}:{port} as {fake.alias}", file=sys.stderr,
          flush=True)
    threading.Thread(target=server.serve_forever, daemon=True, name="fake-serve").start()
    fake.deaf.wait()
    server.shutdown()
    server.server_close()
    print("fake llama-server: deaf - listening no more, and still running", file=sys.stderr,
          flush=True)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

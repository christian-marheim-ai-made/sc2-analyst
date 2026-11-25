"""What llama-server b10724 reads from a chat request, and which of it is a job's.

    CHAT_FIELDS        every field its /v1/chat/completions route reads from a request
    OFFERED            what a job's `gen` names beside the queue's own three: the
                       contract's list (jobs-submit), which a row holds to this reading
    OWN                the bootstrap's own - never a job's to set - and why
    NOT_YET            read on the route and not offered yet, and why
    SET_BY_THE_SERVER  what the route's chat layer sets itself on every request

THE SOURCE: llama.cpp at tag b10724 - commit 2d8d612e4, which the build this
machine runs carries in its libraries - read, never run: it is a build for the
card, and nothing here starts it. The chat route reads a request in three
places:

    tools/server/server-common.cpp   `oaicompat_chat_params_parse`, the chat layer:
                                     the messages, `response_format`, the thinking's
                                     budget, the template's switches - which then
                                     copies every field it did not set itself into
                                     the completion's parameters
    tools/server/server-schema.cpp   `make_llama_cmpl_schema`: the 66 fields a
                                     completion reads, and the other names some
                                     of them answer to
    tools/server/README.md           the route's documented options: `id_slot`

NOT MEASURED, and owed to the real card with its owner's go (M1): what
llama-server does with a field it does not read. Its source reads the fields it
knows and nothing else, which suggests one it does not know is ignored - as
whisper-server 1.9.1 was measured to ignore one. The bootstrap refuses one at
submit either way: a job's `gen` is closed, so a misspelt parameter never leaves
it. Nor measured: that a schema or a grammar really forces the answer's shape,
that the prompt cache is really reused, that a thinking budget really ends the
thinking - the model's side of every parameter is the real card's to show.

READ FROM THE SOURCE, not measured: a thinking budget's own tags
(`reasoning_budget_start_tag`, `reasoning_budget_end_tags`) are the template's
wherever the template has thinking tags - the chat layer sets them from it -
and a request's are taken only for a template without; `continue_final_message`
continues with `true`, `"content"` or `"reasoning_content"` and reads anything
else as no continuing, silently - so the contract takes a boolean and those two
strings, and refuses a misspelt one; and a request that sets both it and
`add_generation_prompt` is refused by llama-server, in its words.

A new build of llama-server is a new reading of its source, and a new list.
"""
from __future__ import annotations

CHAT_FIELDS = (
    # the completion's schema, in its order
    "verbose", "timings_per_token", "stream", "stream_options", "cache_prompt",
    "return_tokens", "return_progress", "sse_ping_interval", "n_predict", "n_indent",
    "n_keep", "n_discard", "n_cmpl", "n_cache_reuse", "t_max_predict_ms", "response_fields",
    "top_k", "top_p", "min_p", "top_n_sigma", "xtc_probability", "xtc_threshold", "typical_p",
    "temperature", "dynatemp_range", "dynatemp_exponent", "repeat_last_n", "repeat_penalty",
    "frequency_penalty", "presence_penalty", "dry_multiplier", "dry_base",
    "dry_allowed_length", "dry_penalty_last_n", "mirostat", "mirostat_tau", "mirostat_eta",
    "adaptive_target", "adaptive_decay", "seed", "n_probs", "min_keep", "backend_sampling",
    "post_sampling_probs", "lora", "dry_sequence_breakers", "json_schema", "grammar_lazy",
    "chat_format", "reasoning_format", "generation_prompt", "parse_tool_calls",
    "chat_parser", "continue_final_message", "echo", "preserved_tokens", "grammar_triggers",
    "reasoning_control", "reasoning_budget_tokens", "reasoning_budget_start_tag",
    "reasoning_budget_end_tags", "reasoning_budget_message", "logit_bias", "ignore_eos",
    "stop", "samplers",
    # the other names the completion's schema answers to
    "max_completion_tokens", "max_tokens", "n", "logprobs", "grammar",
    "reasoning_budget_end_tag",
    # the chat layer's own
    "messages", "tools", "tool_choice", "top_logprobs", "chat_template_kwargs",
    "reasoning_effort", "thinking_budget_tokens", "add_generation_prompt",
    "parallel_tool_calls", "response_format", "model",
    # the route's documented options
    "id_slot",
)

#: The bootstrap's own: what it sets itself on every request, or another name
#: for a parameter a job already names - one name each, so two can never
#: disagree about one parameter.
OWN = {
    "model": "the load's id: how a request names the load it is for",
    "messages": "the request itself - a prompt, or a conversation - which is what the "
                "application's build hook returns, never a parameter",
    "stream": "the bootstrap always streams: its progress, the stall watch and a cancel "
              "all read the stream",
    "stream_options": "the bootstrap always asks for the usage at the stream's end",
    "sse_ping_interval": "how the stream to the bootstrap is kept alive: the bootstrap's "
                         "transport, not the application's",
    "n": "a job's `n` is the bootstrap's own - how many answers, each asked on its own "
         "with its own seed and judged on its own",
    "n_cmpl": "llama-server's name for its own `n`: the bootstrap's `n` asks one answer "
              "at a time",
    "n_predict": "`max_tokens` by another name",
    "max_completion_tokens": "`max_tokens` by another name",
    "thinking_budget_tokens": "`reasoning_budget_tokens` by another name",
    "reasoning_budget_end_tag": "`reasoning_budget_end_tags` by another name",
    "reasoning_format": "the answer's split - its text, and what the model deliberated "
                        "beside it in `reasoning` - is the bootstrap's own, and rests on "
                        "llama-server parsing the thinking out",
}

#: Read on the route, and not offered yet: what each one gives back has no
#: place in a candidate yet, so offering it would be a parameter that looks as
#: if it took effect - or it needs a route the bootstrap does not send.
NOT_YET = {
    "logprobs": "its probabilities come back beside the text, and a candidate does not "
                "carry them yet",
    "top_logprobs": "the same as `logprobs`",
    "n_probs": "the same as `logprobs`",
    "post_sampling_probs": "the same as `logprobs`",
    "return_tokens": "the token ids come back beside the text, and a candidate does not "
                     "carry them yet",
    "return_progress": "the prompt's progress comes back in the stream, and the bootstrap "
                       "does not report it yet",
    "timings_per_token": "timings in every piece of the stream: the bootstrap keeps the "
                         "last, which it has without it",
    "verbose": "a field of debugging beside the answer, which a candidate does not carry",
    "echo": "the prompt written back into the answer's text",
    "response_fields": "it chooses which fields of the answer come back - the ones the "
                       "bootstrap reads among them",
    "tools": "a tool call comes back instead of text, and a candidate does not carry one "
             "yet: a job whose answer is only a call would fail as an empty answer",
    "tool_choice": "the same as `tools`",
    "parallel_tool_calls": "the same as `tools`",
    "parse_tool_calls": "the same as `tools`",
    "reasoning_control": "it arms /v1/chat/completions/control, to end the thinking "
                         "while it runs, and the bootstrap sends nothing there",
}

#: The chat layer's own plumbing into the completion: it sets each from the
#: model's template - `chat_parser` whenever the template has a parser, the
#: rest on every request - over whatever a request carried, since its copy of
#: the request takes only what it did not set itself. Never an application's.
SET_BY_THE_SERVER = ("chat_format", "chat_parser", "generation_prompt", "grammar_lazy",
                     "grammar_triggers", "preserved_tokens")

OFFERED = tuple(f for f in CHAT_FIELDS
                if f not in OWN and f not in NOT_YET and f not in SET_BY_THE_SERVER)

"""A model's log, read for the lines that say the card's memory ran out
(PLAN-4 D3): the runner reports them, and the coordinator marks the rental
degraded - the card is over-full, and the work fails saying so instead of
hanging or crawling.

THE LINES ARE LLAMA.CPP'S OWN, read from b10724's source - never guessed:

  ggml/src/ggml-cuda/ggml-cuda.cu, ggml_backend_cuda_buffer_type_alloc_buffer
      - every buffer on the card: the weights, the KV cache, the compute:
          GGML_LOG_ERROR("%s: allocating %.2f MiB on device %d: cudaMalloc failed: %s\\n", ...)
      looked for: "cudaMalloc failed"
  ggml/src/ggml-cuda/ggml-cuda.cu, ggml_cuda_error - every CUDA_CHECK that
      fails, a pool's allocation in the middle of an answer among them; the
      process then ends (GGML_ABORT):
          GGML_LOG_ERROR(GGML_CUDA_NAME " error: %s\\n", msg);
      GGML_CUDA_NAME is "CUDA" (ggml/include/ggml-cuda.h), and msg the CUDA
      runtime's own word for the error - "out of memory" for
      cudaErrorMemoryAllocation, which is CUDA's, not llama.cpp's: recalled,
      not read in that tree.
      looked for: "CUDA error: out of memory"
  ggml/src/ggml-alloc.c - a graph's or a context's buffers:
          GGML_LOG_ERROR("%s: failed to allocate %s buffer of size %zu\\n", ...)
      where the second %s is the buffer type's name: "CUDA0", "CUDA1" ... for
      a device's memory (GGML_CUDA_NAME + the device's number).
      looked for: "failed to allocate CUDA<n> buffer of size" - never
      CUDA_Host (pinned memory of the machine's own) or CPU

NOT LOOKED FOR: "failed to allocate compute buffers" (llama-context.cpp,
graph_reserve) follows the lines above and does not say which memory; "failed
to find a memory slot for batch" is the KV cache's slots, not the card; a
pool's "alloc of ... failed, flushing ... and retrying" is a debug line, and
retried.

WHISPER-SERVER IS GGML TOO: whisper.cpp 1.9.1 carries its own copy of ggml,
and its source (ggml/src/ggml-cuda/ggml-cuda.cu, ggml/src/ggml-alloc.c,
ggml/include/ggml-cuda.h at v1.9.1) writes the same three lines, word for
word - read, not assumed. Its engine reads its own load's log for them the
same way (the seam: `Backend.memory_errors`, `llamacpp.ModelServer`'s). NOT
LOOKED FOR there: whisper.cpp's own "failed to allocate memory for the kv
cache" (src/whisper.cpp, whisper_kv_cache_init) and its kin follow ggml's
line when a buffer on the card could not be had, and do not say which
memory.
"""
from __future__ import annotations

import re

#: the lines that say the card's memory ran out - see the module's docstring
#: for where llama.cpp b10724 writes each
OUT_OF_MEMORY = (
    re.compile(r"cudaMalloc failed"),
    re.compile(r"CUDA error: out of memory"),
    re.compile(r"failed to allocate CUDA\d+ buffer of size"),
)


def says_out_of_memory(line: str) -> bool:
    """Does this line of a model's log say the card's memory ran out?"""
    return any(p.search(line) for p in OUT_OF_MEMORY)


def read_new(path, start: int):
    """The lines of the log at *path* from byte *start* that say the card's
    memory ran out - whole lines only: one still being written is read the
    next time. -> (the lines, where the next read starts). A log that cannot
    be read says nothing, and is read from the same place next time."""
    try:
        with open(path, "rb") as fh:
            fh.seek(start)
            data = fh.read()
    except OSError:
        return [], start
    end = data.rfind(b"\n")
    if end < 0:
        return [], start
    text = data[:end + 1].decode("utf-8", "replace")
    return ([line.strip() for line in text.splitlines() if says_out_of_memory(line)],
            start + end + 1)

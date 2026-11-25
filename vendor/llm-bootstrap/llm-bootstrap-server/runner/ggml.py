"""Reading a whisper model file's header: the ggml magic, and eleven numbers.

whisper-server's model files are not GGUF (`gguf.py` reads those). One starts
with the ggml magic - 0x67676d6c, a little-endian 32-bit number - and then
eleven little-endian 32-bit integers, in whisper.cpp's own order and under its
own names:

    n_vocab  n_audio_ctx  n_audio_state  n_audio_head  n_audio_layer
    n_text_ctx  n_text_state  n_text_head  n_text_layer  n_mels  ftype

Those 48 bytes are all that is read: never the weights. `ftype` is reported
as the file stores it.

ITS SHAPE IS NAMED only when it is one of the two named here - 128 mels with
32 layers in both stacks is large v3, 80 mels with 24 in both is medium - and
otherwise said in its own numbers, never guessed at. Both stacks decide: a
file with large v3's mels and encoder but a smaller decoder is another model,
and is not called large v3.

A file that is not a whisper model is refused in words (`GGMLError`); a file
that cannot be read at all raises the operating system's own error, because
not there is not the same as not a model.
"""
from __future__ import annotations

import struct
from pathlib import Path

MAGIC = 0x67676d6c
#: the eleven numbers after the magic, in the file's order
FIELDS = ("n_vocab", "n_audio_ctx", "n_audio_state", "n_audio_head", "n_audio_layer",
          "n_text_ctx", "n_text_state", "n_text_head", "n_text_layer", "n_mels", "ftype")
_HEADER = struct.Struct("<I11i")
#: the shapes named here, by (mels, audio layers, text layers)
NAMED = {(128, 32, 32): "large v3", (80, 24, 24): "medium"}


class GGMLError(ValueError):
    """The file is not a whisper model: no ggml magic, a header cut short, or
    numbers no model has."""


def read_header(path) -> dict:
    """The header of a whisper model file: its eleven numbers, the file's size
    and its shape in words."""
    path = Path(path)
    size = path.stat().st_size
    with open(path, "rb") as fh:
        head = fh.read(_HEADER.size)
    if len(head) < 4 or struct.unpack("<I", head[:4])[0] != MAGIC:
        raise GGMLError(f"{path.name} is not a whisper model: it does not start with the ggml "
                        f"magic 0x67676d6c (its first bytes are {head[:4]!r})")
    if len(head) < _HEADER.size:
        raise GGMLError(f"{path.name} is not a whisper model: its header ends after "
                        f"{len(head)} of {_HEADER.size} bytes")
    numbers = dict(zip(FIELDS, _HEADER.unpack(head)[1:]))
    impossible = [f"{name} is {value}" for name, value in numbers.items()
                  if value < (0 if name == "ftype" else 1)]
    if impossible:
        raise GGMLError(f"{path.name} is not a usable whisper model: "
                        f"{', '.join(impossible)}")
    return {"path": str(path), "file_size": size, **numbers, "shape": shape(numbers)}


def shape(numbers: dict) -> str:
    """Its name when it is one named here, else its mels and its layers."""
    mels, audio, text = numbers["n_mels"], numbers["n_audio_layer"], numbers["n_text_layer"]
    return NAMED.get((mels, audio, text)) or \
        f"{mels} mels, {audio} audio layers, {text} text layers"

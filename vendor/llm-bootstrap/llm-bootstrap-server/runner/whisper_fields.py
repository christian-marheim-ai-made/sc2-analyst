"""What whisper-server 1.9.1 reads from a transcription request, besides the audio.

    INFERENCE_FIELDS   every field its request parser reads
    AUDIO_PART         the part the audio travels in - not a field

THE SOURCE: whisper.cpp at tag v1.9.1, examples/server/server.cpp, the
function `get_req_parameters` - every field it asks the request for, in its
order, 36 of them. It was read twice, by two different questions, and the two
readings agreed. The /inference handler reads the audio, `file`, before it
calls that function, and refuses a request without it.

Closed on purpose. whisper-server ignores a field it does not know: measured
on 1.9.1, a request with a field it does not read was answered 200, and
nothing said the field was not used. A list of what it reads is the only way
to tell a misspelt field from one that took effect. A new version of the
server is a new reading of its source, and a new list.
"""
INFERENCE_FIELDS = (
    "offset_t", "offset_n", "duration", "max_context", "max_len", "best_of", "beam_size",
    "audio_ctx", "word_thold", "entropy_thold", "logprob_thold", "no_speech_thold",
    "debug_mode", "translate", "diarize", "tinydiarize", "split_on_word", "no_timestamps",
    "token_timestamps", "language", "detect_language", "prompt", "carry_initial_prompt",
    "response_format", "temperature", "temperature_inc", "suppress_non_speech", "suppress_nst",
    "vad", "vad_threshold", "vad_min_speech_duration_ms", "vad_min_silence_duration_ms",
    "vad_max_speech_duration_s", "vad_speech_pad_ms", "vad_samples_overlap",
    "no_language_probabilities",
)
#: the multipart part the audio travels in: read by the handler itself
AUDIO_PART = "file"


def as_form(value) -> str:
    """A field's value - as a request names it, in JSON - as whisper-server
    reads it from its form: true and false as the words it takes for them
    (a field is true for "true", "1", "yes" or "y" alone, so Python's "True"
    would read as false), a number as JSON writes it, and text as it is."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    import json
    return json.dumps(value)

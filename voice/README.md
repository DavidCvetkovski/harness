# harness-voice

An optional sidecar that gives Harness a voice — specifically **your** voice,
cloned from about fifteen seconds of you reading a paragraph aloud.

It runs as its own process, in its own virtual environment, and Harness speaks
to it over HTTP. That separation is the point: the main project still has no
dependencies, still runs on the standard library alone, and still works
perfectly with this switched off. The speaker button simply does not appear.

## Why a sidecar rather than part of Harness

The model needs PyTorch, which is around 2.5 GB installed — more than the rest
of the project by two orders of magnitude. Making that a hard requirement of an
assistant whose main job is reading files would be a bad trade. Here it is
opt-in, and the cost falls only on people who want it.

## Several voices

Voices are a library, not a single setting. Record yourself bored, record
yourself awake, name them, and pick whichever one an answer should be read in.
Each is a recording plus the exact transcript of that recording, kept in
`~/.harness/voice/`.

Recordings are levelled to a consistent peak on the way in. The clone inherits
the loudness of its reference, so a quiet recording produces quiet speech — and
carries a worse signal-to-noise ratio into the part of the model that decides
what the voice sounds like.

## Setup

```bash
uv venv --python 3.13 ~/llm/voice/.venv
VIRTUAL_ENV=~/llm/voice/.venv uv pip install -r voice/requirements.txt
python3 voice/server.py
```

First run downloads ~1.6 GB of weights to the Hugging Face cache.

Then record a voice, either in Harness under **Settings → Voice**, or at
<http://127.0.0.1:8421> if you are running the sidecar on its own. Both do the
same thing; the standalone page exists so the sidecar can be set up and checked
without Harness running at all.

Microphone access needs a secure context, so recording works on the Mac itself
and not from a phone over plain HTTP. Playback works everywhere.

## Speed

Measured on an M5, synthesising with nothing else running:

| | |
|---|---|
| CPU | ~1.3x real time |
| MPS | ~1.6x real time |

CPU is the default, and that is not a typo. The model is small and deeply
sequential — ten codebooks per frame, about 21.5 frames a second — so per-step
dispatch overhead on the GPU costs more than the parallelism wins back.

With a large language model also resident on a 16 GB machine, expect this to
roughly double, and the first call after an idle period to be much worse than
that: llama.cpp memory-maps its weights, and the two models evict each other
from the page cache. Synthesised audio is cached by voice and text, so anything
said twice is instant.

## Licence

The code here is MIT, like the rest of Harness. The **model is not**: Audio8 TTS
Preview 0.1b is released under the Audio8 Community License v1.0, which is free
for non-commercial use and for companies under $2M revenue. No weights are
distributed in this repository — they are downloaded from Hugging Face at first
run, and the licence is yours to accept.

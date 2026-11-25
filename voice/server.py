#!/usr/bin/env python3
"""
harness-voice — speaks Harness's answers in a voice cloned from your own.

Runs beside Harness rather than inside it. Harness sends text over HTTP and
gets a WAV back; if this process is not running, Harness carries on exactly as
before. That is why the model's 2.5 GB of PyTorch does not appear anywhere in
the main project's requirements.

    python3 voice/server.py            # then open http://127.0.0.1:8421

Two endpoints do the real work:

    POST /enroll   record yourself once; the reference is kept on disk
    POST /speak    text in, 44.1 kHz WAV out, in that voice
"""

import argparse
import contextlib
import hashlib
import io
import ipaddress
import json
import os
import threading
import time
import urllib.parse
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.expanduser("~/.harness/voice")
VOICES_DIR = os.path.join(STATE, "voices")
INDEX_FILE = os.path.join(STATE, "voices.json")
CACHE = os.path.join(STATE, "cache")

# Older installs kept exactly one voice, here.
LEGACY_WAV = os.path.join(STATE, "reference.wav")
LEGACY_TXT = os.path.join(STATE, "reference.txt")

MODEL_ID = "Audio8/Audio8-TTS-Preview-0.1b"
SAMPLE_RATE = 44100

# Measured on an M5: CPU runs at about 1.3x real time, MPS at about 1.6x. The
# model is small and deeply sequential — ten codebooks per frame, ~21.5 frames a
# second — so the per-step overhead on the GPU costs more than the parallelism
# wins back. CPU is the honest default here; --device mps is left available
# because that balance may not hold on every machine.
DEVICE = "cpu"

# The model is a couple of gigabytes resident, on a machine that is probably
# also holding a language model. Load it on first use and let it go again once
# nobody has asked for anything in a while.
IDLE_UNLOAD_SECONDS = 600


# --------------------------------------------------------------------------
# The model, loaded lazily and dropped when idle
# --------------------------------------------------------------------------

class Voice:
    """Owns the model. Thread-safe, because generation is not reentrant."""

    def __init__(self, device=DEVICE):
        self.device = device
        self.lock = threading.Lock()
        self._model = None
        self._processor = None
        self._last_used = 0.0
        threading.Thread(target=self._reaper, daemon=True).start()

    def _reaper(self):
        while True:
            time.sleep(30)
            with self.lock:
                idle = time.time() - self._last_used
                if self._model is not None and idle > IDLE_UNLOAD_SECONDS:
                    self._model = self._processor = None
                    _free_memory()
                    log(f"released the model after {idle:.0f} seconds idle")

    @property
    def loaded(self):
        return self._model is not None

    def _ensure(self):
        """Load on first use. Held under the same lock generation uses."""
        if self._model is not None:
            return
        from transformers import AutoModel, AutoProcessor
        started = time.time()
        log(f"loading {MODEL_ID} on {self.device}…")
        self._processor = AutoProcessor.from_pretrained(
            MODEL_ID, trust_remote_code=True)
        self._model = AutoModel.from_pretrained(
            MODEL_ID, trust_remote_code=True).to(self.device).eval()
        log(f"ready in {time.time() - started:.1f}s")

    def speak(self, text, reference_wav, reference_text):
        """One sentence-sized chunk of text as a float32 waveform."""
        import torch
        with self.lock:
            self._ensure()
            self._last_used = time.time()
            inputs = self._processor(
                text=[text],
                reference_audio=[reference_wav],
                reference_text=[reference_text],
                return_tensors="pt")
            inputs = {k: (v.to(self.device) if hasattr(v, "to") else v)
                      for k, v in inputs.items()}
            with torch.inference_mode():
                waveforms, lengths, _ = self._model.generate_audio(
                    **inputs, max_new_tokens=512)
            audio = waveforms[0].detach().float().cpu().numpy()
            self._last_used = time.time()
            return audio[:int(lengths[0])]


def _free_memory():
    import gc
    gc.collect()
    try:
        import torch
        if torch.backends.mps.is_available():
            torch.mps.empty_cache()
    except Exception:                                         # noqa: BLE001
        pass


# --------------------------------------------------------------------------
# Text in, audio out
# --------------------------------------------------------------------------

def sentences(text, limit=240):
    """
    Split into chunks the model can hold in one context.

    It has 2,048 packed positions and degrades on long inputs, so a paragraph
    is spoken a sentence at a time and stitched back together. Splitting on
    punctuation rather than a fixed width keeps the prosody of each chunk
    intact — a sentence cut in half sounds like a sentence cut in half.
    """
    import re
    text = " ".join(str(text).split())
    if not text:
        return []
    parts, current = [], ""
    for piece in re.split(r"(?<=[.!?;:])\s+", text):
        while len(piece) > limit:                 # a very long clause
            cut = piece.rfind(" ", 0, limit) or limit
            parts.append(piece[:cut].strip())
            piece = piece[cut:].strip()
        if len(current) + len(piece) + 1 <= limit:
            current = (current + " " + piece).strip()
        else:
            if current:
                parts.append(current)
            current = piece
    if current:
        parts.append(current)
    return [p for p in parts if p]


def speakable(text):
    """
    Strip what should not be read aloud.

    Answers are full of code blocks and file paths. Reading a fenced block
    character by character is worse than useless, so it is replaced with a
    short spoken note and the prose around it is kept.
    """
    import re
    text = re.sub(r"```[\w]*\n.*?```", " . Code block omitted. ", text, flags=re.S)
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"https?://\S+", " a link ", text)
    return " ".join(text.split())


def to_wav_bytes(audio, sample_rate=SAMPLE_RATE):
    """float32 in [-1, 1] to a 16-bit mono WAV, without touching the disk."""
    import numpy as np
    clipped = np.clip(np.asarray(audio, dtype=np.float32), -1.0, 1.0)
    pcm = (clipped * 32767.0).astype("<i2").tobytes()
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)
    return buffer.getvalue()


def cache_key(text, device, voice_id):
    """
    Same words in the same voice means the same audio; synthesise it once.

    Prefixed with the voice id so deleting a voice can drop exactly its own
    cached audio and leave everybody else's alone.
    """
    digest = hashlib.sha256(f"{voice_id}|{device}|{text}".encode()).hexdigest()
    return os.path.join(CACHE, f"{voice_id}-{digest[:24]}.wav")


def log(message):
    print(f"  {message}", flush=True)


# --------------------------------------------------------------------------
# Enrolment
# --------------------------------------------------------------------------

# Read once, aloud, at a normal pace. Around fifteen seconds is the sweet spot:
# the model needs enough to characterise a voice, and the card warns that long
# or noisy references make things worse rather than better. The wording is
# chosen to cover a wide spread of vowels and consonants while still being
# something a person can read without stumbling.
ENROLMENT_SCRIPT = (
    "My name is David, and this is my voice for the assistant on this machine. "
    "I am reading a short paragraph so it can learn how I sound. "
    "The quick brown fox jumps over the lazy dog. "
    "Bright orange leaves fell through the cold November air, "
    "and somewhere further down the valley a church bell rang twice."
)


def _read_index():
    try:
        with open(INDEX_FILE) as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        data = {}
    data.setdefault("voices", [])
    data.setdefault("selected", "")
    return data


def _write_index(data):
    os.makedirs(STATE, exist_ok=True)
    temporary = INDEX_FILE + ".tmp"
    with open(temporary, "w") as handle:
        json.dump(data, handle, indent=1)
    os.replace(temporary, INDEX_FILE)


def migrate_legacy():
    """
    Carry a single-voice install forward into the library.

    Early versions kept one unnamed reference. Rather than make somebody record
    themselves again, it becomes the first entry and keeps working.
    """
    if os.path.isfile(INDEX_FILE) or not os.path.isfile(LEGACY_WAV):
        return
    with open(LEGACY_TXT) as handle:
        transcript = handle.read().strip()
    with open(LEGACY_WAV, "rb") as handle:
        wav = handle.read()
    entry = add_voice("My voice", wav, transcript)
    log(f"migrated the existing recording into the library as {entry['id']}")


def voice_path(voice_id):
    return os.path.join(VOICES_DIR, voice_id + ".wav")


def list_voices():
    data = _read_index()
    selected = data["selected"]
    return [{**v, "selected": v["id"] == selected} for v in data["voices"]]


def get_voice(voice_id=""):
    """The named voice, or whichever is currently selected."""
    data = _read_index()
    wanted = voice_id or data["selected"]
    for entry in data["voices"]:
        if entry["id"] == wanted:
            path = voice_path(entry["id"])
            if os.path.isfile(path):
                return entry, path
    return None, ""


def normalise(audio, peak=0.75):
    """
    Bring a recording up to a consistent level.

    The clone inherits the loudness of its reference: a quiet recording produces
    quiet speech, and a quiet recording also carries a worse signal-to-noise
    ratio into the part of the model that characterises the voice. Recording is
    done with the browser's automatic gain control switched off — it pumps, and
    pumping is exactly the kind of artefact that confuses a speaker encoder — so
    the levelling happens once, here, instead.
    """
    import numpy as np
    audio = np.asarray(audio, dtype=np.float32)
    loudest = float(abs(audio).max()) if audio.size else 0.0
    return audio if loudest < 1e-6 else audio * (peak / loudest)


def add_voice(name, wav_bytes, transcript, level=True):
    """Store one recording under a name and make it the selected voice."""
    import numpy as np

    audio, rate = _decode_wav(wav_bytes)
    audio = _resample(audio, rate)
    if level:
        audio = normalise(audio)

    data = _read_index()
    voice_id = hashlib.sha256(
        f"{name}|{time.time()}".encode()).hexdigest()[:12]
    os.makedirs(VOICES_DIR, exist_ok=True)
    with open(voice_path(voice_id), "wb") as handle:
        handle.write(to_wav_bytes(audio))

    entry = {
        "id": voice_id,
        "name": " ".join(str(name).split())[:60] or "Untitled",
        "transcript": " ".join(transcript.split()),
        "seconds": round(len(audio) / SAMPLE_RATE, 1),
        "peak": round(float(abs(np.asarray(audio)).max()), 3),
        "created": time.strftime("%Y-%m-%d %H:%M"),
    }
    data["voices"].append(entry)
    data["selected"] = voice_id
    _write_index(data)
    return entry


def rename_voice(voice_id, name):
    data = _read_index()
    for entry in data["voices"]:
        if entry["id"] == voice_id:
            entry["name"] = " ".join(str(name).split())[:60] or entry["name"]
            _write_index(data)
            return entry
    return None


def select_voice(voice_id):
    data = _read_index()
    if any(v["id"] == voice_id for v in data["voices"]):
        data["selected"] = voice_id
        _write_index(data)
        return True
    return False


def delete_voice(voice_id):
    data = _read_index()
    remaining = [v for v in data["voices"] if v["id"] != voice_id]
    if len(remaining) == len(data["voices"]):
        return False
    data["voices"] = remaining
    if data["selected"] == voice_id:
        data["selected"] = remaining[0]["id"] if remaining else ""
    _write_index(data)
    with contextlib.suppress(OSError):
        os.remove(voice_path(voice_id))
    _clear_cache(voice_id)
    return True


def _clear_cache(voice_id=""):
    """Drop cached audio — all of it, or just one voice's."""
    if not os.path.isdir(CACHE):
        return
    for name in os.listdir(CACHE):
        if not voice_id or name.startswith(voice_id + "-"):
            with contextlib.suppress(OSError):
                os.remove(os.path.join(CACHE, name))


def _decode_wav(wav_bytes):
    import numpy as np
    with wave.open(io.BytesIO(wav_bytes), "rb") as handle:
        channels = handle.getnchannels()
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    audio = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    return audio, rate


def _resample(audio, rate):
    """
    Browsers record at whatever their hardware runs at — 48 kHz here — and the
    model wants 44.1. Converting once at enrolment beats doing it per synthesis.
    """
    if rate == SAMPLE_RATE:
        return audio
    import torch
    import torchaudio
    tensor = torch.from_numpy(audio).unsqueeze(0)
    return torchaudio.functional.resample(
        tensor, rate, SAMPLE_RATE).squeeze(0).numpy()


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Who may ask
# --------------------------------------------------------------------------

# The same two checks as harness/server.py, kept here because this runs as a
# script in its own environment and does not import Harness.
#
# This answered every website with Access-Control-Allow-Origin: *. Nothing
# needed it — enroll.html is served from here, and Harness proxies from Python
# — but it let any page you visited list your voices, download the recording
# each was made from, and have it say whatever the page liked in your voice.

def host_allowed(host):
    """A Host header that names this machine, not a domain rebound onto it."""
    if host is None:
        return True
    name = host.strip().lower()
    if name.startswith("["):
        name = name[1:name.find("]")] if "]" in name else name[1:]
    elif name.count(":") == 1:
        name = name.split(":")[0]
    if not name:
        return False
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    return (name == "localhost" or "." not in name
            or name.endswith((".localhost", ".local", ".ts.net")))


def cross_site(headers):
    """True if a request that changes something came from another site's page."""
    origin = headers.get("Origin")
    if origin is None:
        return (headers.get("Sec-Fetch-Site") or "").lower() == "cross-site"
    if origin == "null":
        return True
    return (urllib.parse.urlparse(origin).netloc.lower()
            != (headers.get("Host") or "").strip().lower())


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    voice = None

    def log_message(self, *args):
        pass

    def reply(self, code, payload, content_type="application/json", **headers):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload)
        data = payload.encode() if isinstance(payload, str) else payload
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        for name, value in headers.items():
            self.send_header(name.replace("_", "-"), value)
        self.end_headers()
        self.wfile.write(data)

    def refused(self, changes=False):
        """Turn away a foreign host, or another site's page changing things."""
        if not host_allowed(self.headers.get("Host")):
            self.reply(403, {"error": "unrecognised host name"})
            return True
        if changes and cross_site(self.headers):
            self.reply(403, {"error": "refused: this came from another website"})
            return True
        return False

    def do_OPTIONS(self):
        self.reply(204, b"", "text/plain")

    def do_GET(self):
        if self.refused():
            return
        path = self.path.split("?")[0]
        if path == "/":
            with open(os.path.join(HERE, "enroll.html"), "rb") as handle:
                return self.reply(200, handle.read(), "text/html; charset=utf-8",
                                  Cache_Control="no-store")
        if path == "/health":
            return self.reply(200, {
                "ok": True,
                "model_loaded": self.voice.loaded,
                "device": self.voice.device,
                "script": ENROLMENT_SCRIPT,
                "voices": list_voices(),
            })
        if path == "/voices":
            return self.reply(200, {"voices": list_voices(),
                                    "script": ENROLMENT_SCRIPT})
        # /voices/<id>/sample.wav — the reference recording, for playback in
        # the picker so a voice can be recognised before it is chosen.
        if path.startswith("/voices/") and path.endswith("/sample.wav"):
            entry, wav = get_voice(path.split("/")[2])
            if not entry:
                return self.reply(404, {"error": "no such voice"})
            with open(wav, "rb") as handle:
                return self.reply(200, handle.read(), "audio/wav",
                                  Cache_Control="no-store")
        self.reply(404, {"error": "not found"})

    def body(self):
        try:
            length = max(0, int(self.headers.get("Content-Length", 0)))
        except (TypeError, ValueError):
            length = 0
        length = min(length, 50 * 1024 * 1024)
        return self.rfile.read(length) if length else b""

    def json_body(self):
        try:
            return json.loads(self.body() or b"{}")
        except ValueError:
            return None

    def do_DELETE(self):
        self.body()             # drained either way, or it poisons the next request
        if self.refused(changes=True):
            return
        path = self.path.split("?")[0]
        if path.startswith("/voices/"):
            voice_id = path.split("/")[2]
            if delete_voice(voice_id):
                return self.reply(200, {"ok": True, "voices": list_voices()})
            return self.reply(404, {"error": "no such voice"})
        self.reply(404, {"error": "not found"})

    def do_POST(self):
        if self.refused(changes=True):
            self.body()         # drain it, so the connection stays usable
            return
        path = self.path.split("?")[0]
        if path in ("/voices", "/enroll"):
            return self.enroll()
        if path.startswith("/voices/") and path.endswith("/select"):
            voice_id = path.split("/")[2]
            if select_voice(voice_id):
                return self.reply(200, {"ok": True, "voices": list_voices()})
            return self.reply(404, {"error": "no such voice"})
        if path.startswith("/voices/") and path.endswith("/rename"):
            payload = self.json_body() or {}
            entry = rename_voice(path.split("/")[2], payload.get("name", ""))
            if entry:
                return self.reply(200, {"ok": True, "voices": list_voices()})
            return self.reply(404, {"error": "no such voice"})
        if path == "/speak":
            return self.speak()
        self.reply(404, {"error": "not found"})

    def enroll(self):
        payload = self.json_body()
        if payload is None:
            return self.reply(400, {"error": "malformed request"})
        try:
            audio = bytes.fromhex(payload["wav_hex"])
        except (KeyError, ValueError) as exc:
            return self.reply(400, {"error": f"bad audio: {exc}"})

        transcript = payload.get("transcript") or ENROLMENT_SCRIPT
        name = payload.get("name") or "Untitled voice"
        try:
            entry = add_voice(name, audio, transcript,
                              level=bool(payload.get("normalise", True)))
        except Exception as exc:                              # noqa: BLE001
            return self.reply(400, {"error": f"could not read that audio: {exc}"})

        if entry["seconds"] < 3:
            delete_voice(entry["id"])
            return self.reply(400, {"error": "that clip is under three seconds — "
                                             "read the whole paragraph"})
        log(f"enrolled {entry['name']!r}: {entry['seconds']}s")
        self.reply(200, {"ok": True, "voice": entry, "voices": list_voices()})

    def speak(self):
        payload = self.json_body()
        if payload is None:
            return self.reply(400, {"error": "malformed request"})

        entry, reference_wav = get_voice(payload.get("voice_id", ""))
        if not entry:
            return self.reply(409, {"error": "no voice enrolled yet"})

        text = speakable(payload.get("text", ""))
        if not text:
            return self.reply(400, {"error": "nothing to say"})

        cached = cache_key(text, self.voice.device, entry["id"])
        if os.path.isfile(cached):
            with open(cached, "rb") as handle:
                return self.reply(200, handle.read(), "audio/wav",
                                  X_Harness_Cached="1", X_Harness_Voice=entry["id"])

        import numpy as np
        chunks = sentences(text)
        started = time.time()
        pieces = []
        for index, chunk in enumerate(chunks, 1):
            pieces.append(self.voice.speak(chunk, reference_wav, entry["transcript"]))
            # A short breath between sentences; without one they run together.
            if index < len(chunks):
                pieces.append(np.zeros(int(SAMPLE_RATE * 0.18), dtype=np.float32))
        audio = np.concatenate(pieces) if pieces else np.zeros(1, dtype=np.float32)
        data = to_wav_bytes(audio)

        os.makedirs(CACHE, exist_ok=True)
        with open(cached, "wb") as handle:
            handle.write(data)

        spoken = len(audio) / SAMPLE_RATE
        elapsed = time.time() - started
        log(f"spoke {spoken:.1f}s as {entry['name']!r} in {elapsed:.1f}s "
            f"(RTF {elapsed / max(spoken, 0.01):.2f}x, {len(chunks)} chunks)")
        self.reply(200, data, "audio/wav", X_Harness_Voice=entry["id"])


def main():
    parser = argparse.ArgumentParser(description="Harness voice sidecar.")
    parser.add_argument("--port", type=int, default=8421)
    parser.add_argument("--device", default=DEVICE, choices=["cpu", "mps"],
                        help="cpu is the measured default; see the note in the source")
    parser.add_argument("--warm", action="store_true",
                        help="Load the model now rather than on first use.")
    args = parser.parse_args()

    os.makedirs(CACHE, exist_ok=True)
    migrate_legacy()
    Handler.voice = Voice(args.device)
    if args.warm:
        Handler.voice._ensure()

    voices = list_voices()
    print(f"\nharness-voice  ->  http://127.0.0.1:{args.port}")
    print(f"  device   {args.device}")
    if voices:
        for entry in voices:
            mark = "*" if entry["selected"] else " "
            print(f"  {mark} {entry['name']:<28} {entry['seconds']:>5}s")
    else:
        print("  voices   none yet — open the page above and record one")
    print("\nCtrl-C to stop.\n")
    try:
        ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()

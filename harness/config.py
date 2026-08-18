"""
harness.config — everything kept on disk, as plain JSON under ~/.harness/.

Settings, presets, automations, the access token, and which model is chosen.
All of it is safe to edit by hand.
"""

import contextlib
import datetime
import json
import os
import secrets
import subprocess
import threading

BASE = os.path.expanduser("~/.harness")
CONFIG_FILE = os.path.join(BASE, "config.json")
AUTOMATIONS_FILE = os.path.join(BASE, "automations.json")
TOKEN_FILE = os.path.join(BASE, "access-token")
MODEL_CHOICE_FILE = os.path.join(BASE, "model")
MODELS_DIR = os.path.expanduser("~/llm/models")
SESSION_DIR = os.path.join(BASE, "sessions")

TOOL_NAMES = ("list_files", "read_file", "write_file", "edit_file",
              "web_search", "fetch_url", "run_command")

# The wording matters more than it looks. An earlier version said "use tools to
# find real answers instead of guessing", which pushed the model to run
# echo "Hello!" just to say hi. Tools have to be framed as the exception.
DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful assistant on a macOS machine.\n\n"
    "Answer directly from what you know. Only use a tool when the question "
    "genuinely needs information you do not have — something about this "
    "machine, a file, or current events. Never use a tool for greetings, "
    "chit-chat, or questions you can already answer.\n\n"
    "When you do have the answer, state it plainly in one or two sentences."
)

DEFAULTS = {
    "system_prompt": DEFAULT_SYSTEM_PROMPT,
    "thinking": False,
    "temperature": 0.6,
    "top_p": 0.95,
    "top_k": 20,
    "max_tokens": 1200,
    "max_steps": 8,
    # Tools can be switched off individually. A 3B model degrades as the tool
    # list grows, so trimming this is a real tuning knob, not decoration.
    "tools": dict.fromkeys(TOOL_NAMES, True),
    # Extra commands to treat as read-only, on top of the built-in allow-list.
    "extra_safe_commands": [],
    # When to stop and ask. Protected system paths are refused whatever these
    # say — "never" buys you speed, not the keys to /System.
    #   files:    always | outside | never   (outside = trust the project folder)
    #   commands: always | destructive | never
    "approve_files": "always",
    "approve_commands": "always",
    "preset": "balanced",
    # Relative paths in tool calls resolve against this. Without it every
    # request needs a full absolute path, which is unusable on a phone.
    "project_dir": "",
}

# Named starting points, so the common cases don't require understanding
# sampling parameters. Each lists only what it changes from DEFAULTS, plus
# `off` for the tools it withholds — spelling out all seven every time let
# them drift apart as tools were added.
PRESETS = {
    "balanced": {
        "label": "Balanced",
        "blurb": "Sensible defaults. Can read, search, and write files (asking first).",
    },
    "safe": {
        "label": "Read only",
        "blurb": "Can look at things and search the web, but cannot change anything.",
        "temperature": 0.5, "top_p": 0.9, "max_tokens": 1000, "max_steps": 6,
        "off": ["write_file", "edit_file"],
    },
    "quick": {
        "label": "Quick answers",
        "blurb": "Short replies, no web, no file changes. Fastest option.",
        "max_tokens": 600, "max_steps": 4,
        "off": ["write_file", "edit_file", "web_search", "fetch_url"],
    },
    "thorough": {
        "label": "Thorough",
        "blurb": "Extended reasoning and more tool steps. Slow but tries harder.",
        "temperature": 1.0, "max_tokens": 2000, "max_steps": 12, "thinking": True,
    },
}

# One-tap automations, phrased as jobs rather than prompts.
AUTOMATION_TEMPLATES = [
    {"name": "Disk space check", "schedule": {"kind": "daily", "time": "09:00"},
     "prompt": "Check how much free disk space this Mac has and say whether it "
               "is running low."},
    {"name": "Uptime check", "schedule": {"kind": "interval", "minutes": 360},
     "prompt": "Report how long this Mac has been running and the current load "
               "average."},
    {"name": "Downloads folder tidy-up reminder",
     "schedule": {"kind": "daily", "time": "18:00"},
     "prompt": "List what is in my Downloads folder and tell me how big it is."},
]

_lock = threading.Lock()


def _read(path, fallback):
    try:
        with open(path) as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return fallback


def _write(path, payload):
    """Write via a temp file so an interrupted save can't truncate the real one."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w") as handle:
        json.dump(payload, handle, indent=1)
    os.replace(temporary, path)


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

def load_config():
    stored = _read(CONFIG_FILE, {})
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in stored.items() if k in DEFAULTS})
    # Tools gain entries as the agent grows; fill in any the file predates.
    merged["tools"] = {**DEFAULTS["tools"], **stored.get("tools", {})}
    return merged


def save_config(patch):
    with _lock:
        current = load_config()
        current.update({k: v for k, v in patch.items() if k in DEFAULTS})
        _write(CONFIG_FILE, current)
        return current


def reset_config():
    with _lock:
        _write(CONFIG_FILE, dict(DEFAULTS))
    return load_config()


def apply_preset(name):
    """Overwrite the tuning values with a preset, keeping the system prompt."""
    preset = PRESETS.get(name)
    if not preset:
        return load_config()
    values = {k: v for k, v in preset.items() if k in DEFAULTS}
    values["tools"] = {t: t not in preset.get("off", ()) for t in TOOL_NAMES}
    # Anything the preset stays quiet about goes back to a shipped default, so
    # picking a preset twice can't leave a stray value from the one before.
    for key in ("max_tokens", "max_steps", "thinking"):
        values.setdefault(key, DEFAULTS[key])
    # Sampling falls back to the loaded model's profile rather than DEFAULTS —
    # otherwise picking a preset would quietly undo what set_model tuned.
    profile = profile_for(current_model())
    for key in ("temperature", "top_p", "top_k"):
        values.setdefault(key, profile[key])
    values["preset"] = name
    return save_config(values)


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------

# Per-model tuning, matched on a lowercased file-name prefix. `kv_kb` is what
# one token of context costs in the KV cache at f16, and `ctx` the context to
# open with — both mirror the PROFILES table in model.sh, which is what
# actually launches llama-server. Keep the two in step.
#
# The sampling is the half that governs chat: agent.py sends these values on
# every request, so they, not model.sh's flags, are what a reply is sampled
# with. Values come from each model's own card.
# `when_thinking` holds only what a reasoning turn changes — named apart from
# config's `thinking`, which is the bool for whether reasoning is on at all.
# Cards tune the two modes separately (Qwen asks for 1.0/0.95 while thinking
# against 0.7/0.80 while not), and listing just the deltas means a model that
# tunes nothing keeps whatever is configured.
MODEL_PROFILES = {
    "nanbeige":    {"kv_kb": 167.5, "ctx": 16384,
                    "temperature": 0.6, "top_p": 0.95, "top_k": 20,
                    "when_thinking": {"temperature": 1.0}},
    "qwen3.8-27b": {"kv_kb": 64.0, "ctx": 32768,
                    "temperature": 0.7, "top_p": 0.80, "top_k": 20,
                    "when_thinking": {"temperature": 1.0, "top_p": 0.95}},
}
DEFAULT_PROFILE = {"kv_kb": 80.0, "ctx": 16384,
                   "temperature": 0.6, "top_p": 0.95, "top_k": 20,
                   "when_thinking": {"temperature": 1.0}}


def profile_for(path):
    """The tuning profile for a model file, or middling defaults if it's new."""
    name = os.path.basename(path).lower()
    for prefix, profile in MODEL_PROFILES.items():
        if name.startswith(prefix):
            return profile
    return DEFAULT_PROFILE


def sampling_for(cfg, thinking, path=None):
    """
    The sampling values one turn should be sent with.

    Normally just what is configured. When reasoning is on, the loaded model's
    `when_thinking` deltas are layered over the top, because a card's instruct
    settings are usually wrong for a reasoning turn — Qwen's top_p of 0.80
    keeps the distribution far too tight to think in.

    Temperature is raised to the profile's value rather than pinned to it: the
    behaviour this replaces used a floor, and someone who has deliberately
    turned the temperature up should keep it.
    """
    values = {"temperature": cfg["temperature"], "top_p": cfg["top_p"],
              "top_k": cfg["top_k"]}
    if not thinking:
        return values
    profile = profile_for(current_model() if path is None else path)
    for key, value in profile.get("when_thinking", {}).items():
        values[key] = max(values[key], value) if key == "temperature" else value
    return values


def metal_budget_gb():
    """
    How much memory Metal will actually wire for the GPU.

    model.sh loads every layer onto the GPU (-ngl 99), so the weights and the
    whole KV cache have to fit in the GPU working set. That ceiling, not total
    RAM, is what a model has to fit under: macOS defaults it to about
    two-thirds of physical memory, and iogpu.wired_limit_mb overrides it when
    set. Returns 0.0 if neither can be read, which callers treat as "unknown".
    """
    try:
        limit = int(subprocess.run(["sysctl", "-n", "iogpu.wired_limit_mb"],
                                   capture_output=True, text=True,
                                   timeout=4).stdout)
        if limit > 0:
            return limit / 1024
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    try:
        total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (OSError, ValueError):
        return 0.0
    return (total / 1073741824) * 2 / 3


def list_models():
    """Every GGUF on disk, with its size and whether it will fit in memory."""
    found = []
    try:
        names = sorted(os.listdir(MODELS_DIR))
    except OSError:
        return found
    budget = metal_budget_gb()
    for name in names:
        if not name.endswith(".gguf"):
            continue
        path = os.path.join(MODELS_DIR, name)
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        profile = profile_for(path)
        # The same estimate model.sh prints: weights, a flat allowance for the
        # compute buffers, and the KV cache at q8_0 — half its f16 cost.
        estimate = (size / 1e9 + 0.4
                    + (profile["ctx"] * profile["kv_kb"] * 0.5) / 1048576)
        if not budget or estimate <= budget * 0.85:
            fit = "ok"
        elif estimate <= budget:
            fit = "tight"
        else:
            fit = "over"
        found.append({"name": name[:-5], "path": path,
                      "gb": round(size / 1e9, 1), "est_gb": round(estimate, 1),
                      "ctx": profile["ctx"], "fit": fit,
                      "budget_gb": round(budget, 1)})
    return found


def current_model():
    """The model that was picked, or — on a fresh install — whatever is there."""
    try:
        with open(MODEL_CHOICE_FILE) as handle:
            path = handle.read().strip()
        if os.path.isfile(path):
            return path
    except OSError:
        pass
    models = list_models()
    return models[0]["path"] if models else ""


def set_model(path):
    if not os.path.isfile(path):
        return False
    os.makedirs(BASE, exist_ok=True)
    with open(MODEL_CHOICE_FILE, "w") as handle:
        handle.write(path)
    # Sampling belongs to the weights, so it travels with them. agent.py reads
    # these from config.json on every request; without this, a model picked in
    # the UI would keep being sampled the way the previous one wanted.
    profile = profile_for(path)
    save_config({key: profile[key] for key in ("temperature", "top_p", "top_k")})
    return True


# --------------------------------------------------------------------------
# Automations
# --------------------------------------------------------------------------

def load_automations():
    items = _read(AUTOMATIONS_FILE, [])
    return items if isinstance(items, list) else []


def save_automations(items):
    with _lock:
        _write(AUTOMATIONS_FILE, items)
    return items


def upsert_automation(entry):
    entry.setdefault("id", secrets.token_hex(5))
    entry.setdefault("enabled", True)
    entry.setdefault("last_run", None)
    entry.setdefault("last_result", "")

    items = load_automations()
    for index, existing in enumerate(items):
        if existing.get("id") == entry["id"]:
            items[index] = {**existing, **entry}
            break
    else:
        items.append(entry)

    save_automations(items)
    return entry


def delete_automation(automation_id):
    save_automations([a for a in load_automations()
                      if a.get("id") != automation_id])


def record_run(automation_id, result):
    items = load_automations()
    for entry in items:
        if entry.get("id") == automation_id:
            entry["last_run"] = datetime.datetime.now().isoformat(timespec="seconds")
            entry["last_result"] = result[:2000]
    save_automations(items)


def is_due(entry, now=None):
    """
    Whether a scheduled automation should fire.

    Deliberately conservative: a missed window (laptop asleep, app closed) is
    skipped rather than replayed, because catching up would fire a burst of
    identical runs the moment the machine wakes.
    """
    if not entry.get("enabled"):
        return False
    now = now or datetime.datetime.now()
    schedule = entry.get("schedule") or {}
    kind = schedule.get("kind")

    try:
        last = datetime.datetime.fromisoformat(entry["last_run"])
    except (KeyError, TypeError, ValueError):
        last = None

    if kind == "interval":
        minutes = max(1, int(schedule.get("minutes", 60)))
        return last is None or (now - last).total_seconds() >= minutes * 60

    if kind == "daily":
        try:
            clock = str(schedule.get("time", "09:00")).split(":")
            hour, minute = (int(part) for part in clock)
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        except ValueError:
            return False
        # Fire once per day: skip if the last run was already past today's time.
        return now >= target and (last is None or last < target)

    return False  # "manual" and anything unrecognised


# --------------------------------------------------------------------------
# Access token — only enforced when the server is exposed beyond loopback
# --------------------------------------------------------------------------

def access_token():
    """Stable per-machine token, created on first use with 0600 permissions."""
    try:
        with open(TOKEN_FILE) as handle:
            existing = handle.read().strip()
        if existing:
            return existing
    except OSError:
        pass

    token = secrets.token_urlsafe(24)
    os.makedirs(BASE, exist_ok=True)
    # Opened with restrictive permissions before anything is in it.
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(token)
    return token


def rotate_token():
    with contextlib.suppress(OSError):
        os.remove(TOKEN_FILE)
    return access_token()

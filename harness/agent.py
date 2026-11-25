"""
harness.agent — the loop that turns a text generator into an agent.

The model only ever writes text. This module reads that text, notices when it
asked for a tool, runs the tool, and feeds the result back — then lets the model
keep going. That loop is the whole difference between a chatbot and an agent.

There is one loop, not two. Where its output goes is a Sink: the terminal
prints, the web server queues events, a scheduled automation throws away. The
same code runs `harness chat` and the browser, so a fix lands in both.

    harness chat "how much disk space is free?"
    harness chat            # interactive
"""

import argparse
import contextlib
import datetime
import http.client
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request

from . import config as settings
from . import tools

SERVER = "http://127.0.0.1:8080/v1/chat/completions"
PROPS = "http://127.0.0.1:8080/props"
# How long to wait for the next piece of a reply. The first piece is the slow
# one: the whole prompt is read before anything comes back, and a long prompt on
# a Mac in Low Power Mode can take minutes. At 300 the read timed out, the error
# was swallowed, and the turn ended on an empty reply with nothing to say why.
REQUEST_TIMEOUT = 900

# What trim_history keeps when it is not told otherwise: roughly 8K tokens of
# conversation, leaving the rest of a 16K window for the system prompt, the
# tool schemas, and the reply. run() sizes it to the real window instead.
HISTORY_CHAR_BUDGET = 24000

# For turning a context window into a character budget. Prose runs nearer four
# characters a token, but tool output — paths, JSON, page fragments — tokenises
# far worse, and overshooting the window is an error, not a slow answer.
CHARS_PER_TOKEN = 2.5
PROMPT_OVERHEAD_TOKENS = 2500    # the system prompt and the tool schemas

# Streamed tool calls arrive as plain text as often as they arrive parsed.
# Printing that raw dumps a wall of escaped source on the user, so output stops
# the moment this tag appears.
TAG = "<tool_call>"


# --------------------------------------------------------------------------
# Sinks — where a run reports to
# --------------------------------------------------------------------------

class Sink:
    """
    Somewhere for a run to report to. Silent by default, and refuses every
    approval — which is exactly right for an unattended run.
    """

    def token(self, text):
        """A piece of the visible answer."""

    def reasoning(self, text):
        """A piece of the model's thinking, when that is switched on."""

    def building(self, size, budget=0):
        """
        Bytes of tool-call argument so far, and how close the reply is to its
        token ceiling. Without this the model looks frozen for minutes while it
        writes a file, and there is no warning before the cap truncates it.
        """

    def tool_start(self, name, args):
        """About to run a tool."""

    def tool_result(self, name, result):
        """What the tool returned."""

    def error(self, message):
        """Something went wrong that the user needs to see."""

    def stats(self, seconds, tokens, steps):
        """What the turn cost, once it is over."""

    def confirm(self, question, detail="", kind=""):
        """
        Ask before doing something irreversible. Refusing is the safe default.

        `kind` is the tool being approved, so a front end can offer "allow this
        for the rest of the session" and remember the answer.
        """
        return False

    def cancelled(self):
        """True if the user has abandoned this turn."""
        return False


class TerminalSink(Sink):
    """Prints as it goes, and asks y/N at the prompt."""

    def __init__(self, verbose=False):
        self.verbose = verbose
        self.wrote = False
        self.tty = sys.stdout.isatty()
        self.step = 0
        self.granted = set()   # tools the user said "always" to, this run only

    def token(self, text):
        sys.stdout.write(text)
        sys.stdout.flush()
        self.wrote = True

    def reasoning(self, text):
        sys.stdout.write(f"\033[2m{text}\033[0m")
        sys.stdout.flush()

    def building(self, size, budget=0):
        # A multi-KB file argument takes minutes and produces no visible output
        # — without this the agent looks frozen.
        if self.tty:
            near = "  — near the reply limit" if budget > 0.85 else ""
            sys.stdout.write(f"\r  \033[2m… generating ({size:,} bytes){near}\033[0m")
            sys.stdout.flush()

    def _clear(self):
        if self.tty:
            sys.stdout.write("\r" + " " * 46 + "\r")
            sys.stdout.flush()

    def tool_start(self, name, args):
        self._clear()
        self.step += 1
        shown = ", ".join(f"{k}={v!r}" for k, v in args.items())
        print(f"  \033[2m[{self.step}] {name}({shown})\033[0m")

    def tool_result(self, name, result):
        if self.verbose:
            for line in result.splitlines()[:10]:
                print(f"      \033[2m{line}\033[0m")

    def error(self, message):
        self._clear()
        print(f"  \033[2m{message}\033[0m")

    def stats(self, seconds, tokens, steps):
        rate = f", {tokens / seconds:.0f} tok/s" if seconds and tokens else ""
        print(f"  \033[2m{seconds:.1f}s · {tokens or '?'} tokens{rate}\033[0m")

    def confirm(self, question, detail="", kind=""):
        self._clear()
        if kind and kind in self.granted:
            return True
        if detail:
            print(detail)
        # A piped or scripted run has nobody to answer, and silently proceeding
        # is the wrong default when the action overwrites a file.
        if not sys.stdin.isatty():
            print("  (not a terminal — declining automatically)")
            return False
        try:
            answer = input(f"  {question} [y/N/a=always] ").strip().lower()
        except EOFError:
            return False
        if answer == "a" and kind:
            self.granted.add(kind)
        return answer in ("y", "a")

    def end_turn(self):
        self._clear()
        if self.wrote:
            print()
            self.wrote = False


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------

SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def new_session_id():
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def session_path(session_id):
    """Where a session lives. Only alphanumeric, hyphens, and underscores allowed."""
    sid = (session_id or "").strip()
    if not SESSION_ID_RE.match(sid):
        raise ValueError(f"Invalid session id: {session_id!r}")
    return os.path.join(settings.SESSION_DIR, sid + ".json")


def save_session(session_id, history):
    """
    Persist after every turn, so an interrupted run keeps what it had.

    Written to a temp file and renamed, because a crash partway through a plain
    write would leave a truncated file that no longer parses as JSON.
    """
    if not history:
        return
    title = next((m.get("content") for m in history
                  if m.get("role") == "user" and m.get("content")), "")
    if not title:
        has_img = any(m.get("role") == "user" and m.get("images") for m in history)
        title = "(image)" if has_img else "untitled"
    try:
        settings._write(session_path(session_id), {
            "id": session_id,
            "updated": datetime.datetime.now().isoformat(timespec="seconds"),
            "title": " ".join(str(title).split())[:70],
            "messages": history,
        })
    except (OSError, ValueError) as exc:
        print(f"  (could not save session: {exc})")


def list_sessions():
    """Saved sessions, newest first."""
    found = []
    try:
        names = os.listdir(settings.SESSION_DIR)
    except OSError:
        return found
    for name in names:
        if name.endswith(".json"):
            session = settings._read(
                os.path.join(settings.SESSION_DIR, name), None)
            if session:
                found.append(session)
    # Tie-broken on the id, which is a timestamp, so two sessions saved in the
    # same second still come back in a stable order rather than in whatever
    # order the directory happened to be read in. "latest" depends on this.
    return sorted(found, key=lambda s: (s.get("updated", ""), s.get("id", "")),
                  reverse=True)


def load_session(session_id):
    """Return (id, messages), or (id, []) if there is no such session."""
    if session_id == "latest":
        sessions = list_sessions()
        if not sessions:
            return "", []
        return sessions[0]["id"], sessions[0].get("messages", [])
    try:
        path = session_path(session_id)
    except ValueError:
        return session_id, []
    session = settings._read(path, None)
    if not session:
        return session_id, []
    return session["id"], session.get("messages", [])


def delete_session(session_id):
    try:
        os.remove(session_path(session_id))
        return True
    except (OSError, ValueError):
        return False


def rewind_session(session_id):
    """
    Drop the most recent exchange and hand back the message that started it.

    Retry and edit are the same operation underneath: unwind to just before you
    asked, then either send it again or change it first.
    """
    _, messages = load_session(session_id)
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") != "user":
            continue
        kept = messages[:index]
        # save_session ignores an empty history, which would leave the old file
        # on disk complete with the turn we just unwound.
        if kept:
            save_session(session_id, kept)
        else:
            delete_session(session_id)
        return messages[index].get("content", "")
    return ""


def as_markdown(session_id):
    """A saved session as a readable document."""
    session = settings._read(session_path(session_id), None)
    if not session:
        return ""
    lines = [f"# {session.get('title') or 'Harness session'}",
             "", f"*{session.get('updated', '')}*", ""]
    for message in session.get("messages", []):
        role, content = message.get("role"), (message.get("content") or "").strip()
        if role == "user":
            if message.get("images"):
                names = ", ".join(os.path.basename(p) for p in message["images"])
                note = f"*[Attached: {names}]*"
                content = f"{note}\n\n{content}".strip() if content else note
            lines += ["## You", "", content, ""]
        elif role == "assistant" and content:
            lines += ["## Harness", "", content, ""]
        elif role == "assistant" and message.get("tool_calls"):
            for call in message["tool_calls"]:
                name = call.get("function", {}).get("name", "?")
                args = call.get("function", {}).get("arguments", "")
                lines += [f"> **{name}**", "", "```json", args.strip() or "{}", "```", ""]
        elif role == "tool" and content:
            lines += ["```", content, "```", ""]
    return "\n".join(lines)


def server_props():
    """
    What the running llama-server can do: its context window, and whether it
    can see images.

    Asked of the server rather than read from the model's profile, because
    `harness model -c` overrides the profile, and only the server knows
    whether a vision projector was actually loaded. The profile is the
    fallback for when the server cannot say.
    """
    found = {"n_ctx": settings.profile_for(settings.current_model())["ctx"],
             "vision": False}
    try:
        with urllib.request.urlopen(PROPS, timeout=2) as response:
            props = json.load(response)
        n_ctx = int(props["default_generation_settings"]["n_ctx"])
        if n_ctx > 0:
            found["n_ctx"] = n_ctx
        found["vision"] = bool((props.get("modalities") or {}).get("vision"))
    except (OSError, ValueError, KeyError, TypeError, AttributeError,
            urllib.error.URLError, http.client.HTTPException):
        pass
    return found


def context_window():
    """The context llama-server is actually running with."""
    return server_props()["n_ctx"]


def history_budget(cfg, n_ctx):
    """
    How many characters of conversation fit in a window of n_ctx tokens.

    What is left after the system prompt, the tool schemas and room for the
    reply. The reply's share is capped at a quarter of the window: max_tokens
    is a ceiling on one answer, and reserving all of a generous one would
    leave almost nothing for the conversation it is answering.
    """
    reply = min(int(cfg.get("max_tokens", 1200)), n_ctx // 4)
    tokens = n_ctx - PROMPT_OVERHEAD_TOKENS - reply
    return max(4000, int(tokens * CHARS_PER_TOKEN))


def trim_history(history, budget=HISTORY_CHAR_BUDGET):
    """
    Drop the oldest exchanges once the conversation outgrows the budget.

    Keeping everything would push the system prompt out of context, which is
    worse than forgetting turn one.

    Where it cuts matters as much as how much. llama-server reuses its work
    on a prompt only as far as the prompt starts the same way, so a cut that
    creeps forward a message at a time changes the opening at every step, and
    every step re-reads the whole conversation from scratch — measured at
    70-90 seconds a step on a 12K-token chat with a 9B, which is most of what
    made a phone conversation feel broken. So a cut only ever lands at the
    start of an exchange, and only at fixed places spaced a third of the budget
    apart along the history. Those places depend on what came first, which
    never changes, so the chosen one holds still while the conversation grows
    by about a third of the budget before it has to move again.
    """
    sizes = [_size(message) for message in history]
    total = sum(sizes)
    if total <= budget:
        return list(history)

    spacing = max(1, budget // 3)
    before = 0               # characters ahead of the message being looked at
    mark = spacing           # the next place a cut may land
    for index, message in enumerate(history):
        if message.get("role") == "user" and before >= mark:
            if total - before <= budget:
                return history[index:]
            mark = before + spacing
        before += sizes[index]

    # The current exchange alone is over budget — no cut at an exchange
    # boundary leaves room. Its question stays if it fits at all: dropping it
    # left the model reading tool output with no idea what it was looking for.
    # What goes instead is the oldest work in the exchange, a whole step at a
    # time, so a tool result never outlives the call that asked for it.
    start = max((i for i, m in enumerate(history) if m.get("role") == "user"),
                default=-1)
    question = [history[start]] if start >= 0 else []
    if question and _size(question[0]) <= budget:
        steps = []
        for message in history[start + 1:]:
            if message.get("role") == "tool" and steps:
                steps[-1].append(message)
            else:
                steps.append([message])
        room = budget - _size(question[0])
        kept = []
        for step in reversed(steps):
            cost = sum(_size(m) for m in step)
            if cost > room and kept:
                break
            kept = step + kept
            room -= cost
        while kept and kept[0].get("role") == "tool":
            kept.pop(0)
        return question + kept

    # Not even the question fits, so keep what is newest and drop the rest.
    kept = []
    for message in reversed(history):
        budget -= _size(message)
        if budget < 0:
            break
        kept.append(message)
    kept.reverse()

    # Never open on a tool result — it would reference a call that is now gone.
    while kept and kept[0].get("role") == "tool":
        kept.pop(0)
    return kept


# What one picture costs, in the characters the budget counts. A shown image
# takes ~1,000-1,400 tokens of context, which is about this much text.
IMAGE_CHARS = 3500


def _size(message):
    """What a message costs the budget: its words, its thinking, and overhead."""
    images = (1 if message.get("image") else 0) + len(message.get("images") or ())
    return (len(str(message.get("content") or ""))
            + len(str(message.get("reasoning_content") or ""))
            + IMAGE_CHARS * images + 200)


def for_model(history):
    """
    The conversation as the model is sent it.

    Its thinking goes back with it for the exchange in progress, and only that
    one. Sent without, the chat template shows every earlier step of the turn
    as an empty <think></think> followed by a tool call, and the model copies
    the pattern: it writes its next call inside its thinking and never closes
    it, so the server files the whole step as reasoning — no answer, no tool
    call, and a turn that simply stopped. Measured against the running model,
    8 of 12 second steps hid their call that way with the thinking dropped,
    and none of 12 with it kept. Earlier exchanges go without it, which is all
    the template keeps of them anyway.
    """
    last = max((i for i, m in enumerate(history) if m.get("role") == "user"),
               default=-1)
    sent = []
    for index, message in enumerate(history):
        current = index > last
        if "reasoning_content" in message and not current:
            message = {k: v for k, v in message.items() if k != "reasoning_content"}
        if "image" in message:
            # A picture view_image showed goes back with its tool result for the
            # rest of the turn, as the image itself. The session keeps only its
            # path; a later turn gets the text alone, which is all it needs.
            url = tools.image_url(message["image"]) if current else ""
            message = {k: v for k, v in message.items() if k != "image"}
            if url:
                message["content"] = [
                    {"type": "text", "text": str(message.get("content") or "")},
                    {"type": "image_url", "image_url": {"url": url}}]
        if "images" in message:
            # Pictures attached by the user go back only for the active
            # exchange (index == last), just like view_image results. Earlier
            # turns keep only the text so images don't eat future context.
            paths = list(message["images"] or ()) if index == last else []
            message = {k: v for k, v in message.items() if k != "images"}
            urls = [u for u in (tools.image_url(p) for p in paths) if u]
            if urls:
                parts = [{"type": "text", "text": str(message.get("content") or "")}]
                parts += [{"type": "image_url", "image_url": {"url": u}} for u in urls]
                message["content"] = parts
            elif not message.get("content"):
                message["content"] = "[attached image]"
        sent.append(message)
    return sent


def system_prompt(cfg):
    """
    The configured prompt, plus what the model cannot know about this machine.

    Without the home folder in front of it the model guessed: it listed
    /Users/username/Downloads, then /Users/$(whoami)/Downloads, then asked the
    user where their own Downloads folder was. The date is there because
    searches went out for "2025 2026", and the working folder because a bare
    "ls" otherwise runs somewhere the model can only guess at.
    """
    home = os.path.expanduser("~")
    facts = [f"The user's home folder is {home} (so ~/Downloads is {home}/Downloads)."]
    project = cfg.get("project_dir")
    if project:
        # Put as the user thinks of it. "Relative paths work in X" was read as
        # a technicality: asked to fix the tests "here", the model searched the
        # whole home folder for files that were in front of it all along.
        facts.append(f"The folder you are working in is {project}: when the user "
                     "says \"here\" or names a file without a folder, it is there. "
                     "Refer to files in it by name — paths are relative to it, and "
                     "shell commands already run in it.")
    else:
        facts.append("No project folder is set, so use full paths starting with ~ or /.")
    # The model knows Linux: it tried `ps --sort` six ways on a Mac.
    facts.append("This is macOS, so shell tools are the BSD versions — GNU-only "
                 "options such as `ps --sort` do not exist here.")
    # A Mac has python3 and no `python`, and every coding task opened with a
    # "command not found" and a second try.
    if not shutil.which("python") and shutil.which("python3"):
        facts.append("Run Python as python3; there is no `python` command here.")
    today = datetime.date.today()
    facts.append(f"Today is {today:%A} {today.day} {today:%B %Y}.")
    return f"{str(cfg.get('system_prompt') or '').rstrip()}\n\n{' '.join(facts)}"


# --------------------------------------------------------------------------
# Reading what the model said
# --------------------------------------------------------------------------

def parse_xml_tool_calls(content):
    """
    Recover tool calls that arrived as plain text.

    Nanbeige emits tool calls in its own XML dialect, and llama-server's parser
    only reliably converts the first one into a structured `tool_calls` field —
    later turns come back as raw text like:

        <tool_call><function=read_file><parameter=path>/some/file</parameter></function></tool_call>

    Without this the loop sees "no tool calls", treats the XML as the final
    answer, and stops one step short.
    """
    calls = []
    # Also recover a call whose closing </tool_call> was omitted after </function>,
    # or a JSON object inside <tool_call>...</tool_call>, while still ignoring a
    # truncated call that never reached </function> or }.
    pattern = r"<tool_call>(.*?)(?:</tool_call>|(?<=</function>)\s*$)"
    for block in re.findall(pattern, content, re.DOTALL):
        name = re.search(r"<function=([\w.-]+)>", block)
        if not name:
            try:
                parsed = json.loads(block.strip())
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(parsed, dict) and parsed.get("name"):
                raw_args = parsed.get("arguments", {})
                if not isinstance(raw_args, str):
                    raw_args = json.dumps(raw_args if isinstance(raw_args, dict) else {})
                calls.append({"id": f"xml_{len(calls)}", "type": "function",
                              "function": {"name": str(parsed["name"]),
                                           "arguments": raw_args}})
            continue
        func_name = name.group(1)
        args = {}
        # A parameter runs until its own closing tag, the next parameter, the
        # end of the function, or the end of the block — models close these
        # tags inconsistently, and one unclosed tag must not eat the rest.
        parameter = re.compile(
            r"<parameter=([\w.-]+)>(.*?)(?=</parameter>|<parameter=|</function>|$)",
            re.DOTALL)
        for match in parameter.finditer(block):
            args[match.group(1)] = match.group(2).strip()
        calls.append({"id": f"xml_{len(calls)}", "type": "function",
                      "function": {"name": func_name,
                                   "arguments": json.dumps(args)}})
    return calls


def clean_argument(value):
    """
    Strip XML bleed out of a tool argument.

    When the model emits two tool calls in one response, llama-server's parser
    closes the first and dumps everything after it into that call's argument:

        command = 'uptime\\n</function>\\n</tool_call>\\n<tool_call>...'

    Left alone that string trips the shell-metacharacter guard, gets refused,
    and the model retries into the same broken parse forever. Cut at the first
    closing tag and keep the part the model actually meant.
    """
    if not isinstance(value, str):
        return value
    for tag in ("</parameter>", "</function>", "</tool_call>", "<tool_call>"):
        cut = value.find(tag)
        if cut != -1:
            value = value[:cut]
    return value.strip()


def _parsed_arguments(call):
    """A call's arguments as the model sent them: a dict, or why they are not one."""
    raw = (call.get("function") or {}).get("arguments")
    if isinstance(raw, dict):            # some servers send them already parsed
        return raw, ""
    try:
        args = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError) as exc:
        return {}, f"they are not valid JSON ({getattr(exc, 'msg', exc)})"
    if not isinstance(args, dict):
        return {}, "they are not a JSON object of named arguments"
    return args, ""


def call_arguments(call):
    """The arguments of one tool call, parsed and cleaned of XML bleed."""
    args, _ = _parsed_arguments(call)
    return {k: clean_argument(v) for k, v in args.items()}


def argument_problem(call):
    """
    Why a call cannot be run as asked, or "" if it can.

    Unparseable arguments used to become no arguments at all, so the tool ran
    with nothing — read_file of "", a command of "" — and the model was told
    the file did not exist rather than that it had written the call wrong.
    """
    _, problem = _parsed_arguments(call)
    if not problem:
        return ""
    name = (call.get("function") or {}).get("name") or "the tool"
    return (f"Error: the call to {name} was not run because its arguments could "
            f"not be read: {problem}. Call it again with a JSON object.")


# --------------------------------------------------------------------------
# One completion
# --------------------------------------------------------------------------

class Completion:
    """What one request to the model came back with."""

    def __init__(self):
        self.text = ""        # the answer, as the user sees it
        self.reasoning = ""   # the thinking that led to it, when that is on
        self.calls = []       # the tools it asked for
        self.tokens = 0       # what it cost, when the server says
        self.finish = ""      # why the server stopped: stop, length, tool_calls
        self.error = ""       # why it failed, when it did — for the user to read


class Forward:
    """
    Passes a growing piece of text to the sink, and stops where a tool call
    starts.

    Holds back the last few characters while they could still be the start of
    the tag, so a raw "<tool" never reaches the screen. It used to: and when
    the tag completed, the whole answer so far was sent a second time, so
    every "Let me check." before a tool call came out twice.
    """

    def __init__(self, emit, batch=1):
        self.emit = emit
        self.batch = batch
        self.text = ""
        self.shown = 0
        self.stopped = False

    def add(self, piece):
        self.text += piece
        if self.stopped:
            return
        cut = self.text.find(TAG, self.shown)
        if cut != -1:
            self._flush(cut, trim=True)
            self.stopped = True
            return
        safe = len(self.text)
        for size in range(min(len(TAG) - 1, len(self.text)), 0, -1):
            if TAG.startswith(self.text[-size:]):
                safe -= size
                break
        # Trailing space waits for the next word, so the blank lines a model
        # puts before a tool call never reach the screen.
        while safe > self.shown and self.text[safe - 1].isspace():
            safe -= 1
        if safe - self.shown >= self.batch:
            self._flush(safe)

    def close(self):
        if not self.stopped:
            self._flush(len(self.text))

    def _flush(self, upto, trim=False):
        if upto > self.shown:
            piece = self.text[self.shown:upto]
            piece = piece.rstrip() if trim else piece
            if piece:
                self.emit(piece)
            self.shown = upto


def server_message(error):
    """The reason llama-server gave for refusing a request, from its error body."""
    try:
        body = json.loads(error.read() or b"{}")
        detail = body.get("error", body)
        message = detail.get("message") if isinstance(detail, dict) else detail
        if message:
            return str(message)
    except (OSError, ValueError, AttributeError):
        pass
    return str(error)


def stream(messages, cfg, sink, use_tools=True):
    """
    Stream one completion, reporting to `sink` as it arrives.

    Returns a Completion. Streaming is the whole point: the model generates
    around 30 tokens/sec, so a 400-token answer takes ~13s. Held back until
    complete that reads as a hang; streamed it reads as fast typing.

    Nothing that goes wrong here is swallowed any more. A dropped connection,
    a timeout, or the server refusing the request all used to end with an
    empty reply and no word of why, which is indistinguishable from the model
    simply stopping — the exact symptom that made Harness look unreliable.
    """
    thinking = bool(cfg.get("thinking"))
    payload = {
        "messages": messages,
        "tools": tools.schemas_for(cfg) if use_tools else [],
        # Reasoning turns are sampled differently from instruct ones, and by
        # more than temperature alone — see MODEL_PROFILES in config.py.
        **settings.sampling_for(cfg, thinking),
        "max_tokens": cfg["max_tokens"],
        "stream": True,
        # Ask for the token counts in a final chunk, so the cost of a turn is
        # measured rather than guessed. Servers that ignore this just don't
        # send it, and the readout falls back to wall-clock alone.
        "stream_options": {"include_usage": True},
        "chat_template_kwargs": {"enable_thinking": thinking},
    }
    request = urllib.request.Request(
        SERVER, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    reply = Completion()

    try:
        response = urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT)
    except urllib.error.HTTPError as exc:
        # Reaching the server and being refused by it are different failures,
        # and the refusal says why — usually that the conversation no longer
        # fits the context window. It used to be reported as "cannot reach".
        with exc:
            reply.error = f"The model server refused the request: {server_message(exc)}"
        return reply
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        reason = getattr(exc, "reason", exc)
        reply.error = (f"Cannot reach the model server at {SERVER}: {reason}. "
                       "Start it with: harness model")
        return reply

    if hasattr(sink, "active_response"):
        sink.active_response = response

    calls = {}       # index -> partial tool call, assembled across deltas
    building = 0     # bytes of tool-call payload, for the progress report
    ended = False    # the server said it had finished, rather than just going quiet
    answer = Forward(sink.token, batch=4)
    thought = Forward(sink.reasoning)

    # Fraction of max_tokens spent so far. The server only reports usage at the
    # end, so estimate from characters — roughly 3.6 per token — which is
    # accurate enough to warn before a long tool call is cut off mid-string.
    cap = max(1, int(cfg["max_tokens"]))
    def spent():
        return min(1.0, (len(answer.text) + len(thought.text) + building) / 3.6 / cap)

    try:
        with response:
            for raw in response:
                # Stop has to mean stop. Checked per chunk rather than per
                # completion: a tool call that takes minutes to write would
                # otherwise ignore the button until it had finished writing.
                # Leaving the block closes the socket, which tells llama.cpp to
                # abandon the generation too.
                if sink.cancelled():
                    with contextlib.suppress(Exception):
                        response.close()
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data: "):
                    continue
                body = line[6:]
                if body == "[DONE]":
                    ended = True
                    break
                try:
                    chunk = json.loads(body)
                except json.JSONDecodeError:
                    continue
                if not isinstance(chunk, dict):
                    continue
                # A failure partway through arrives as a chunk of its own, with
                # no choices — it used to be skipped like a usage chunk.
                if chunk.get("error"):
                    detail = chunk["error"]
                    message = (detail.get("message") if isinstance(detail, dict)
                               else detail) or "unknown error"
                    reply.error = f"The model server stopped partway: {message}"
                    break
                # The usage chunk carries no choices, so read it before indexing.
                # Count generated tokens, not the total: a cached prompt costs no
                # wall-clock, so including it reported things like 4460 tok/s.
                if chunk.get("usage"):
                    usage = chunk["usage"]
                    reply.tokens = (usage.get("completion_tokens")
                                    or usage.get("total_tokens") or reply.tokens)
                choices = chunk.get("choices") or []
                if not choices or not isinstance(choices[0], dict):
                    continue
                choice = choices[0]
                reply.finish = choice.get("finish_reason") or reply.finish
                delta = choice.get("delta") or {}

                # Reasoning tokens arrive either in `reasoning_content` (DeepSeek)
                # or enclosed in <think> tags. Forward them so the user sees progress.
                reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                if reasoning:
                    thought.add(reasoning)

                content = delta.get("content") or ""
                if content:
                    answer.add(content)

                for call in delta.get("tool_calls") or []:
                    idx = call.get("index", 0)
                    if idx not in calls:
                        calls[idx] = {"id": call.get("id", ""), "type": "function",
                                      "function": {"name": "", "arguments": ""}}
                    function = call.get("function") or {}
                    if function.get("name"):
                        calls[idx]["function"]["name"] += function["name"]
                    if function.get("arguments"):
                        calls[idx]["function"]["arguments"] += function["arguments"]
                        building += len(function["arguments"])
                        sink.building(building, spent())
    except (OSError, urllib.error.URLError, http.client.HTTPException) as exc:
        # Stop closes the socket on purpose, and that lands here too.
        if not sink.cancelled():
            reply.error = ("The connection to the model server dropped partway "
                           f"through the reply ({getattr(exc, 'reason', exc)}).")
    finally:
        if hasattr(sink, "active_response"):
            sink.active_response = None

    # A connection that dies partway through a streamed reply does not raise:
    # reading it just stops, exactly as if the reply were over. llama-server
    # ends every reply it finishes with a reason and [DONE], so one that has
    # neither was cut off — a crash, or the Mac running out of memory.
    if not (ended or reply.finish or reply.error or sink.cancelled()):
        reply.error = ("The model server stopped partway through the reply without "
                       "finishing it. It may have crashed or run out of memory — "
                       "see Settings → System & Controls → Model log.")

    answer.close()
    thought.close()

    reply.text = answer.text.strip()
    reply.calls = [calls[k] for k in sorted(calls)]
    # Recover calls the server emitted as plain XML rather than parsing.
    if not reply.calls and TAG in reply.text:
        reply.calls = parse_xml_tool_calls(reply.text)
    # And calls written inside the thinking, by a model that never closed it.
    # The server files all of that as reasoning, so the step looked empty and
    # the turn ended — though the call was there, correct, every time. Only
    # when there is no answer: a call the model thought about and then chose
    # not to make is not one to run.
    reasoning = thought.text
    if not reply.calls and not reply.text and TAG in reasoning:
        reply.calls = parse_xml_tool_calls(reasoning)
    if TAG in reasoning:
        reasoning = reasoning[:reasoning.find(TAG)]
    reply.reasoning = reasoning.strip()
    return reply


# What the user is told when the model gives nothing back, even when asked again.
EMPTY_REPLY = ("The model stopped without answering or using a tool, twice in a "
               "row, so the turn ends here. Send the message again, or say "
               "\"continue\".")


def complete(messages, cfg, sink, use_tools=True):
    """
    One step's reply, asked for a second time if it comes back with nothing.

    An empty reply — no answer and no tool call — is never what was meant.
    It is what this model produces when its thinking goes wrong, so the second
    attempt goes without thinking: asked that way, the same steps came back as
    proper tool calls every time.
    """
    reply = stream(messages, cfg, sink, use_tools)
    if reply.error or reply.text or reply.calls or sink.cancelled():
        return reply
    again = stream(messages, dict(cfg, thinking=False), sink, use_tools)
    again.tokens += reply.tokens
    return again


def assistant_message(reply, calls=None):
    """A reply as it goes into the history — with its thinking, when it had any."""
    message = {"role": "assistant", "content": "" if calls else reply.text}
    if calls:
        message["tool_calls"] = calls
    if reply.reasoning:
        message["reasoning_content"] = reply.reasoning
    return message


# Tools whose result can change what other tools see. After one runs, a repeat
# of anything earlier is a fresh look, not the model going round in circles.
CHANGES_THINGS = {"write_file", "edit_file", "run_command"}

# How many pictures one turn may look at. Asked to email the newest version of
# each photo in a folder, the model looked at twelve full-size images — some
# twice, some that did not exist — to tell versions apart that their names
# already told apart. Each costs ~1,000-1,400 tokens of context and seconds of
# encoding, and twelve pushed the turn past its budget.
PICTURES_PER_TURN = 6

# How many times the user can decline the same tool in one turn before the loop
# refuses further attempts automatically. A 9B model ignored the refusal wording
# and kept tweaking denied commands six to eleven times in a row.
DENIALS_PER_TOOL = 2


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------

def run(history, user_input, cfg, sink, images=None):
    """Run one exchange to completion, extending `history` in place."""
    user_message = {"role": "user", "content": user_input}
    if images:
        user_message["images"] = list(images)
    history.append(user_message)
    asked = len(history) - 1          # where this exchange starts
    system = {"role": "system", "content": system_prompt(cfg)}
    # Sized to the window the server really has. A fixed 24K-character budget
    # written for a 16K window kept trimming a 32K one at half its capacity,
    # and every trim cost a full re-read of the prompt (see trim_history).
    props = server_props()
    budget = history_budget(cfg, props["n_ctx"])
    # A model with no vision projector is not offered a tool it cannot use:
    # the server refuses a request with an image in it outright.
    if not props["vision"]:
        cfg = dict(cfg, tools={**cfg.get("tools", {}), "view_image": False})

    seen = {}
    denials = {}
    repeats = 0
    started = time.monotonic()
    tokens = 0
    steps = 0
    max_steps = max(1, int(cfg["max_steps"]))

    def done():
        sink.stats(round(time.monotonic() - started, 1), tokens, steps)

    def ask(extra=(), use_tools=True):
        nonlocal tokens, budget
        # Re-trimmed every step, not just at the start. The budget was applied
        # once to the conversation and then several thousand characters of
        # tool output were appended to it, which is how a turn that read two
        # large files pushed the system prompt out of a 16K window mid-turn.
        # Always from the whole history: trimming an already-trimmed list put
        # its cut points somewhere else, and moved the opening between turns.
        # The extra goes on after trimming, not before. The nudge that ends a
        # stuck turn is a user message, and trimmed along with the rest it
        # counted as the start of a new exchange: a turn that had outgrown the
        # budget was cut back to the nudge alone, and the model, asked to
        # "answer my original question" with no question in front of it,
        # recited its own system prompt.
        trimmed = trim_history(for_model(history), budget)
        messages = [system] + trimmed + list(extra)
        reply = complete(messages, cfg, sink, use_tools)
        tokens += reply.tokens
        # CHARS_PER_TOKEN is an estimate, and dense JSON/paths can overshoot
        # n_ctx slightly. Trim tighter once if doing so actually drops steps.
        if (reply.error and "context" in reply.error.lower()
                and "refused" in reply.error.lower()):
            smaller = max(2000, min(budget // 2, sum(_size(m) for m in trimmed) // 2))
            tighter = trim_history(for_model(history), smaller)
            if len(tighter) < len(trimmed):
                budget = smaller
                messages = [system] + tighter + list(extra)
                reply = complete(messages, cfg, sink, use_tools)
                tokens += reply.tokens
        return reply

    def answered(reply):
        """Record a final reply, or say plainly why there is none."""
        if reply.error:
            sink.error(reply.error)
        elif reply.finish == "length":
            sink.error(f"The reply was cut off at its length limit ({cfg['max_tokens']} "
                       "tokens). Raise Max reply in Settings → Model & Behavior, "
                       "or ask for something shorter.")
        if reply.text or reply.reasoning:
            history.append(assistant_message(reply))
        elif not reply.error and not sink.cancelled():
            sink.error(EMPTY_REPLY)

    for _ in range(max_steps):
        if sink.cancelled():
            return done()
        steps += 1
        reply = ask()
        if reply.error or sink.cancelled() or not reply.calls:
            answered(reply)
            return done()

        history.append(assistant_message(reply, reply.calls))

        step_repeated = False
        for index, call in enumerate(reply.calls):
            name = (call.get("function") or {}).get("name", "")
            args = call_arguments(call)
            sink.tool_start(name, args)

            problem = argument_problem(call)
            if problem and reply.finish == "length" and index == len(reply.calls) - 1:
                # Not a malformed call: the reply ran out of room while writing it.
                problem = (f"Error: the call to {name} was cut off at the reply "
                           f"length limit (max_tokens {cfg['max_tokens']}) and was "
                           "not run. Do it in smaller pieces — for a long file, "
                           "write part of it and add the rest with edit_file.")
                sink.error(f"A {name} call was cut off at the reply length limit "
                           f"({cfg['max_tokens']} tokens), so it was not run.")

            # Running the same call twice means the model is stuck. Replaying
            # the first result with a nudge ends the turn instead of looping.
            signature = (name, json.dumps(args, sort_keys=True))
            pictures = sum(1 for m in history[asked:] if m.get("image"))
            if problem:
                result = problem
            elif denials.get(name, 0) >= DENIALS_PER_TOOL:
                step_repeated = True
                result = (f"Error: the user already declined {name} "
                          f"{denials[name]} times this turn, so it will not be "
                          "asked again. Work with what you have or ask the user "
                          "in plain text.")
            elif name == "view_image" and pictures >= PICTURES_PER_TURN and signature not in seen:
                result = (f"Error: {PICTURES_PER_TURN} pictures have been shown this "
                          "turn, which is as many as one turn can hold. Work from what "
                          "you have seen, or ask the user which pictures matter.")
            elif signature in seen:
                step_repeated = True
                result = (f"{seen[signature]}\n\n[You already ran this and got "
                          "the result above. Do not call it again with the same "
                          "arguments — move on to the next step or answer the user.]")
            else:
                result = tools.call(name, args, cfg, sink)
                if str(result).startswith("Error: the user declined"):
                    denials[name] = denials.get(name, 0) + 1
                # Once something actually changed, an earlier look is out of
                # date: run the tests, edit, run them again. A failed or
                # refused command changed nothing — clearing `seen` then let
                # the model re-fetch the same URL four times after denied curls.
                elif name in CHANGES_THINGS and not str(result).startswith("Error:"):
                    seen.clear()
                seen[signature] = result

            # Give the model a heads-up right before max_steps runs out so it
            # finishes any required write_file/edit_file before tools turn off.
            if (max_steps >= 4 and steps == max_steps - 2
                    and index == len(reply.calls) - 1):
                result_text = (f"{result}\n\n[Note: 2 tool steps remaining in "
                               "this turn — finish any needed writes now and "
                               "give your final answer.]")
            else:
                result_text = str(result)

            sink.tool_result(name, result_text)
            message = {"role": "tool", "tool_call_id": call.get("id", ""),
                       "content": result_text}
            if getattr(result, "image", ""):
                message["image"] = result.image
            history.append(message)

        if step_repeated:
            repeats += 1
        if repeats >= 2:
            break

    # Stopping means stopping: without this, abandoning a turn on its last step
    # fell through and opened one more request to the model before giving up.
    if sink.cancelled():
        return done()

    # Stuck or out of steps: ask once more with no tools, so prose is all it can
    # produce. An imperfect answer beats reporting nothing.
    # Sent, not kept: the nudge is the loop talking, not the user.
    nudge = {"role": "user", "content":
             "Answer my original question now, using what you found above."}
    answered(ask([nudge], use_tools=False))
    done()


# --------------------------------------------------------------------------
# Terminal chat
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="harness chat", description="Terminal chat with the agent.")
    parser.add_argument("prompt", nargs="*", help="Ask once and exit.")
    parser.add_argument("--think", action="store_true",
                        help="Enable reasoning (slower, uses far more tokens).")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Show tool output as it comes back.")
    # Deliberately NOT nargs="?": an optional value would swallow the prompt,
    # so `--resume "some question"` would look for a session by that name.
    parser.add_argument("--resume", action="store_true",
                        help="Continue the most recent session.")
    parser.add_argument("--session", metavar="ID",
                        help="Continue a specific session (see --sessions).")
    parser.add_argument("--sessions", action="store_true",
                        help="List saved sessions and exit.")
    args = parser.parse_args()

    if args.sessions:
        sessions = list_sessions()
        if not sessions:
            print("No saved sessions yet.")
        for entry in sessions:
            turns = sum(1 for m in entry.get("messages", [])
                        if m.get("role") == "user")
            print(f"  {entry['id']}  {entry.get('updated', ''):19}  "
                  f"{turns:2} turns  {entry.get('title', '')}")
        return

    resuming = args.session or ("latest" if args.resume else None)
    session_id, history = (load_session(resuming) if resuming
                           else (new_session_id(), []))
    if resuming and not session_id:
        sys.exit(f"No such session: {resuming}")

    cfg = settings.load_config()
    if args.think:
        cfg["thinking"] = True
    sink = TerminalSink(args.verbose)

    def turn(text):
        run(history, text, cfg, sink)
        sink.end_turn()
        save_session(session_id, history)

    if args.prompt:
        turn(" ".join(args.prompt))
        return

    if resuming:
        turns = sum(1 for m in history if m.get("role") == "user")
        print(f"Resumed session {session_id} ({turns} turns).")
    print("Harness. 'exit' to quit, 'reset' to start a new session.\n")

    while True:
        try:
            entered = input("\033[1m> \033[0m").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if entered.lower() in ("exit", "quit"):
            return
        if entered.lower() == "reset":
            history, session_id = [], new_session_id()
            print(f"  (new session {session_id})\n")
        elif entered:
            turn(entered)
            print()


if __name__ == "__main__":
    main()

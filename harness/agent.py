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
import sys
import time
import urllib.error
import urllib.request

from . import config as settings
from . import tools

SERVER = "http://127.0.0.1:8080/v1/chat/completions"
REQUEST_TIMEOUT = 300    # generation runs ~30 t/s, so be patient

# Roughly 8K tokens of conversation, leaving the rest of the 16K window for the
# system prompt, the tool schemas, and the reply being generated.
HISTORY_CHAR_BUDGET = 24000

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
                  if m.get("role") == "user" and m.get("content")), "untitled")
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


def trim_history(history):
    """
    Drop the oldest exchanges once the conversation outgrows the budget.

    Tool output is bulky, and the window is 16K. Keeping everything would push
    the system prompt out of context, which is worse than forgetting turn one.
    """
    budget = HISTORY_CHAR_BUDGET
    kept = []
    for message in reversed(history):
        budget -= len(str(message.get("content") or "")) + 200
        if budget < 0:
            break
        kept.append(message)
    kept.reverse()

    # Never open on a tool result — it would reference a call that is now gone.
    while kept and kept[0].get("role") == "tool":
        kept.pop(0)
    return kept


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
    for block in re.findall(r"<tool_call>(.*?)</tool_call>", content, re.DOTALL):
        name = re.search(r"<function=([\w.-]+)>", block)
        if not name:
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


def call_arguments(call):
    """The arguments of one tool call, parsed and cleaned of XML bleed."""
    try:
        args = json.loads(call["function"]["arguments"] or "{}")
    except json.JSONDecodeError:
        return {}
    return {k: clean_argument(v) for k, v in args.items()}


# --------------------------------------------------------------------------
# One completion
# --------------------------------------------------------------------------

def stream(messages, cfg, sink, use_tools=True):
    """
    Stream one completion, reporting to `sink` as it arrives.

    Returns (text, tool_calls, tokens). Streaming is the whole point: the model
    generates around 30 tokens/sec, so a 400-token answer takes ~13s. Held back
    until complete that reads as a hang; streamed it reads as fast typing.
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

    try:
        response = urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT)
    except urllib.error.URLError as exc:
        sink.error(f"Cannot reach the model server at {SERVER}: {exc}. "
                   "Start it with: harness model")
        return "", [], 0

    if hasattr(sink, "active_response"):
        sink.active_response = response

    calls = {}       # index -> partial tool call, assembled across deltas
    full = ""        # everything the model has said this completion
    shown = 0        # how much of it has reached the sink
    building = 0     # bytes of tool-call payload, for the progress report
    quiet = False    # a tool call has started; stop forwarding prose
    used = 0         # tokens this completion cost, if the server says

    # Fraction of max_tokens spent so far. The server only reports usage at the
    # end, so estimate from characters — roughly 3.6 per token — which is
    # accurate enough to warn before a long tool call is cut off mid-string.
    cap = max(1, int(cfg["max_tokens"]))
    def spent():
        return min(1.0, (len(full) + building) / 3.6 / cap)

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
                    break
                try:
                    chunk = json.loads(body)
                except json.JSONDecodeError:
                    continue
                # The usage chunk carries no choices, so read it before indexing.
                # Count generated tokens, not the total: a cached prompt costs no
                # wall-clock, so including it reported things like 4460 tok/s.
                if chunk.get("usage"):
                    usage = chunk["usage"]
                    used = (usage.get("completion_tokens")
                            or usage.get("total_tokens") or used)
                try:
                    delta = chunk["choices"][0].get("delta", {})
                except (IndexError, KeyError, TypeError):
                    continue

                # Reasoning tokens arrive either in `reasoning_content` (DeepSeek)
                # or enclosed in <think> tags. Forward them so the user sees progress.
                reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                if reasoning:
                    sink.reasoning(reasoning)

                content = delta.get("content") or ""
                if content:
                    full += content
                    if TAG in full and not quiet:
                        quiet = True
                        sink.token(full[:full.find(TAG)].strip())
                    elif not quiet and len(full) - shown >= 4:
                        sink.token(full[shown:])
                        shown = len(full)

                for call in delta.get("tool_calls") or []:
                    idx = call.get("index", 0)
                    if idx not in calls:
                        calls[idx] = {"id": call.get("id", ""), "type": "function",
                                      "function": {"name": "", "arguments": ""}}
                    function = call.get("function", {})
                    if function.get("name"):
                        calls[idx]["function"]["name"] += function["name"]
                    if function.get("arguments"):
                        calls[idx]["function"]["arguments"] += function["arguments"]
                        building += len(function["arguments"])
                        sink.building(building, spent())
    except (OSError, urllib.error.URLError, http.client.HTTPException):
        pass
    finally:
        if hasattr(sink, "active_response"):
            sink.active_response = None

    if not quiet and len(full) > shown:
        sink.token(full[shown:])

    text = full.strip()
    parsed = [calls[k] for k in sorted(calls)]
    # Recover calls the server emitted as plain XML rather than parsing.
    if not parsed and TAG in text:
        parsed = parse_xml_tool_calls(text)
    return text, parsed, used


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------

def run(history, user_input, cfg, sink):
    """Run one exchange to completion, extending `history` in place."""
    history.append({"role": "user", "content": user_input})
    messages = ([{"role": "system", "content": cfg["system_prompt"]}]
                + trim_history(history))

    seen = {}
    repeats = 0
    started = time.monotonic()
    tokens = 0
    steps = 0

    def done():
        sink.stats(round(time.monotonic() - started, 1), tokens, steps)

    for _ in range(max(1, int(cfg["max_steps"]))):
        if sink.cancelled():
            return done()
        steps += 1
        # Re-trimmed every step, not just at the start. The budget was applied
        # once to the conversation and then several thousand characters of tool
        # output were appended to it, which is how a turn that read two large
        # files pushed the system prompt out of a 16K window mid-turn.
        text, calls, used = stream(
            [messages[0]] + trim_history(messages[1:]), cfg, sink)
        tokens += used
        if sink.cancelled() or not calls:
            history.append({"role": "assistant", "content": text})
            return done()

        turn = {"role": "assistant", "content": "", "tool_calls": calls}
        messages.append(turn)
        history.append(turn)

        for call in calls:
            name = call["function"]["name"]
            args = call_arguments(call)
            sink.tool_start(name, args)

            # Running the same call twice means the model is stuck. Replaying
            # the first result with a nudge ends the turn instead of looping.
            signature = (name, json.dumps(args, sort_keys=True))
            if signature in seen:
                repeats += 1
                result = (f"{seen[signature]}\n\n[You already ran this and this "
                          "was the result. Answer the user now. Do not call any "
                          "more tools.]")
            else:
                result = seen[signature] = tools.call(name, args, cfg, sink)

            sink.tool_result(name, result)
            tool_turn = {"role": "tool", "tool_call_id": call.get("id", ""),
                         "content": result}
            messages.append(tool_turn)
            history.append(tool_turn)

        if repeats >= 2:
            break

    # Stopping means stopping: without this, abandoning a turn on its last step
    # fell through and opened one more request to the model before giving up.
    if sink.cancelled():
        return done()

    # Stuck or out of steps: ask once more with no tools, so prose is all it can
    # produce. An imperfect answer beats reporting nothing.
    messages.append({"role": "user", "content":
                     "Answer my original question now, using what you found above."})
    text, _, used = stream([messages[0]] + trim_history(messages[1:]),
                           cfg, sink, use_tools=False)
    tokens += used
    if text:
        history.append({"role": "assistant", "content": text})
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

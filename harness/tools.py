"""
harness.tools — the seven things the assistant can actually do.

Every tool has the same shape: `f(args, cfg, sink) -> str`. `args` is what the
model asked for, `cfg` is the current settings, and `sink` is where a preview
goes when something needs approving. Nothing here reads global state, so two
chats with different project folders cannot tread on each other.

Web pages and command output are untrusted input. The model reads whatever a
page says, so treat its conclusions with the same suspicion you would treat the
page itself.
"""

import json
import os
import re
import shlex
import signal
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request

OUTPUT_CAP = 6000    # chars — keeps tool output from eating the context window

# Search backend. Google's Custom Search JSON API is used when both variables
# are set (100 free queries/day); otherwise DuckDuckGo, which needs no key.
#   export GOOGLE_API_KEY=...      https://console.cloud.google.com/apis/credentials
#   export GOOGLE_CSE_ID=...       https://programmablesearchengine.google.com
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
GOOGLE_CSE_ID = os.environ.get("GOOGLE_CSE_ID", "")

# Commands safe to run without asking. Read-only inspection only — anything
# that writes, deletes, installs, or reaches the network is not here.
SAFE_COMMANDS = {
    "cat", "date", "df", "du", "echo", "file", "find", "grep", "head",
    "hostname", "ls", "ps", "pwd", "stat", "sw_vers", "tail", "uname",
    "uptime", "wc", "which", "whoami",
}

# Shell syntax that can chain past the command we approved. Presence of any of
# these forces a confirmation prompt even for an allow-listed command.
SHELL_METACHARS = (";", "&&", "||", "|", ">", "<", "`", "$(", "\n")

# Flags that turn an otherwise safe command — find, or an interpreter — into
# one that writes, deletes, or executes something of its own.
DANGEROUS_FIND_FLAGS = {"-exec", "-execdir", "-delete", "-ok", "-okdir"}
INLINE_SCRIPT_FLAGS = {"-c", "-e", "-r", "--eval"}
INTERPRETERS = {"python", "python3", "node", "ruby", "perl", "bash", "sh", "zsh"}

# Commands that ask even on the relaxed "destructive only" setting. These either
# destroy data, escalate, or run something fetched from the network — the cases
# where being asked one extra time is worth the interruption.
DESTRUCTIVE = {
    "rm", "rmdir", "mv", "dd", "mkfs", "shred", "truncate", "chmod", "chown",
    "chgrp", "ln", "kill", "killall", "pkill", "shutdown", "reboot", "halt",
    "sudo", "su", "doas", "launchctl", "systemctl", "diskutil", "fdisk",
    "curl", "wget", "ssh", "scp", "rsync", "nc", "ncat", "telnet",
    "brew", "pip", "pip3", "npm", "npx", "yarn", "pnpm", "gem", "cargo",
    "apt", "apt-get", "dnf", "yum", "port", "softwareupdate",
    "git", "docker", "kubectl", "terraform", "defaults", "crontab", "osascript",
}

# Locations the agent may never write to, confirmation or not. Editing anything
# here breaks the OS or another application rather than the user's own work.
# Both the plain and /private forms are listed because macOS symlinks /etc,
# /var, and /tmp into /private, and realpath() resolves them before matching.
PROTECTED_PREFIXES = (
    "/System", "/usr", "/bin", "/sbin", "/Library", "/Applications",
    "/opt/homebrew", "/etc", "/private/etc", "/var", "/private/var",
)

# Configuration and credential paths in the user's own home directory, which
# are protected from automated modification for the same reason.
SENSITIVE_USER_PATHS = (
    "~/.ssh", "~/.gnupg", "~/.aws", "~/.harness",
    "~/.bashrc", "~/.zshrc", "~/.bash_profile", "~/.zprofile",
)

# Of those, the ones whose *contents* are the secret. The prefixes above are
# about not breaking the machine, so they only guard writing; these guard
# reading too. Nothing stopped the model from reading a private key, and what
# a tool returns is kept verbatim in the session file — in plain text, on a
# machine that answers to a phone. Reading one now asks first, every time,
# whatever the approval setting says, and an unattended run refuses outright.
SECRET_PATHS = ("~/.ssh", "~/.gnupg", "~/.aws", "~/.harness")

# Carved back out of the above: scratch space is a legitimate write target, and
# macOS puts the per-user temp dir under /var/folders, inside a protected tree.
WRITABLE_PREFIXES = (os.path.realpath(tempfile.gettempdir()), "/private/tmp")


# --------------------------------------------------------------------------
# Paths and permission
# --------------------------------------------------------------------------

def resolve(path, cfg):
    """Expand ~ and make a relative path mean 'inside the project folder'."""
    path = os.path.expanduser((path or "").strip())
    project = cfg.get("project_dir", "")
    if path and project and not os.path.isabs(path):
        return os.path.normpath(os.path.join(project, path))
    return path


def _under(path, prefix):
    return path == prefix or path.startswith(prefix + os.sep)


def is_protected(path):
    """True if the resolved path lives somewhere the agent must not modify."""
    real = os.path.realpath(os.path.expanduser(path))
    if any(_under(real, ok) for ok in WRITABLE_PREFIXES):
        return False
    if any(_under(real, bad) for bad in PROTECTED_PREFIXES):
        return True
    for user_path in SENSITIVE_USER_PATHS:
        expanded = os.path.realpath(os.path.expanduser(user_path))
        if _under(real, expanded):
            return True
    return False


def _arguments(command):
    """
    The parts of a command line that look like paths.

    Best effort by design: anything that fails to split, or that chains, is
    already forced through a confirmation by needs_confirmation below.
    """
    try:
        parts = shlex.split(command)
    except ValueError:
        return []
    return [p for p in parts if p.startswith(("~", "/", "./", "../"))]


def is_secret(path):
    """True if the path is inside somewhere whose contents are credentials."""
    real = os.path.realpath(os.path.expanduser((path or "").strip()))
    return any(_under(real, os.path.realpath(os.path.expanduser(secret)))
               for secret in SECRET_PATHS)


def confirm_secret(path, sink, verb):
    """
    Ask before a credential directory is read. True if it may go ahead.

    Separate from the approval settings on purpose: "never ask" is a promise
    about the user's own files being convenient to work with, not permission to
    put an SSH key into a transcript without mentioning it.
    """
    if not is_secret(path):
        return True
    return sink.confirm(
        f"{verb} a file containing credentials?",
        f"\n  {path}\n\n  Whatever this returns is kept in the session\n"
        "  transcript in plain text.",
        "")   # no kind: "always" must not switch this off for the session


def needs_confirmation(command, cfg):
    """
    Whether this command should stop and ask, under the configured setting.

    "never" means never — the honest reading of what the user picked. The other
    two still ask about anything chained, because the first word of a pipeline
    tells you nothing about what the rest of it does.
    """
    mode = cfg.get("approve_commands", "always")
    if mode == "never":
        return False
    if any(meta in command for meta in SHELL_METACHARS):
        return True
    try:
        parts = shlex.split(command)
    except ValueError:
        return True
    if not parts:
        return True

    name = os.path.basename(parts[0])

    # find with destructive/exec flags (-delete, -exec, etc.)
    if name == "find" and any(arg in parts for arg in DANGEROUS_FIND_FLAGS):
        return True

    # Script interpreters running inline code (-c, -e, --eval)
    if name in INTERPRETERS and any(arg in parts for arg in INLINE_SCRIPT_FLAGS):
        return True

    if mode == "destructive":
        return name in DESTRUCTIVE or name in INTERPRETERS
    return name not in SAFE_COMMANDS | set(cfg.get("extra_safe_commands", []))


def needs_file_approval(path, cfg):
    """Whether changing this file should stop and ask."""
    mode = cfg.get("approve_files", "always")
    if mode == "never":
        return False
    if mode == "outside":
        project = cfg.get("project_dir", "")
        if not project:
            return True          # no project set means nothing is "inside" it
        return not _under(os.path.realpath(os.path.expanduser(path)),
                          os.path.realpath(os.path.expanduser(project)))
    return True


def truncate(text):
    """Keep tool output from eating the context window."""
    if len(text) <= OUTPUT_CAP:
        return text
    return text[:OUTPUT_CAP] + f"\n... [truncated, {len(text)} chars total]"


# --------------------------------------------------------------------------
# The tools
# --------------------------------------------------------------------------

def list_files(args, cfg, sink):
    path = resolve(args.get("path", "."), cfg)
    if not os.path.isdir(path):
        return f"Error: not a directory: {path}"
    try:
        entries = sorted(os.listdir(path))
    except OSError as exc:
        return f"Error: {exc}"
    return "\n".join(entries[:200]) if entries else "(empty directory)"


def _whole_number(value, fallback, low, high):
    """One tool argument as a number in range. Models send strings and junk."""
    try:
        return max(low, min(high, int(value)))
    except (ValueError, TypeError):
        return fallback


def read_file(args, cfg, sink):
    path = resolve(args.get("path", ""), cfg)
    if not os.path.isfile(path):
        return f"Error: not a file: {path}"
    if not confirm_secret(path, sink, "Read"):
        return "Error: the user declined to share this file."

    start = _whole_number(args.get("start_line", 1), 1, 1, 10_000_000)
    count = _whole_number(args.get("num_lines", 100), 100, 1, 500)

    try:
        with open(path, "r", errors="replace") as handle:
            lines = handle.readlines()
    except OSError as exc:
        return f"Error: {exc}"

    total = len(lines)
    if total == 0:
        return f"(empty file: {path})"
    if start > total:
        return (f"Error: start_line {start} is past the end of {path}, "
                f"which has {total} lines.")

    shown = lines[start - 1:start - 1 + count]
    last = start - 1 + len(shown)

    # Numbered, because the edit tool is told to quote exact text back and
    # line numbers are how the model refers to what it is looking at.
    out = [f"File: {path} (lines {start}-{last} of {total})"]
    out += [f"{n:4d} | {line.rstrip(chr(13) + chr(10))}"
            for n, line in enumerate(shown, start=start)]
    if last < total:
        out.append(f"... {total - last} more lines "
                   f"(read on with start_line={last + 1})")
    return "\n".join(out)


def write_file(args, cfg, sink):
    path = resolve(args.get("path", ""), cfg)
    content = args.get("content", "")

    if not path:
        return "Error: no path given"
    if is_protected(path):
        return f"Error: refusing to write to a protected system location: {path}"

    exists = os.path.isfile(path)
    lines = content.splitlines()
    detail = [f"\n  {'OVERWRITE' if exists else 'CREATE'}  {path}"]
    if exists:
        detail.append(f"  existing file is {os.path.getsize(path)} bytes "
                      "and will be replaced")
    detail.append(f"  writing {len(content)} bytes:")
    detail += [f"    | {line[:100]}" for line in lines[:8]]
    if len(lines) > 8:
        detail.append(f"    | ... {len(lines) - 8} more lines")

    if (needs_file_approval(path, cfg)
            and not sink.confirm("Write this file?", "\n".join(detail), "write_file")):
        return "Error: the user declined to write this file."

    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w") as handle:
            handle.write(content)
    except OSError as exc:
        return f"Error: {exc}"
    return f"Wrote {len(content)} bytes to {path}"


def edit_file(args, cfg, sink):
    path = resolve(args.get("path", ""), cfg)
    old_text = args.get("old_text", "")
    new_text = args.get("new_text", "")

    if not path or not old_text:
        return "Error: both path and old_text are required"
    if is_protected(path):
        return f"Error: refusing to edit a protected system location: {path}"
    if not os.path.isfile(path):
        return f"Error: not a file: {path}"

    try:
        with open(path, "r", errors="replace") as handle:
            original = handle.read()
    except OSError as exc:
        return f"Error: {exc}"

    # Ambiguity is a correctness bug, not a warning: replacing the wrong one of
    # three identical snippets corrupts the file quietly.
    hits = original.count(old_text)
    if hits == 0:
        return f"Error: that exact text was not found in {path}"
    if hits > 1:
        # Point at where they are, so the model can pick more context to
        # disambiguate rather than guessing at a longer snippet blind.
        head = (old_text.splitlines() or [old_text])[0]
        at = [str(n) for n, line in enumerate(original.splitlines(), 1)
              if head in line]
        return (f"Error: that text appears {hits} times in {path} "
                f"(near lines {', '.join(at[:8])}). Include more surrounding "
                "context so it matches exactly once.")

    detail = [f"\n  EDIT  {path}"]
    detail += [f"    - {line[:100]}" for line in old_text.splitlines()[:6]]
    detail += [f"    + {line[:100]}" for line in new_text.splitlines()[:6]]

    if (needs_file_approval(path, cfg)
            and not sink.confirm("Apply this edit?", "\n".join(detail), "edit_file")):
        return "Error: the user declined this edit."

    try:
        with open(path, "w") as handle:
            handle.write(original.replace(old_text, new_text, 1))
    except OSError as exc:
        return f"Error: {exc}"
    return f"Edited {path}"


def run_command(args, cfg, sink):
    command = args.get("command", "").strip()
    if not command:
        return "Error: empty command"

    # `cat` is on the safe list, which made `cat ~/.ssh/id_rsa` a command that
    # never asked. Any argument pointing into a credential directory asks,
    # whatever the mode.
    for token in _arguments(command):
        if not confirm_secret(token, sink, "Run a command that reads"):
            return "Error: the user declined to run this command."

    if needs_confirmation(command, cfg):
        if not sink.confirm("Allow this command?", f"\n  RUN  {command}",
                            "run_command"):
            return "Error: the user declined to run this command."

    if sink and sink.cancelled():
        return "Error: command execution cancelled by user."

    try:
        # Its own process group, so cancelling can take the whole pipeline
        # down rather than just the shell that spawned it.
        proc = subprocess.Popen(
            command, shell=True, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=cfg.get("project_dir") or None,
            start_new_session=True,
        )
        stdout, stderr = "", ""
        deadline = time.time() + 30
        while time.time() < deadline:
            if sink and sink.cancelled():
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except OSError:
                    pass
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except OSError:
                        pass
                return "Error: command execution cancelled by user."
            try:
                out, err = proc.communicate(timeout=0.2)
                stdout += out
                stderr += err
                break
            except subprocess.TimeoutExpired:
                continue
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except OSError:
                pass
            return "Error: command timed out after 30s"
    except Exception as exc:
        return f"Error executing command: {exc}"

    return (stdout + stderr).strip() or \
        f"(no output, exit code {proc.returncode})"


def _search_google(query):
    """Google Custom Search JSON API. Free tier is 100 queries/day."""
    params = urllib.parse.urlencode(
        {"key": GOOGLE_API_KEY, "cx": GOOGLE_CSE_ID, "q": query, "num": 5})
    with urllib.request.urlopen(
            f"https://www.googleapis.com/customsearch/v1?{params}",
            timeout=20) as response:
        items = json.loads(response.read()).get("items", [])
    return [(i.get("title"), i.get("link"), i.get("snippet")) for i in items]


def _search_duckduckgo(query):
    """No API key required, but depends on the `ddgs` package."""
    from ddgs import DDGS
    return [(r.get("title"), r.get("href"), r.get("body"))
            for r in DDGS().text(query, max_results=5)]


def web_search(args, cfg, sink):
    query = args.get("query", "").strip()
    if not query:
        return "Error: empty query"

    google = bool(GOOGLE_API_KEY and GOOGLE_CSE_ID)
    try:
        results = _search_google(query) if google else _search_duckduckgo(query)
    except ImportError:
        return ("Error: no search backend available. Either install ddgs "
                "(harness deps) or set GOOGLE_API_KEY and GOOGLE_CSE_ID.")
    except Exception as exc:  # network, quota, parsing — all recoverable
        return f"Error: {'Google' if google else 'DuckDuckGo'} search failed: {exc}"

    if not results:
        return "No results."
    return "\n\n".join(f"{n}. {title}\n   {link}\n   {snippet}"
                       for n, (title, link, snippet) in enumerate(results, 1))


def fetch_url(args, cfg, sink):
    url = args.get("url", "").strip()
    if not url.startswith(("http://", "https://")):
        return "Error: only http(s) URLs are allowed."

    request = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (compatible; harness)"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            html = response.read(500_000).decode("utf-8", errors="replace")
    except Exception as exc:
        return f"Error: could not fetch: {exc}"

    text = re.sub(r"(?is)<(script|style|nav|footer|header).*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"&[a-zA-Z#0-9]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip() or "(no readable text)"


# --------------------------------------------------------------------------
# What the model is told about them
# --------------------------------------------------------------------------

def _schema(name, description, required_params=None, **params):
    """Build OpenAI function schema with optional required params specification."""
    req = list(params) if required_params is None else required_params
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {k: {"type": "string", "description": v}
                               for k, v in params.items()},
                "required": req,
            },
        },
    }


SCHEMAS = [
    _schema("list_files", "List the files in a directory.",
            path="Directory path."),
    _schema("read_file",
            "Read a text file, numbered by line. Long files come back a page "
            "at a time — read on with start_line.",
            required_params=["path"],
            path="File path.",
            start_line="Optional 1-indexed starting line number (default: 1).",
            num_lines="Optional number of lines to read (default: 100, max: 500)."),
    _schema("write_file",
            "Create a file, or replace an existing one entirely. The user is "
            "shown the content and must approve before anything is written.",
            path="File path.", content="Full file contents."),
    _schema("edit_file",
            "Replace one exact snippet in an existing file. old_text must "
            "appear exactly once — include surrounding lines to be sure. "
            "Prefer this over write_file for changing part of a file.",
            path="File path.", old_text="Exact text to replace.",
            new_text="Replacement text."),
    _schema("web_search",
            "Search the web for current information. Use this for anything you "
            "are unsure about or that may have changed recently.",
            query="Search query."),
    _schema("fetch_url", "Fetch a web page and return its visible text.",
            url="An http(s) URL."),
    _schema("run_command",
            "Run a read-only shell command such as df, ls, du, or uname. "
            "Anything that writes or deletes requires the user to confirm.",
            command="The command."),
]

DISPATCH = {
    "list_files": list_files,
    "read_file": read_file,
    "write_file": write_file,
    "edit_file": edit_file,
    "web_search": web_search,
    "fetch_url": fetch_url,
    "run_command": run_command,
}


def schemas_for(cfg):
    """Only advertise the tools the user left switched on."""
    allowed = cfg.get("tools", {})
    return [s for s in SCHEMAS if allowed.get(s["function"]["name"], True)]


def call(name, args, cfg, sink):
    """Run one tool by name, refusing anything switched off or unknown."""
    if not cfg.get("tools", {}).get(name, True):
        return (f"Error: {name} is switched off for this run. Do not retry. "
                "Use the tools you do have, or answer without them.")
    handler = DISPATCH.get(name)
    if not handler:
        return f"Error: no such tool: {name}"
    return truncate(handler(args, cfg, sink))

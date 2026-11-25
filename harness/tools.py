"""
harness.tools — the eleven things the assistant can actually do.

Every tool has the same shape: `f(args, cfg, sink) -> str`. `args` is what the
model asked for, `cfg` is the current settings, and `sink` is where a preview
goes when something needs approving. Nothing here reads global state, so two
chats with different project folders cannot tread on each other.

Web pages and command output are untrusted input. The model reads whatever a
page says, so treat its conclusions with the same suspicion you would treat the
page itself.
"""

import base64
import contextlib
import fnmatch
import html
import html.parser
import ipaddress
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
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

# Shell syntax that can chain past the command we approved. Any of these outside
# quotes forces a confirmation even for an allow-listed command. A lone `&` and
# a bracket were missing, so `echo hi & rm -rf ~/x` ran the rm unasked: `echo`
# is on the safe list, and nothing else was looked at.
SHELL_OPERATORS = set(";&|<>()")
# These run something even inside double quotes, so they count anywhere.
SHELL_SUBSTITUTIONS = ("`", "$(", "\n", "\r")

# Flags that turn an otherwise safe command — find, or an interpreter — into
# one that writes, deletes, or executes something of its own.
DANGEROUS_FIND_FLAGS = {"-exec", "-execdir", "-delete", "-ok", "-okdir",
                        "-fprint", "-fprint0", "-fprintf", "-fls"}
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
    # Copying over a file destroys the one that was there.
    "cp", "ditto", "install", "patch",
}

# Commands that run another command. `env rm -rf ~` starts with env, which is
# not destructive, so the relaxed setting waved it through; so did a leading
# `FOO=1`. What they run is the point, so they always ask.
WRAPPERS = {
    "env", "nice", "nohup", "time", "command", "exec", "builtin", "xargs",
    "timeout", "gtimeout", "stdbuf", "caffeinate", "arch", "script", "watch",
}

# Editors that rewrite a file in place when given -i.
IN_PLACE_EDITORS = {"sed", "gsed", "perl"}

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

HOME_VARIABLES = re.compile(r"\$(\{HOME\}|HOME(?![A-Za-z0-9_]))")
USER_VARIABLES = re.compile(r"\$(\{USER\}|USER(?![A-Za-z0-9_]))")


def _expand_vars(path):
    """Expand $HOME/$USER and ~ without calling a shell."""
    home = os.path.expanduser("~")
    path = HOME_VARIABLES.sub(lambda _: home, path)
    path = USER_VARIABLES.sub(lambda _: os.path.basename(home), path)
    return os.path.expanduser(path)


def resolve(path, cfg):
    """Expand ~ and make a relative path mean 'inside the project folder'."""
    path = _expand_vars(str(path or "").strip())
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


def _arguments(command, cwd=""):
    """
    The parts of a command line that could be paths, resolved.

    Every word that is not a flag counts — `cat .ssh/id_rsa` run from the home
    folder names a key without starting with ~ or /. Best effort by design:
    anything that fails to split, or that chains, is already forced through a
    confirmation by needs_confirmation below.
    """
    try:
        parts = shlex.split(command)
    except ValueError:
        return []
    found = []
    for part in parts[1:]:
        if not part or part.startswith("-"):
            continue
        path = _expand_vars(part)
        if not os.path.isabs(path) and cwd:
            path = os.path.join(cwd, path)
        found.append(path)
    return found


RECURSIVE_GREP_FLAGS = ("-r", "-R", "--recursive", "--dereference-recursive")


def _reads_secret(command, cwd=""):
    """
    The credential path a command would read, or "".

    A path into ~/.ssh is caught by name. So is a recursive grep of a folder
    that has one inside it — `grep -r PRIVATE ~` prints the key's lines, and
    ~ on its own looks like nothing in particular.
    """
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = []
    recursive = (bool(parts) and os.path.basename(parts[0]) in ("grep", "egrep", "rg")
                 and any(p in RECURSIVE_GREP_FLAGS
                         or (p.startswith("-") and not p.startswith("--")
                             and ("r" in p or "R" in p))
                         for p in parts[1:]))
    for path in _arguments(command, cwd):
        if is_secret(path):
            return path
        if recursive:
            real = os.path.realpath(path)
            for secret in SECRET_PATHS:
                if _under(os.path.realpath(os.path.expanduser(secret)), real):
                    return path
    return ""


def is_secret(path):
    """True if the path is inside somewhere whose contents are credentials."""
    real = os.path.realpath(os.path.expanduser((path or "").strip()))
    return any(_under(real, os.path.realpath(os.path.expanduser(secret)))
               for secret in SECRET_PATHS)


def declined(sink, what):
    """
    What a tool reports when its approval came back no.

    Only "you declined" when somebody did. A turn stopped while the question
    was on screen, or a question nobody answered all day, also comes back no,
    and the transcript claiming a refusal you never gave is how the model ends
    up apologising for a decision you didn't make.
    """
    if sink is not None and sink.cancelled():
        return "Error: the turn was stopped before this was approved."
    # Said outright, because a bare "declined" read to the model as an
    # obstacle: refused one command, it tried nine variations of it, and each
    # one put another approval in front of the user.
    return (f"Error: the user declined {what}. Do not try another way of doing "
            "the same thing. Tell them what you wanted to do and why, and let "
            "them decide.")


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
    if chained(command):
        return True
    try:
        parts = shlex.split(command)
    except ValueError:
        return True
    if not parts:
        return True

    first = parts[0]
    name = os.path.basename(first)

    # A command name the shell will rewrite before running — `{rm,-rf,x}` is
    # brace-expanded into rm -rf x, `$CMD` is whatever CMD holds — or an
    # assignment in front of the real one. What runs is not what it says.
    if "=" in first or any(mark in first for mark in "{}*?[]$\\"):
        return True
    # A path to an executable (./cat, /tmp/ls) is not the system utility whose
    # name it borrows, so only bare names or standard system bins skip asking.
    if "/" in first and os.path.dirname(first) not in ("/bin", "/usr/bin"):
        return True
    if name in WRAPPERS:
        return True

    # find with destructive/exec flags (-delete, -exec, etc.)
    if name == "find" and any(arg in parts for arg in DANGEROUS_FIND_FLAGS):
        return True

    # Script interpreters running inline code (-c, -e, --eval)
    if name in INTERPRETERS and any(arg in parts for arg in INLINE_SCRIPT_FLAGS):
        return True

    if mode == "destructive":
        in_place = name in IN_PLACE_EDITORS and any(
            arg.startswith("-i") or arg == "--in-place" for arg in parts[1:])
        return name in DESTRUCTIVE or name in INTERPRETERS or in_place
    return name not in SAFE_COMMANDS | set(cfg.get("extra_safe_commands", []))


def chained(command):
    """
    True if the shell would run more than the one command the line starts with.

    Operators count only where the shell sees them — outside quotes and not
    escaped — so `grep "a|b" notes.txt` and `find ~ \\( -name x -o -name y \\)`
    are one command each. shlex cannot tell `\\(` from `(`, which made every
    find with a group ask for approval. Substitutions count anywhere, because
    a shell runs `$(...)` inside double quotes too. A quote left open means the
    line cannot be read, and that asks.
    """
    if any(mark in command for mark in SHELL_SUBSTITUTIONS):
        return True
    quote, escaped = "", False
    for char in command:
        if escaped:
            escaped = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif quote:
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
        elif char in SHELL_OPERATORS:
            return True
    return bool(quote)


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


def truncate(text, tail=False):
    """
    Keep tool output from eating the context window.

    With `tail`, the end survives as well as the start: a build or a test run
    puts its errors last, and cutting at 6000 characters kept the noise and
    threw away the one line that mattered.
    """
    if len(text) <= OUTPUT_CAP:
        return text
    if not tail:
        return text[:OUTPUT_CAP] + f"\n... [truncated, {len(text)} chars total]"
    marker = f"\n... [{len(text):,} chars in all; the middle is cut] ...\n"
    room = OUTPUT_CAP - len(marker) - 40     # leaves room for an exit code line
    head = room // 3
    return text[:head] + marker + text[-(room - head):]


# --------------------------------------------------------------------------
# The tools
# --------------------------------------------------------------------------

LIST_CAP = 200


def _missing_hint(raw_path, resolved_path, cfg, want_dir=False):
    """
    Point the model at ~/... when it asked for a home folder without the ~.

    With a project folder set, `Documents/Lightroom/Finals` resolves inside the
    project and fails; the model then spent five steps searching for Documents
    blind before trying /Users/... directly.
    """
    raw = str(raw_path or "").strip()
    if not raw or os.path.isabs(raw) or raw.startswith(("~", "$")):
        return ""
    candidate = os.path.normpath(os.path.join(os.path.expanduser("~"), raw))
    if candidate != resolved_path:
        exists = os.path.isdir(candidate) if want_dir else os.path.isfile(candidate)
        if exists:
            return f" (did you mean {candidate}?)"
    if cfg.get("project_dir"):
        return " (use a full path or ~/... if it is outside the working folder)"
    return ""


def list_files(args, cfg, sink):
    """
    A folder's contents, folders marked with a trailing slash.

    Without the mark the model could not tell "Lightroom" the folder from a
    file, and finding one picture took four steps of listing things blind.
    Optional `details` sorts newest first with size and modified date, so the
    model does not have to view every photo just to tell versions apart.
    """
    raw = args.get("path", ".")
    path = resolve(raw, cfg)
    if not os.path.isdir(path):
        return f"Error: not a directory: {path}{_missing_hint(raw, path, cfg, want_dir=True)}"
    try:
        entries = sorted(os.listdir(path))
    except OSError as exc:
        return f"Error: {exc}"
    if not entries:
        return "(empty directory)"
    details = args.get("details", "").strip().lower() in ("1", "true", "yes")
    if details:
        rows = []
        for name in entries:
            full = os.path.join(path, name)
            is_dir = os.path.isdir(full)
            try:
                info = os.stat(full)
                mtime, size = info.st_mtime, info.st_size
            except OSError:
                mtime, size = 0.0, 0
            rows.append((mtime, name, is_dir, size))
        rows.sort(key=lambda r: (-r[0], r[1].lower()))
        # Print the folder path above detailed listings so the model remembers
        # to prepend it when calling read_file or view_image on entries outside
        # the working folder.
        shown = [f"Folder: {path} ({len(entries)} items, newest first):"]
        for mtime, name, is_dir, size in rows[:LIST_CAP]:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime)) if mtime else "?"
            if is_dir:
                shown.append(f"{name}/  (folder, changed {when})")
            else:
                weight = (f"{size / 1e6:.1f} MB" if size >= 1_000_000
                          else f"{size / 1e3:.0f} KB" if size >= 1000 else f"{size} bytes")
                shown.append(f"{name}  ({weight}, changed {when})")
    else:
        shown = [name + "/" if os.path.isdir(os.path.join(path, name)) else name
                 for name in entries[:LIST_CAP]]
    if len(entries) > LIST_CAP:
        shown.append(f"... and {len(entries) - LIST_CAP} more")
    return "\n".join(shown)


def _whole_number(value, fallback, low, high):
    """One tool argument as a number in range. Models send strings and junk."""
    try:
        return max(low, min(high, int(value)))
    except (ValueError, TypeError):
        return fallback


# Documents are read as their words, not their bytes. A PDF opened as text is
# thousands of lines of object syntax with the words compressed out of reach:
# asked to find a job matching a CV, the model read three CVs that way, learned
# nothing from any of them, guessed from the link annotations, and spent 12K
# tokens of context doing it. macOS ships both extractors, so nothing new is
# installed — PDFKit through JavaScript for Automation for PDFs, textutil for
# Word, RTF and OpenDocument.
PDF_SCRIPT = """
ObjC.import('PDFKit');
function run(argv) {
  const doc = $.PDFDocument.alloc.initWithURL($.NSURL.fileURLWithPath(argv[0]));
  if (doc.isNil()) return '\\u0000unreadable';
  if (doc.isLocked) return '\\u0000locked';
  const text = doc.string;
  return text.isNil() ? '' : text.js;
}
"""
TEXTUTIL_TYPES = (".doc", ".docx", ".rtf", ".odt", ".wordml", ".webarchive")
EXTRACT_TIMEOUT = 60


class Unreadable(Exception):
    """A file read_file recognised but cannot turn into text."""


def document_text(path):
    """
    The text of a PDF or word-processor file, and what kind it was.

    Returns (None, None) for anything else, which is read as plain text. Other
    binary files are refused outright: a JPEG decoded as UTF-8 is pages of
    replacement characters that cost context and tell the model nothing.
    """
    with open(path, "rb") as handle:
        head = handle.read(8192)
    if head.startswith(b"%PDF-"):
        kind, command = "PDF", ["osascript", "-l", "JavaScript", "-e", PDF_SCRIPT, path]
    elif path.lower().endswith(TEXTUTIL_TYPES):
        kind, command = "document", ["textutil", "-convert", "txt", "-stdout", path]
    elif b"\0" in head:
        raise Unreadable(f"{path} is a binary file, not text.")
    else:
        return None, None

    try:
        done = subprocess.run(command, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=EXTRACT_TIMEOUT)
    except FileNotFoundError:
        raise Unreadable(f"{path} is a {kind}, and extracting its text needs "
                         "macOS.") from None
    except subprocess.TimeoutExpired:
        raise Unreadable(f"extracting the text of {path} took over "
                         f"{EXTRACT_TIMEOUT}s.") from None

    text = done.stdout
    if text.startswith("\0locked"):
        raise Unreadable(f"{path} is password-protected.")
    if done.returncode or text.startswith("\0"):
        raise Unreadable(f"{path} looks like a {kind} but could not be opened.")
    if not text.strip():
        raise Unreadable(f"{path} has no text in it — most likely a scan, "
                         "which is images of pages rather than words.")
    return text, kind


def read_file(args, cfg, sink):
    raw = args.get("path", "")
    path = resolve(raw, cfg)
    if not os.path.isfile(path):
        return f"Error: not a file: {path}{_missing_hint(raw, path, cfg)}"
    if not confirm_secret(path, sink, "Read"):
        return declined(sink, "to share this file")

    start = _whole_number(args.get("start_line", 1), 1, 1, 10_000_000)
    count = _whole_number(args.get("num_lines", 100), 100, 1, 500)

    try:
        text, kind = document_text(path)
        if text is None:
            with open(path, encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()
        else:
            lines = text.splitlines(keepends=True)
    except Unreadable as exc:
        return f"Error: {exc}"
    except OSError as exc:
        return f"Error: {exc}"

    total = len(lines)
    if total == 0:
        return f"(empty file: {path})"
    if start > total:
        return (f"Error: start_line {start} is past the end of {path}, "
                f"which has {total} lines.")

    shown = lines[start - 1:start - 1 + count]

    # Numbered, because the edit tool is told to quote exact text back and
    # line numbers are how the model refers to what it is looking at. An
    # extracted document says so, or the model tries edit_file on a PDF.
    # Stop on a complete line before OUTPUT_CAP so truncate() does not chop off
    # the `start_line=` footer and skip unread lines in the middle.
    source = f", text extracted from the {kind}" if kind else ""
    out = [""]
    chars = 160
    last = start - 1
    for n, line in enumerate(shown, start=start):
        formatted = f"{n:4d} | {line.rstrip(chr(13) + chr(10))}"
        if last >= start and chars + len(formatted) + 1 > OUTPUT_CAP:
            break
        out.append(formatted)
        chars += len(formatted) + 1
        last = n
    out[0] = f"File: {path} (lines {start}-{last} of {total}{source})"
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
        return declined(sink, "to write this file")

    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
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

    # Read and written exactly as stored. The default text mode turned every
    # \r\n into \n on the way in and wrote the whole file back that way, and
    # errors="replace" wrote U+FFFD over every byte that was not UTF-8 — an
    # edit of one line quietly rewrote the rest of the file.
    try:
        with open(path, encoding="utf-8", newline="") as handle:
            original = handle.read()
    except UnicodeDecodeError:
        return (f"Error: {path} is not UTF-8 text, so it cannot be edited "
                "without damaging it.")
    except OSError as exc:
        return f"Error: {exc}"
    # The model writes \n; a file with Windows line endings has \r\n.
    if "\r\n" in original and "\r\n" not in old_text:
        crlf = old_text.replace("\n", "\r\n")
        if crlf in original:
            old_text, new_text = crlf, new_text.replace("\n", "\r\n")

    # Ambiguity is a correctness bug, not a warning: replacing the wrong one of
    # three identical snippets corrupts the file quietly.
    hits = original.count(old_text)
    if hits == 0:
        hint = ""
        target_lines = [line.strip() for line in old_text.splitlines() if line.strip()]
        if target_lines:
            first = target_lines[0]
            near = [str(n) for n, line in enumerate(original.splitlines(), 1)
                    if first in line or line.strip() == first]
            if near:
                hint = (f" (similar text appears near lines {', '.join(near[:5])} — "
                        "check indentation and line breaks)")
        return f"Error: that exact text was not found in {path}{hint}"
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
        return declined(sink, "this edit")

    try:
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(original.replace(old_text, new_text, 1))
    except OSError as exc:
        return f"Error: {exc}"
    return f"Edited {path}"


def run_command(args, cfg, sink):
    command = args.get("command", "").strip()
    if not command:
        return "Error: empty command"

    project = cfg.get("project_dir") or ""
    if project and not os.path.isdir(project):
        return (f"Error: the working folder {project} does not exist any more. "
                "Choose another in Settings, or use absolute paths.")

    # `cat` is on the safe list, which made `cat ~/.ssh/id_rsa` a command that
    # never asked. Any argument pointing into a credential directory asks,
    # whatever the mode.
    secret = _reads_secret(command, project)
    if secret and not confirm_secret(secret, sink, "Run a command that reads"):
        return declined(sink, "to run this command")

    if needs_confirmation(command, cfg) and not sink.confirm(
            "Allow this command?", f"\n  RUN  {command}", "run_command"):
        return declined(sink, "to run this command")

    if sink and sink.cancelled():
        return "Error: command execution cancelled by user."

    try:
        # Its own process group, so cancelling can take the whole pipeline
        # down rather than just the shell that spawned it.
        # stdin is closed, not inherited: a command that waits for input —
        # a login prompt, `mail` with no body — used to sit there for the
        # whole 30 seconds and then report a timeout. Output is decoded
        # forgivingly, because one invalid byte used to throw the whole of it
        # away and report the command as having failed to run.
        proc = subprocess.Popen(
            command, shell=True, encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=project or None,
            start_new_session=True,
        )
        stdout, stderr = "", ""
        deadline = time.time() + 30
        while time.time() < deadline:
            if sink and sink.cancelled():
                with contextlib.suppress(OSError):
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(OSError):
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                return "Error: command execution cancelled by user."
            try:
                out, err = proc.communicate(timeout=0.2)
                stdout += out
                stderr += err
                break
            except subprocess.TimeoutExpired:
                continue
        else:
            with contextlib.suppress(OSError):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return "Error: command timed out after 30s"
    except Exception as exc:
        return f"Error executing command: {exc}"

    output = (stdout + stderr).strip()
    if not output:
        return f"(no output, exit code {proc.returncode})"
    # A failure says so. Without the code, output that read like progress was
    # taken for success — the model reported an email sent that never left.
    output = truncate(output, tail=True)
    return output if proc.returncode == 0 else f"{output}\n(exit code {proc.returncode})"


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


FETCH_LIMIT = 3_000_000         # bytes of a page read before giving up on the rest
FETCH_DOCUMENT_LIMIT = 30_000_000
TEXT_TYPES = ("text/plain", "text/markdown", "text/csv", "application/json",
              "application/xml", "text/xml", "application/rss+xml",
              "application/atom+xml")


def _global_ip(address):
    addr = ipaddress.ip_address(address)
    mapped = getattr(addr, "ipv4_mapped", None)
    return (mapped or addr).is_global


def is_public(host):
    """
    Whether a host is on the public internet, judged by every address it has.

    127.0.0.1 is Harness itself — its settings, its access token, the model
    server — and 192.168.x is the router. A page the model has read can tell it
    to fetch either, and nothing asked first.
    """
    host = (host or "").strip("[]")
    try:
        return _global_ip(host)
    except ValueError:
        pass
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except (OSError, UnicodeError):
        return True      # it will not resolve for urlopen either, which says so
    try:
        return all(_global_ip(a.split("%")[0]) for a in addresses)
    except ValueError:
        return False


class _PublicRedirectsOnly(urllib.request.HTTPRedirectHandler):
    """A public page that redirects to a private address is not followed there."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urlparse(newurl)
        if target.scheme not in ("http", "https") or not is_public(target.hostname):
            raise urllib.error.URLError(
                f"it redirected to {newurl}, which is not a public web address. "
                "Fetch that address directly if it is really wanted")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class PageText(html.parser.HTMLParser):
    """
    The words of a page, without its code, styling and chrome.

    Done with a parser rather than regular expressions. The expressions only
    removed a <script> or <style> block they could see the end of, and a page
    cut off partway — which a large one always was — handed the model its
    stylesheet: asked for the news, it read "@font-face { font-family: ...".
    """

    CODE = {"script", "style", "noscript", "template", "svg", "head", "iframe",
            "canvas", "object"}
    CHROME = {"nav", "header", "footer", "aside"}
    BLOCKS = {"p", "div", "br", "li", "ul", "ol", "tr", "table", "section",
              "article", "main", "h1", "h2", "h3", "h4", "h5", "h6", "pre",
              "blockquote", "dd", "dt", "form", "hr", "figcaption"}
    VOID = {"br", "hr", "img", "input", "meta", "link", "source", "wbr", "col",
            "area", "base", "embed", "param", "track"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.everything = []     # all the words outside code
        self.content = []        # the same, without menus and footers
        self.code = 0
        self.chrome = 0
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self.in_title = True
        elif tag in self.CODE and tag not in self.VOID:
            self.code += 1
        elif tag in self.CHROME:
            self.chrome += 1
        if tag in self.BLOCKS:
            self._words("\n")

    def handle_startendtag(self, tag, attrs):
        if tag in self.BLOCKS:
            self._words("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        elif tag in self.CODE and self.code:
            self.code -= 1
        elif tag in self.CHROME and self.chrome:
            self.chrome -= 1
        if tag in self.BLOCKS:
            self._words("\n")

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        elif not self.code:
            self._words(data)

    def _words(self, text):
        self.everything.append(text)
        if not self.chrome:
            self.content.append(text)

    def text(self):
        """The content, or everything if leaving out the chrome left nothing."""
        content = _tidy("".join(self.content))
        if len(content) < 200:
            everything = _tidy("".join(self.everything))
            if len(everything) > len(content):
                content = everything
        title = " ".join(self.title.split())
        return f"{title}\n\n{content}".strip() if title else content


def _tidy(text):
    lines = (" ".join(line.split()) for line in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def page_text(markup):
    """The readable text of an HTML document."""
    parser = PageText()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:                                         # noqa: BLE001
        pass   # whatever was read before the parser gave up is still worth having
    return parser.text()


def _charset(response, raw):
    declared = response.headers.get_content_charset()
    if not declared:
        found = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", raw[:4096], re.I)
        declared = found.group(1).decode("ascii", "ignore") if found else ""
    try:
        "".encode(declared or "utf-8")
        return declared or "utf-8"
    except LookupError:
        return "utf-8"


def _filter_page(text, query):
    """Keep only lines matching any keyword in `query`, with 1 line of context."""
    words = [w.lower() for w in query.split() if w.strip()]
    if not words:
        return text
    lines = text.splitlines()
    keep = set()
    for idx, line in enumerate(lines):
        low = line.lower()
        if any(w in low for w in words):
            for near in range(max(0, idx - 1), min(len(lines), idx + 2)):
                keep.add(near)
    if not keep:
        return f"(no lines matched {query!r} in page; total {len(lines)} lines)"
    out, prev = [], -2
    for idx in sorted(keep):
        if prev >= 0 and idx > prev + 1:
            out.append("---")
        out.append(lines[idx])
        prev = idx
    return "\n".join(out)


def fetch_url(args, cfg, sink):
    url = args.get("url", "").strip()
    query = args.get("query", "").strip()
    if not url.startswith(("http://", "https://")):
        return "Error: only http(s) URLs are allowed."

    target = urllib.parse.urlparse(url)
    if not target.hostname:
        return f"Error: there is no host name in {url}"
    if not is_public(target.hostname) and not sink.confirm(
            "Fetch an address on this Mac or your private network?",
            f"\n  GET  {url}\n\n  Addresses like this reach things a web page"
            "\n  should never be able to — Harness's own settings, the model"
            "\n  server, your router.",
            "fetch_url"):
        return declined(sink, "to fetch this address")

    opener = urllib.request.build_opener(_PublicRedirectsOnly())
    request = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (compatible; harness)"})
    try:
        with opener.open(request, timeout=20) as response:
            kind = (response.headers.get_content_type() or "").lower()
            path = urllib.parse.urlparse(response.geturl()).path.lower()
            document = (kind == "application/pdf" or path.endswith(".pdf")
                        or path.endswith(TEXTUTIL_TYPES))
            raw = response.read(FETCH_DOCUMENT_LIMIT if document else FETCH_LIMIT)
            encoding = _charset(response, raw)
    except urllib.error.HTTPError as exc:
        exc.close()
        return f"Error: the server answered {exc.code} {exc.reason} for {url}"
    except Exception as exc:                                  # noqa: BLE001
        return f"Error: could not fetch {url}: {getattr(exc, 'reason', exc)}"

    # A PDF read as a web page was pages of compressed object syntax. They
    # get the same treatment read_file gives one on disk.
    if document or raw.startswith(b"%PDF-"):
        suffix = os.path.splitext(path)[1] if path.endswith(TEXTUTIL_TYPES) else ".pdf"
        handle, temporary = tempfile.mkstemp(suffix=suffix)
        try:
            with os.fdopen(handle, "wb") as out:
                out.write(raw)
            text, found = document_text(temporary)
        except Unreadable as exc:
            return f"Error: {str(exc).replace(temporary, url)}"
        finally:
            with contextlib.suppress(OSError):
                os.remove(temporary)
        if text is None:
            return "Error: that address did not return a readable document."
        body = _tidy(text)
        if query:
            body = _filter_page(body, query)
        return f"({found} text from {url})\n\n{body}"

    markup = raw.decode(encoding, errors="replace")
    if kind in TEXT_TYPES or (kind.startswith("text/") and kind != "text/html"):
        body = markup.strip() or "(empty)"
        return _filter_page(body, query) if query and body != "(empty)" else body
    if kind and "html" not in kind and "xml" not in kind:
        return (f"Error: {url} is {kind}, not a page or a document, so there is "
                "no text to read.")
    body = page_text(markup) or "(no readable text)"
    return _filter_page(body, query) if query and body != "(no readable text)" else body


# --------------------------------------------------------------------------
# How the Mac is doing
# --------------------------------------------------------------------------

def _quietly(command, timeout=15):
    """A command's output, or "" if it is missing or fails — Linux CI has none of these."""
    try:
        return subprocess.run(command, capture_output=True, text=True,
                              timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _megabytes(amount):
    """top's '2993M', '1.2G', '512K' (sometimes with a trailing + or -) in MB."""
    found = re.match(r"([\d.]+)([KMGT]?)", amount or "")
    if not found:
        return 0.0
    scale = {"K": 1 / 1024, "M": 1, "G": 1024, "T": 1024 * 1024, "": 1 / 1048576}
    return float(found.group(1)) * scale[found.group(2)]


def app_name(path):
    """The app a process belongs to: the outermost .app in its path, else its name."""
    for part in (path or "").split("/"):
        if part.endswith(".app"):
            return part[:-4]
    return os.path.basename(path or "") or "?"


def memory_verdict(free_percent, swap_used_gb):
    """Plain words for macOS's own free-memory figure, which is what matters."""
    if free_percent is None:
        return "unknown"
    if free_percent < 10:
        words = "short — macOS is swapping hard, and everything will be slow"
    elif free_percent < 20:
        words = "tight"
    elif free_percent < 35:
        words = "fine"
    else:
        words = "plenty free"
    if swap_used_gb >= 2:
        words += (f", and {swap_used_gb:.1f} GB has been pushed out to swap, so it has "
                  "been short recently")
    return words


def system_status(args, cfg, sink):
    """
    How this Mac is doing, read and interpreted in one go.

    Asked whether the Mac was short on memory, the model tried six GNU-only
    `ps --sort` spellings that macOS does not have, misread `top`, and said
    there was plenty free — with 14% free and 5.6 GB in swap. The figures here
    are the ones macOS itself goes by, and they come with what they mean.
    """
    lines = []
    total = int(_quietly(["sysctl", "-n", "hw.memsize"]).strip() or 0)
    pressure = re.search(r"free percentage: (\d+)%", _quietly(["memory_pressure"]))
    free_percent = int(pressure.group(1)) if pressure else None
    swap = re.search(r"used = ([\d.]+)M", _quietly(["sysctl", "-n", "vm.swapusage"]))
    swap_gb = float(swap.group(1)) / 1024 if swap else 0.0
    top = _quietly(["top", "-l", "1", "-o", "mem", "-n", "40", "-stats", "pid,mem"])
    if total or pressure:
        figures = []
        if total:
            figures.append(f"{total / 1073741824:.0f} GB installed")
        if free_percent is not None:
            figures.append(f"{free_percent}% free")
        phys = re.search(r"\((\S+) wired, (\S+) compressor\)", top)
        if phys:
            figures.append(f"{_megabytes(phys.group(1)) / 1024:.1f} GB wired "
                           "(held by the system and the GPU, never swapped)")
            figures.append(f"{_megabytes(phys.group(2)) / 1024:.1f} GB compressed")
        figures.append(f"{swap_gb:.1f} GB in swap")
        lines.append(f"Memory: {memory_verdict(free_percent, swap_gb)}.")
        lines.append("  " + "; ".join(figures))

    # Grouped by app: Claude, Arc and Chrome each run as a dozen helper
    # processes, and "Browser Helper (Renderer)" is not an answer anyone wants.
    rows = re.findall(r"^\s*(\d+)\s+(\S+)\s*$", top, re.M)
    if rows:
        paths = dict(re.findall(r"^\s*(\d+)\s+(.*\S)\s*$", _quietly(
            ["ps", "-o", "pid=,comm=", "-p", ",".join(pid for pid, _ in rows)]), re.M))
        apps = {}
        for pid, amount in rows:
            name = app_name(paths.get(pid, pid))
            apps[name] = apps.get(name, 0.0) + _megabytes(amount)
        biggest = sorted(apps.items(), key=lambda item: -item[1])[:6]
        lines.append("Using the most memory: " + ", ".join(
            f"{name} {mb / 1024:.1f} GB" if mb >= 1024 else f"{name} {mb:.0f} MB"
            for name, mb in biggest) + ".")

    load = _quietly(["sysctl", "-n", "vm.loadavg"]).strip("{} \n").split()
    cores = _quietly(["sysctl", "-n", "hw.ncpu"]).strip()
    if load and cores.isdigit():
        busy = float(load[0]) / int(cores)
        state = "idle" if busy < 0.3 else "busy" if busy < 0.9 else "flat out"
        lines.append(f"CPU: {state} — load {load[0]} over the last minute on {cores} cores.")

    try:
        disk = shutil.disk_usage("/System/Volumes/Data" if os.path.isdir(
            "/System/Volumes/Data") else "/")
        lines.append(f"Disk: {disk.free / 1e9:.0f} GB free of {disk.total / 1e9:.0f} GB "
                     f"({disk.used / disk.total:.0%} used).")
    except OSError:
        pass

    battery = re.search(r"(\d+)%; ([^;]+);", _quietly(["pmset", "-g", "batt"]))
    if battery:
        lines.append(f"Battery: {battery.group(1)}%, {battery.group(2).strip()}.")
    low_power = re.search(r"lowpowermode\s+(\d)", _quietly(["pmset", "-g"]))
    if low_power:
        lines.append("Low Power Mode: on — it slows the GPU, and with it the model."
                     if low_power.group(1) == "1" else "Low Power Mode: off.")
    return "\n".join(lines) or "Error: could not read this machine's status."


# --------------------------------------------------------------------------
# Finding files
# --------------------------------------------------------------------------

# Not worth walking: other programs' innards, caches, and package bundles that
# look like folders but are one thing to a person.
SKIP_FOLDERS = {"Library", "node_modules", "__pycache__", "venv", "DerivedData",
                "Pods", "Caches", "site-packages", "build", "dist", "target"}
SKIP_SUFFIXES = (".app", ".photoslibrary", ".bundle", ".framework", ".xcarchive",
                 ".musiclibrary", ".tvlibrary", ".imovielibrary", ".fcpbundle",
                 ".lrdata", ".xcodeproj", ".xcworkspace")
FIND_SECONDS = 15
FIND_SHOWN = 40


def _matches(name, words, pattern):
    lowered = name.lower()
    if pattern:
        return fnmatch.fnmatch(lowered, pattern)
    return all(word in lowered for word in words)


def find_files(args, cfg, sink):
    """
    Everything under a folder whose name matches, newest first.

    The model used to look for a file one folder at a time — four steps of
    list_files to reach Documents/Lightroom/Finals — or with `find ~ ... | head`,
    which asks for approval every time because it is a pipeline, and which
    walks every cache in ~/Library on the way.
    """
    wanted = args.get("name", "").strip()
    if not wanted:
        return "Error: say what to look for in `name`, e.g. cv or *.pdf"
    raw_folder = args.get("folder", "")
    folder = resolve(raw_folder or "~", cfg)
    if not os.path.isdir(folder):
        hint = _missing_hint(raw_folder, folder, cfg, want_dir=True)
        return f"Error: not a folder: {folder}{hint}"
    folders = args.get("kind", "").strip().lower().startswith("folder")
    pattern = wanted.lower() if any(mark in wanted for mark in "*?[") else ""
    words = wanted.lower().split()

    deadline = time.monotonic() + FIND_SECONDS
    found, looked, cut_short = [], 0, False
    for root, subfolders, files in os.walk(folder):
        if time.monotonic() > deadline:
            cut_short = True
            break
        subfolders[:] = [name for name in subfolders
                         if not name.startswith(".") and name not in SKIP_FOLDERS
                         and not name.lower().endswith(SKIP_SUFFIXES)
                         and not is_secret(os.path.join(root, name))]
        candidates = subfolders if folders else files
        looked += len(candidates)
        for name in candidates:
            if name.startswith(".") or not _matches(name, words, pattern):
                continue
            path = os.path.join(root, name)
            try:
                info = os.stat(path)
            except OSError:
                continue
            found.append((info.st_mtime, info.st_size, path))

    what = "folder" if folders else "file"
    if not found:
        note = (f" (stopped after {FIND_SECONDS}s — search a smaller folder)"
                if cut_short else "")
        return f"No {what}s named like '{wanted}' under {folder}{note}."
    found.sort(reverse=True)
    lines = [f"{len(found)} {what}{'s' if len(found) != 1 else ''} named like "
             f"'{wanted}' under {folder}, newest first:"]
    for mtime, size, path in found[:FIND_SHOWN]:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))
        # Exact enough to quote: "0.1 MB" for a 145 KB file came back from the
        # model as "102 KB".
        weight = ("" if folders else f"{size / 1e6:.1f} MB, " if size >= 1_000_000
                  else f"{size / 1e3:.0f} KB, " if size >= 1000 else f"{size} bytes, ")
        lines.append(f"{path}  ({weight}changed {when})")
    if len(found) > FIND_SHOWN:
        lines.append(f"... and {len(found) - FIND_SHOWN} more — "
                     "give more of the name to narrow it")
    if cut_short:
        lines.append(f"(stopped after {FIND_SECONDS}s, so there may be more — "
                     "search a smaller folder to be sure)")
    return "\n".join(lines)


SEARCH_SECONDS = 15
SEARCH_MATCHES = 60
SEARCH_PER_FILE = 10
SEARCH_MAX_BYTES = 2_000_000


def _search_one_file(path, regex, label, hits, cap):
    try:
        if os.path.getsize(path) > SEARCH_MAX_BYTES:
            return 0
        with open(path, "rb") as handle:
            head = handle.read(4096)
            if b"\0" in head:
                return 0
        with open(path, encoding="utf-8", errors="replace") as handle:
            per_file = 0
            for number, line in enumerate(handle, 1):
                if regex.search(line):
                    snippet = line.strip()
                    if len(snippet) > 180:
                        snippet = snippet[:177] + "..."
                    hits.append(f"{label}:{number}: {snippet}")
                    per_file += 1
                    if per_file >= SEARCH_PER_FILE or len(hits) >= cap:
                        return per_file
            return per_file
    except OSError:
        return 0


def search_in_files(args, cfg, sink):
    """
    Search text inside files without spawning grep or walking caches/secrets.

    Returns `path:line: snippet` lines, capped so a noisy log or broad match
    cannot blow the context window. Skips hidden directories, caches, app
    bundles, binary files, and credential folders (SECRET_PATHS).
    """
    pattern = args.get("pattern", "").strip()
    if not pattern:
        return "Error: say what to search for in `pattern`."
    raw_folder = args.get("folder", "").strip()
    default_root = cfg.get("project_dir") or "~"
    target = resolve(raw_folder or default_root, cfg)
    if not os.path.exists(target):
        hint = _missing_hint(raw_folder, target, cfg, want_dir=True)
        return f"Error: not found: {target}{hint}"
    if is_secret(target):
        return f"Error: refusing to search inside a credentials folder: {target}"

    file_glob = args.get("file_pattern", "").strip().lower()
    case_sensitive = args.get("case_sensitive", "").strip().lower() in ("1", "true", "yes")
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        regex = re.compile(pattern, flags)
    except re.error:
        regex = re.compile(re.escape(pattern), flags)

    proj_cfg = cfg.get("project_dir") or ""
    project = os.path.realpath(os.path.expanduser(proj_cfg)) if proj_cfg else ""

    def label_for(path):
        if project and _under(os.path.realpath(path), project):
            return os.path.relpath(os.path.realpath(path), project)
        return path

    hits = []
    files_matched = 0
    cut_short = False
    truncated_hits = False
    deadline = time.monotonic() + SEARCH_SECONDS

    if os.path.isfile(target):
        if _search_one_file(target, regex, label_for(target), hits, SEARCH_MATCHES):
            files_matched = 1
    else:
        for root, subfolders, files in os.walk(target):
            if time.monotonic() > deadline:
                cut_short = True
                break
            subfolders[:] = sorted(
                name for name in subfolders
                if not name.startswith(".") and name not in SKIP_FOLDERS
                and not name.lower().endswith(SKIP_SUFFIXES)
                and not is_secret(os.path.join(root, name))
            )
            for name in sorted(files):
                if name.startswith("."):
                    continue
                if file_glob and not fnmatch.fnmatch(name.lower(), file_glob):
                    continue
                full = os.path.join(root, name)
                if is_secret(full):
                    continue
                found = _search_one_file(full, regex, label_for(full), hits, SEARCH_MATCHES)
                if found:
                    files_matched += 1
                if len(hits) >= SEARCH_MATCHES:
                    truncated_hits = True
                    break
            if truncated_hits:
                break

    if not hits:
        note = (f" (stopped after {SEARCH_SECONDS}s — narrow `folder` or `file_pattern`)"
                if cut_short else "")
        return f"No matches for {pattern!r} under {target}{note}."
    header = (f"{len(hits)} match{'es' if len(hits) != 1 else ''} across "
              f"{files_matched} file{'s' if files_matched != 1 else ''} "
              f"for {pattern!r}:")
    out = [header] + hits
    if truncated_hits:
        out.append(f"... (capped at {SEARCH_MATCHES} matches — "
                   "narrow `pattern`, `folder`, or `file_pattern`)")
    if cut_short:
        out.append(f"(stopped after {SEARCH_SECONDS}s — search a smaller folder to be sure)")
    return "\n".join(out)


# --------------------------------------------------------------------------
# Images
# --------------------------------------------------------------------------

# Formats llama.cpp decodes itself. Anything else — HEIC from an iPhone, WebP,
# TIFF — goes through sips first, which every Mac has.
NATIVE_IMAGES = {b"\x89PNG": "image/png", b"\xff\xd8\xff": "image/jpeg",
                 b"GIF8": "image/gif", b"BM": "image/bmp"}
IMAGE_SIDE = 1024          # longest side sent; ~1,000 tokens of context each
IMAGE_LIMIT = 60_000_000   # bytes; past this it is not a photo worth reading


class Picture(str):
    """A tool result that comes with an image for the model to look at."""
    image = ""


def _image_kind(head):
    for magic, kind in NATIVE_IMAGES.items():
        if head.startswith(magic):
            return kind
    if head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"mif1", b"msf1", b"hevc"):
        return "image/heic"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    return ""


def _dimensions(path):
    try:
        out = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", path],
                             capture_output=True, text=True, timeout=30).stdout
        width = int(re.search(r"pixelWidth: (\d+)", out).group(1))
        height = int(re.search(r"pixelHeight: (\d+)", out).group(1))
        return width, height
    except (OSError, subprocess.SubprocessError, AttributeError, ValueError):
        return 0, 0


_prepared = {}


def image_url(path):
    """
    An image as a data URL the model can take, made small enough to afford.

    Kept until the file changes, because the same picture goes back with every
    later step of the turn. "" if it cannot be read any more.
    """
    try:
        info = os.stat(path)
    except OSError:
        return ""
    key = (path, info.st_mtime, info.st_size)
    if key in _prepared:
        return _prepared[key]
    with open(path, "rb") as handle:
        head = handle.read(16)
    kind = _image_kind(head)
    width, height = _dimensions(path)
    if kind in NATIVE_IMAGES.values() and max(width, height) <= IMAGE_SIDE:
        with open(path, "rb") as handle:
            data = handle.read()
    else:
        handle, converted = tempfile.mkstemp(suffix=".jpg")
        os.close(handle)
        try:
            command = ["sips", "-s", "format", "jpeg", "-s", "formatOptions", "85"]
            if max(width, height) > IMAGE_SIDE or not width:
                command += ["-Z", str(IMAGE_SIDE)]
            done = subprocess.run(command + [path, "--out", converted],
                                  capture_output=True, timeout=60)
            if done.returncode:
                return ""
            with open(converted, "rb") as out:
                data = out.read()
            kind = "image/jpeg"
        except (OSError, subprocess.SubprocessError):
            return ""
        finally:
            with contextlib.suppress(OSError):
                os.remove(converted)
    url = f"data:{kind};base64,{base64.b64encode(data).decode()}"
    if len(_prepared) > 32:
        _prepared.clear()
    _prepared[key] = url
    return url


def view_image(args, cfg, sink):
    """
    Look at a picture. The text says which one; the picture itself goes to
    the model alongside it (see agent.for_model).
    """
    raw = args.get("path", "")
    path = resolve(raw, cfg)
    if not os.path.isfile(path):
        return f"Error: not a file: {path}{_missing_hint(raw, path, cfg)}"
    if not confirm_secret(path, sink, "Show the model"):
        return declined(sink, "to show this file")
    size = os.path.getsize(path)
    if size > IMAGE_LIMIT:
        return f"Error: {path} is {size / 1e6:.0f} MB, too large to look at."
    with open(path, "rb") as handle:
        kind = _image_kind(handle.read(16))
    if not kind:
        return (f"Error: {path} is not an image this can show "
                "(PNG, JPEG, HEIC, GIF, WebP, TIFF).")
    if not image_url(path):
        return f"Error: {path} could not be opened as an image."
    width, height = _dimensions(path)
    shape = f"{width}×{height}, " if width else ""
    result = Picture(f"Here is {path} ({shape}{size / 1e6:.1f} MB). Describe only "
                     "what you can actually see in it.")
    result.image = path
    return result


# --------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------

# Through the Mail app, because nothing else on a Mac can send: `mail` and
# `sendmail` hand messages to a local postfix with no relay configured, which
# accepts them, exits 0, and never delivers. The model read that exit code as
# success and told the user 324 MB of photos were on their way.
#
# The values reach the script as arguments, never pasted into its source, so
# a subject line cannot turn into AppleScript.
MAIL_SCRIPT = """
on run argv
    set theSubject to item 1 of argv
    set theBody to item 2 of argv
    set settleSeconds to (item 3 of argv) as number
    set recipientCount to (item 4 of argv) as number
    tell application "Mail"
        if (count of accounts) is 0 then error "NO_ACCOUNT"
        set theMessage to make new outgoing message with properties {visible:false}
        set subject of theMessage to theSubject
        set content of theMessage to theBody & return & return
        tell theMessage
            repeat with i from 5 to (4 + recipientCount)
                set addr to item i of argv
                make new to recipient at end of to recipients with properties {address:addr}
            end repeat
        end tell
        repeat with i from (5 + recipientCount) to (count of argv)
            set theFile to (POSIX file (item i of argv)) as alias
            tell content of theMessage
                set attProps to {file name:theFile}
                make new attachment with properties attProps at after the last paragraph
            end tell
        end repeat
        delay settleSeconds
        send theMessage
    end tell
    return "SENT"
end run
"""
MAIL_TIMEOUT = 180
ADDRESS = re.compile(r"^[^@\s,;<>\"]+@[^@\s,;<>\"]+\.[^@\s,;<>\"]+$")


def attachment_paths(value, cfg):
    """
    The files to attach, from however the model wrote the list.

    A JSON list, one per line, or separated by commas — file names can hold
    commas, so a comma only splits where every piece is a real file.
    """
    value = (value or "").strip()
    if not value:
        return []
    if value.startswith("["):
        try:
            items = [str(item) for item in json.loads(value)]
            return [resolve(item, cfg) for item in items if str(item).strip()]
        except ValueError:
            pass
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if len(lines) == 1 and "," in lines[0]:
        pieces = [resolve(piece.strip(), cfg) for piece in lines[0].split(",") if piece.strip()]
        if all(os.path.isfile(piece) for piece in pieces):
            return pieces
    return [resolve(line, cfg) for line in lines]


def send_email(args, cfg, sink):
    recipients = [part.strip() for part in re.split(r"[,;\s]+", args.get("to", ""))
                  if part.strip()]
    subject = args.get("subject", "").strip()
    body = args.get("body", "")
    if not recipients:
        return "Error: no recipient. Give the address to send to in `to`."
    bad = [r for r in recipients if not ADDRESS.match(r)]
    if bad:
        return f"Error: not an email address: {', '.join(bad)}"

    files = attachment_paths(args.get("attachments", ""), cfg)
    missing = [path for path in files if not os.path.isfile(path)]
    if missing:
        return ("Error: these attachments are not files, so nothing was sent: "
                + ", ".join(missing))
    secret = next((path for path in files if is_secret(path)), "")
    if secret:
        return f"Error: refusing to email a file from a credentials folder: {secret}"
    total = sum(os.path.getsize(path) for path in files)

    lines = [f"\n  TO       {', '.join(recipients)}",
             f"  SUBJECT  {subject or '(none)'}", ""]
    lines += [f"    | {line[:100]}" for line in body.splitlines()[:8]]
    if len(body.splitlines()) > 8:
        lines.append(f"    | ... {len(body.splitlines()) - 8} more lines")
    if files:
        lines.append("")
        lines += [f"  ATTACH   {path}  ({os.path.getsize(path) / 1e6:.1f} MB)"
                  for path in files]
    if total > 20e6:
        lines.append(f"\n  {total / 1e6:.0f} MB in all — over what most mail servers accept"
                     "\n  unless Mail Drop is on for this account.")
    lines.append("\n  Sent from the Mail app's default account. Once sent, it cannot be"
                 "\n  taken back.")
    # Asked every time, whatever the approval settings, and never "always":
    # mail leaves the machine and cannot be recalled. An unattended run refuses.
    if not sink.confirm("Send this email?", "\n".join(lines), ""):
        return declined(sink, "to send this email")

    settle = min(30, 2 + int(total / 10e6))    # Mail loads attachments in the background
    command = ["osascript"]
    for line in MAIL_SCRIPT.strip().splitlines():
        command += ["-e", line]
    command += [subject, body, str(settle), str(len(recipients))] + recipients + files
    try:
        done = subprocess.run(command, capture_output=True, text=True,
                              timeout=MAIL_TIMEOUT + settle)
    except FileNotFoundError:
        return "Error: sending email needs the Mail app, which needs macOS."
    except subprocess.TimeoutExpired:
        return ("Error: Mail did not answer. macOS may be asking, on the Mac, whether "
                "Harness may control Mail — allow it there and try again.")
    if "NO_ACCOUNT" in done.stderr:
        return ("Error: Mail has no email account set up, so it cannot send. Add one in "
                "Mail → Settings → Accounts, then try again. Nothing was sent.")
    if done.returncode or "SENT" not in done.stdout:
        reason = done.stderr.strip() or done.stdout.strip() or f"exit code {done.returncode}"
        if "-1743" in reason or "Not authorized" in reason:
            reason = ("macOS has not allowed Harness to control Mail. Allow it in System "
                      "Settings → Privacy & Security → Automation")
        return f"Error: the email was not sent: {reason}"
    return (f"Mail accepted the email for sending: to {', '.join(recipients)}, subject "
            f"\"{subject}\", {len(files)} attachment{'s' if len(files) != 1 else ''}"
            f"{f' ({total / 1e6:.1f} MB)' if files else ''}. Delivery happens in Mail — "
            "if it does not arrive, look in Mail's Outbox.")


# --------------------------------------------------------------------------
# What the model is told about them
# --------------------------------------------------------------------------

def _schema(tool, description, required_params=None, **params):
    """Build OpenAI function schema with optional required params specification."""
    req = list(params) if required_params is None else required_params
    return {
        "type": "function",
        "function": {
            "name": tool,
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
    _schema("list_files",
            "List the files in a directory. For finding files by name or type "
            "(such as *.pdf) across folders, use find_files instead.",
            required_params=["path"],
            path="Directory path.",
            details="Optional: \"true\" to include file sizes and dates, newest first."),
    _schema("read_file",
            "Read a file, numbered by line. PDFs and Word, RTF or OpenDocument "
            "files come back as their text. Long files come back a page at a "
            "time — read on with start_line.",
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
    _schema("fetch_url",
            "Fetch a web page and return its visible text. On long docs pages, "
            "pass `query` to filter to relevant sections instead of using curl.",
            required_params=["url"],
            url="An http(s) URL.",
            query="Optional keywords to filter a long page to matching lines."),
    _schema("run_command",
            "Run a read-only shell command such as df, ls, du, or uname. "
            "Anything that writes or deletes requires the user to confirm.",
            command="The command."),
    _schema("system_status",
            "How this Mac is doing right now: whether it is short on memory and "
            "which apps use the most, CPU load, free disk space, battery and Low "
            "Power Mode. Use this rather than ps, top or vm_stat.",
            required_params=[]),
    _schema("find_files",
            "Search a folder and everything inside it for files by name — much "
            "faster than listing folders one by one. Results come newest first, "
            "with size and date.",
            required_params=["name"],
            name="Part of the name, any case. Several words must all appear "
                 "(\"cv en\"). Or a pattern such as *.pdf.",
            folder="Where to look. Leave out to search the whole home folder.",
            kind="Optional: \"folders\" to find folders instead of files."),
    _schema("search_in_files",
            "Search inside text files for a word or regular expression and "
            "return file:line snippets. Much faster and cleaner than reading "
            "long logs or running grep pipelines.",
            required_params=["pattern"],
            pattern="Text or regex to search for (case-insensitive by default).",
            folder="Optional folder or file to search (defaults to working folder).",
            file_pattern="Optional filename glob such as *.py or *.log."),
    _schema("view_image",
            "Look at a picture: a photo, a screenshot, a scan. You see the image "
            "itself. Use it only when what is in the picture matters — to tell "
            "versions of a file apart, or pick the newest, use names and dates "
            "instead. Each picture is slow and costly; a turn can show at most "
            "six.",
            path="Image file path."),
    _schema("send_email",
            "Send an email, with files attached if wanted, through the Mail app. "
            "The user is shown exactly what will be sent and must approve it. "
            "Use this rather than the mail or sendmail commands, which cannot "
            "deliver from this Mac. Attach files directly by their full path; "
            "do not copy or zip them first.",
            required_params=["to", "subject", "body"],
            to="Recipient address, or several separated by commas.",
            subject="Subject line.", body="The message text.",
            attachments="Optional. Full paths of files to attach, one per line."),
]

DISPATCH = {
    "list_files": list_files,
    "read_file": read_file,
    "write_file": write_file,
    "edit_file": edit_file,
    "web_search": web_search,
    "fetch_url": fetch_url,
    "run_command": run_command,
    "send_email": send_email,
    "view_image": view_image,
    "find_files": find_files,
    "search_in_files": search_in_files,
    "system_status": system_status,
}


def schemas_for(cfg):
    """Only advertise the tools the user left switched on."""
    allowed = cfg.get("tools", {})
    return [s for s in SCHEMAS if allowed.get(s["function"]["name"], True)]


def _as_text(value):
    """One argument as the string every tool expects."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, indent=2)
    return str(value)


def call(name, args, cfg, sink):
    """
    Run one tool by name, refusing anything switched off or unknown.

    Every argument arrives as text, whatever the model sent: a path written as
    a number reached `.strip()` and the AttributeError ended the whole turn.
    And whatever a tool raises comes back as its result, for the model to work
    around, rather than taking the turn down with it.
    """
    if not cfg.get("tools", {}).get(name, True):
        return (f"Error: {name} is switched off for this run. Do not retry. "
                "Use the tools you do have, or answer without them.")
    handler = DISPATCH.get(name)
    if not handler:
        return f"Error: no such tool: {name}"
    args = {str(key): _as_text(value) for key, value in (args or {}).items()}
    try:
        return truncate(handler(args, cfg, sink))
    except Exception as exc:                                  # noqa: BLE001
        return f"Error: {name} failed: {type(exc).__name__}: {exc}"

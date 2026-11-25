"""
harness.server — the web front end.

Adds an HTTP layer over harness.agent and turns the terminal's y/N prompt into
a pair of buttons. Same safety rules apply — protected paths are still refused
outright, and nothing is written without an explicit approval.

    harness            # starts the server and opens a browser
    harness --port 7000

The model server must already be running (harness model).
"""

import argparse
import base64
import contextlib
import datetime
import ipaddress
import json
import os
import queue
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import agent
from . import config as settings
from . import tools

HERE = os.path.dirname(os.path.abspath(__file__))
UI_FILE = os.path.join(HERE, "ui.html")
MODEL_HEALTH = "http://127.0.0.1:8080/health"
AGENT_PLIST = os.path.expanduser("~/Library/LaunchAgents/com.harness.app.plist")

PORT = 8420
# How to spot the app in the process table. It runs as `harness.server` when
# started in the background and as `harness.cli serve` in the foreground under
# launchd, and stop/status/the System panel have to find either.
APP_PROCESS = r"harness\.(server|cli serve)"

# The caffeinate wrappers Harness started, and only those. "caffeinate -i" on
# its own matched every caffeinate on the Mac, so stopping Harness also ended
# whatever else was keeping the machine awake.
OWN_CAFFEINATE = r"caffeinate -i .*harness"

# Set by main() when --lan or --remote is used. While loopback-only there is
# nothing to authenticate: reaching the socket already means local access.
REQUIRE_TOKEN = False
# Set by main() when --remote is used, even while Tailscale is still down so
# access_mode() and relaunch() keep --remote instead of falling back to local.
REMOTE = False
# Set when --remote pins the server to a single Tailscale address.
SERVE_HOST = ""
# Optional TLS for the tailnet listener (loopback stays plain HTTP).
TLS_WANTED = False
TLS_ACTIVE = False
TLS_CERT = ""
TLS_KEY = ""
TAILNET_POLL = 15
MAX_BODY = 50 * 1024 * 1024
MAX_UPLOAD = 25 * 1024 * 1024
UPLOAD_NAME_RE = re.compile(r"^[a-zA-Z0-9._-]+$")
EXT_FOR_MIME = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
    "image/bmp": ".bmp", "image/heic": ".heic", "image/webp": ".webp",
    "image/tiff": ".tiff",
}

# Seconds between SSE keep-alive comments on an idle chat stream. This is what
# detects a browser that went away without closing its socket — a phone that
# slept, a tailnet that dropped — because writing to it is the only way to find
# out. Short enough to notice quickly, long enough to be free.
HEARTBEAT = 15

# The optional voice sidecar (voice/server.py), if it is running. Requests are
# proxied through this server rather than called directly from the browser: on
# a phone, 127.0.0.1 is the phone, and the sidecar is on the Mac. Proxying also
# means the browser never needs to know the sidecar exists.
VOICE_URL = "http://127.0.0.1:8421"
VOICE_TIMEOUT = 600      # synthesis runs slower than real time

# How long an approval may wait for an answer. Not a timeout in any meaningful
# sense: the heartbeat above is what catches an absent browser. This only stops
# a forgotten tab from holding a thread open indefinitely.
APPROVAL_BACKSTOP = 12 * 3600


# --------------------------------------------------------------------------
# Poking at the machine
# --------------------------------------------------------------------------

def shell(*command, timeout=8):
    """Run a command and return its stdout, or "" if anything goes wrong."""
    try:
        return subprocess.run(command, capture_output=True, text=True,
                              timeout=timeout).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def pids(pattern, exact=False):
    return [int(p) for p in shell("pgrep", "-x" if exact else "-f", pattern).split()
            if p.isdigit()]


def rss_gb(pid):
    try:
        return round(int(shell("ps", "-o", "rss=", "-p", str(pid))) / 1048576, 2)
    except ValueError:
        return 0.0


def alive(url, timeout=2):
    try:
        with urllib.request.urlopen(url, timeout=timeout):
            return True
    except Exception:                                        # noqa: BLE001
        return False


def loaded_model():
    """
    The model the running server actually has in memory.

    The choice file records what was *picked*, which is not the same thing: a
    fresh install has no choice file, and switching models only takes effect
    after a restart. Reading the live process is the only honest answer.
    """
    for pid in pids("llama-server", exact=True):
        argv = shell("ps", "-o", "command=", "-p", str(pid)).split()
        if "-m" in argv:
            try:
                return argv[argv.index("-m") + 1]
            except IndexError:
                pass
    return ""


def restart_model():
    """
    Stop llama-server and start it again on the newly chosen model.

    Waits for the *process* to disappear, not just for the health endpoint to
    stop answering. model.sh refuses to start while another llama-server is
    alive, and a dying process stops serving well before it exits — so watching
    health alone raced the guard and left nothing running at all.
    """
    shell("pkill", "-x", "llama-server")
    for _ in range(60):
        if not pids("llama-server", exact=True):
            break
        time.sleep(0.5)
    else:
        shell("pkill", "-9", "-x", "llama-server")
        time.sleep(1)

    # The child gets its own handle on the file, so closing this one straight
    # afterwards is right — left open, every model restart leaked a descriptor.
    with open(os.path.join(settings.BASE, "model.log"), "w") as log:
        subprocess.Popen(["caffeinate", "-i", os.path.join(HERE, "model.sh")],
                         stdout=log, stderr=log, start_new_session=True)


def tailnet_address():
    """
    This machine's Tailscale address, if the VPN is up.

    Tailscale hands out 100.64.0.0/10 (the CGNAT range) on a utun interface.
    Binding to that address specifically means the app is reachable over the
    tailnet and nowhere else — not on the local Wi-Fi, not on the public
    internet — so a coffee-shop network can't reach it even by accident.
    """
    for line in shell("ifconfig").splitlines():
        line = line.strip()
        if line.startswith("inet 100."):
            address = line.split()[1]
            if 64 <= int(address.split(".")[1]) <= 127:   # 100.64/10 only
                return address
    return ""


def lan_address():
    """This machine's address on the local network, or empty if offline."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Never actually sends anything; just asks the routing table which
        # interface would be used, which is how we learn our own LAN IP.
        probe.connect(("192.168.1.1", 1))
        return probe.getsockname()[0]
    except OSError:
        return ""
    finally:
        probe.close()


def _tailscale_bin():
    for candidate in ("/usr/local/bin/tailscale", "/opt/homebrew/bin/tailscale",
                      "/Applications/Tailscale.app/Contents/MacOS/Tailscale"):
        if os.path.isfile(candidate):
            return candidate
    return shutil.which("tailscale") or ""


def tailnet_fqdn():
    """The full tailnet DNS name, e.g. davids-macbook-air.tail1234.ts.net."""
    binary = _tailscale_bin()
    if not binary:
        return ""
    try:
        info = json.loads(shell(binary, "status", "--json"))
        return (info.get("Self", {}).get("DNSName") or "").rstrip(".")
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
        return ""


def magic_dns_name():
    """The short tailnet hostname, e.g. davids-macbook-air — nicer than an IP."""
    fqdn = tailnet_fqdn()
    return fqdn.split(".")[0] if fqdn else ""


def find_tls_certs(cert_dir=None, fqdn=""):
    """Locate an existing (cert, key) PEM pair for HTTPS on the tailnet listener."""
    if TLS_CERT and TLS_KEY and os.path.isfile(TLS_CERT) and os.path.isfile(TLS_KEY):
        return TLS_CERT, TLS_KEY
    folder = cert_dir or settings.CERTS_DIR
    if not os.path.isdir(folder):
        return "", ""
    if fqdn:
        crt = os.path.join(folder, f"{fqdn}.crt")
        key = os.path.join(folder, f"{fqdn}.key")
        if os.path.isfile(crt) and os.path.isfile(key):
            return crt, key
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return "", ""
    for name in names:
        if name.endswith(".crt"):
            key = os.path.join(folder, name[:-4] + ".key")
            if os.path.isfile(key):
                return os.path.join(folder, name), key
    return "", ""


def provision_tailscale_cert(cert_dir=None):
    """Ask `tailscale cert` for a real TLS certificate when --tls is requested."""
    binary = _tailscale_bin()
    fqdn = tailnet_fqdn()
    if not binary or not fqdn:
        return "", ""
    folder = cert_dir or settings.CERTS_DIR
    os.makedirs(folder, exist_ok=True)
    crt = os.path.join(folder, f"{fqdn}.crt")
    key = os.path.join(folder, f"{fqdn}.key")
    try:
        done = subprocess.run(
            [binary, "cert", "--cert-file", crt, "--key-file", key, fqdn],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return "", ""
    if done.returncode == 0 and os.path.isfile(crt) and os.path.isfile(key):
        with contextlib.suppress(OSError):
            os.chmod(key, 0o600)
        return crt, key
    return "", ""


def make_ssl_context(cert_path, key_path):
    """Build a TLS 1.2+ server SSLContext, or None if the files cannot be loaded."""
    if not cert_path or not key_path:
        return None
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
        return ctx
    except (OSError, ssl.SSLError):
        return None


def phone_url():
    # In remote mode the server never binds LAN (0.0.0.0), so do not fall back
    # to lan_address() while waiting for Tailscale to come up.
    scheme = "https" if TLS_ACTIVE else "http"
    host = (tailnet_fqdn() or SERVE_HOST) if (REMOTE and TLS_ACTIVE) else (
        SERVE_HOST if REMOTE else (SERVE_HOST or lan_address()))
    if not host:
        return ""
    if REMOTE:
        return f"{scheme}://{host}:{PORT}"
    return f"{scheme}://{host}:{PORT}/?key={settings.access_token()}"


def access_mode():
    return "remote" if (REMOTE or SERVE_HOST) else "lan" if REQUIRE_TOKEN else "local"


def system_state():
    """Everything the System panel shows, so the UI needs one request not six."""
    model = pids("llama-server", exact=True)
    app = pids(APP_PROCESS)

    free = next((line.split(":")[-1].strip()
                 for line in shell("memory_pressure").splitlines()
                 if "free percentage" in line), "")
    disk = shell("df", "-h", "/").splitlines()
    uptime = shell("uptime")

    return {
        "model": {"running": bool(model), "pid": model[0] if model else None,
                  "gb": rss_gb(model[0]) if model else 0,
                  "name": os.path.basename(loaded_model())[:-5]},
        "app": {"running": bool(app), "pid": app[0] if app else None,
                "gb": rss_gb(app[0]) if app else 0},
        "memory_free": free,
        "disk_free": disk[-1].split()[3] if len(disk) > 1 else "?",
        "uptime": uptime.split("up")[-1].split(",")[0].strip() if uptime else "",
        "mode": access_mode(),
        "tls": TLS_ACTIVE,
        "url": phone_url() if REQUIRE_TOKEN else f"http://127.0.0.1:{PORT}",
        "tailnet": tailnet_address(),
        "autostart": os.path.exists(AGENT_PLIST),
    }


PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.harness.app</string>
  <key>ProgramArguments</key>
  <array><string>{binary}</string><string>serve</string>{mode}
    <string>--no-browser</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key><false/>
  </dict>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict></plist>
"""


def write_autostart(enable):
    """Install or remove the login agent that keeps Harness running."""
    if not enable:
        shell("launchctl", "unload", AGENT_PLIST)
        with contextlib.suppress(OSError):
            os.remove(AGENT_PLIST)
        return True

    # HERE is the package dir, so bin/ is one level up — not two.
    binary = os.path.join(os.path.dirname(HERE), "bin", "harness")
    if not os.path.exists(binary):
        return False
    mode = {"remote": "<string>--remote</string>",
            "lan": "<string>--lan</string>"}.get(access_mode(), "")

    os.makedirs(os.path.dirname(AGENT_PLIST), exist_ok=True)
    with open(AGENT_PLIST, "w") as handle:
        handle.write(PLIST_TEMPLATE.format(
            binary=binary, mode=mode,
            log=os.path.join(settings.BASE, "autostart.log")))
    shell("launchctl", "load", AGENT_PLIST)
    return True


def relaunch(mode):
    """
    Restart this process in a different access mode.

    execv keeps the PID and replaces the image, which is the cleanest way to
    rebind the listening socket — the alternative is spawning a child that
    races the parent for the port. Delayed so the HTTP response goes out first.
    """
    time.sleep(0.6)
    # If Harness was launched from a shell or pipe whose stdin/stdout later
    # closed, execv hands the new Python a closed fd 0/1/2 and Python aborts in
    # init_sys_streams with EBADF. Re-point any closed stdio fd to /dev/null.
    for fd, flags in ((0, os.O_RDONLY), (1, os.O_WRONLY), (2, os.O_WRONLY)):
        try:
            os.fstat(fd)
        except OSError:
            null = os.open(os.devnull, flags)
            if null != fd:
                os.dup2(null, fd)
                os.close(null)
    argv = [sys.executable, "-m", "harness.server", "--port", str(PORT),
            "--no-browser"]
    if mode in ("remote", "lan"):
        argv.append("--" + mode)
    if TLS_WANTED and mode == "remote":
        argv.append("--tls")
    if TLS_CERT and TLS_KEY:
        argv += ["--cert", TLS_CERT, "--key", TLS_KEY]
    os.execv(sys.executable, argv)


# --------------------------------------------------------------------------
# Running a turn for a browser
# --------------------------------------------------------------------------

# Approvals in flight, keyed by id. The chat thread blocks on the Event while
# the browser POSTs a decision from a separate request.
approvals = {}
approvals_lock = threading.Lock()

# Chat turns in flight, so /api/cancel can find the sink to stop.
live_turns = {}

# Left in the transcript when an approval sat unanswered until the backstop.
# Said in the transcript rather than only on screen, because the turn may have
# had no screen by then.
EXPIRED_STOP = ("(Stopped here: the next step waited {hours} hours for your "
                "approval and nobody answered. Send the request again to carry on.)")

# Left the same way when a turn ends on an error with no answer. The error is
# shown on the page as it happens — but a phone has usually gone by then, and
# reopening the chat showed the question with nothing after it and no reason,
# which is exactly what "it just stops sometimes" looked like.
ERROR_STOP = "(Stopped here: {error})"


def session_running(session_id):
    """Whether a turn is still being worked on for this chat."""
    return any(sink.session_id == session_id for sink in list(live_turns.values()))


def waiting_approvals(session_id):
    """
    The questions a chat's turn is waiting on, for a page that comes back to it.

    The page that was shown a question is not always there to answer it: the
    phone locks, the tab closes, the chat is opened on the Mac instead. The
    question waits on the server either way, and whichever page opens the chat
    next is given it.
    """
    with approvals_lock:
        return [{"id": approval_id, "question": record.get("question", ""),
                 "kind": record.get("kind", ""), "detail": record.get("detail", "")}
                for approval_id, record in approvals.items()
                if record["sink"].session_id == session_id
                and not record["event"].is_set()]


def release_approvals(sink):
    """
    Unblock the approvals a given turn is waiting on, because that turn has
    been abandoned.

    Scoped to one turn on purpose. Releasing every approval in the process
    meant that stopping a turn on the Mac silently refused whatever the phone
    was being asked about at the same moment.
    """
    with approvals_lock:
        for record in approvals.values():
            if record["sink"] is sink:
                record["event"].set()


# Tools the user granted for the rest of a chat, keyed by session id. Held in
# memory only: restarting the app forgets every grant, which is the right way
# round for something that switches approvals off.
granted = {}


class WebSink(agent.Sink):
    """Turns the agent's progress into server-sent events for one request."""

    def __init__(self, channel, session_id=""):
        self.channel = channel
        self.session_id = session_id
        self.stopped = False
        # The browser has gone but the turn carries on (see chat()).
        self.detached = False
        # Ended by an approval nobody answered before the backstop.
        self.expired = False
        self.active_response = None
        self.errors = []

    def emit(self, event, **fields):
        self.channel.put({"type": event, **fields})

    def token(self, text):
        self.emit("token", text=text)

    def reasoning(self, text):
        self.emit("reasoning", text=text)

    def building(self, size, budget=0):
        self.emit("building", bytes=size, budget=round(budget, 3))

    def tool_start(self, name, args):
        self.emit("tool_start", name=name, args=args)

    def tool_result(self, name, result):
        self.emit("tool_result", name=name, result=result)

    def error(self, message):
        self.errors.append(message)
        self.emit("error", message=message)

    def stats(self, seconds, tokens, steps):
        self.emit("stats", seconds=seconds, tokens=tokens, steps=steps)

    def cancelled(self):
        return self.stopped

    def confirm(self, question, detail="", kind=""):
        """
        Send the preview to the UI, then wait for the user to click Allow or
        Deny.

        It waits until it is answered. It used to give up after five minutes,
        which sounds generous until you watch a real turn: generation runs at
        a couple of tokens a second, a turn that writes
        three files takes the better part of an hour, and the person who asked
        for it has quite reasonably gone to do something else. Coming back to
        three refusals — of work the model had already done — is the worst
        possible outcome, because refusing is meant to be the *safe* default,
        not the likely one.

        Nor does it matter whether the page that asked is still there. A phone
        locks within a minute of being put down, and a turn that gave up the
        moment it had nobody to ask threw away everything it had done to get
        that far. The question waits here instead, and whichever page opens
        the chat next is shown it (see waiting_approvals()). The backstop is
        only there so a wait cannot outlive the day it started in.
        """
        if kind and kind in granted.get(self.session_id, ()):
            return True

        approval_id = uuid.uuid4().hex[:12]
        detail = detail.strip("\n")
        record = {"event": threading.Event(), "allowed": False, "always": False,
                  "sink": self, "question": question, "kind": kind,
                  "detail": detail}
        with approvals_lock:
            approvals[approval_id] = record

        self.emit("approval", id=approval_id, question=question, kind=kind,
                  detail=detail)

        # Waited in slices rather than one long block, so that stopping the
        # turn stops the asking too.
        deadline = time.monotonic() + APPROVAL_BACKSTOP
        answered = False
        while not answered and time.monotonic() < deadline and not self.stopped:
            answered = record["event"].wait(timeout=1.0)

        with approvals_lock:
            approvals.pop(approval_id, None)
        if not answered:
            if not self.stopped:
                # Nobody answered all day. Refusing and carrying on would have
                # the model tell you that you declined something you never
                # saw, so the turn ends here, and says why in the transcript.
                self.emit("approval_resolved", id=approval_id, allowed=False,
                          timed_out=True)
                self.stopped = self.expired = True
            return False
        if record["always"] and kind:
            granted.setdefault(self.session_id, set()).add(kind)
        return record["allowed"]


class QuietSink(agent.Sink):
    """
    For scheduled runs: nothing to report to, and every approval refused.

    Errors are kept, though. Thrown away, a run whose model server was down
    recorded "(no answer)" and gave no hint why.
    """

    def __init__(self):
        self.errors = []

    def error(self, message):
        self.errors.append(message)


def run_automation(entry):
    """
    Execute one automation with nobody watching.

    Write tools are switched off for the run rather than left to ask and be
    refused. A bare "declined" reads to the model as bad luck, so it rewrites
    the file slightly and tries again — burning every step in the budget.
    """
    cfg = settings.load_config()
    cfg["tools"] = {**cfg["tools"], "write_file": False, "edit_file": False,
                    "send_email": False}
    cfg["thinking"] = bool(entry.get("thinking"))
    # Whatever the approval settings say for interactive use, an unattended run
    # asks about everything — and QuietSink refuses. "Never ask" is a promise
    # made to someone sitting at the keyboard, not to a 3am cron tick.
    cfg["approve_files"] = cfg["approve_commands"] = "always"

    history = []
    sink = QuietSink()
    try:
        agent.run(history, entry["prompt"], cfg, sink)
        answer = next((m.get("content") for m in reversed(history)
                       if m.get("role") == "assistant" and m.get("content")), "")
        if not answer and sink.errors:
            answer = "Error: " + " ".join(sink.errors)
    except Exception as exc:                                  # noqa: BLE001
        answer = f"Error: {exc}"

    settings.record_run(entry["id"], answer or "(no answer)")
    # Keep the transcript so results are readable in the sidebar afterwards.
    agent.save_session(f"auto-{entry['id']}-{agent.new_session_id()}", history)
    return answer


def scheduler_loop():
    """Check every 30s for due automations. One at a time, never concurrently."""
    while True:
        time.sleep(30)
        try:
            for entry in settings.load_automations():
                if settings.is_due(entry):
                    run_automation(entry)
        except Exception:                                     # noqa: BLE001
            pass  # a broken automation must not kill the scheduler


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

GET = {}
POST = {}


def route(table, path):
    def register(function):
        table[path] = function
        return function
    return register


@route(GET, "/api/config")
def _get_config(handler):
    return settings.load_config()


@route(POST, "/api/config")
def _post_config(handler):
    return settings.save_config(handler.body())


@route(POST, "/api/config/reset")
def _reset_config(handler):
    return settings.reset_config()


@route(POST, "/api/preset")
def _apply_preset(handler):
    return settings.apply_preset(handler.body().get("id", ""))


@route(GET, "/api/presets")
def _presets(handler):
    return {
        "presets": [{"id": key, "label": value["label"], "blurb": value["blurb"]}
                    for key, value in settings.PRESETS.items()],
        "templates": settings.AUTOMATION_TEMPLATES,
    }


def save_upload(raw_bytes, filename=""):
    """
    Store an uploaded image under ~/.harness/uploads and warm its model data URL.

    Keeping images on disk and storing only their paths in session JSON avoids
    multi-megabyte base64 blobs inside chat transcripts while still letting the
    web UI and iOS app render thumbnails in reopened sessions.
    """
    if not raw_bytes:
        raise ValueError("empty upload")
    if len(raw_bytes) > MAX_UPLOAD:
        raise ValueError(f"image exceeds {MAX_UPLOAD // (1024 * 1024)} MB limit")
    kind = tools._image_kind(raw_bytes[:16])
    if not kind:
        raise ValueError("unsupported image format (use PNG, JPEG, HEIC, GIF, WebP, or TIFF)")
    ext = EXT_FOR_MIME.get(kind, ".bin")
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"{stamp}-{uuid.uuid4().hex[:8]}{ext}"
    os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
    path = os.path.join(settings.UPLOAD_DIR, name)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(raw_bytes)
    if not tools.image_url(path):
        with contextlib.suppress(OSError):
            os.remove(path)
        raise ValueError("image could not be decoded or converted")
    return {"id": name, "path": path, "url": f"/api/uploads/{name}",
            "mime": kind, "bytes": len(raw_bytes)}


def _decode_image_data(text):
    value = str(text or "").strip()
    if "," in value and value.startswith("data:"):
        value = value.split(",", 1)[1]
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"invalid base64 image data: {exc}") from None


def resolve_upload(item):
    """Turn an upload id or inline base64 payload from /api/chat into a disk path."""
    if isinstance(item, str):
        name = item.strip()
        if name.startswith("/api/uploads/"):
            name = name.rsplit("/", 1)[-1]
        if not UPLOAD_NAME_RE.match(name):
            raise ValueError(f"invalid upload id: {item!r}")
        path = os.path.join(settings.UPLOAD_DIR, name)
        if not os.path.isfile(path):
            raise ValueError(f"no such uploaded image: {name}")
        return path
    if isinstance(item, dict):
        if item.get("id") and not item.get("data"):
            return resolve_upload(item["id"])
        raw = _decode_image_data(item.get("data", ""))
        return save_upload(raw, item.get("filename", ""))["path"]
    raise ValueError("image entry must be an upload id or {data, filename}")


def _session_message_for_client(message):
    """Attach safe /api/uploads/<id> URLs for any user-uploaded images."""
    paths = message.get("images")
    if not paths:
        return message
    upload_root = os.path.realpath(settings.UPLOAD_DIR)
    urls = []
    for path in paths:
        real = os.path.realpath(path)
        if real.startswith(upload_root + os.sep) and os.path.isfile(real):
            urls.append(f"/api/uploads/{os.path.basename(real)}")
    return {**message, "image_urls": urls}


@route(GET, "/api/capabilities")
def _capabilities(handler):
    """Single-call capability handshake for web and native iOS clients."""
    props = agent.server_props()
    return {
        "version": 1,
        "mode": access_mode(),
        "tls": TLS_ACTIVE,
        "scheme": "https" if TLS_ACTIVE else "http",
        "model_loaded": alive(MODEL_HEALTH, timeout=1),
        "vision": props["vision"],
        "n_ctx": props["n_ctx"],
        "voice": alive(VOICE_URL + "/health", timeout=0.5),
        "max_upload_bytes": MAX_UPLOAD,
        "max_images_per_message": agent.PICTURES_PER_TURN,
        "tools": list(settings.TOOL_NAMES),
    }


@route(GET, "/api/models")
def _models(handler):
    live = loaded_model()
    chosen = settings.current_model()
    models = settings.list_models()
    for entry in models:
        entry["active"] = entry["path"] == live
        entry["selected"] = entry["path"] == chosen
    name = os.path.basename(live)
    loaded = alive(MODEL_HEALTH)
    props = agent.server_props() if loaded else {"vision": False}
    return {"models": models, "loaded": loaded,
            "vision": bool(props["vision"]),
            # Blind [:-5] ate the last five characters of anything not named
            # .gguf, including the empty string when no model is loaded.
            "live": name[:-5] if name.endswith(".gguf") else name}


@route(POST, "/api/model")
def _set_model(handler):
    if not settings.set_model(handler.body().get("path", "")):
        return handler.send(404, {"error": "no such model"})
    # Swapping models means restarting llama-server: the weights are loaded at
    # startup and there is no reload command.
    threading.Thread(target=restart_model, daemon=True).start()
    return {"ok": True}


@route(GET, "/api/system")
def _system(handler):
    return system_state()


@route(GET, "/api/access")
def _access(handler):
    # In remote mode the friendly MagicDNS name works and needs no token, so
    # show that rather than an IP with a key glued on. Under TLS, the full
    # *.ts.net name matches the Tailscale certificate so iOS ATS accepts it.
    scheme = "https" if TLS_ACTIVE else "http"
    fqdn = tailnet_fqdn() if SERVE_HOST else ""
    short_host = fqdn if TLS_ACTIVE else (fqdn.split(".")[0] if fqdn else "")
    return {"mode": access_mode(),
            "tls": TLS_ACTIVE,
            "scheme": scheme,
            "fqdn": fqdn,
            "url": phone_url() if REQUIRE_TOKEN else "",
            "short": f"{scheme}://{short_host}:{PORT}" if short_host else "",
            "tailnet": tailnet_address()}


@route(GET, "/api/logs")
def _logs(handler):
    name = "model.log" if "model" in handler.path else "app.log"
    try:
        with open(os.path.join(settings.BASE, name)) as log:
            text = "".join(log.readlines()[-200:])
    except OSError:
        text = "(no log yet)"
    return {"name": name, "text": text}


@route(GET, "/api/folders")
def _folders(handler):
    # parse_qs has already decoded, so "%7E/Documents" arrives as "~/Documents"
    # and expanduser can do its job — decoding by hand afterwards would mangle
    # any folder with a literal % in the name.
    base = os.path.expanduser(handler.query.get("path", ["~"])[0])
    if not os.path.isdir(base):
        base = os.path.expanduser("~")
    try:
        folders = sorted(e for e in os.listdir(base) if not e.startswith(".")
                         and os.path.isdir(os.path.join(base, e)))
    except OSError:
        folders = []
    return {"path": base, "parent": os.path.dirname(base.rstrip("/")) or "/",
            "folders": folders[:200]}


@route(GET, "/api/sessions")
def _sessions(handler):
    return [{"id": s["id"], "title": s.get("title", ""),
             "updated": s.get("updated", ""),
             "turns": sum(1 for m in s.get("messages", [])
                          if m.get("role") == "user")}
            for s in agent.list_sessions()]


@route(POST, "/api/sessions/delete")
def _delete_session(handler):
    session_id = handler.body().get("id", "")
    granted.pop(session_id, None)
    return {"ok": agent.delete_session(session_id)}


@route(POST, "/api/sessions/rewind")
def _rewind_session(handler):
    """Unwind the last exchange. Retry re-sends it; edit puts it back in the box."""
    return {"message": agent.rewind_session(handler.body().get("id", ""))}


@route(GET, "/api/automations")
def _automations(handler):
    return settings.load_automations()


@route(POST, "/api/automations")
def _upsert_automation(handler):
    return settings.upsert_automation(handler.body())


@route(POST, "/api/automations/delete")
def _delete_automation(handler):
    settings.delete_automation(handler.body().get("id", ""))
    return {"ok": True}


@route(POST, "/api/automations/run")
def _run_automation(handler):
    entry = next((a for a in settings.load_automations()
                  if a.get("id") == handler.body().get("id")), None)
    if not entry:
        return handler.send(404, {"error": "no such automation"})
    threading.Thread(target=run_automation, args=(entry,), daemon=True).start()
    return {"ok": True, "started": entry["name"]}


@route(POST, "/api/approve")
def _approve(handler):
    body = handler.body()
    with approvals_lock:
        record = approvals.get(body.get("id"))
    if not record:
        return handler.send(404, {"error": "approval expired"})
    record["allowed"] = bool(body.get("allow"))
    record["always"] = bool(body.get("always"))
    record["event"].set()
    return {"ok": True}


@route(POST, "/api/cancel")
def _cancel(handler):
    """
    Stop one turn — the one whose id was sent, and only that one.

    An unknown id used to fall back to stopping every turn in flight, so a
    stale Stop from a reloaded tab could kill a turn running somewhere else.
    """
    sink = live_turns.get(handler.body().get("turn_id", ""))
    if not sink:
        return {"ok": False, "error": "no such turn"}

    sink.stopped = True
    # Closing the socket to llama-server is what makes it abandon the
    # generation, rather than finishing it into a queue nobody is reading.
    if sink.active_response:
        with contextlib.suppress(Exception):
            sink.active_response.close()
    release_approvals(sink)
    return {"ok": True}


@route(POST, "/api/system/action")
def _system_action(handler):
    body = handler.body()
    action = body.get("action", "")

    if action in ("start-model", "restart-model"):
        threading.Thread(target=restart_model, daemon=True).start()
    elif action == "stop-model":
        shell("pkill", "-x", "llama-server")
    elif action == "rotate-token":
        settings.rotate_token()
        return {"ok": True, "url": phone_url()}
    elif action in ("restart-server", "restart-app"):
        threading.Thread(target=relaunch, args=(access_mode(),), daemon=True).start()
    elif action == "set-mode":
        threading.Thread(target=relaunch, args=(body.get("mode", "local"),),
                         daemon=True).start()
    elif action == "shutdown":
        def bye():
            # Model first: if this process dies before killing it, the model
            # keeps holding 4 GB with nothing left to manage it.
            time.sleep(0.5)
            shell("pkill", "-x", "llama-server")
            shell("pkill", "-f", OWN_CAFFEINATE)
            os._exit(0)
        threading.Thread(target=bye, daemon=True).start()
    elif action in ("enable-autostart", "disable-autostart"):
        return {"ok": write_autostart(action == "enable-autostart")}
    else:
        return handler.send(400, {"error": "unknown action"})
    return {"ok": True}


def voice_proxy(handler, method, path):
    """
    Relay one request to the voice sidecar, or explain that it is not running.

    Kept deliberately dumb — it forwards bytes and a content type and takes no
    view on what they mean. The sidecar owns the voice API; this only makes it
    reachable from wherever Harness itself is reachable.
    """
    body = handler.body_bytes() if method in ("POST", "DELETE") else None
    request = urllib.request.Request(
        VOICE_URL + path, data=body, method=method,
        headers={"Content-Type": "application/json"} if body else {})
    try:
        with urllib.request.urlopen(request, timeout=VOICE_TIMEOUT) as response:
            payload = response.read()
            kind = response.headers.get("Content-Type", "application/json")
            cached = response.headers.get("X-Harness-Cached", "")
        return handler.send(200, payload, kind, X_Harness_Cached=cached)
    except urllib.error.HTTPError as exc:
        return handler.send(exc.code, exc.read(), "application/json")
    except (urllib.error.URLError, OSError):
        # Not running is a normal state, not an error: the UI hides the voice
        # controls when it sees this rather than showing something broken.
        return handler.send(503, {"error": "voice sidecar is not running",
                                  "hint": "python3 voice/server.py"})


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def host_allowed(host):
    """
    Whether a Host header names this machine the way a person reaches it.

    Any web page can make the browser send requests to 127.0.0.1. With DNS
    rebinding it can read the answers too: point evil.example at 127.0.0.1
    and the page is same-origin with Harness, settings, access token and all.
    What it cannot change is the Host header, which still says evil.example.
    So a request has to name an IP address, localhost, a bare machine name,
    a Bonjour .local name or a Tailscale .ts.net one — the ways Harness is
    actually reached — and nothing else.
    """
    if host is None:
        return True              # no browser leaves it off; curl and tests may
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
    """
    True if a request that changes something came from another site's page.

    Nothing stopped one. Harness on 127.0.0.1 asks for no token, so a page on
    any website you visited could post {"approve_commands": "never"} to the
    settings and then start a chat — a form submit needs no permission from
    anyone. Browsers say where a request came from, and that is checked here.
    """
    origin = headers.get("Origin")
    if origin is None:
        return (headers.get("Sec-Fetch-Site") or "").lower() == "cross-site"
    if origin == "null":
        return True
    return (urllib.parse.urlparse(origin).netloc.lower()
            != (headers.get("Host") or "").strip().lower())


class Server(ThreadingHTTPServer):
    """
    The HTTP server, without a traceback for every phone that went to sleep.

    A phone drops its connections the moment the screen locks, and each one
    printed twenty lines of socketserver internals to app.log — 168 of them,
    and nothing else, which buried any error that did matter.
    """

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # the browser's own console is enough; keep the terminal quiet

    # ---- request helpers ----

    def body(self):
        """
        The JSON body of the request being handled, parsed once.

        The socket can only be read through once, so calling this twice in a
        handler left the second call with nothing — the request then threw, no
        response was ever sent, and the browser waited forever. Hence the cache;
        see do_POST for why it has to be cleared per request.
        """
        if getattr(self, "_body", None) is None:
            # Via body_bytes so the socket is read exactly once however the
            # body is wanted — parsed here, or raw by the voice proxy.
            raw = self.body_bytes()
            try:
                self._body = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                self._body = {}
        return self._body

    def body_bytes(self):
        """The raw request body, for relaying somewhere else unparsed."""
        if getattr(self, "_raw", None) is None:
            try:
                length = max(0, int(self.headers.get("Content-Length", 0)))
            except (TypeError, ValueError):
                length = 0
            length = min(length, MAX_BODY)
            self._raw = self.rfile.read(length) if length else b""
        return self._raw

    def send(self, code, payload, content_type="application/json", **headers):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload)
        data = payload.encode() if isinstance(payload, str) else payload
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        for name, value in headers.items():
            self.send_header(name.replace("_", "-"), value)
        self.end_headers()
        if not getattr(self, "head_only", False):
            self.wfile.write(data)

    def send_file(self, name, content_type, **headers):
        try:
            with open(os.path.join(HERE, name), "rb") as handle:
                data = handle.read()
        except OSError:
            return self.send(404, {"error": f"{name} is missing"})
        self.send(200, data, content_type, **headers)

    # ---- access ----

    def authorised(self):
        """
        Token check, only when exposed beyond loopback. The token may arrive as
        ?key= (the phone's first visit, from the QR code) or as the cookie that
        first visit sets.
        """
        if not REQUIRE_TOKEN:
            return True

        # Loopback is always trusted, even in --lan mode. Reaching the socket
        # from 127.0.0.1 already means local access to this machine, so the
        # token would protect nothing — it would only lock the owner out of
        # their own browser.
        client = self.client_address[0]
        if client in ("127.0.0.1", "::1"):
            return True

        # In --remote mode the socket exists only on the Tailscale address, so
        # anything reaching it was already admitted to the tailnet by Tailscale
        # itself — device keys plus account auth, strictly stronger than a token
        # pasted in a URL. Requiring the token as well only breaks things: the
        # cookie is per-hostname, so reaching the Mac by IP and by MagicDNS name
        # would each need their own sign-in.
        if SERVE_HOST and client.startswith("100."):
            octet = client.split(".")[1]
            if octet.isdigit() and 64 <= int(octet) <= 127:
                return True

        token = settings.access_token()
        auth = (self.headers.get("Authorization") or "").strip()
        bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        return (bearer == token
                or f"key={token}" in (self.path or "")
                or f"nbkey={token}" in (self.headers.get("Cookie") or ""))

    def trusted(self, changes=False):
        """Refuse a request from a page that is not Harness's own. True if fine."""
        if not host_allowed(self.headers.get("Host")):
            self.send(403, {"error": "Harness does not answer to that host name."})
            return False
        if changes and cross_site(self.headers):
            self.send(403, {"error": "Refused: this request came from another website."})
            return False
        return True

    def deny(self):
        # Cache-Control matters: without it the browser caches the refusal and
        # keeps showing it after access is granted, which looks exactly like
        # the fix not working. API routes get JSON so native clients can decode it.
        if (self.path or "").startswith("/api/"):
            return self.send(401, {"error": "Unauthorised. Provide a valid token."},
                             Cache_Control="no-store, must-revalidate")
        self.send(401, b"Unauthorised. Open the link from the QR code shown in "
                       b"the terminal.", "text/plain",
                  Cache_Control="no-store, must-revalidate")

    # ---- dispatch ----

    def handle_route(self, table):
        if not self.authorised():
            return self.deny()
        parsed = urllib.parse.urlparse(self.path or "/")
        self.query = urllib.parse.parse_qs(parsed.query)

        handler = table.get(parsed.path)
        if not handler:
            return self.send(404, {"error": "not found"})
        result = handler(self)
        if result is not None:
            self.send(200, result)

    def do_POST(self):
        # One handler object serves every request on a keep-alive connection, so
        # last request's parsed body is still on `self` — clear it or this
        # request reads that one and leaves its own bytes in the socket.
        self._body = None
        self._raw = None
        # Drain the raw body even for routes that ignore it, so keep-alive stays
        # aligned. Raw image uploads are not JSON, so only JSON routes parse it.
        raw = self.body_bytes()
        if not self.trusted(changes=True):
            return
        path = self.path.split("?")[0]
        if path == "/api/uploads":
            if not self.authorised():
                return self.deny()
            return self.upload(raw)
        if path == "/api/chat":
            if not self.authorised():
                return self.deny()
            return self.chat()
        if path.startswith("/api/voice/"):
            if not self.authorised():
                return self.deny()
            return voice_proxy(self, "POST", path[len("/api/voice"):])
        self.handle_route(POST)

    def upload(self, raw):
        ctype = (self.headers.get("Content-Type") or "").lower()
        try:
            if ctype.startswith("image/"):
                info = save_upload(raw)
            else:
                body = self.body()
                info = save_upload(_decode_image_data(body.get("data", "")),
                                   body.get("filename", ""))
        except ValueError as exc:
            return self.send(400, {"error": str(exc)})
        return self.send(200, info)

    def do_DELETE(self):
        self._raw = None
        self.body_bytes()
        if not self.trusted(changes=True):
            return
        path = (self.path or "/").split("?")[0]
        if path.startswith("/api/voice/"):
            if not self.authorised():
                return self.deny()
            return voice_proxy(self, "DELETE", path[len("/api/voice"):])
        self.send(404, {"error": "not found"})

    def do_GET(self):
        if not self.trusted():
            return
        path = (self.path or "/").split("?")[0]

        if path == "/":
            if not self.authorised():
                return self.deny()
            # No cache headers meant browsers cached the page heuristically, so
            # editing ui.html and reloading could still show the old UI. The
            # icons below are still cached hard — those really don't change.
            headers = {"Cache_Control": "no-cache"}
            # Persist the key so later requests from the phone carry it without
            # the token sitting in every URL.
            if REQUIRE_TOKEN and "key=" in self.path:
                headers["Set_Cookie"] = (f"nbkey={settings.access_token()}; "
                                         "Path=/; Max-Age=31536000; SameSite=Lax; "
                                         "HttpOnly")
            return self.send_file("ui.html", "text/html; charset=utf-8", **headers)

        # Icons. Served from disk rather than inlined so they can be edited
        # without touching the HTML, and cached hard because they never change.
        if path in ("/icon.svg", "/icon-180.png", "/favicon.ico"):
            svg = path.endswith((".svg", ".ico"))
            return self.send_file("icon.svg" if svg else "icon-180.png",
                                  "image/svg+xml" if svg else "image/png",
                                  Cache_Control="public, max-age=604800")

        if path.startswith("/api/voice/"):
            if not self.authorised():
                return self.deny()
            return voice_proxy(self, "GET", path[len("/api/voice"):])

        if path.startswith("/api/uploads/"):
            if not self.authorised():
                return self.deny()
            name = path.rsplit("/", 1)[-1]
            if not UPLOAD_NAME_RE.match(name):
                return self.send(404, {"error": "no such upload"})
            full = os.path.join(settings.UPLOAD_DIR, name)
            if not os.path.isfile(full):
                return self.send(404, {"error": "no such upload"})
            with open(full, "rb") as handle:
                data = handle.read()
            kind = tools._image_kind(data[:16]) or "application/octet-stream"
            return self.send(200, data, kind, Cache_Control="private, max-age=86400")

        if path.startswith("/api/session/"):
            if not self.authorised():
                return self.deny()
            session_id = path.rsplit("/", 1)[-1]
            _, messages = agent.load_session(session_id)
            shown = [_session_message_for_client(m) for m in messages]
            return self.send(200, {"id": session_id, "messages": shown,
                                   "running": session_running(session_id),
                                   "approvals": waiting_approvals(session_id)})

        if path.startswith("/api/export/"):
            if not self.authorised():
                return self.deny()
            session_id = path.rsplit("/", 1)[-1]
            try:
                text = agent.as_markdown(session_id)
            except ValueError:
                # A malformed id raised out of the handler, and the browser
                # waited on a request that was never going to be answered.
                text = ""
            if not text:
                return self.send(404, {"error": "no such session"})
            return self.send(
                200, text.encode(), "text/markdown; charset=utf-8",
                Content_Disposition=f'attachment; filename="{session_id}.md"')

        self.handle_route(GET)

    def do_HEAD(self):
        """
        Same headers as GET, no body — uptime checks and `curl -I` expect it,
        and answering 501 made the app look down.
        """
        self.head_only = True
        try:
            self.do_GET()
        finally:
            self.head_only = False

    # ---- chat, which streams rather than returning ----

    def chat(self):
        body = self.body()
        message = (body.get("message") or "").strip()
        raw_images = body.get("images") or []
        if not isinstance(raw_images, list):
            return self.send(400, {"error": "images must be a list"})
        if len(raw_images) > agent.PICTURES_PER_TURN:
            return self.send(400, {"error": f"at most {agent.PICTURES_PER_TURN} images per message"})
        if not message and not raw_images:
            return self.send(400, {"error": "empty message"})

        image_paths = []
        if raw_images:
            if not agent.server_props()["vision"]:
                return self.send(400, {"error": "The loaded model does not support images. "
                                                "Switch to a vision model in Settings."})
            try:
                image_paths = [resolve_upload(item) for item in raw_images]
            except ValueError as exc:
                return self.send(400, {"error": str(exc)})
            if not message:
                message = "Look at the attached image."

        session_id = body.get("session_id") or agent.new_session_id()
        turn_id = body.get("turn_id") or uuid.uuid4().hex[:12]
        # One turn per chat at a time. A turn left running by a page that went
        # away saves the whole chat when it finishes, which would silently
        # overwrite whatever a second turn had added in the meantime.
        if body.get("session_id") and session_running(session_id):
            if waiting_approvals(session_id):
                return self.send(409, {"error": "The last message in this chat is "
                                                "waiting for your approval — answer "
                                                "it above first."})
            return self.send(409, {"error": "Still working on the last message "
                                            "in this chat — wait for it to finish."})
        history = agent.load_session(session_id)[1] if body.get("session_id") else []

        cfg = settings.load_config()
        cfg["thinking"] = bool(body.get("thinking"))

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        channel = queue.Queue()
        sink = WebSink(channel, session_id)
        live_turns[turn_id] = sink
        # Saved with the question before any work starts, so reopening the chat
        # mid-turn shows what was asked rather than the chat as it was before.
        # The full exchange overwrites this when the turn ends.
        initial_user = {"role": "user", "content": message}
        if image_paths:
            initial_user["images"] = list(image_paths)
        agent.save_session(session_id, history + [initial_user])
        # First, so a brand-new chat knows its id even if the connection drops
        # before the end — the id used to arrive only with "done", and a phone
        # that lost the stream had no way back to the answer.
        channel.put({"type": "session", "session_id": session_id})

        def worker():
            try:
                if image_paths:
                    agent.run(history, message, cfg, sink, images=image_paths)
                else:
                    agent.run(history, message, cfg, sink)
            except Exception as exc:                          # noqa: BLE001
                sink.error(str(exc))
            finally:
                if sink.stopped:
                    channel.put({"type": "cancelled"})
                    if sink.expired:
                        hours = round(APPROVAL_BACKSTOP / 3600)
                        history.append({"role": "assistant",
                                        "content": EXPIRED_STOP.format(hours=hours)})
                elif sink.errors and not (history and history[-1].get("role") == "assistant"
                                          and history[-1].get("content")):
                    history.append({"role": "assistant",
                                    "content": ERROR_STOP.format(error=sink.errors[-1])})
                agent.save_session(session_id, history)
                live_turns.pop(turn_id, None)
                channel.put({"type": "done", "session_id": session_id})

        threading.Thread(target=worker, daemon=True).start()

        while True:
            # A turn can legitimately go minutes without producing an event —
            # a slow tool, or an approval nobody has answered yet. Rather than
            # block on the queue forever, wake up periodically and write a
            # comment down the pipe. If the browser has gone, that write is
            # what tells us, and it is the only thing that can.
            try:
                event = channel.get(timeout=HEARTBEAT)
                payload = f"data: {json.dumps(event)}\n\n"
            except queue.Empty:
                event, payload = None, ": ping\n\n"

            try:
                self.wfile.write(payload.encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                # The page went away. On a phone that is usually nothing more
                # than the screen locking or a switch to another app — iOS
                # drops the connection within seconds — and stopping the turn
                # threw away a minute of work every time it happened. So the
                # turn carries on, and saves when it finishes; the page polls
                # for that and shows the answer when it lands. An approval
                # waits too, for whichever page opens the chat next — ending
                # the turn there instead is what made you send the whole
                # request again. Stop still means stop — that is /api/cancel.
                sink.detached = True
                return

            if event and event["type"] == "done":
                return


# --------------------------------------------------------------------------
# Startup
# --------------------------------------------------------------------------

def print_qr(url):
    """Terminal QR so the phone can just scan it. Optional dependency."""
    try:
        import qrcode
    except ImportError:
        print("  (install qrcode for a scannable code: harness deps)")
        return
    code = qrcode.QRCode(border=1)
    code.add_data(url)
    code.make(fit=True)
    code.print_ascii(invert=True)


REMOTE_HELP = """Tailscale isn't running, so there is no private address to serve on.

  1. brew install --cask tailscale-app
  2. Open Tailscale and sign in
  3. Install Tailscale on your phone and sign in to the same account

Then run this again. Use --lan instead if you only need it on your home Wi-Fi."""


def make_tailnet_server(addr, port, server_factory=Server):
    """
    Create the tailnet listener, wrapping it with TLS if valid certs are present.

    Loopback (127.0.0.1) stays plain HTTP so local tools and scripts keep
    working; the tailnet socket automatically upgrades to HTTPS when a
    `tailscale cert` pair exists in ~/.harness/certs (or --tls/--cert/--key
    were passed), and falls back cleanly to HTTP otherwise.
    """
    global TLS_ACTIVE
    tailnet_server = server_factory((addr, port), Handler)
    cert, key = find_tls_certs(fqdn=tailnet_fqdn())
    if not cert and TLS_WANTED:
        cert, key = provision_tailscale_cert()
    ctx = make_ssl_context(cert, key)
    if ctx is not None:
        tailnet_server.socket = ctx.wrap_socket(tailnet_server.socket, server_side=True)
        TLS_ACTIVE = True
    else:
        TLS_ACTIVE = False
    return tailnet_server


def _attach_tailnet(port, active=None, server_factory=Server):
    """
    Bind a listener on the current Tailscale address if it appeared or changed.

    Starting in --remote mode while Tailscale is stopped used to exit, which
    also killed Harness on restart whenever the VPN was down. Keeping loopback
    up and attaching the tailnet listener here means remote access starts the
    moment Tailscale reconnects.
    """
    global SERVE_HOST
    addr = tailnet_address()
    if not addr:
        return active
    if active and active[0] == addr:
        return active
    try:
        tailnet_server = make_tailnet_server(addr, port, server_factory=server_factory)
    except OSError:
        return active
    if active:
        with contextlib.suppress(Exception):
            active[1].shutdown()
            active[1].server_close()
    SERVE_HOST = addr
    scheme = "https" if TLS_ACTIVE else "http"
    print(f"           also on {scheme}://{addr}:{port}", flush=True)
    threading.Thread(target=tailnet_server.serve_forever, daemon=True).start()
    return (addr, tailnet_server)


def watch_tailnet(port, active=None, interval=TAILNET_POLL, stop=None,
                  server_factory=Server):
    """Poll for a Tailscale address and attach a listener when one appears."""
    while REMOTE and not (stop and stop.is_set()):
        active = _attach_tailnet(port, active, server_factory=server_factory)
        if stop:
            stop.wait(interval)
        else:
            time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(description="Harness web app.")
    parser.add_argument("--port", type=int, default=8420)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--lan", action="store_true",
                        help="Serve on the local Wi-Fi network. Requires the "
                             "access token.")
    parser.add_argument("--remote", action="store_true",
                        help="Serve over Tailscale only — reachable from "
                             "anywhere, including cellular, but exposed to "
                             "nothing except your own devices.")
    parser.add_argument("--tls", action="store_true",
                        help="Serve HTTPS on the Tailscale listener using "
                             "tailscale cert (falls back to HTTP if unavailable).")
    parser.add_argument("--cert", default="", help="Path to a PEM TLS certificate.")
    parser.add_argument("--key", default="", help="Path to a PEM TLS private key.")
    parser.add_argument("--rotate-token", action="store_true",
                        help="Invalidate the current token and print a new one.")
    args = parser.parse_args()

    if args.rotate_token:
        settings.rotate_token()
        print("New access token issued. Old phone links no longer work.")

    global REQUIRE_TOKEN, REMOTE, PORT, SERVE_HOST, TLS_WANTED, TLS_CERT, TLS_KEY, TLS_ACTIVE
    REMOTE = args.remote
    REQUIRE_TOKEN = args.lan or args.remote
    PORT = args.port
    TLS_WANTED = bool(args.tls)
    TLS_CERT = args.cert or ""
    TLS_KEY = args.key or ""
    TLS_ACTIVE = False

    if args.remote:
        SERVE_HOST = tailnet_address()

    # This exposes file writing and shell execution, so the socket must not
    # exist anywhere it doesn't have to. Loopback is always served: binding the
    # tailnet address alone made the app unreachable from the Mac running it,
    # and switching modes could strand you with no way back in. Remote mode
    # therefore adds a second listener rather than widening the first.
    primary = "0.0.0.0" if args.lan else "127.0.0.1"
    try:
        servers = [Server((primary, PORT), Handler)]
        if args.remote and SERVE_HOST and primary != "0.0.0.0" and SERVE_HOST != primary:
            servers.append(make_tailnet_server(SERVE_HOST, PORT))
    except OSError as exc:
        if exc.errno != 48:
            raise
        # A stray copy from a previous run is the usual cause, and the raw
        # traceback buries that under nine frames of socketserver internals.
        # `from None`: the socket traceback is noise next to the one line
        # that actually helps.
        raise SystemExit(
            f"Port {PORT} is already in use.\n"
            f"  Another copy is probably still running:  harness stop\n"
            f"  Or pick a different port:  harness --port {PORT + 1}") from None

    print(f"Harness  ->  http://127.0.0.1:{PORT}")
    if len(servers) > 1:
        scheme = "https" if TLS_ACTIVE else "http"
        print(f"           also on {scheme}://{SERVE_HOST}:{PORT}")

    if REQUIRE_TOKEN:
        print()
        if args.remote and SERVE_HOST:
            print(f"  Private address: {SERVE_HOST}")
            print("  Reachable from any network your phone is on — as long as")
            print("  Tailscale is running on both. Not exposed to the internet.")
        elif args.remote:
            print("  Tailscale isn't running yet — serving on 127.0.0.1 and")
            print("  watching for it. Start Tailscale to connect from your phone.")
        else:
            print("  Reachable from your local network.")
            print("  This app can run commands and write files, so the link below")
            print("  is a password. Anyone who has it has that access.")
        print()
        url = phone_url()
        if url:
            print(f"  {url}\n")
            print_qr(url)
        elif not args.remote:
            print("  No network address found — are you connected to Wi-Fi?")
        if url or not args.remote:
            print("  Revoke with:  harness --rotate-token\n")

    print("Ctrl-C to stop.")

    threading.Thread(target=scheduler_loop, daemon=True).start()
    if args.remote:
        active = (SERVE_HOST, servers[1]) if len(servers) > 1 else None
        threading.Thread(target=watch_tailnet, args=(PORT, active), daemon=True).start()
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(
            f"http://127.0.0.1:{PORT}")).start()

    # Every listener but the first runs in a thread; the first blocks here.
    for extra in servers[1:]:
        threading.Thread(target=extra.serve_forever, daemon=True).start()
    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()

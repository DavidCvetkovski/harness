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
import json
import os
import queue
import socket
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

HERE = os.path.dirname(os.path.abspath(__file__))
UI_FILE = os.path.join(HERE, "ui.html")
MODEL_HEALTH = "http://127.0.0.1:8080/health"
AGENT_PLIST = os.path.expanduser("~/Library/LaunchAgents/com.harness.app.plist")

PORT = 8420
# How to spot the app in the process table. It runs as `harness.server` when
# started in the background and as `harness.cli serve` in the foreground under
# launchd, and stop/status/the System panel have to find either.
APP_PROCESS = r"harness\.(server|cli serve)"

# Set by main() when --lan is used. While loopback-only there is nothing to
# authenticate: reaching the socket already means local access.
REQUIRE_TOKEN = False
# Set when --remote pins the server to a single Tailscale address.
SERVE_HOST = ""


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


def magic_dns_name():
    """The tailnet hostname, e.g. davids-macbook-air — nicer than an IP."""
    try:
        name = json.loads(shell("/usr/local/bin/tailscale", "status", "--json"))
        return name["Self"]["DNSName"].rstrip(".").split(".")[0]
    except (json.JSONDecodeError, KeyError, TypeError):
        return ""


def phone_url():
    host = SERVE_HOST or lan_address()
    return f"http://{host}:{PORT}/?key={settings.access_token()}" if host else ""


def access_mode():
    return "remote" if SERVE_HOST else "lan" if REQUIRE_TOKEN else "local"


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
        try:
            os.remove(AGENT_PLIST)
        except OSError:
            pass
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
    argv = [sys.executable, "-m", "harness.server", "--port", str(PORT),
            "--no-browser"]
    if mode in ("remote", "lan"):
        argv.append("--" + mode)
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
        self.active_response = None

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
        self.emit("error", message=message)

    def stats(self, seconds, tokens, steps):
        self.emit("stats", seconds=seconds, tokens=tokens, steps=steps)

    def cancelled(self):
        return self.stopped

    def confirm(self, question, detail="", kind=""):
        """
        Send the preview to the UI, then block until the user clicks Allow or
        Deny. Times out into a refusal — a browser tab closed mid-approval must
        not leave the request hanging forever, and refusing is the safe default.
        """
        if kind and kind in granted.get(self.session_id, ()):
            return True

        approval_id = uuid.uuid4().hex[:12]
        record = {"event": threading.Event(), "allowed": False, "always": False,
                  "sink": self}
        with approvals_lock:
            approvals[approval_id] = record

        self.emit("approval", id=approval_id, question=question, kind=kind,
                  detail=detail.strip("\n"))
        answered = record["event"].wait(timeout=300)

        with approvals_lock:
            approvals.pop(approval_id, None)
        if not answered:
            self.emit("approval_resolved", id=approval_id, allowed=False,
                      timed_out=True)
            return False
        if record["always"] and kind:
            granted.setdefault(self.session_id, set()).add(kind)
        return record["allowed"]


class QuietSink(agent.Sink):
    """For scheduled runs: nothing to report to, and every approval refused."""


def run_automation(entry):
    """
    Execute one automation with nobody watching.

    Write tools are switched off for the run rather than left to ask and be
    refused. A bare "declined" reads to the model as bad luck, so it rewrites
    the file slightly and tries again — burning every step in the budget.
    """
    cfg = settings.load_config()
    cfg["tools"] = {**cfg["tools"], "write_file": False, "edit_file": False}
    cfg["thinking"] = bool(entry.get("thinking"))
    # Whatever the approval settings say for interactive use, an unattended run
    # asks about everything — and QuietSink refuses. "Never ask" is a promise
    # made to someone sitting at the keyboard, not to a 3am cron tick.
    cfg["approve_files"] = cfg["approve_commands"] = "always"

    history = []
    try:
        agent.run(history, entry["prompt"], cfg, QuietSink())
        answer = next((m.get("content") for m in reversed(history)
                       if m.get("role") == "assistant" and m.get("content")), "")
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


@route(GET, "/api/models")
def _models(handler):
    live = loaded_model()
    chosen = settings.current_model()
    models = settings.list_models()
    for entry in models:
        entry["active"] = entry["path"] == live
        entry["selected"] = entry["path"] == chosen
    name = os.path.basename(live)
    return {"models": models, "loaded": alive(MODEL_HEALTH),
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
    # show that rather than an IP with a key glued on.
    short = magic_dns_name() if SERVE_HOST else ""
    return {"mode": access_mode(),
            "url": phone_url() if REQUIRE_TOKEN else "",
            "short": f"http://{short}:{PORT}" if short else "",
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
        try:
            sink.active_response.close()
        except Exception:                                     # noqa: BLE001
            pass
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
            shell("pkill", "-f", "caffeinate -i")
            os._exit(0)
        threading.Thread(target=bye, daemon=True).start()
    elif action in ("enable-autostart", "disable-autostart"):
        return {"ok": write_autostart(action == "enable-autostart")}
    else:
        return handler.send(400, {"error": "unknown action"})
    return {"ok": True}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

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
            length = int(self.headers.get("Content-Length", 0))
            try:
                self._body = json.loads(self.rfile.read(length)) if length else {}
            except json.JSONDecodeError:
                self._body = {}
        return self._body

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
        return (f"key={token}" in (self.path or "")
                or f"nbkey={token}" in (self.headers.get("Cookie") or ""))

    def deny(self):
        # Cache-Control matters: without it the browser caches the refusal and
        # keeps showing it after access is granted, which looks exactly like
        # the fix not working.
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
        # And read it here even for routes that ignore it. Left unread it
        # becomes the start of the next request on the connection, surfacing
        # later as a bewildering 501 whose "method" is a fragment of JSON.
        self.body()
        if self.path.split("?")[0] == "/api/chat":
            if not self.authorised():
                return self.deny()
            return self.chat()
        self.handle_route(POST)

    def do_GET(self):
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
                                         "Path=/; Max-Age=31536000; SameSite=Lax")
            return self.send_file("ui.html", "text/html; charset=utf-8", **headers)

        # Icons. Served from disk rather than inlined so they can be edited
        # without touching the HTML, and cached hard because they never change.
        if path in ("/icon.svg", "/icon-180.png", "/favicon.ico"):
            svg = path.endswith((".svg", ".ico"))
            return self.send_file("icon.svg" if svg else "icon-180.png",
                                  "image/svg+xml" if svg else "image/png",
                                  Cache_Control="public, max-age=604800")

        if path.startswith("/api/session/"):
            if not self.authorised():
                return self.deny()
            session_id = path.rsplit("/", 1)[-1]
            _, messages = agent.load_session(session_id)
            return self.send(200, {"id": session_id, "messages": messages})

        if path.startswith("/api/export/"):
            if not self.authorised():
                return self.deny()
            session_id = path.rsplit("/", 1)[-1]
            text = agent.as_markdown(session_id)
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
        if not message:
            return self.send(400, {"error": "empty message"})

        session_id = body.get("session_id") or agent.new_session_id()
        turn_id = body.get("turn_id") or uuid.uuid4().hex[:12]
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

        def worker():
            try:
                agent.run(history, message, cfg, sink)
            except Exception as exc:                          # noqa: BLE001
                channel.put({"type": "error", "message": str(exc)})
            finally:
                if sink.stopped:
                    channel.put({"type": "cancelled"})
                agent.save_session(session_id, history)
                live_turns.pop(turn_id, None)
                channel.put({"type": "done", "session_id": session_id})

        threading.Thread(target=worker, daemon=True).start()

        while True:
            event = channel.get()
            try:
                self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                # Tab closed. Stop the turn and release anything it is blocked
                # on, so the thread can finish instead of waiting forever.
                sink.stopped = True
                release_approvals(sink)
                return
            if event["type"] == "done":
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
    parser.add_argument("--rotate-token", action="store_true",
                        help="Invalidate the current token and print a new one.")
    args = parser.parse_args()

    if args.rotate_token:
        settings.rotate_token()
        print("New access token issued. Old phone links no longer work.")

    global REQUIRE_TOKEN, PORT, SERVE_HOST
    REQUIRE_TOKEN = args.lan or args.remote
    PORT = args.port

    if args.remote:
        SERVE_HOST = tailnet_address()
        if not SERVE_HOST:
            raise SystemExit(REMOTE_HELP)

    # This exposes file writing and shell execution, so the socket must not
    # exist anywhere it doesn't have to. Loopback is always served: binding the
    # tailnet address alone made the app unreachable from the Mac running it,
    # and switching modes could strand you with no way back in. Remote mode
    # therefore adds a second listener rather than widening the first.
    binds = ["0.0.0.0"] if args.lan else ["127.0.0.1"]
    if args.remote and SERVE_HOST and "0.0.0.0" not in binds and SERVE_HOST not in binds:
        binds.append(SERVE_HOST)

    try:
        servers = [ThreadingHTTPServer((address, PORT), Handler)
                   for address in binds]
    except OSError as exc:
        if exc.errno != 48:
            raise
        # A stray copy from a previous run is the usual cause, and the raw
        # traceback buries that under nine frames of socketserver internals.
        raise SystemExit(f"Port {PORT} is already in use.\n"
                         f"  Another copy is probably still running:  harness stop\n"
                         f"  Or pick a different port:  harness --port {PORT + 1}")

    print(f"Harness  ->  http://127.0.0.1:{PORT}")
    if len(binds) > 1:
        print(f"           also on {binds[1]}:{PORT}")

    if REQUIRE_TOKEN:
        print()
        if args.remote:
            print(f"  Private address: {SERVE_HOST}")
            print("  Reachable from any network your phone is on — as long as")
            print("  Tailscale is running on both. Not exposed to the internet.")
        else:
            print("  Reachable from your local network.")
            print("  This app can run commands and write files, so the link below")
            print("  is a password. Anyone who has it has that access.")
        print()
        url = phone_url()
        if url:
            print(f"  {url}\n")
            print_qr(url)
        else:
            print("  No network address found — are you connected to Wi-Fi?")
        print("  Revoke with:  harness --rotate-token\n")

    print("Ctrl-C to stop.")

    threading.Thread(target=scheduler_loop, daemon=True).start()
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

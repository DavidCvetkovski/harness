"""
harness.cli — one command for the whole thing.

    harness              start the model and the web app, open the browser
    harness --remote     same, but reachable from your phone anywhere
    harness stop         stop both
    harness status       what's running, how much memory
    harness chat         terminal chat instead of the web app
    harness model        just the model server, in the foreground
    harness serve        model + app in the foreground — what launchd runs
"""

import argparse
import contextlib
import os
import signal
import subprocess
import sys
import time

from . import config as settings
from .server import APP_PROCESS, MODEL_HEALTH, alive, pids, rss_gb, shell

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_SH = os.path.join(HERE, "model.sh")
APP_LOG = os.path.join(settings.BASE, "app.log")
MODEL_LOG = os.path.join(settings.BASE, "model.log")


def app_url(port):
    return f"http://127.0.0.1:{port}/"


def spawn(command, log_path):
    """
    Start something in the background, its output going to a log.

    caffeinate -i holds off idle sleep for as long as it runs. Without it the
    Mac sleeps, the server stops answering, and a phone on the other side of
    the world just sees a dead page with no explanation.
    """
    os.makedirs(settings.BASE, exist_ok=True)
    with open(log_path, "w") as log:
        subprocess.Popen(["caffeinate", "-i"] + command, stdout=log, stderr=log,
                         start_new_session=True, cwd=os.path.dirname(HERE))


def start_model():
    if alive(MODEL_HEALTH):
        return True
    spawn([MODEL_SH], MODEL_LOG)

    print("starting model", end="", flush=True)
    for _ in range(120):
        if alive(MODEL_HEALTH):
            print(" ok")
            return True
        print(".", end="", flush=True)
        time.sleep(1)
    print(" failed — see " + MODEL_LOG)
    return False


def serving_port(pid, port):
    """True if this harness.server process was started on `port`."""
    command = shell("ps", "-o", "command=", "-p", str(pid))
    return f"--port {port}" in command


def start_app(args):
    # Match the port too. APP_PROCESS alone matches a copy serving *any* port,
    # so a forgotten `harness --port 8499` made this return without ever
    # binding 8420 — and Harness.app then waited 90s for a server that was
    # never going to start.
    if alive(app_url(args.port)):
        print(f"app already running on :{args.port}")
        return
    other = [p for p in pids(APP_PROCESS) if not serving_port(p, args.port)]
    if pids(APP_PROCESS) and not other:
        print(f"app already running on :{args.port}")
        return
    if other:
        print(f"note: another copy is running on a different port "
              f"(pid {other[0]}); starting this one on :{args.port}")

    command = [sys.executable, "-m", "harness.server", "--port", str(args.port)]
    for flag in ("remote", "lan", "no_browser"):
        if getattr(args, flag):
            command.append("--" + flag.replace("_", "-"))
    spawn(command, APP_LOG)

    for _ in range(30):
        time.sleep(1)
        # Only our port counts — a copy on another port would end the wait
        # immediately and leave the caller thinking this one had come up.
        if alive(app_url(args.port)):
            break

    # The app prints the phone URL and QR code on startup; show what it said.
    time.sleep(1)
    try:
        with open(APP_LOG) as log:
            print(log.read().rstrip())
    except OSError:
        pass


def pids_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def do_stop():
    plist_path = os.path.expanduser("~/Library/LaunchAgents/com.harness.app.plist")
    if os.path.exists(plist_path):
        shell("launchctl", "unload", plist_path)

    stopped = []
    target_pids = []
    for name, pattern, exact in (('app', APP_PROCESS, False),
                                 ("model", "llama-server", True)):
        for pid in pids(pattern, exact):
            try:
                os.kill(pid, signal.SIGTERM)
                stopped.append(f"{name} ({pid})")
                target_pids.append(pid)
            except OSError:
                pass

    if not stopped:
        print("nothing running")
        return

    deadline = time.time() + 5
    while time.time() < deadline:
        active = [p for p in target_pids if pids_alive(p)]
        if not active:
            break
        time.sleep(0.5)

    for pid in target_pids:
        if pids_alive(pid):
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)

    for pid in pids("caffeinate -i"):
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)

    print("stopped: " + ", ".join(stopped))


def do_status():
    model = pids("llama-server", exact=True)
    app = pids(APP_PROCESS)
    print(f"  model  {f'running  {rss_gb(model[0]):.2f} GB' if model else 'stopped'}")
    print(f"  app    {'running' if app else 'stopped'}")
    if app:
        try:
            with open(APP_LOG) as log:
                for line in log:
                    if "http://" in line and "127.0.0.1" not in line:
                        print(f"  url    {line.strip()}")
                        break
        except OSError:
            pass
    for line in shell("memory_pressure").splitlines():
        if "free percentage" in line:
            print(f"  memory {line.split(':')[-1].strip()} free")


def main():
    # These two hand off to a parser of their own. Let argparse near them and it
    # eats --help — and anything else it happens to recognise — before the
    # delegate ever sees it, so `harness model --help` printed this help instead.
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "model":
        os.execv(MODEL_SH, [MODEL_SH] + sys.argv[2:])
    if command == "chat":
        from . import agent
        sys.argv = ["harness chat"] + sys.argv[2:]
        return agent.main()

    parser = argparse.ArgumentParser(
        prog="harness", description="Local AI assistant with tools.")
    parser.add_argument("command", nargs="?", default="start",
                        choices=["start", "stop", "status", "restart",
                                 "chat", "model", "serve", "deps"])
    parser.add_argument("--port", type=int, default=8420)
    parser.add_argument("--remote", action="store_true",
                        help="Reachable from your phone anywhere, over Tailscale.")
    parser.add_argument("--lan", action="store_true",
                        help="Reachable on your local Wi-Fi.")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--rotate-token", action="store_true",
                        help="Invalidate the current phone link and issue a new one.")
    args = parser.parse_args()

    if args.rotate_token:
        settings.rotate_token()
        print("New access token issued. Old phone links no longer work.")

    if args.command == "stop":
        do_stop()
    elif args.command == "status":
        do_status()
    elif args.command == "serve":
        # Foreground, and it stays that way. `start` spawns a detached child and
        # exits, which launchd's KeepAlive reads as a crash — so it relaunched
        # every few seconds forever, each run printing "app already running".
        #
        # `start` got its caffeinate from spawn(); here we hold the assertion
        # ourselves. -w makes it exit with us, so no stray process outlives the
        # server and pins the Mac awake.
        subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
        start_model()
        from . import server
        sys.argv = ["harness"] + [f"--{f.replace('_', '-')}"
                                  for f in ("remote", "lan", "no_browser")
                                  if getattr(args, f)]
        sys.argv += ["--port", str(args.port)]
        server.main()
    elif args.command == "deps":
        subprocess.run([sys.executable, "-m", "pip", "install", "-q",
                        "ddgs", "qrcode"])
        print("dependencies installed")
    else:
        if args.command == "restart":
            do_stop()
        if start_model():
            start_app(args)


if __name__ == "__main__":
    main()

# Harness

A local AI assistant that can actually do things — read and write files, run
commands, search the web — reachable from a browser on this Mac or from a phone
anywhere in the world. The model runs entirely on this machine; nothing you type
is sent to anyone.

## Start it

```bash
bin/harness              # this Mac only
bin/harness --lan        # also on your local Wi-Fi
bin/harness --remote     # from anywhere, over Tailscale
```

Then open <http://127.0.0.1:8420>. Turn on auto-start in the System tab and you
never need to run this again.

Other commands: `stop`, `status`, `restart`, `chat` (terminal instead of the
browser), `model` (model server only), `deps` (install Python dependencies).

## Layout

```
bin/harness          entry point — sets PYTHONPATH and runs harness.cli
harness/
  cli.py             start / stop / status
  server.py          HTTP routes, streaming, approvals, admin actions
  agent.py           the loop: stream, call a tool, feed the result back
  tools.py           the seven tools, and the guards around them
  config.py          settings, presets, automations, access token
  ui.html            the whole front end, one file
  model.sh           llama.cpp launcher
```

There is one agent loop, not one per front end. Where its output goes is a
`Sink` — the terminal prints, the web server queues events for the browser, a
scheduled run throws away — so `harness chat` and the browser run the same code.

State lives in `~/.harness/` — sessions, settings, automations, access token,
logs. Models live in `~/llm/models/`.

## What it can do

Seven tools: list files, read, write, edit, search the web, fetch a page, run a
shell command. Anything that changes a file or runs a command that is not
plainly read-only asks for approval first, showing exactly what it will do.

Some paths are refused outright regardless of approval — `/System`, `/usr`,
`/etc`, `/Library`, and friends.

Reading `~/.ssh`, `~/.gnupg`, `~/.aws` or `~/.harness` always asks, whatever
the approval settings say, and whether it is `read_file` or a `cat` that gets
there. What a tool returns is kept in the session file in plain text, so a key
read once is a key written to disk.

When it thinks, the thinking is shown as it arrives and stays in the
transcript afterwards, collapsed to a line you can open again.

Automations run prompts on a schedule. The write tools are switched off for
those runs rather than left to ask and be refused, because nobody is watching to
approve anything.

## Access modes

| Mode | Reachable from | Token needed |
|---|---|---|
| default | this Mac | no |
| `--lan` | your Wi-Fi network | yes |
| `--remote` | anywhere, via Tailscale | no — Tailscale already authenticated the device |

`--remote` binds only the Tailscale address, so the socket does not exist on
your local network or the public internet.

## Requirements

- macOS on Apple silicon
- llama.cpp built with Metal at `~/llm/llama.cpp`
- A GGUF model in `~/llm/models/`
- Python 3.9+
- Optional: `ddgs` for web search, `qrcode` for the phone QR (`bin/harness deps`)
- Optional: Tailscale, for `--remote`

## Notes

Sessions are stored in plain text and include whatever the tools returned —
file contents, command output, fetched pages. Treat `~/.harness/sessions/` the
way you would treat a shell history file.

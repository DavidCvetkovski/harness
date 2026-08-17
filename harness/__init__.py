"""Harness — a local AI assistant with tools. See README.md."""

import glob
import os
import sys

# ddgs (web search) and qrcode (the phone QR) are installed in the llama.cpp
# venv rather than in the system Python this runs under. Done here so it holds
# however the package was entered — bin/harness, `python -m harness.server`,
# or an import from a test.
for _site in glob.glob(os.path.expanduser("~/llm/.venv/lib/python*/site-packages")):
    if _site not in sys.path:
        sys.path.append(_site)

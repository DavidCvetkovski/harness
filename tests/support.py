"""
Shared scaffolding.

The one rule these tests follow above all others: never touch ~/.harness or
~/llm. Every test that reads or writes state redirects the module-level paths
in harness.config at setUp and puts them back afterwards, so running the suite
on the machine Harness is installed on cannot disturb a real session, a real
automation, or the real access token.
"""

import os
import shutil
import tempfile
import unittest

from harness import config as settings


class Recorder:
    """
    A Sink that records what it was asked and answers however it was told to.

    The real Sink protocol is small — the tools only ever call confirm() and
    cancelled() — so this is the whole of it.
    """

    def __init__(self, allow=False, cancelled=False):
        self.allow = allow
        self.asked = []
        self._cancelled = cancelled

    def confirm(self, question, detail="", kind=""):
        self.asked.append({"question": question, "detail": detail, "kind": kind})
        return self.allow

    def cancelled(self):
        return self._cancelled

    @property
    def questions(self):
        return [a["question"] for a in self.asked]


class TempStateTest(unittest.TestCase):
    """Base class that gives each test its own ~/.harness and project folder."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="harness-test-")
        self.project = os.path.join(self.tmp, "project")
        os.makedirs(self.project)

        self._saved = {name: getattr(settings, name) for name in
                       ("BASE", "CONFIG_FILE", "AUTOMATIONS_FILE", "TOKEN_FILE",
                        "MODEL_CHOICE_FILE", "SESSION_DIR", "MODELS_DIR")}
        state = os.path.join(self.tmp, "state")
        settings.BASE = state
        settings.CONFIG_FILE = os.path.join(state, "config.json")
        settings.AUTOMATIONS_FILE = os.path.join(state, "automations.json")
        settings.TOKEN_FILE = os.path.join(state, "access-token")
        settings.MODEL_CHOICE_FILE = os.path.join(state, "model")
        settings.SESSION_DIR = os.path.join(state, "sessions")
        settings.MODELS_DIR = os.path.join(self.tmp, "models")
        os.makedirs(settings.SESSION_DIR)
        os.makedirs(settings.MODELS_DIR)

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(settings, name, value)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, text):
        """Create a file inside the throwaway project folder."""
        path = os.path.join(self.project, name)
        with open(path, "w") as handle:
            handle.write(text)
        return path

    def cfg(self, **overrides):
        """A config dict with the safe defaults, plus whatever the test needs."""
        base = {
            "project_dir": self.project,
            "approve_files": "always",
            "approve_commands": "always",
            "extra_safe_commands": [],
            "tools": dict.fromkeys(settings.TOOL_NAMES, True),
            "max_tokens": 1200,
            "max_steps": 8,
        }
        base.update(overrides)
        return base

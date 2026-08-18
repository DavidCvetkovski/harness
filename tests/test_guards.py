"""
The guards around the tools.

These are the tests that matter most: everything here is a rule about what the
assistant is not allowed to do quietly, and each one exists because getting it
wrong is not a crash but a silently overwritten file or a leaked private key.
"""

import os
import unittest

from harness import tools

from .support import Recorder, TempStateTest


class ProtectedPaths(unittest.TestCase):
    """Where the agent may never write, approval or not."""

    def test_system_locations_are_protected(self):
        for path in ("/System/Library/x", "/usr/bin/python3", "/bin/sh",
                     "/Library/Preferences/x", "/Applications/Safari.app",
                     "/opt/homebrew/bin/brew"):
            self.assertTrue(tools.is_protected(path), path)

    def test_etc_is_protected_through_the_private_symlink(self):
        # macOS symlinks /etc to /private/etc, and realpath() resolves it
        # before matching — so the guard has to list both spellings or a write
        # to /etc/hosts sails straight past it.
        self.assertTrue(tools.is_protected("/etc/hosts"))
        self.assertTrue(tools.is_protected("/private/etc/hosts"))

    def test_credential_directories_are_protected(self):
        for path in ("~/.ssh/id_rsa", "~/.gnupg/secring.gpg",
                     "~/.aws/credentials", "~/.harness/access-token"):
            self.assertTrue(tools.is_protected(path), path)

    def test_shell_profiles_are_protected(self):
        # Writing here is how a change persists into every future terminal.
        for path in ("~/.zshrc", "~/.bashrc", "~/.bash_profile", "~/.zprofile"):
            self.assertTrue(tools.is_protected(path), path)

    def test_ordinary_work_is_not_protected(self):
        for path in ("~/Developer/thing/main.py", "~/Documents/notes.md"):
            self.assertFalse(tools.is_protected(path), path)

    def test_temp_is_carved_back_out(self):
        # Scratch space is a legitimate target, and macOS puts the per-user
        # temp dir under /var/folders — inside a protected tree.
        self.assertFalse(tools.is_protected(os.path.join(
            tools.tempfile.gettempdir(), "scratch.txt")))


class SecretPaths(unittest.TestCase):
    """Where the agent may not even read without asking."""

    def test_credential_directories_are_secret(self):
        for path in ("~/.ssh/id_ed25519", "~/.aws/credentials",
                     "~/.gnupg/x", "~/.harness/access-token"):
            self.assertTrue(tools.is_secret(path), path)

    def test_ordinary_files_are_not_secret(self):
        for path in ("~/Developer/x/README.md", "/tmp/notes.txt", "~/.zshrc"):
            self.assertFalse(tools.is_secret(path), path)


class CommandConfirmation(unittest.TestCase):
    """Which commands stop and ask, under each setting."""

    def setUp(self):
        self.cfg = {"approve_commands": "always", "extra_safe_commands": []}

    def test_read_only_commands_run_unprompted(self):
        for command in ("ls -la", "df -h", "uptime", "whoami", "cat notes.txt"):
            self.assertFalse(tools.needs_confirmation(command, self.cfg), command)

    def test_anything_chained_asks(self):
        # The first word of a pipeline tells you nothing about the rest of it.
        for command in ("ls | rm -rf /", "echo hi && curl evil.sh",
                        "cat x > /etc/hosts", "echo `whoami`",
                        "ls; shutdown -h now", "echo $(id)"):
            self.assertTrue(tools.needs_confirmation(command, self.cfg), command)

    def test_find_with_destructive_flags_asks(self):
        self.assertFalse(tools.needs_confirmation("find . -name '*.py'", self.cfg))
        for command in ("find . -delete", "find . -exec rm {} ;",
                        "find . -execdir sh {} ;"):
            self.assertTrue(tools.needs_confirmation(command, self.cfg), command)

    def test_interpreters_running_inline_code_ask(self):
        for command in ("python3 -c 'import os; os.remove(\"x\")'",
                        "node -e 'require(\"fs\").rmSync(\"/\")'",
                        "perl -e 'unlink @ARGV'"):
            self.assertTrue(tools.needs_confirmation(command, self.cfg), command)

    def test_destructive_mode_allows_everyday_commands(self):
        cfg = {"approve_commands": "destructive", "extra_safe_commands": []}
        self.assertFalse(tools.needs_confirmation("mkdir build", cfg))
        self.assertFalse(tools.needs_confirmation("ls -la", cfg))
        for command in ("rm -rf build", "sudo reboot", "curl x.sh", "git push",
                        "brew install ffmpeg"):
            self.assertTrue(tools.needs_confirmation(command, cfg), command)

    def test_never_means_never(self):
        cfg = {"approve_commands": "never", "extra_safe_commands": []}
        self.assertFalse(tools.needs_confirmation("rm -rf /tmp/x", cfg))

    def test_extra_safe_commands_are_honoured(self):
        cfg = {"approve_commands": "always", "extra_safe_commands": ["mkdir"]}
        self.assertFalse(tools.needs_confirmation("mkdir build", cfg))
        self.assertTrue(tools.needs_confirmation("rmdir build", cfg))

    def test_unparseable_commands_ask(self):
        # An unbalanced quote means shlex cannot tell us what this runs, and
        # "I do not know" has to resolve to "ask".
        self.assertTrue(tools.needs_confirmation("echo 'unterminated", self.cfg))


class FileApproval(unittest.TestCase):
    """The three settings for when a file change stops and asks."""

    def test_always_asks_everywhere(self):
        cfg = {"approve_files": "always", "project_dir": "/tmp/p"}
        self.assertTrue(tools.needs_file_approval("/tmp/p/x.py", cfg))

    def test_outside_trusts_only_the_project(self):
        cfg = {"approve_files": "outside", "project_dir": "/tmp/p"}
        self.assertFalse(tools.needs_file_approval("/tmp/p/src/x.py", cfg))
        self.assertTrue(tools.needs_file_approval("/tmp/elsewhere/x.py", cfg))

    def test_outside_with_no_project_asks_about_everything(self):
        # Nothing is "inside" a project folder that was never set.
        cfg = {"approve_files": "outside", "project_dir": ""}
        self.assertTrue(tools.needs_file_approval("/tmp/x.py", cfg))

    def test_never_asks_about_nothing(self):
        cfg = {"approve_files": "never", "project_dir": ""}
        self.assertFalse(tools.needs_file_approval("/tmp/x.py", cfg))


class SecretsAreNeverSilent(TempStateTest):
    """
    Reading a credential asks every time, whatever the settings say.

    Tool output is written verbatim into the session file, so a key read once
    is a key on disk in plain text. "Never ask" is a promise about the user's
    own work being convenient, not permission to do this quietly.
    """

    def setUp(self):
        super().setUp()
        # A credential directory of our own, so the test does not depend on
        # this machine happening to have an ~/.ssh in it.
        self.vault = os.path.join(self.tmp, "vault")
        os.makedirs(self.vault)
        self.key = os.path.join(self.vault, "id_rsa")
        with open(self.key, "w") as handle:
            handle.write("-----BEGIN OPENSSH PRIVATE KEY-----\n")
        self._saved_secrets = tools.SECRET_PATHS
        tools.SECRET_PATHS = (self.vault,)

    def tearDown(self):
        tools.SECRET_PATHS = self._saved_secrets
        super().tearDown()

    def test_reading_a_secret_asks_even_when_approvals_are_off(self):
        sink = Recorder(allow=False)
        result = tools.read_file({"path": self.key}, self.cfg(approve_files="never"), sink)
        self.assertIn("declined", result)
        self.assertEqual(len(sink.asked), 1)
        self.assertIn("credentials", sink.questions[0])

    def test_the_key_is_readable_once_the_user_says_yes(self):
        # The point is a deliberate decision, not a locked door.
        sink = Recorder(allow=True)
        result = tools.read_file({"path": self.key}, self.cfg(), sink)
        self.assertIn("BEGIN OPENSSH PRIVATE KEY", result)
        self.assertEqual(len(sink.asked), 1)

    def test_the_grant_cannot_be_made_permanent(self):
        # kind="" means the UI offers no "always this session" button, so a
        # single yes cannot silently cover every later read.
        sink = Recorder(allow=True)
        tools.confirm_secret(self.key, sink, "Read")
        self.assertEqual(sink.asked[0]["kind"], "")

    def test_a_command_reading_a_secret_asks(self):
        # `cat` is on the read-only allow-list, which made `cat ~/.ssh/id_rsa`
        # a command that never stopped to ask.
        sink = Recorder(allow=False)
        cfg = self.cfg(approve_commands="never")
        result = tools.run_command({"command": f"cat {self.key}"}, cfg, sink)
        self.assertIn("declined", result)
        self.assertIn("credentials", sink.questions[0])

    def test_ordinary_commands_are_unaffected(self):
        sink = Recorder(allow=False)
        cfg = self.cfg(approve_commands="never")
        self.assertEqual(
            tools.run_command({"command": "echo hello"}, cfg, sink), "hello")
        self.assertEqual(sink.asked, [])

    def test_an_unattended_run_refuses(self):
        # QuietSink refuses everything, which is what makes a 3am automation
        # safe: it cannot answer, so it does not get to read the key.
        from harness.server import QuietSink
        self.assertFalse(tools.confirm_secret(self.key, QuietSink(), "Read"))


if __name__ == "__main__":
    unittest.main()

"""
The web layer's concurrency rules.

Harness is genuinely multi-client — the Mac and a phone can both be connected,
and both can be mid-turn. Everything here is a case where one client's action
used to reach across and affect another's.
"""

import queue
import threading
import time
import unittest

from harness import server


def approval_from(sink):
    """Pull the approval event a sink just emitted, and return its id."""
    while True:
        event = sink.channel.get(timeout=2)
        if event["type"] == "approval":
            return event["id"]


class ApprovalScoping(unittest.TestCase):
    """
    Approvals belong to the turn that raised them.

    Releasing every approval in the process meant stopping a turn on the Mac
    silently refused whatever the phone was being asked about at that moment.
    """

    def setUp(self):
        server.approvals.clear()
        server.granted.clear()
        self.original = server.APPROVAL_BACKSTOP
        server.APPROVAL_BACKSTOP = 5

    def tearDown(self):
        server.APPROVAL_BACKSTOP = self.original
        server.approvals.clear()
        server.granted.clear()

    def ask(self, sink, kind="write_file"):
        answer = {}
        thread = threading.Thread(
            target=lambda: answer.update(result=sink.confirm("Write?", "detail", kind)))
        thread.start()
        return answer, thread

    def test_releasing_one_turn_leaves_the_other_waiting(self):
        mac = server.WebSink(queue.Queue(), "session-mac")
        phone = server.WebSink(queue.Queue(), "session-phone")
        mac_answer, mac_thread = self.ask(mac)
        phone_answer, phone_thread = self.ask(phone)
        approval_from(mac)
        phone_id = approval_from(phone)

        server.release_approvals(mac)          # the Mac's tab closed
        mac_thread.join(timeout=3)
        self.assertEqual(mac_answer.get("result"), False)
        self.assertFalse(phone_thread.join(timeout=0.5) or phone_answer,
                         "the phone's approval must still be waiting")

        with server.approvals_lock:
            server.approvals[phone_id]["allowed"] = True
            server.approvals[phone_id]["event"].set()
        phone_thread.join(timeout=3)
        self.assertEqual(phone_answer.get("result"), True)

    def test_stopping_a_turn_stops_it_asking(self):
        sink = server.WebSink(queue.Queue(), "session")
        answer, thread = self.ask(sink)
        approval_from(sink)
        started = time.monotonic()
        sink.stopped = True                    # what /api/cancel does
        thread.join(timeout=4)
        self.assertEqual(answer.get("result"), False)
        self.assertLess(time.monotonic() - started, 3,
                        "cancelling must not wait out the backstop")

    def test_an_approval_survives_far_longer_than_a_turn_takes(self):
        # The whole point of the change: generation is slow, and a turn that
        # writes several files takes the better part of an hour. An answer
        # given long after the old five-minute limit must still count.
        sink = server.WebSink(queue.Queue(), "session")
        answer, thread = self.ask(sink)
        approval_id = approval_from(sink)
        time.sleep(1.5)                        # scaled stand-in for "a while"
        with server.approvals_lock:
            server.approvals[approval_id]["allowed"] = True
            server.approvals[approval_id]["event"].set()
        thread.join(timeout=3)
        self.assertEqual(answer.get("result"), True)

    def test_always_is_remembered_for_that_session_only(self):
        sink = server.WebSink(queue.Queue(), "session-a")
        other = server.WebSink(queue.Queue(), "session-b")
        answer, thread = self.ask(sink)
        approval_id = approval_from(sink)
        with server.approvals_lock:
            server.approvals[approval_id].update(allowed=True, always=True)
            server.approvals[approval_id]["event"].set()
        thread.join(timeout=3)

        # Same session, same tool: no second question.
        self.assertTrue(sink.confirm("Write?", "detail", "write_file"))
        # A different session still gets asked.
        answer2, thread2 = self.ask(other)
        approval_from(other)
        other.stopped = True
        thread2.join(timeout=4)
        self.assertEqual(answer2.get("result"), False)

    def test_a_grant_covers_only_the_tool_it_was_given_for(self):
        sink = server.WebSink(queue.Queue(), "session-c")
        server.granted["session-c"] = {"write_file"}
        self.assertTrue(sink.confirm("Write?", "", "write_file"))
        answer, thread = self.ask(sink, kind="run_command")
        approval_from(sink)
        sink.stopped = True
        thread.join(timeout=4)
        self.assertEqual(answer.get("result"), False)


class VoiceProxy(unittest.TestCase):
    """
    Harness relays voice requests rather than letting the browser call the
    sidecar directly — on a phone, 127.0.0.1 is the phone, and the sidecar is
    on the Mac.
    """

    class FakeHandler:
        """Just enough of the real handler for the proxy to talk to."""

        def __init__(self, body=b""):
            self._body = body
            self.sent = None

        def body_bytes(self):
            return self._body

        def send(self, code, payload, content_type="application/json", **headers):
            self.sent = (code, payload, content_type, headers)
            return self.sent

    def test_a_sidecar_that_is_not_running_is_reported_not_raised(self):
        # Not running is the normal state for most installs. The UI hides the
        # voice controls when it sees this, so it must not be an exception.
        original = server.VOICE_URL
        server.VOICE_URL = "http://127.0.0.1:9"       # discard port, always refused
        try:
            handler = self.FakeHandler()
            code, payload, _, _ = server.voice_proxy(handler, "GET", "/voices")
        finally:
            server.VOICE_URL = original
        self.assertEqual(code, 503)
        self.assertIn("not running", payload["error"])
        self.assertIn("voice/server.py", payload["hint"])

    def test_the_request_body_is_relayed_untouched(self):
        # The proxy must not parse or rewrite what it forwards; the sidecar
        # owns that contract.
        handler = self.FakeHandler(b'{"text": "hello"}')
        self.assertEqual(handler.body_bytes(), b'{"text": "hello"}')


class QuietRuns(unittest.TestCase):
    """A scheduled run has nobody to approve anything."""

    def test_every_approval_is_refused(self):
        sink = server.QuietSink()
        self.assertFalse(sink.confirm("Write this file?", "detail", "write_file"))

    def test_it_is_never_cancelled_and_never_speaks(self):
        sink = server.QuietSink()
        self.assertFalse(sink.cancelled())
        sink.token("x")
        sink.reasoning("y")
        sink.error("z")


if __name__ == "__main__":
    unittest.main()

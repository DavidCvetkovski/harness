"""
The web layer's concurrency rules.

Harness is genuinely multi-client — the Mac and a phone can both be connected,
and both can be mid-turn. Everything here is a case where one client's action
used to reach across and affect another's.
"""

import json
import queue
import socket
import threading
import time
import unittest
import urllib.error
import urllib.request

from harness import server

from .support import TempStateTest


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


class PageGoesAway(unittest.TestCase):
    """
    A phone locking mid-answer used to throw the answer away.

    iOS drops the connection within seconds of the screen locking, and the
    server read that as "tab closed" and stopped the turn — so a reply that
    had already spent a minute reading the conversation came back empty. Then
    it ended the turn at the first approval instead, which was the same loss
    one step later: the question has to wait for the phone to come back.
    """

    setUp = ApprovalScoping.setUp
    tearDown = ApprovalScoping.tearDown
    ask = ApprovalScoping.ask

    def answer(self, approval_id, allow):
        with server.approvals_lock:
            server.approvals[approval_id]["allowed"] = allow
            server.approvals[approval_id]["event"].set()

    def test_a_detached_turn_is_not_cancelled(self):
        sink = server.WebSink(queue.Queue(), "session")
        sink.detached = True
        self.assertFalse(sink.cancelled())

    def test_a_detached_turn_still_asks_and_waits(self):
        sink = server.WebSink(queue.Queue(), "session")
        sink.detached = True
        answer, thread = self.ask(sink)
        approval_id = approval_from(sink)
        self.assertFalse(thread.join(timeout=1.2) or answer,
                         "nobody has answered yet, so it must still be waiting")
        self.answer(approval_id, True)          # the phone comes back
        thread.join(timeout=3)
        self.assertEqual(answer.get("result"), True)
        self.assertFalse(sink.stopped)

    def test_an_approval_already_showing_survives_the_page_going(self):
        sink = server.WebSink(queue.Queue(), "session")
        answer, thread = self.ask(sink)
        approval_id = approval_from(sink)
        sink.detached = True                   # what chat() does on a dead write
        time.sleep(1.2)
        self.assertFalse(answer, "the page going is not an answer")
        self.answer(approval_id, True)
        thread.join(timeout=3)
        self.assertEqual(answer.get("result"), True)

    def test_a_waiting_question_is_there_for_the_next_page(self):
        sink = server.WebSink(queue.Queue(), "session-w")
        other = server.WebSink(queue.Queue(), "session-x")
        answer, thread = self.ask(sink)
        approval_id = approval_from(sink)
        _, other_thread = self.ask(other)
        approval_from(other)

        waiting = server.waiting_approvals("session-w")
        self.assertEqual([a["id"] for a in waiting], [approval_id],
                         "only this chat's questions, not another's")
        self.assertEqual(waiting[0]["question"], "Write?")
        self.assertEqual(waiting[0]["kind"], "write_file")
        self.assertEqual(waiting[0]["detail"], "detail")

        self.answer(approval_id, False)
        thread.join(timeout=3)
        self.assertEqual(server.waiting_approvals("session-w"), [])
        other.stopped = True
        other_thread.join(timeout=3)

    def test_nobody_answering_all_day_ends_the_turn_rather_than_refusing(self):
        server.APPROVAL_BACKSTOP = 0.5
        sink = server.WebSink(queue.Queue(), "session")
        sink.detached = True
        self.assertFalse(sink.confirm("Write?", "detail", "write_file"))
        self.assertTrue(sink.stopped)
        self.assertTrue(sink.expired)

    def test_stopping_is_not_mistaken_for_nobody_answering(self):
        sink = server.WebSink(queue.Queue(), "session")
        answer, thread = self.ask(sink)
        approval_from(sink)
        sink.stopped = True
        thread.join(timeout=3)
        self.assertEqual(answer.get("result"), False)
        self.assertFalse(sink.expired)

    def test_a_grant_still_counts_with_nobody_watching(self):
        sink = server.WebSink(queue.Queue(), "session-g")
        server.granted["session-g"] = {"write_file"}
        sink.detached = True
        self.assertTrue(sink.confirm("Write?", "detail", "write_file"))
        self.assertFalse(sink.stopped)


class DisconnectEndToEnd(TempStateTest):
    """Through the real handler: drop the socket mid-turn, get the answer later."""

    def setUp(self):
        super().setUp()
        self.release = threading.Event()
        self.original_run = server.agent.run
        self.needs_approval = False

        def slow_run(history, message, cfg, sink):
            history.append({"role": "user", "content": message})
            sink.token("Thinking it over")
            self.release.wait(timeout=10)      # the phone locks during this
            if self.needs_approval:
                allowed = sink.confirm("Allow this command?", "  RUN  ls", "run_command")
                history.append({"role": "tool", "tool_call_id": "1",
                                "content": "ran it" if allowed else "refused"})
            history.append({"role": "assistant", "content": "The answer."})

        server.agent.run = slow_run
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.port = self.httpd.server_address[1]
        self.original_heartbeat = server.HEARTBEAT
        server.HEARTBEAT = 0.2

    def tearDown(self):
        self.release.set()
        server.agent.run = self.original_run
        server.HEARTBEAT = self.original_heartbeat
        self.httpd.shutdown()
        self.httpd.server_close()
        server.live_turns.clear()
        with server.approvals_lock:
            for record in server.approvals.values():
                record["event"].set()
            server.approvals.clear()
        super().tearDown()

    def start_turn(self):
        body = json.dumps({"message": "what now?", "turn_id": "t1"}).encode()
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        sock.sendall(b"POST /api/chat HTTP/1.1\r\nHost: x\r\n"
                     b"Content-Type: application/json\r\n"
                     + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        received = b""
        while b'"type": "session"' not in received:
            received += sock.recv(4096)
        session_id = json.loads(received.split(b"data: ", 1)[1].split(b"\n\n")[0])["session_id"]
        return sock, session_id

    def session(self, session_id):
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/api/session/{session_id}") as response:
            return json.load(response)

    def test_the_answer_is_saved_after_the_page_has_gone(self):
        sock, session_id = self.start_turn()
        # The question is on disk before the work starts.
        mid = self.session(session_id)
        self.assertTrue(mid["running"])
        self.assertEqual(mid["messages"][-1]["content"], "what now?")

        sock.close()                           # the phone locks
        time.sleep(0.8)                        # several heartbeats: it noticed
        self.release.set()                     # the model finishes anyway

        deadline = time.monotonic() + 5
        while self.session(session_id)["running"] and time.monotonic() < deadline:
            time.sleep(0.1)
        done = self.session(session_id)
        self.assertFalse(done["running"])
        self.assertEqual(done["messages"][-1]["content"], "The answer.")

    def test_an_approval_asked_after_the_page_went_waits_for_it_to_return(self):
        self.needs_approval = True
        sock, session_id = self.start_turn()
        sock.close()                           # the phone locks
        time.sleep(0.8)                        # it noticed
        self.release.set()                     # ...and only then is there a question

        deadline = time.monotonic() + 5
        while not self.session(session_id)["approvals"] and time.monotonic() < deadline:
            time.sleep(0.1)
        waiting = self.session(session_id)
        self.assertTrue(waiting["running"], "the turn must still be alive")
        self.assertEqual(waiting["approvals"][0]["question"], "Allow this command?")

        # A new message is turned away, and told why.
        body = json.dumps({"message": "and?", "session_id": session_id}).encode()
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(urllib.request.Request(
                f"http://127.0.0.1:{self.port}/api/chat", data=body,
                headers={"Content-Type": "application/json"}))
        refusal = json.load(caught.exception)
        caught.exception.close()
        self.assertIn("approval", refusal["error"])

        # The phone comes back and says yes.
        answer = json.dumps({"id": waiting["approvals"][0]["id"], "allow": True}).encode()
        with urllib.request.urlopen(urllib.request.Request(
                f"http://127.0.0.1:{self.port}/api/approve", data=answer,
                headers={"Content-Type": "application/json"})) as response:
            self.assertTrue(json.load(response)["ok"])

        deadline = time.monotonic() + 5
        while self.session(session_id)["running"] and time.monotonic() < deadline:
            time.sleep(0.1)
        done = self.session(session_id)
        self.assertEqual([m["content"] for m in done["messages"][-2:]],
                         ["ran it", "The answer."])
        self.assertEqual(done["approvals"], [])

    def test_a_second_message_waits_for_the_first(self):
        sock, session_id = self.start_turn()
        body = json.dumps({"message": "and?", "session_id": session_id}).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/chat", data=body,
            headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request)
        caught.exception.close()
        self.assertEqual(caught.exception.code, 409)
        sock.close()


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

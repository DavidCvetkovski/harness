"""
The agent loop against a scripted model server.

The loop is where a turn lives or dies, and it had no tests: a stand-in for
llama-server here replays chunks the way the real one streams them, so every
way a step can come back — a call in the thinking, nothing at all, a refusal,
a connection that drops — can be played on purpose rather than waited for.
"""

import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from harness import agent

from .support import TempStateTest


# --------------------------------------------------------------------------
# What the model "says"
# --------------------------------------------------------------------------

def think(text):
    return {"choices": [{"delta": {"reasoning_content": text}, "finish_reason": None}]}


def say(text):
    return {"choices": [{"delta": {"content": text}, "finish_reason": None}]}


def call(name, args, call_id="call_1", raw=None):
    arguments = raw if raw is not None else json.dumps(args)
    return {"choices": [{"delta": {"tool_calls": [{
        "index": 0, "id": call_id,
        "function": {"name": name, "arguments": arguments}}]},
        "finish_reason": None}]}


def end(reason="stop"):
    return {"choices": [{"delta": {}, "finish_reason": reason}]}


def xml(name, **params):
    """A tool call written out as text, the way Qwen writes it."""
    body = "".join(f"<parameter={k}>\n{v}\n</parameter>\n" for k, v in params.items())
    return f"<tool_call>\n<function={name}>\n{body}</function>\n</tool_call>"


class Refuse:
    """The server turns the request down, as llama-server does with a 400."""

    def __init__(self, code, message):
        self.code, self.message = code, message


class Drop:
    """The server sends these chunks and then the connection dies."""

    def __init__(self, *chunks):
        self.chunks = chunks


# --------------------------------------------------------------------------
# The stand-in server
# --------------------------------------------------------------------------

class FakeModel(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        body = json.dumps({"default_generation_settings": {"n_ctx": 32768},
                           "modalities": {"vision": self.server.vision}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.server.requests.append(json.loads(self.rfile.read(length)))
        reply = self.server.script.pop(0) if self.server.script else [say("(script ran out)"), end()]

        if isinstance(reply, Refuse):
            body = json.dumps({"error": {"code": reply.code, "message": reply.message}}).encode()
            self.send_response(reply.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # Chunked, like llama-server: a connection that dies before the final
        # empty chunk is then visibly broken rather than a clean end.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        chunks = reply.chunks if isinstance(reply, Drop) else list(reply)
        for chunk in chunks:
            self.piece(f"data: {json.dumps(chunk)}\n\n")
        if isinstance(reply, Drop):
            self.wfile.flush()
            self.close_connection = True
            return
        self.piece("data: [DONE]\n\n")
        self.wfile.write(b"0\r\n\r\n")

    def piece(self, text):
        data = text.encode()
        self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")


class Watcher(agent.Sink):
    """Records everything a turn reports, and answers approvals as told."""

    def __init__(self, allow=True):
        self.allow = allow
        self.tokens, self.thoughts, self.errors = [], [], []
        self.tools, self.results, self.asked = [], [], []

    def token(self, text):
        self.tokens.append(text)

    def reasoning(self, text):
        self.thoughts.append(text)

    def tool_start(self, name, args):
        self.tools.append((name, args))

    def tool_result(self, name, result):
        self.results.append(result)

    def error(self, message):
        self.errors.append(message)

    def confirm(self, question, detail="", kind=""):
        self.asked.append(question)
        return self.allow

    @property
    def said(self):
        return "".join(self.tokens)


class LoopTest(TempStateTest):

    def setUp(self):
        super().setUp()
        self.model = ThreadingHTTPServer(("127.0.0.1", 0), FakeModel)
        self.model.script, self.model.requests, self.model.vision = [], [], False
        threading.Thread(target=self.model.serve_forever, daemon=True).start()
        port = self.model.server_address[1]
        self._saved_urls = agent.SERVER, agent.PROPS
        agent.SERVER = f"http://127.0.0.1:{port}/v1/chat/completions"
        agent.PROPS = f"http://127.0.0.1:{port}/props"

    def tearDown(self):
        agent.SERVER, agent.PROPS = self._saved_urls
        self.model.shutdown()
        self.model.server_close()
        super().tearDown()

    def script(self, *replies):
        self.model.script.extend(replies)

    def config(self, **overrides):
        return self.cfg(system_prompt="You are a test.", temperature=0.6,
                        top_p=0.95, top_k=20, thinking=True, **overrides)

    def turn(self, message, history=None, sink=None, **overrides):
        history = [] if history is None else history
        sink = sink or Watcher()
        agent.run(history, message, self.config(**overrides), sink)
        return history, sink

    def sent(self, index):
        """The messages of the index'th request the model was sent."""
        return self.model.requests[index]["messages"]


# --------------------------------------------------------------------------
# The stall: a tool call written inside the thinking
# --------------------------------------------------------------------------

class CallInsideTheThinking(LoopTest):
    """
    What the saved chats showed, twelve times: after a tool result the model
    wrote its next call inside its thinking and never closed it, so the server
    returned no answer and no call, and the turn ended with nothing on screen.
    """

    def test_a_call_in_the_thinking_is_run(self):
        self.write("photo.png", "x")
        self.script(
            [think("I should look in the folder.\n\n" + xml("list_files", path=".")), end()],
            [say("There is one picture: photo.png."), end()],
        )
        history, sink = self.turn("what pictures are here?")
        self.assertEqual([name for name, _ in sink.tools], ["list_files"])
        self.assertIn("photo.png", sink.results[0])
        self.assertEqual(history[-1]["content"], "There is one picture: photo.png.")
        self.assertEqual(sink.errors, [])

    def test_the_tool_call_xml_is_not_shown_as_thinking(self):
        self.script(
            [think("Let me list it.\n" + xml("list_files", path=".")), end()],
            [say("Empty."), end()],
        )
        _, sink = self.turn("anything here?")
        thought = "".join(sink.thoughts)
        self.assertIn("Let me list it.", thought)
        self.assertNotIn("<tool", thought)

    def test_a_call_the_model_only_thought_about_is_not_run(self):
        # It considered a tool, then answered instead. That answer stands.
        self.script([think("Maybe " + xml("run_command", command="ls")), say("No need — it is 4."), end()])
        history, sink = self.turn("what is 2+2?")
        self.assertEqual(sink.tools, [])
        self.assertEqual(history[-1]["content"], "No need — it is 4.")

    def test_json_tool_call_and_missing_closing_tag_are_recovered(self):
        self.write("a.txt", "hi")
        self.script(
            [think('Checking.\n<tool_call>{"name": "list_files", "arguments": {"path": "."}}</tool_call>'), end()],
            [think("Reading.\n<tool_call><function=read_file><parameter=path>a.txt</parameter></function>"), end()],
            [say("Saw hi."), end()],
        )
        history, sink = self.turn("read a.txt")
        self.assertEqual([n for n, _ in sink.tools], ["list_files", "read_file"])
        self.assertEqual(history[-1]["content"], "Saw hi.")


class ThinkingGoesBack(LoopTest):
    """
    The cause of the stall: each step's thinking was thrown away, so the
    template showed the model its own earlier steps as empty <think> blocks
    followed by a call — and it copied them. Kept, the steps came back as
    proper calls every time.
    """

    def test_a_steps_thinking_is_sent_with_it_for_the_rest_of_the_turn(self):
        self.script(
            [think("The folder first."), call("list_files", {"path": "."}), end("tool_calls")],
            [think("Now I can answer."), say("It is empty."), end()],
        )
        history, _ = self.turn("what is in the folder?")
        step = next(m for m in self.sent(1) if m.get("tool_calls"))
        self.assertEqual(step["reasoning_content"], "The folder first.")
        self.assertEqual(history[-1]["reasoning_content"], "Now I can answer.")

    def test_an_earlier_exchanges_thinking_is_not_sent_again(self):
        self.script([think("Simple."), say("Hello."), end()],
                    [say("Fine, thanks."), end()])
        history, _ = self.turn("hi")
        self.turn("how are you?", history)
        old = [m for m in self.sent(1) if m["role"] == "assistant"]
        self.assertEqual(old[0]["content"], "Hello.")
        self.assertNotIn("reasoning_content", old[0])
        # Kept in the saved chat all the same, for reading back later.
        self.assertEqual(history[1]["reasoning_content"], "Simple.")


# --------------------------------------------------------------------------
# Nothing back
# --------------------------------------------------------------------------

class EmptyReplies(LoopTest):

    def test_an_empty_step_is_asked_again_without_thinking(self):
        self.script([think("Hmm."), end()],
                    [say("Here is the answer."), end()])
        history, sink = self.turn("tell me something")
        self.assertTrue(self.model.requests[0]["chat_template_kwargs"]["enable_thinking"])
        self.assertFalse(self.model.requests[1]["chat_template_kwargs"]["enable_thinking"])
        self.assertEqual(history[-1]["content"], "Here is the answer.")
        self.assertEqual(sink.errors, [])

    def test_nothing_twice_is_said_out_loud_not_saved_as_an_empty_answer(self):
        self.script([end()], [end()])
        history, sink = self.turn("tell me something")
        self.assertEqual(len(sink.errors), 1)
        self.assertIn("stopped without answering", sink.errors[0])
        self.assertEqual([m["role"] for m in history], ["user"])

    def test_an_empty_step_after_a_tool_is_retried_too(self):
        self.script(
            [call("list_files", {"path": "."}), end("tool_calls")],
            [think("I will look at the folder again"), end()],
            [say("Nothing there."), end()],
        )
        history, sink = self.turn("look around")
        self.assertEqual(history[-1]["content"], "Nothing there.")
        self.assertEqual(sink.errors, [])


# --------------------------------------------------------------------------
# Failures that used to be swallowed
# --------------------------------------------------------------------------

class ServerFailures(LoopTest):

    def test_a_refusal_is_reported_with_the_servers_reason(self):
        self.script(Refuse(400, "the request exceeds the available context size"))
        history, sink = self.turn("hello")
        self.assertEqual(len(sink.errors), 1)
        self.assertIn("refused", sink.errors[0])
        self.assertIn("exceeds the available context size", sink.errors[0])
        self.assertNotIn("Cannot reach", sink.errors[0])
        self.assertEqual([m["role"] for m in history], ["user"])

    def test_an_error_partway_through_is_reported(self):
        self.script([say("Starting"), {"error": {"message": "out of memory"}}])
        history, sink = self.turn("hello")
        self.assertTrue(any("out of memory" in e for e in sink.errors), sink.errors)
        self.assertEqual(history[-1]["content"], "Starting")

    def test_a_server_that_dies_partway_is_reported(self):
        # The connection just ends: no exception, no [DONE], no finish reason.
        self.script(Drop(say("Halfway")))
        history, sink = self.turn("hello")
        self.assertTrue(any("stopped partway" in e for e in sink.errors), sink.errors)
        self.assertEqual(history[-1]["content"], "Halfway")

    def test_a_server_that_is_not_there_is_reported(self):
        agent.SERVER = "http://127.0.0.1:9/v1/chat/completions"
        _, sink = self.turn("hello")
        self.assertIn("Cannot reach the model server", sink.errors[0])

    def test_an_answer_cut_off_at_the_limit_says_so(self):
        self.script([say("The first half of a long"), end("length")])
        history, sink = self.turn("write a lot")
        self.assertIn("cut off", sink.errors[0])
        self.assertEqual(history[-1]["content"], "The first half of a long")

    def test_a_tool_call_cut_off_at_the_limit_is_not_run(self):
        self.script(
            [call("write_file", None, raw='{"path": "a.txt", "content": "half a fi'),
             end("length")],
            [say("I will write it in parts."), end()],
        )
        _, sink = self.turn("write a big file")
        self.assertFalse(os.path.exists(os.path.join(self.project, "a.txt")))
        self.assertIn("length limit", sink.results[0])
        self.assertEqual(sink.asked, [], "nothing to approve: it was never run")


# --------------------------------------------------------------------------
# Calls the model got wrong
# --------------------------------------------------------------------------

class BadCalls(LoopTest):

    def test_arguments_that_are_not_an_object_are_explained_not_run(self):
        self.script([call("list_files", None, raw='["."]'), end("tool_calls")],
                    [say("Sorry."), end()])
        history, sink = self.turn("list it")
        self.assertIn("could not be read", sink.results[0])
        self.assertEqual(history[-1]["content"], "Sorry.")

    def test_a_tool_that_raises_does_not_end_the_turn(self):
        original = agent.tools.DISPATCH["list_files"]
        agent.tools.DISPATCH["list_files"] = lambda args, cfg, sink: 1 / 0
        try:
            self.script([call("list_files", {"path": "."}), end("tool_calls")],
                        [say("That failed."), end()])
            history, sink = self.turn("list it")
        finally:
            agent.tools.DISPATCH["list_files"] = original
        self.assertIn("ZeroDivisionError", sink.results[0])
        self.assertEqual(history[-1]["content"], "That failed.")

    def test_a_path_sent_as_a_number_still_works(self):
        os.makedirs(os.path.join(self.project, "2026"))
        self.write("2026/notes.txt", "x")
        self.script([call("list_files", {"path": 2026}), end("tool_calls")],
                    [say("One file."), end()])
        _, sink = self.turn("what is in 2026?")
        self.assertIn("notes.txt", sink.results[0])


class Repeats(LoopTest):

    def test_looking_again_after_a_change_is_a_fresh_look(self):
        # Run, change, run again: the second run used to be handed the first
        # one's output and told to stop calling tools.
        self.script(
            [call("list_files", {"path": "."}, "a"), end("tool_calls")],
            [call("write_file", {"path": "new.txt", "content": "hi"}, "b"), end("tool_calls")],
            [call("list_files", {"path": "."}, "c"), end("tool_calls")],
            [say("Done."), end()],
        )
        _, sink = self.turn("add a file and show me")
        self.assertNotIn("new.txt", sink.results[0])
        self.assertIn("new.txt", sink.results[2])
        self.assertNotIn("already ran this", sink.results[2])

    def test_the_same_look_twice_in_a_row_is_still_caught(self):
        self.script(
            [call("list_files", {"path": "."}, "a"), end("tool_calls")],
            [call("list_files", {"path": "."}, "b"), end("tool_calls")],
            [say("Nothing new."), end()],
        )
        _, sink = self.turn("look twice")
        self.assertIn("already ran this", sink.results[1])

    def test_a_batch_of_repeats_gives_one_step_to_recover(self):
        self.write("a.txt", "hi")
        self.script(
            [call("list_files", {"path": "."}, "1"), end("tool_calls")],
            [call("list_files", {"path": "."}, "2"), call("list_files", {"path": "."}, "3"),
             end("tool_calls")],
            [call("read_file", {"path": "a.txt"}, "4"), end("tool_calls")],
            [say("Got hi."), end()],
        )
        history, sink = self.turn("read a.txt")
        self.assertIn("read_file", [n for n, _ in sink.tools])
        self.assertEqual(history[-1]["content"], "Got hi.")

    def test_a_declined_command_does_not_clear_the_repeat_cache(self):
        self.script(
            [call("list_files", {"path": "."}, "1"), end("tool_calls")],
            [call("run_command", {"command": "rm x"}, "2"), end("tool_calls")],
            [call("list_files", {"path": "."}, "3"), end("tool_calls")],
            [say("Done."), end()],
        )
        sink = Watcher(allow=False)
        self.turn("check", sink=sink)
        self.assertIn("already ran this", sink.results[2])

    def test_repeated_denials_stop_prompting_the_user(self):
        self.script(
            [call("run_command", {"command": "rm a"}, "1"), end("tool_calls")],
            [call("run_command", {"command": "rm b"}, "2"), end("tool_calls")],
            [call("run_command", {"command": "rm c"}, "3"), end("tool_calls")],
            [say("Okay, stopping."), end()],
        )
        sink = Watcher(allow=False)
        self.turn("delete files", sink=sink)
        self.assertEqual(len(sink.asked), agent.DENIALS_PER_TOOL)
        self.assertIn("will not be asked again", sink.results[2])

    def test_step_budget_warns_two_steps_before_limit(self):
        self.script(
            [call("list_files", {"path": "."}, "1"), end("tool_calls")],
            [call("find_files", {"name": "x", "folder": "."}, "2"), end("tool_calls")],
            [say("Wrapping up."), end()],
        )
        _, sink = self.turn("look around", max_steps=4)
        self.assertIn("2 tool steps remaining", sink.results[1])


# --------------------------------------------------------------------------
# What reaches the screen
# --------------------------------------------------------------------------

class LongTurns(LoopTest):

    def test_the_closing_nudge_keeps_the_question_in_front_of_the_model(self):
        # Two steps' worth of big results outgrow a small window, then the
        # model repeats itself and is asked to answer. It used to be sent the
        # nudge and nothing else.
        self.write("big.txt", "x" * 5000)
        self.script(*[[call("read_file", {"path": "big.txt"}, f"c{n}"), end("tool_calls")]
                      for n in range(3)],
                    [say("It is a file of x."), end()])
        saved = agent.history_budget
        agent.history_budget = lambda cfg, n_ctx: 12000
        try:
            history, _ = self.turn("what is in big.txt?")
        finally:
            agent.history_budget = saved
        last = self.sent(len(self.model.requests) - 1)
        self.assertEqual(last[1]["content"], "what is in big.txt?")
        self.assertIn("Answer my original question", last[-1]["content"])
        self.assertEqual(history[-1]["content"], "It is a file of x.")

    def test_context_refusal_trims_tighter_and_retries(self):
        self.write("a.txt", "a" * 4000)
        self.write("b.txt", "b" * 4000)
        self.script(
            [call("read_file", {"path": "a.txt"}, "1"), end("tool_calls")],
            [call("read_file", {"path": "b.txt"}, "2"), end("tool_calls")],
            Refuse(400, "the request exceeds the available context size"),
            [say("Trimmed and answered."), end()],
        )
        history, sink = self.turn("summarise both")
        self.assertEqual(history[-1]["content"], "Trimmed and answered.")
        self.assertEqual(sink.errors, [])


class Streaming(LoopTest):

    def test_words_before_a_written_out_call_are_shown_once(self):
        call_text = xml("list_files", path=".")
        self.script([say("Let me "), say("look.\n\n" + call_text[:5]),
                     say(call_text[5:]), end()],
                    [say("Empty."), end()])
        _, sink = self.turn("anything here?")
        first_step = sink.said.split("Empty.")[0]
        self.assertEqual(first_step, "Let me look.")

    def test_a_tag_that_never_completes_is_still_shown(self):
        self.script([say("Use a <tool"), say("box."), end()])
        _, sink = self.turn("what do I need?")
        self.assertEqual(sink.said, "Use a <toolbox.")


class SystemPrompt(LoopTest):

    def test_the_model_is_told_where_home_is(self):
        self.script([say("Hi."), end()])
        self.turn("hi")
        system = self.sent(0)[0]["content"]
        self.assertTrue(system.startswith("You are a test."))
        self.assertIn(os.path.expanduser("~"), system)
        self.assertIn(self.project, system)


class Budget(unittest.TestCase):

    def test_the_question_survives_a_long_exchange(self):
        # The exchange alone is over budget. Its oldest steps go, not the
        # question that the whole exchange is about.
        history = [{"role": "user", "content": "find the CV"}]
        for n in range(10):
            history += [{"role": "assistant", "content": "", "tool_calls": [{"id": str(n)}]},
                        {"role": "tool", "tool_call_id": str(n), "content": "x" * 3000}]
        kept = agent.trim_history(history, budget=10000)
        self.assertEqual(kept[0]["content"], "find the CV")
        self.assertEqual(kept[1]["role"], "assistant")
        self.assertEqual(kept[-1], history[-1])
        self.assertLess(len(kept), len(history))

    def test_thinking_counts_against_the_budget(self):
        # Only the thinking makes the first step too big to keep.
        history = [{"role": "user", "content": "q"},
                   {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}],
                    "reasoning_content": "r" * 20000},
                   {"role": "tool", "tool_call_id": "1", "content": "first"},
                   {"role": "assistant", "content": "", "tool_calls": [{"id": "2"}]},
                   {"role": "tool", "tool_call_id": "2", "content": "second"}]
        kept = agent.trim_history(history, budget=10000)
        self.assertEqual([m.get("content") for m in kept], ["q", "", "second"])



def tiny_png(rgb=(200, 30, 30), side=8):
    import struct
    import zlib
    raw = b"".join(b"\x00" + bytes(rgb) * side for _ in range(side))

    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", side, side, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class Pictures(LoopTest):
    """view_image hands the model the picture itself, for the rest of the turn."""

    def picture(self, name="red.png"):
        path = os.path.join(self.project, name)
        with open(path, "wb") as handle:
            handle.write(tiny_png())
        return path

    def tool_names(self, index):
        return [t["function"]["name"] for t in self.model.requests[index]["tools"]]

    def test_the_image_goes_back_with_the_tool_result(self):
        self.model.vision = True
        path = self.picture()
        self.script([call("view_image", {"path": path}), end("tool_calls")],
                    [say("It is red."), end()])
        history, sink = self.turn("what colour is red.png?")
        self.assertIn("view_image", self.tool_names(0))
        result = next(m for m in self.sent(1) if m["role"] == "tool")
        self.assertEqual(result["content"][0]["type"], "text")
        self.assertTrue(result["content"][1]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertEqual(history[-1]["content"], "It is red.")
        saved = next(m for m in history if m["role"] == "tool")
        self.assertEqual(saved["image"], path, "the session keeps the path, not the pixels")
        self.assertIsInstance(saved["content"], str)

    def test_a_later_turn_gets_the_words_without_the_picture(self):
        self.model.vision = True
        path = self.picture()
        self.script([call("view_image", {"path": path}), end("tool_calls")],
                    [say("Red."), end()], [say("You're welcome."), end()])
        history, _ = self.turn("look at red.png")
        self.turn("thanks", history)
        result = next(m for m in self.sent(2) if m["role"] == "tool")
        self.assertIsInstance(result["content"], str)
        self.assertNotIn("image", result)

    def test_a_turn_can_look_at_only_so_many(self):
        self.model.vision = True
        paths = [self.picture(f"p{n}.png") for n in range(agent.PICTURES_PER_TURN + 1)]
        self.script(*[[call("view_image", {"path": p}, f"c{n}"), end("tool_calls")]
                      for n, p in enumerate(paths)],
                    [say("Seen enough."), end()])
        history, sink = self.turn("look at all of them")
        self.assertEqual(sum(1 for m in history if m.get("image")), agent.PICTURES_PER_TURN)
        self.assertIn("as many as one turn can hold", sink.results[-1])

    def test_a_model_without_vision_is_not_offered_it(self):
        self.script([say("Hi."), end()])
        self.turn("hi")
        self.assertNotIn("view_image", self.tool_names(0))

    def test_user_attached_images_go_only_with_the_active_turn(self):
        self.model.vision = True
        path = self.picture("shot.png")
        self.script([say("It is red."), end()], [say("Still red."), end()])
        history = []
        agent.run(history, "what color?", self.config(), Watcher(), images=[path])
        first_user = self.sent(0)[1]
        self.assertEqual(first_user["content"][0]["type"], "text")
        self.assertTrue(first_user["content"][1]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertEqual(history[0]["images"], [path], "session keeps paths, not base64")

        agent.run(history, "thanks", self.config(), Watcher())
        old_user = self.sent(1)[1]
        self.assertIsInstance(old_user["content"], str)
        self.assertNotIn("images", old_user)


if __name__ == "__main__":
    unittest.main()

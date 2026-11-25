"""
The agent loop's own machinery: sessions, history budgeting, and the recovery
paths for models that emit tool calls as plain text.
"""

import json
import unittest

from harness import agent

from .support import TempStateTest


class SessionIds(unittest.TestCase):
    """A session id becomes a filename, so it is checked before it is used."""

    def test_traversal_is_rejected(self):
        for bad in ("../../etc/passwd", "/etc/passwd", "a/b", "..",
                    "x/../../y", "with space", ""):
            with self.assertRaises(ValueError, msg=bad):
                agent.session_path(bad)

    def test_ordinary_ids_are_accepted(self):
        for good in ("20260817-164408", "auto-a1b2c3-20260817-120000", "x_1"):
            self.assertTrue(agent.session_path(good).endswith(good + ".json"))

    def test_a_generated_id_is_acceptable_to_the_checker(self):
        agent.session_path(agent.new_session_id())


class Sessions(TempStateTest):

    def turn(self, question, answer):
        return [{"role": "user", "content": question},
                {"role": "assistant", "content": answer}]

    def test_save_and_load_round_trip(self):
        history = self.turn("what is 2+2", "4")
        agent.save_session("20260101-000000", history)
        loaded_id, messages = agent.load_session("20260101-000000")
        self.assertEqual(loaded_id, "20260101-000000")
        self.assertEqual(messages, history)

    def test_the_title_comes_from_the_first_question(self):
        agent.save_session("20260101-000001", self.turn("how much disk is free", "lots"))
        self.assertEqual(agent.list_sessions()[0]["title"], "how much disk is free")

    def test_an_empty_history_is_not_saved(self):
        agent.save_session("20260101-000002", [])
        self.assertEqual(agent.list_sessions(), [])

    def test_sessions_come_back_newest_first(self):
        for n, stamp in enumerate(["20260101-000010", "20260101-000011"]):
            agent.save_session(stamp, self.turn(f"q{n}", f"a{n}"))
        ids = [s["id"] for s in agent.list_sessions()]
        self.assertEqual(ids, sorted(ids, reverse=True))

    def test_loading_something_that_does_not_exist_is_empty_not_an_error(self):
        self.assertEqual(agent.load_session("20260101-999999"), ("20260101-999999", []))

    def test_latest_resolves_to_the_newest_session(self):
        agent.save_session("20260101-000020", self.turn("old", "a"))
        agent.save_session("20260101-000021", self.turn("new", "b"))
        self.assertEqual(agent.load_session("latest")[0], "20260101-000021")

    def test_delete(self):
        agent.save_session("20260101-000030", self.turn("q", "a"))
        self.assertTrue(agent.delete_session("20260101-000030"))
        self.assertEqual(agent.list_sessions(), [])

    def test_deleting_a_bad_id_returns_false_rather_than_raising(self):
        self.assertFalse(agent.delete_session("../../etc/passwd"))

    def test_rewind_returns_the_question_and_drops_the_exchange(self):
        history = self.turn("first", "one") + self.turn("second", "two")
        agent.save_session("20260101-000040", history)
        self.assertEqual(agent.rewind_session("20260101-000040"), "second")
        self.assertEqual(agent.load_session("20260101-000040")[1], self.turn("first", "one"))

    def test_rewinding_the_only_exchange_deletes_the_session(self):
        # Rather than leaving an empty one behind, which would otherwise be
        # reopened complete with the turn just unwound.
        agent.save_session("20260101-000050", self.turn("only", "answer"))
        agent.rewind_session("20260101-000050")
        self.assertEqual(agent.list_sessions(), [])

    def test_export_renders_the_transcript(self):
        agent.save_session("20260101-000060", [
            {"role": "user", "content": "list the files"},
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "1", "type": "function",
                             "function": {"name": "list_files",
                                          "arguments": '{"path": "."}'}}]},
            {"role": "tool", "tool_call_id": "1", "content": "a.py\nb.py"},
            {"role": "assistant", "content": "Two files."},
        ])
        text = agent.as_markdown("20260101-000060")
        for expected in ("## You", "list the files", "**list_files**", "a.py", "Two files."):
            self.assertIn(expected, text)


class HistoryBudget(unittest.TestCase):
    """
    What gets dropped when a conversation outgrows the context window.

    Tool output is bulky and the window is small, so forgetting turn one beats
    pushing the system prompt out of context.
    """

    def test_a_short_conversation_is_kept_whole(self):
        history = [{"role": "user", "content": "hi"},
                   {"role": "assistant", "content": "hello"}]
        self.assertEqual(agent.trim_history(history), history)

    def test_the_oldest_turns_go_first(self):
        history = ([{"role": "user", "content": "x" * 30000}]
                   + [{"role": "assistant", "content": "recent"}])
        kept = agent.trim_history(history)
        self.assertEqual([m["content"] for m in kept], ["recent"])

    def test_it_never_opens_on_a_tool_result(self):
        # A tool message that leads the conversation references a call that is
        # no longer there, which some servers reject outright.
        history = [
            {"role": "user", "content": "x" * 30000},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
            {"role": "tool", "tool_call_id": "1", "content": "output"},
            {"role": "assistant", "content": "done"},
        ]
        kept = agent.trim_history(history)
        self.assertNotEqual(kept[0]["role"], "tool")

    def test_messages_with_no_content_do_not_crash_the_budget(self):
        history = [{"role": "assistant", "content": None, "tool_calls": []},
                   {"role": "assistant", "content": "fine"}]
        self.assertEqual(len(agent.trim_history(history)), 2)

    @staticmethod
    def exchange(n, size=800):
        return [{"role": "user", "content": f"question {n} " + "q" * size},
                {"role": "assistant", "content": "", "tool_calls": [{"id": str(n)}]},
                {"role": "tool", "tool_call_id": str(n), "content": "t" * size},
                {"role": "assistant", "content": f"answer {n} " + "a" * size}]

    def test_a_cut_lands_at_the_start_of_an_exchange(self):
        history = [m for n in range(12) for m in self.exchange(n)]
        kept = agent.trim_history(history, budget=10000)
        self.assertLess(len(kept), len(history))
        self.assertEqual(kept[0]["role"], "user")

    def test_the_opening_holds_still_while_the_conversation_grows(self):
        # llama-server reuses work only for the part of the prompt that starts
        # the same way. A cut that crept forward a message per step changed the
        # opening every step, and every step re-read 12K tokens from scratch.
        budget, history, openings = 10000, [], []
        for n in range(30):
            for message in self.exchange(n):
                history.append(message)
                openings.append(agent.trim_history(history, budget)[0]["content"])
        moves = sum(1 for a, b in zip(openings, openings[1:]) if a != b)
        steps = len(openings) - 1
        grown = sum(len(m["content"]) + 200 for m in history)
        # Once per third of the budget at most, against once per step before.
        self.assertLessEqual(moves, grown // (budget // 3) + 1)
        self.assertLess(moves, steps // 4)

    def test_the_budget_follows_the_window(self):
        cfg = {"max_tokens": 1200}
        small = agent.history_budget(cfg, 16384)
        large = agent.history_budget(cfg, 32768)
        self.assertGreaterEqual(small, agent.HISTORY_CHAR_BUDGET)
        self.assertGreater(large, small * 2)

    def test_a_huge_max_tokens_does_not_starve_the_conversation(self):
        # max_tokens is a ceiling on one answer. Reserving all 16000 of it out
        # of a 32K window would leave the conversation a sliver.
        self.assertGreater(agent.history_budget({"max_tokens": 16000}, 32768), 50000)

    def test_inline_base64_images_count_as_image_chars_not_raw_bytes(self):
        # Regression: after for_model() expands an attached photo into a
        # 400 KB base64 data URL, _size() must count IMAGE_CHARS rather than
        # len(str(content)) — otherwise trim_history() drops the user's turn.
        huge_data_url = "data:image/jpeg;base64," + ("A" * 400_000)
        message = {
            "role": "user",
            "content": [
                {"type": "text", "text": "can you find these"},
                {"type": "image_url", "image_url": {"url": huge_data_url}},
            ],
        }
        self.assertLess(agent._size(message), agent.IMAGE_CHARS + 500)
        kept = agent.trim_history([message], budget=55_000)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["role"], "user")

    def test_single_oversized_user_message_is_never_dropped_to_empty(self):
        history = [{"role": "user", "content": "x" * 60_000}]
        kept = agent.trim_history(history, budget=10_000)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["role"], "user")


class ContextWindow(TempStateTest):

    def test_without_a_server_it_falls_back_to_the_model_profile(self):
        original = agent.PROPS
        agent.PROPS = "http://127.0.0.1:9/props"     # nothing listens on 9
        try:
            self.assertEqual(agent.context_window(),
                             agent.settings.DEFAULT_PROFILE["ctx"])
        finally:
            agent.PROPS = original


class XmlToolCalls(unittest.TestCase):
    """
    Recovering calls that arrived as text.

    Some models emit tool calls in their own XML dialect, and llama-server only
    reliably converts the first one. Without this the loop reads the XML as the
    final answer and stops a step short of doing the work.
    """

    def test_a_single_call_is_recovered(self):
        calls = agent.parse_xml_tool_calls(
            "<tool_call><function=read_file>"
            "<parameter=path>/tmp/x.py</parameter></function></tool_call>")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "read_file")
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]),
                         {"path": "/tmp/x.py"})

    def test_several_calls_in_one_response(self):
        calls = agent.parse_xml_tool_calls(
            "<tool_call><function=list_files><parameter=path>/a</parameter>"
            "</function></tool_call>"
            "<tool_call><function=list_files><parameter=path>/b</parameter>"
            "</function></tool_call>")
        self.assertEqual([json.loads(c["function"]["arguments"])["path"] for c in calls],
                         ["/a", "/b"])

    def test_several_parameters(self):
        calls = agent.parse_xml_tool_calls(
            "<tool_call><function=edit_file>"
            "<parameter=path>/x</parameter>"
            "<parameter=old_text>a</parameter>"
            "<parameter=new_text>b</parameter></function></tool_call>")
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]),
                         {"path": "/x", "old_text": "a", "new_text": "b"})

    def test_prose_is_not_mistaken_for_a_call(self):
        self.assertEqual(agent.parse_xml_tool_calls("Just an ordinary answer."), [])

    def test_a_block_with_no_function_name_is_skipped(self):
        self.assertEqual(agent.parse_xml_tool_calls("<tool_call>garbage</tool_call>"), [])


class ArgumentBleed(unittest.TestCase):
    """
    When two calls arrive at once, the parser closes the first and dumps
    everything after it into that call's argument. Left alone the result trips
    the shell guard, gets refused, and the model retries into the same broken
    parse forever.
    """

    def test_trailing_tags_are_cut(self):
        self.assertEqual(
            agent.clean_argument("uptime\n</function>\n</tool_call>\n<tool_call>x"),
            "uptime")

    def test_a_clean_argument_is_unchanged(self):
        self.assertEqual(agent.clean_argument("df -h"), "df -h")

    def test_non_strings_pass_through(self):
        for value in (42, None, True, ["a"]):
            self.assertEqual(agent.clean_argument(value), value)

    def test_arguments_are_parsed_and_cleaned_together(self):
        call = {"function": {"arguments":
                             '{"command": "ls</function></tool_call>"}'}}
        self.assertEqual(agent.call_arguments(call), {"command": "ls"})

    def test_unparseable_json_becomes_no_arguments(self):
        call = {"function": {"arguments": "{not json"}}
        self.assertEqual(agent.call_arguments(call), {})


if __name__ == "__main__":
    unittest.main()

"""
The tools that change things, and the previews shown before they do.

The preview is not decoration. It is the only thing standing between the model
and the file, so these check that it says what is about to happen and that a
refusal really does leave the disk alone.
"""

import os
import unittest

from harness import tools

from .support import Recorder, TempStateTest


class Resolving(TempStateTest):
    """How a path in a tool call becomes a path on disk."""

    def test_relative_paths_resolve_inside_the_project(self):
        cfg = self.cfg()
        self.assertEqual(tools.resolve("src/main.py", cfg),
                         os.path.join(self.project, "src/main.py"))

    def test_absolute_paths_are_left_alone(self):
        self.assertEqual(tools.resolve("/tmp/x.py", self.cfg()), "/tmp/x.py")

    def test_tilde_is_expanded(self):
        self.assertEqual(tools.resolve("~/x.py", self.cfg()),
                         os.path.expanduser("~/x.py"))

    def test_without_a_project_a_relative_path_stays_relative(self):
        self.assertEqual(tools.resolve("x.py", self.cfg(project_dir="")), "x.py")

    def test_traversal_out_of_the_project_is_normalised_not_hidden(self):
        # It does not pretend ".." cannot happen — it resolves it, so that the
        # approval preview shows the real destination rather than a path that
        # looks like it is inside the project.
        resolved = tools.resolve("../escaped.py", self.cfg())
        self.assertEqual(resolved,
                         os.path.join(os.path.dirname(self.project), "escaped.py"))
        self.assertNotIn("..", resolved)


class Writing(TempStateTest):

    def test_a_refusal_leaves_the_file_untouched(self):
        path = self.write("keep.txt", "original")
        result = tools.write_file(
            {"path": path, "content": "replaced"}, self.cfg(), Recorder(allow=False))
        self.assertIn("declined", result)
        with open(path) as handle:
            self.assertEqual(handle.read(), "original")

    def test_an_approval_writes(self):
        path = os.path.join(self.project, "new.txt")
        result = tools.write_file(
            {"path": path, "content": "hello"}, self.cfg(), Recorder(allow=True))
        self.assertIn("Wrote", result)
        with open(path) as handle:
            self.assertEqual(handle.read(), "hello")

    def test_missing_parent_directories_are_created(self):
        path = os.path.join(self.project, "a/b/c/deep.txt")
        tools.write_file({"path": path, "content": "x"}, self.cfg(), Recorder(allow=True))
        self.assertTrue(os.path.isfile(path))

    def test_the_preview_says_overwrite_and_gives_the_size(self):
        path = self.write("existing.txt", "some original content")
        sink = Recorder(allow=False)
        tools.write_file({"path": path, "content": "new"}, self.cfg(), sink)
        detail = sink.asked[0]["detail"]
        self.assertIn("OVERWRITE", detail)
        self.assertIn(str(len("some original content")), detail)

    def test_the_preview_says_create_for_a_new_file(self):
        sink = Recorder(allow=False)
        tools.write_file({"path": os.path.join(self.project, "fresh.txt"),
                          "content": "x"}, self.cfg(), sink)
        self.assertIn("CREATE", sink.asked[0]["detail"])

    def test_protected_locations_are_refused_without_even_asking(self):
        sink = Recorder(allow=True)      # would say yes to anything
        result = tools.write_file(
            {"path": "/etc/hosts", "content": "x"}, self.cfg(), sink)
        self.assertIn("refusing", result)
        self.assertEqual(sink.asked, [], "a protected path must not be offered")


class Editing(TempStateTest):

    def test_an_ambiguous_match_is_refused_with_the_line_numbers(self):
        # Replacing the wrong one of three identical snippets corrupts the file
        # quietly, so ambiguity is a hard error rather than a warning.
        path = self.write("dup.py", "x = 1\ny = 2\nx = 1\nz = 3\nx = 1\n")
        result = tools.edit_file(
            {"path": path, "old_text": "x = 1", "new_text": "x = 9"},
            self.cfg(), Recorder(allow=True))
        self.assertIn("appears 3 times", result)
        self.assertIn("1", result)
        with open(path) as handle:
            self.assertEqual(handle.read().count("x = 1"), 3)

    def test_text_that_is_not_there_is_reported(self):
        path = self.write("x.py", "hello\n")
        result = tools.edit_file(
            {"path": path, "old_text": "goodbye", "new_text": "hi"},
            self.cfg(), Recorder(allow=True))
        self.assertIn("not found", result)

    def test_a_unique_match_is_replaced(self):
        path = self.write("x.py", "before\ntarget\nafter\n")
        result = tools.edit_file(
            {"path": path, "old_text": "target", "new_text": "replaced"},
            self.cfg(), Recorder(allow=True))
        self.assertIn("Edited", result)
        with open(path) as handle:
            self.assertEqual(handle.read(), "before\nreplaced\nafter\n")

    def test_the_preview_shows_the_change_as_a_diff(self):
        path = self.write("x.py", "old line\n")
        sink = Recorder(allow=False)
        tools.edit_file({"path": path, "old_text": "old line",
                         "new_text": "new line"}, self.cfg(), sink)
        detail = sink.asked[0]["detail"]
        self.assertIn("- old line", detail)
        self.assertIn("+ new line", detail)

    def test_a_refusal_leaves_the_file_untouched(self):
        path = self.write("x.py", "original\n")
        tools.edit_file({"path": path, "old_text": "original",
                         "new_text": "changed"}, self.cfg(), Recorder(allow=False))
        with open(path) as handle:
            self.assertEqual(handle.read(), "original\n")


class Reading(TempStateTest):

    def test_output_is_numbered_and_reports_the_total(self):
        path = self.write("x.py", "".join(f"line {n}\n" for n in range(1, 21)))
        result = tools.read_file({"path": path}, self.cfg(), Recorder())
        self.assertIn("of 20", result)
        self.assertIn("   1 | line 1", result)

    def test_a_long_file_comes_back_a_page_at_a_time(self):
        path = self.write("big.py", "".join(f"line {n}\n" for n in range(1, 501)))
        result = tools.read_file({"path": path, "num_lines": 10}, self.cfg(), Recorder())
        self.assertIn("490 more lines", result)
        self.assertIn("start_line=11", result)

    def test_reading_on_from_a_given_line(self):
        path = self.write("x.py", "".join(f"line {n}\n" for n in range(1, 21)))
        result = tools.read_file(
            {"path": path, "start_line": 15, "num_lines": 3}, self.cfg(), Recorder())
        self.assertIn("line 15", result)
        self.assertIn("line 17", result)
        self.assertNotIn("line 18", result)

    def test_junk_arguments_fall_back_to_the_defaults(self):
        # Models send strings, floats, nulls and occasional prose where a
        # number belongs. None of it should raise.
        path = self.write("x.py", "one\ntwo\n")
        for bad in ("abc", None, "", {}, [], "12.5"):
            result = tools.read_file(
                {"path": path, "start_line": bad, "num_lines": bad},
                self.cfg(), Recorder())
            self.assertIn("one", result, f"start_line={bad!r}")

    def test_starting_past_the_end_says_so(self):
        path = self.write("x.py", "one\n")
        result = tools.read_file({"path": path, "start_line": 99}, self.cfg(), Recorder())
        self.assertIn("past the end", result)

    def test_an_empty_file_is_not_an_error(self):
        path = self.write("empty.py", "")
        self.assertIn("empty file", tools.read_file({"path": path}, self.cfg(), Recorder()))


class Truncation(unittest.TestCase):

    def test_short_output_is_untouched(self):
        self.assertEqual(tools.truncate("hello"), "hello")

    def test_long_output_is_cut_and_says_so(self):
        result = tools.truncate("x" * (tools.OUTPUT_CAP + 500))
        self.assertLess(len(result), tools.OUTPUT_CAP + 200)
        self.assertIn("truncated", result)


if __name__ == "__main__":
    unittest.main()

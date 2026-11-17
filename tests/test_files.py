"""
The tools that change things, and the previews shown before they do.

The preview is not decoration. It is the only thing standing between the model
and the file, so these check that it says what is about to happen and that a
refusal really does leave the disk alone.
"""

import os
import shutil
import unittest

from harness import tools

from .support import Recorder, TempStateTest


def minimal_pdf(lines):
    """A one-page PDF with an uncompressed text layer — enough for PDFKit."""
    text = ("BT /F1 12 Tf 72 720 Td 14 TL "
            + " ".join(f"({line}) Tj T*" for line in lines) + " ET")
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(text)} >>\nstream\n{text}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = "%PDF-1.4\n", []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n"
    out += "".join(f"{offset:010d} 00000 n \n" for offset in offsets)
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n")
    return out.encode("latin-1")


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

    def test_a_stopped_turn_is_not_reported_as_a_refusal(self):
        # Stopping a turn while its question is on screen answers the question
        # no, but you didn't decline anything — and the transcript said you had.
        path = self.write("keep2.txt", "original")
        result = tools.write_file({"path": path, "content": "replaced"}, self.cfg(),
                                  Recorder(allow=False, cancelled=True))
        self.assertNotIn("declined", result)
        self.assertIn("stopped", result)
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

    def write_bytes(self, name, data):
        path = os.path.join(self.project, name)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def test_a_binary_file_is_refused_rather_than_dumped(self):
        # Pages of replacement characters cost context and tell the model
        # nothing, so it is told what the file is instead.
        path = self.write_bytes("photo.jpg", b"\xff\xd8\xff\xe0\0\x10JFIF\0" * 50)
        result = tools.read_file({"path": path}, self.cfg(), Recorder())
        self.assertIn("binary file", result)
        self.assertNotIn("JFIF", result)

    @unittest.skipUnless(shutil.which("osascript"), "PDF text extraction needs macOS")
    def test_a_pdf_comes_back_as_its_words(self):
        # The bug this replaces: a CV read as 4,000 lines of PDF object syntax,
        # with the actual words compressed where the model could not see them.
        path = self.write_bytes("cv.pdf", minimal_pdf(["Jane Doe", "ML engineer"]))
        result = tools.read_file({"path": path}, self.cfg(), Recorder())
        self.assertIn("text extracted from the PDF", result)
        self.assertIn("   1 | Jane Doe", result)
        self.assertIn("   2 | ML engineer", result)
        self.assertNotIn("endobj", result)

    @unittest.skipUnless(shutil.which("osascript"), "PDF text extraction needs macOS")
    def test_a_pdf_is_recognised_by_its_contents_not_its_name(self):
        path = self.write_bytes("download", minimal_pdf(["no extension"]))
        result = tools.read_file({"path": path}, self.cfg(), Recorder())
        self.assertIn("no extension", result)

    @unittest.skipUnless(shutil.which("osascript"), "PDF text extraction needs macOS")
    def test_a_pdf_with_no_text_layer_says_why(self):
        path = self.write_bytes("scan.pdf", minimal_pdf([]))
        result = tools.read_file({"path": path}, self.cfg(), Recorder())
        self.assertIn("no text", result)

    @unittest.skipUnless(shutil.which("osascript"), "PDF text extraction needs macOS")
    def test_a_broken_pdf_is_an_error_not_junk(self):
        path = self.write_bytes("broken.pdf", b"%PDF-1.4\n" + b"\0garbage" * 100)
        result = tools.read_file({"path": path}, self.cfg(), Recorder())
        self.assertTrue(result.startswith("Error:"), result)

    @unittest.skipUnless(shutil.which("textutil"), "document extraction needs macOS")
    def test_a_word_processor_file_comes_back_as_its_words(self):
        path = self.write("letter.rtf", r"{\rtf1\ansi Dear team,\par Regards}")
        result = tools.read_file({"path": path}, self.cfg(), Recorder())
        self.assertIn("text extracted from the document", result)
        self.assertIn("Dear team,", result)
        self.assertNotIn("rtf1", result)


class Truncation(unittest.TestCase):

    def test_short_output_is_untouched(self):
        self.assertEqual(tools.truncate("hello"), "hello")

    def test_long_output_is_cut_and_says_so(self):
        result = tools.truncate("x" * (tools.OUTPUT_CAP + 500))
        self.assertLess(len(result), tools.OUTPUT_CAP + 200)
        self.assertIn("truncated", result)


if __name__ == "__main__":
    unittest.main()

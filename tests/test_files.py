"""
The tools that change things, and the previews shown before they do.

The preview is not decoration. It is the only thing standing between the model
and the file, so these check that it says what is about to happen and that a
refusal really does leave the disk alone.
"""

import os
import shutil
import subprocess
import time
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

    def test_a_miss_with_wrong_indentation_hints_at_line_numbers(self):
        path = self.write("indented.py", "def f():\n    return 1\n")
        result = tools.edit_file(
            {"path": path, "old_text": "return 1", "new_text": "return 2"},
            self.cfg(), Recorder(allow=True))
        self.assertIn("Edited", result)
        result2 = tools.edit_file(
            {"path": path, "old_text": "  def f():\n    return 2", "new_text": "x"},
            self.cfg(), Recorder(allow=True))
        self.assertIn("not found", result2)
        self.assertIn("near lines 1", result2)


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

    def test_long_lines_page_cleanly_without_skipping_lines(self):
        lines = [f"line-{n:02d}-" + ("a" * 580) + "\n" for n in range(1, 21)]
        path = self.write("wide.txt", "".join(lines))
        first = tools.read_file({"path": path, "num_lines": 20}, self.cfg(), Recorder())
        self.assertLessEqual(len(first), tools.OUTPUT_CAP)
        self.assertIn("read on with start_line=", first)
        next_line = int(first.rsplit("start_line=", 1)[1].rstrip(")"))
        second = tools.read_file({"path": path, "start_line": next_line, "num_lines": 20},
                                 self.cfg(), Recorder())
        self.assertIn(f"line-{next_line:02d}-", second)
        self.assertNotIn(f"line-{next_line:02d}-", first)

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


class Listing(TempStateTest):

    def test_folders_are_marked(self):
        os.makedirs(os.path.join(self.project, "Finals"))
        self.write("notes.txt", "x")
        self.assertEqual(tools.list_files({"path": "."}, self.cfg(), Recorder()),
                         "Finals/\nnotes.txt")

    def test_a_long_listing_says_how_much_is_missing(self):
        for n in range(tools.LIST_CAP + 5):
            self.write(f"f{n:03d}", "")
        result = tools.list_files({"path": "."}, self.cfg(), Recorder())
        self.assertTrue(result.endswith("... and 5 more"), result[-40:])

    def test_details_sorts_newest_first_with_sizes(self):
        old = self.write("old.png", "12")
        new = self.write("new.png", "123456")
        stamp = time.time() - 3600
        os.utime(old, (stamp, stamp))
        result = tools.list_files({"path": ".", "details": "true"}, self.cfg(), Recorder())
        lines = result.splitlines()
        self.assertTrue(lines[0].startswith("Folder:"), lines[0])
        self.assertTrue(lines[1].startswith("new.png  (6 bytes, changed "), lines[1])
        self.assertTrue(lines[2].startswith("old.png  (2 bytes, changed "), lines[2])
        self.assertTrue(os.path.isfile(new))

    def test_missing_relative_path_hints_about_working_folder(self):
        result = tools.list_files({"path": "NoSuchSubdir"}, self.cfg(), Recorder())
        self.assertIn("outside the working folder", result)


class HomeVariables(TempStateTest):
    """No shell sees these paths, so $HOME used to mean a folder called "$HOME"."""

    def test_home_is_expanded(self):
        home = os.path.expanduser("~")
        for written in ("$HOME/Downloads", "${HOME}/Downloads"):
            self.assertEqual(tools.resolve(written, self.cfg()),
                             os.path.join(home, "Downloads"), written)

    def test_a_longer_name_is_left_alone(self):
        self.assertTrue(tools.resolve("/x/$HOMEWORK", self.cfg()).endswith("$HOMEWORK"))


class EditingKeepsTheRestOfTheFile(TempStateTest):

    def test_windows_line_endings_survive_an_edit(self):
        path = os.path.join(self.project, "dos.txt")
        with open(path, "wb") as handle:
            handle.write(b"one\r\ntwo\r\nthree\r\n")
        result = tools.edit_file({"path": path, "old_text": "one\ntwo",
                                  "new_text": "uno\ndos"}, self.cfg(), Recorder(allow=True))
        self.assertEqual(result, f"Edited {path}")
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), b"uno\r\ndos\r\nthree\r\n")

    def test_a_file_that_is_not_utf8_is_refused_untouched(self):
        path = os.path.join(self.project, "latin.txt")
        original = "caf\xe9 au lait\n".encode("latin-1")
        with open(path, "wb") as handle:
            handle.write(original)
        result = tools.edit_file({"path": path, "old_text": "au lait",
                                  "new_text": "noir"}, self.cfg(), Recorder(allow=True))
        self.assertIn("not UTF-8", result)
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), original)


class Commands(TempStateTest):

    def run_command(self, command):
        return tools.run_command({"command": command},
                                 self.cfg(approve_commands="never"), Recorder())

    def test_a_failure_says_so(self):
        result = self.run_command("echo partial output; exit 3")
        self.assertEqual(result, "partial output\n(exit code 3)")

    def test_success_is_unchanged(self):
        self.assertEqual(self.run_command("echo fine"), "fine")

    def test_a_command_waiting_for_input_gets_none(self):
        # `cat` with no file reads stdin. Inherited, it waited out the timeout.
        started = time.monotonic()
        self.assertEqual(self.run_command("cat"), "(no output, exit code 0)")
        self.assertLess(time.monotonic() - started, 10)

    def test_the_end_of_long_output_survives(self):
        result = self.run_command("seq 1 5000; echo THE ERROR IS HERE")
        self.assertLessEqual(len(result), tools.OUTPUT_CAP)
        self.assertTrue(result.startswith("1\n2\n"))
        self.assertTrue(result.endswith("THE ERROR IS HERE"), result[-60:])

    def test_output_that_is_not_utf8_is_still_returned(self):
        result = self.run_command("printf 'ok \\377 done'")
        self.assertIn("ok", result)
        self.assertIn("done", result)

    def test_a_working_folder_that_has_gone_is_named(self):
        cfg = self.cfg(approve_commands="never", project_dir=os.path.join(self.tmp, "gone"))
        result = tools.run_command({"command": "ls"}, cfg, Recorder())
        self.assertIn("does not exist any more", result)


class Arguments(TempStateTest):

    def test_anything_the_model_sends_arrives_as_text(self):
        self.write("2026", "")
        result = tools.call("read_file", {"path": "2026", "num_lines": 5},
                            self.cfg(), Recorder())
        self.assertIn("empty file", result)

    def test_a_tool_that_raises_comes_back_as_an_error(self):
        original = tools.DISPATCH["list_files"]
        tools.DISPATCH["list_files"] = lambda args, cfg, sink: {}["boom"]
        try:
            result = tools.call("list_files", {}, self.cfg(), Recorder())
        finally:
            tools.DISPATCH["list_files"] = original
        self.assertTrue(result.startswith("Error: list_files failed: KeyError"), result)



class Email(TempStateTest):
    """
    Sending goes through the Mail app, always asks, and is tested here without
    a single message leaving the machine: osascript is stood in for.
    """

    def setUp(self):
        super().setUp()
        self.ran = []
        self._run = tools.subprocess.run

        def fake_run(command, **kwargs):
            self.ran.append(command)
            return tools.subprocess.CompletedProcess(command, 0, self.reply, "")
        tools.subprocess.run = fake_run
        self.reply = "SENT\n"

    def tearDown(self):
        tools.subprocess.run = self._run
        super().tearDown()

    def send(self, allow=True, **args):
        sink = Recorder(allow=allow)
        args = {"to": "someone@example.com", "subject": "Photos", "body": "Here they are.", **args}
        return tools.send_email(args, self.cfg(approve_commands="never", approve_files="never"), sink), sink

    def test_it_asks_every_time_whatever_the_settings(self):
        result, sink = self.send(allow=True)
        self.assertEqual(sink.questions, ["Send this email?"])
        self.assertEqual(sink.asked[0]["kind"], "", "no 'always' for sending mail")
        self.assertIn("someone@example.com", sink.asked[0]["detail"])
        self.assertTrue(result.startswith("Mail accepted the email"), result)

    def test_a_refusal_sends_nothing(self):
        result, _ = self.send(allow=False)
        self.assertIn("declined", result)
        self.assertEqual(self.ran, [])

    def test_the_text_reaches_mail_as_data_not_as_script(self):
        photo = self.write("a.png", "x")
        self.send(subject='Say "hi" end tell', body="line one\nline two", attachments=photo)
        command = self.ran[0]
        values = command[command.index(tools.MAIL_SCRIPT.strip().splitlines()[-1]) + 1:]
        self.assertEqual(values, ['Say "hi" end tell', "line one\nline two", "2", "1",
                                  "someone@example.com", photo])

    def test_attachments_are_previewed_with_their_size(self):
        photo = self.write("a.png", "x" * 2_000_000)
        _, sink = self.send(attachments=photo)
        self.assertIn("a.png  (2.0 MB)", sink.asked[0]["detail"])

    def test_a_missing_attachment_stops_it_before_asking(self):
        result, sink = self.send(attachments="~/no-such-file.png")
        self.assertIn("not files", result)
        self.assertEqual(sink.asked, [])
        self.assertEqual(self.ran, [])

    def test_a_credential_file_is_never_attached(self):
        os.makedirs(os.path.join(self.tmp, ".ssh"), exist_ok=True)
        saved = tools.SECRET_PATHS
        tools.SECRET_PATHS = (os.path.join(self.tmp, ".ssh"),)
        try:
            key = os.path.join(self.tmp, ".ssh", "id_rsa")
            with open(key, "w") as handle:
                handle.write("KEY")
            result, sink = self.send(attachments=key)
        finally:
            tools.SECRET_PATHS = saved
        self.assertIn("credentials folder", result)
        self.assertEqual(sink.asked, [])

    def test_bad_addresses_are_refused(self):
        for to in ("", "not-an-address", "a@b"):
            result, _ = self.send(to=to)
            self.assertTrue(result.startswith("Error:"), to)
        self.assertEqual(self.ran, [])

    def test_several_recipients(self):
        self.send(to="a@example.com, b@example.org")
        self.assertIn("a@example.com", self.ran[0])
        self.assertIn("b@example.org", self.ran[0])

    def test_attachment_lists_however_they_are_written(self):
        one, two = self.write("one.png", "1"), self.write("two, final.png", "2")
        cfg = self.cfg()
        self.assertEqual(tools.attachment_paths(f'["{one}", "{two}"]', cfg), [one, two])
        self.assertEqual(tools.attachment_paths(f"{one}\n{two}", cfg), [one, two])
        self.assertEqual(tools.attachment_paths("one.png", cfg), [one])

    def test_no_account_in_mail_is_explained(self):
        self.reply = ""
        tools.subprocess.run = lambda command, **kw: tools.subprocess.CompletedProcess(
            command, 1, "", "execution error: NO_ACCOUNT (-2700)")
        result, _ = self.send()
        self.assertIn("no email account set up", result)

    @unittest.skipUnless(shutil.which("osacompile"), "needs macOS")
    def test_the_mail_script_compiles(self):
        tools.subprocess.run = self._run
        command = ["osacompile", "-o", os.path.join(self.tmp, "mail.scpt")]
        for line in tools.MAIL_SCRIPT.strip().splitlines():
            command += ["-e", line]
        done = self._run(command, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)



class Images(TempStateTest):

    def png(self, name="red.png", side=8):
        from .test_loop import tiny_png
        path = os.path.join(self.project, name)
        with open(path, "wb") as handle:
            handle.write(tiny_png(side=side))
        return path

    def test_a_picture_comes_back_with_its_path_attached(self):
        path = self.png()
        result = tools.call("view_image", {"path": path}, self.cfg(), Recorder())
        self.assertIn(path, result)
        self.assertEqual(result.image, path)

    def test_something_that_is_not_a_picture_is_refused(self):
        path = self.write("notes.txt", "hello")
        result = tools.view_image({"path": path}, self.cfg(), Recorder())
        self.assertTrue(result.startswith("Error:"), result)
        self.assertFalse(getattr(result, "image", ""))

    @unittest.skipUnless(shutil.which("sips"), "needs macOS")
    def test_an_iphone_photo_is_converted(self):
        heic = os.path.join(self.project, "photo.heic")
        subprocess.run(["sips", "-s", "format", "heic", self.png(side=64), "--out", heic],
                       capture_output=True, check=True)
        url = tools.image_url(heic)
        self.assertTrue(url.startswith("data:image/jpeg;base64,"), url[:40])

    @unittest.skipUnless(shutil.which("sips"), "needs macOS")
    def test_a_big_picture_is_made_small_enough_to_afford(self):
        import base64
        url = tools.image_url(self.png("big.png", side=2400))
        small = os.path.join(self.project, "small.jpg")
        with open(small, "wb") as handle:
            handle.write(base64.b64decode(url.split(",", 1)[1]))
        self.assertLessEqual(max(tools._dimensions(small)), tools.IMAGE_SIDE)



class Finding(TempStateTest):

    def place(self, relative, age=0):
        path = os.path.join(self.project, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write("x")
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
        return path

    def find(self, **args):
        return tools.find_files({"folder": self.project, **args}, self.cfg(), Recorder())

    def test_every_word_has_to_appear_and_the_newest_comes_first(self):
        old = self.place("2025/David-CV-EN.pdf", age=86400 * 30)
        new = self.place("jobs/acme/David-CV-EN.pdf", age=60)
        self.place("jobs/acme/David-CV-NL.pdf")
        lines = self.find(name="cv en").splitlines()
        self.assertTrue(lines[0].startswith("2 files"), lines[0])
        self.assertTrue(lines[1].startswith(new))
        self.assertTrue(lines[2].startswith(old))

    def test_a_pattern(self):
        self.place("a/report.pdf")
        self.place("a/report.docx")
        result = self.find(name="*.pdf")
        self.assertIn("report.pdf", result)
        self.assertNotIn("report.docx", result)

    def test_folders(self):
        self.place("Lightroom/Finals/boat.png")
        result = self.find(name="finals", kind="folders")
        self.assertIn(os.path.join(self.project, "Lightroom", "Finals"), result)
        self.assertNotIn("boat.png", result)

    def test_caches_bundles_and_hidden_folders_are_not_walked(self):
        for skipped in ("Library/Caches/cv.pdf", ".git/cv.pdf", "Thing.app/Contents/cv.pdf",
                        "node_modules/x/cv.pdf"):
            self.place(skipped)
        self.place("Documents/cv.pdf")
        result = self.find(name="cv")
        self.assertTrue(result.startswith("1 file"), result)

    def test_a_search_that_runs_out_of_time_says_so(self):
        self.place("a/cv.pdf")
        saved = tools.FIND_SECONDS
        tools.FIND_SECONDS = -1
        try:
            result = self.find(name="cv")
        finally:
            tools.FIND_SECONDS = saved
        self.assertIn("stopped after", result)

    def test_nothing_found_is_said_plainly(self):
        self.assertTrue(self.find(name="nothing-like-this").startswith("No files"))


class Searching(TempStateTest):

    def place(self, relative, text):
        path = os.path.join(self.project, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        mode = "wb" if isinstance(text, bytes) else "w"
        with open(path, mode) as handle:
            handle.write(text)
        return path

    def test_finds_regex_and_filters_by_glob(self):
        self.place("src/app.py", "def handle():\n    raise DBPoolExhaustedError('full')\n")
        self.place("logs/app.log", "ERROR DBPoolExhaustedError waited=500ms\n")
        self.place("node_modules/pkg/a.js", "DBPoolExhaustedError\n")
        self.place(".git/config", "DBPoolExhaustedError\n")
        self.place("bin.dat", b"\x00DBPoolExhaustedError\x00")

        all_hits = tools.search_in_files({"pattern": "DBPool\\w+"}, self.cfg(), Recorder())
        self.assertIn("src/app.py:2:", all_hits)
        self.assertIn("logs/app.log:1:", all_hits)
        self.assertNotIn("node_modules", all_hits)
        self.assertNotIn(".git", all_hits)
        self.assertNotIn("bin.dat", all_hits)

        py_only = tools.search_in_files(
            {"pattern": "DBPoolExhaustedError", "file_pattern": "*.py"},
            self.cfg(), Recorder())
        self.assertIn("src/app.py:2:", py_only)
        self.assertNotIn("logs/app.log", py_only)

    def test_skips_and_refuses_secret_folders(self):
        vault = os.path.join(self.project, "vault")
        os.makedirs(vault)
        self.place("vault/id_rsa", "PRIVATE_KEY_TOKEN\n")
        self.place("notes.txt", "normal text\n")
        saved = tools.SECRET_PATHS
        tools.SECRET_PATHS = (vault,)
        try:
            r1 = tools.search_in_files({"pattern": "PRIVATE_KEY_TOKEN"}, self.cfg(), Recorder())
            self.assertTrue(r1.startswith("No matches"), r1)
            r2 = tools.search_in_files({"pattern": "PRIVATE", "folder": vault},
                                       self.cfg(), Recorder())
            self.assertIn("refusing to search", r2)
        finally:
            tools.SECRET_PATHS = saved



class MacStatus(unittest.TestCase):

    def test_the_verdict_follows_macoss_own_free_figure(self):
        self.assertTrue(tools.memory_verdict(5, 0).startswith("short"))
        self.assertEqual(tools.memory_verdict(14, 0), "tight")
        self.assertEqual(tools.memory_verdict(25, 0), "fine")
        self.assertEqual(tools.memory_verdict(50, 0), "plenty free")
        self.assertIn("5.6 GB has been pushed out to swap", tools.memory_verdict(14, 5.6))

    def test_helpers_count_towards_their_app(self):
        self.assertEqual(tools.app_name("/Applications/Arc.app/Contents/Frameworks/ArcCore."
                                        "framework/Helpers/Browser Helper (Renderer).app/"
                                        "Contents/MacOS/Browser Helper (Renderer)"), "Arc")
        self.assertEqual(tools.app_name("/Users/d/llm/llama.cpp/build/bin/llama-server"),
                         "llama-server")

    def test_tops_amounts(self):
        self.assertEqual(tools._megabytes("2993M"), 2993)
        self.assertEqual(tools._megabytes("1.5G+"), 1536)
        self.assertEqual(tools._megabytes("512K"), 0.5)

    @unittest.skipUnless(shutil.which("memory_pressure"), "needs macOS")
    def test_it_reads_this_mac(self):
        result = tools.system_status({}, {}, None)
        for heading in ("Memory:", "Using the most memory:", "CPU:", "Disk:"):
            self.assertIn(heading, result)


if __name__ == "__main__":
    unittest.main()

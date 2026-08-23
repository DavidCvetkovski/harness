"""
The voice sidecar's own logic, and Harness's proxy to it.

The sidecar needs PyTorch, which CI does not install and most people will not
have — so anything touching audio is skipped rather than failed when the
dependencies are absent. What is always tested is the part that decides *what*
gets spoken, which is pure text handling and needs nothing.
"""

import importlib.util
import os
import unittest

VOICE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "voice", "server.py")


def load_sidecar():
    """Import voice/server.py by path — it is a script, not a package."""
    spec = importlib.util.spec_from_file_location("harness_voice", VOICE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


voice = load_sidecar()


class WhatGetsSpoken(unittest.TestCase):
    """
    Answers are full of things that must not be read out character by
    character. Reading a fenced code block aloud is worse than useless.
    """

    def test_code_blocks_become_a_short_note(self):
        spoken = voice.speakable(
            "Here it is:\n```bash\ndu -sh ~/Downloads\nrm -rf build\n```\nThat is all.")
        self.assertIn("Here it is", spoken)
        self.assertIn("Code block omitted", spoken)
        self.assertNotIn("rm -rf", spoken)
        self.assertIn("That is all", spoken)

    def test_inline_code_keeps_its_words(self):
        # `du` should still be said — it is one word, not a wall of shell.
        self.assertEqual(voice.speakable("Run `du -sh` for that."),
                         "Run du -sh for that.")

    def test_bold_markers_are_dropped(self):
        self.assertEqual(voice.speakable("It is **2.1 GB** in total."),
                         "It is 2.1 GB in total.")

    def test_urls_are_not_spelled_out(self):
        spoken = voice.speakable("See https://example.com/a/very/long/path?x=1 for more.")
        self.assertIn("a link", spoken)
        self.assertNotIn("https", spoken)

    def test_whitespace_is_collapsed(self):
        self.assertEqual(voice.speakable("one\n\n  two\t\tthree"), "one two three")


class Chunking(unittest.TestCase):
    """
    The model has 2,048 packed positions and degrades on long inputs, so a
    paragraph is spoken a sentence at a time.
    """

    def test_a_short_line_is_one_chunk(self):
        self.assertEqual(voice.sentences("Just the one."), ["Just the one."])

    def test_it_splits_on_sentence_endings(self):
        parts = voice.sentences("First one. Second one! Third one?", limit=20)
        self.assertEqual(len(parts), 3)

    def test_nothing_exceeds_the_limit(self):
        text = " ".join(f"Sentence number {n} goes here." for n in range(40))
        for part in voice.sentences(text, limit=120):
            self.assertLessEqual(len(part), 120)

    def test_a_single_clause_longer_than_the_limit_is_broken_on_a_space(self):
        # Better an awkward break than an input the model silently truncates.
        parts = voice.sentences("word " * 200, limit=100)
        self.assertTrue(parts)
        for part in parts:
            self.assertLessEqual(len(part), 100)

    def test_no_empty_chunks(self):
        self.assertEqual(voice.sentences("   "), [])
        self.assertNotIn("", voice.sentences("A.  B.   C."))

    def test_every_word_survives_the_round_trip(self):
        text = "One thing happened. Then another; then a third! And finally, this."
        rejoined = " ".join(voice.sentences(text, limit=25))
        self.assertEqual(rejoined.split(), text.split())


class Caching(unittest.TestCase):
    """Saying the same thing twice should cost nothing the second time."""

    def test_same_text_and_voice_is_the_same_key(self):
        self.assertEqual(voice.cache_key("hello", "cpu", "abc"),
                         voice.cache_key("hello", "cpu", "abc"))

    def test_a_different_voice_is_a_different_key(self):
        self.assertNotEqual(voice.cache_key("hello", "cpu", "abc"),
                            voice.cache_key("hello", "cpu", "xyz"))

    def test_a_different_device_is_a_different_key(self):
        # CPU and MPS do not produce bit-identical audio.
        self.assertNotEqual(voice.cache_key("hello", "cpu", "abc"),
                            voice.cache_key("hello", "mps", "abc"))

    def test_the_key_is_prefixed_with_the_voice(self):
        # So deleting one voice can drop exactly its own cached audio.
        key = os.path.basename(voice.cache_key("hello", "cpu", "abc123"))
        self.assertTrue(key.startswith("abc123-"), key)


class Levelling(unittest.TestCase):
    """A quiet reference produces quiet speech, so recordings are levelled."""

    def setUp(self):
        try:
            import numpy                                       # noqa: F401
        except ImportError:
            self.skipTest("numpy not installed — sidecar dependency")

    def test_a_quiet_recording_is_brought_up(self):
        import numpy as np
        quiet = np.array([0.05, -0.04, 0.02], dtype=np.float32)
        self.assertAlmostEqual(float(abs(voice.normalise(quiet)).max()), 0.75, places=5)

    def test_a_loud_recording_is_brought_down(self):
        import numpy as np
        loud = np.array([0.99, -1.0, 0.5], dtype=np.float32)
        self.assertAlmostEqual(float(abs(voice.normalise(loud)).max()), 0.75, places=5)

    def test_silence_is_left_alone_rather_than_amplified(self):
        # Dividing by a near-zero peak would turn room tone into a roar.
        import numpy as np
        silence = np.zeros(16, dtype=np.float32)
        self.assertEqual(float(abs(voice.normalise(silence)).max()), 0.0)


if __name__ == "__main__":
    unittest.main()

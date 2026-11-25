"""Settings, scheduling, and the access token."""

import datetime
import os
import stat
import unittest

from harness import config as settings

from .support import TempStateTest


class Defaults(TempStateTest):

    def test_a_fresh_install_gets_the_defaults(self):
        cfg = settings.load_config()
        self.assertEqual(cfg["approve_files"], "always")
        self.assertEqual(cfg["approve_commands"], "always")
        self.assertTrue(all(cfg["tools"].values()))

    def test_saving_merges_rather_than_replaces(self):
        settings.save_config({"temperature": 0.1})
        cfg = settings.load_config()
        self.assertEqual(cfg["temperature"], 0.1)
        self.assertIn("system_prompt", cfg, "unrelated keys must survive")

    def test_a_new_default_appears_in_an_older_config_file(self):
        # Settings files outlive releases, so anything missing falls back to
        # the default rather than turning into a KeyError at runtime.
        settings._write(settings.CONFIG_FILE, {"temperature": 0.9})
        cfg = settings.load_config()
        self.assertEqual(cfg["temperature"], 0.9)
        self.assertEqual(cfg["max_steps"], settings.DEFAULTS["max_steps"])

    def test_reset_restores_the_defaults(self):
        settings.save_config({"temperature": 1.9})
        self.assertEqual(settings.reset_config()["temperature"],
                         settings.DEFAULTS["temperature"])

    def test_corrupt_json_falls_back_instead_of_crashing(self):
        os.makedirs(settings.BASE, exist_ok=True)
        with open(settings.CONFIG_FILE, "w") as handle:
            handle.write("{ this is not json")
        self.assertEqual(settings.load_config()["max_steps"],
                         settings.DEFAULTS["max_steps"])


class Scheduling(unittest.TestCase):
    """
    When an automation is due.

    Deliberately conservative: a missed window is skipped rather than replayed,
    because catching up would fire a burst of identical runs the moment the
    machine wakes.
    """

    def entry(self, schedule, last_run=None, enabled=True):
        return {"id": "x", "enabled": enabled, "schedule": schedule,
                "last_run": last_run, "prompt": "check"}

    def test_a_disabled_automation_is_never_due(self):
        self.assertFalse(settings.is_due(
            self.entry({"kind": "interval", "minutes": 1}, enabled=False)))

    def test_a_manual_automation_is_never_due(self):
        self.assertFalse(settings.is_due(self.entry({"kind": "manual"})))

    def test_an_interval_that_has_never_run_is_due(self):
        self.assertTrue(settings.is_due(self.entry({"kind": "interval", "minutes": 60})))

    def test_an_interval_is_not_due_again_too_soon(self):
        now = datetime.datetime(2026, 8, 17, 12, 0)
        recent = (now - datetime.timedelta(minutes=10)).isoformat()
        self.assertFalse(settings.is_due(
            self.entry({"kind": "interval", "minutes": 60}, recent), now))

    def test_an_interval_is_due_once_it_has_elapsed(self):
        now = datetime.datetime(2026, 8, 17, 12, 0)
        old = (now - datetime.timedelta(minutes=61)).isoformat()
        self.assertTrue(settings.is_due(
            self.entry({"kind": "interval", "minutes": 60}, old), now))

    def test_a_daily_run_fires_after_its_time(self):
        now = datetime.datetime(2026, 8, 17, 9, 30)
        self.assertTrue(settings.is_due(self.entry({"kind": "daily", "time": "09:00"}), now))

    def test_a_daily_run_does_not_fire_before_its_time(self):
        now = datetime.datetime(2026, 8, 17, 8, 30)
        self.assertFalse(settings.is_due(self.entry({"kind": "daily", "time": "09:00"}), now))

    def test_a_daily_run_fires_only_once_a_day(self):
        now = datetime.datetime(2026, 8, 17, 18, 0)
        today = datetime.datetime(2026, 8, 17, 9, 0).isoformat()
        self.assertFalse(settings.is_due(
            self.entry({"kind": "daily", "time": "09:00"}, today), now))

    def test_a_daily_run_comes_round_again_the_next_day(self):
        now = datetime.datetime(2026, 8, 18, 9, 30)
        yesterday = datetime.datetime(2026, 8, 17, 9, 0).isoformat()
        self.assertTrue(settings.is_due(
            self.entry({"kind": "daily", "time": "09:00"}, yesterday), now))

    def test_a_malformed_time_is_not_due_rather_than_an_exception(self):
        self.assertFalse(settings.is_due(self.entry({"kind": "daily", "time": "nonsense"})))

    def test_an_unrecognised_kind_is_not_due(self):
        self.assertFalse(settings.is_due(self.entry({"kind": "fortnightly"})))


class Automations(TempStateTest):

    def test_create_update_and_delete(self):
        created = settings.upsert_automation({"name": "Disk", "prompt": "check disk",
                                              "schedule": {"kind": "manual"}})
        self.assertTrue(created["id"])
        self.assertEqual(len(settings.load_automations()), 1)

        settings.upsert_automation({**created, "name": "Disk space"})
        entries = settings.load_automations()
        self.assertEqual(len(entries), 1, "an update must not append a second copy")
        self.assertEqual(entries[0]["name"], "Disk space")

        settings.delete_automation(created["id"])
        self.assertEqual(settings.load_automations(), [])

    def test_a_run_is_recorded_against_the_right_entry(self):
        one = settings.upsert_automation({"name": "A", "prompt": "a",
                                          "schedule": {"kind": "manual"}})
        settings.upsert_automation({"name": "B", "prompt": "b",
                                    "schedule": {"kind": "manual"}})
        settings.record_run(one["id"], "all good")
        entries = {e["name"]: e for e in settings.load_automations()}
        self.assertEqual(entries["A"]["last_result"], "all good")
        self.assertIsNone(entries["B"]["last_run"])


class AccessToken(TempStateTest):

    def test_the_token_is_stable_once_issued(self):
        self.assertEqual(settings.access_token(), settings.access_token())

    def test_the_token_file_is_not_readable_by_anyone_else(self):
        settings.access_token()
        mode = stat.S_IMODE(os.stat(settings.TOKEN_FILE).st_mode)
        self.assertEqual(mode, 0o600, f"token file is {oct(mode)}")

    def test_rotating_issues_a_different_token(self):
        first = settings.access_token()
        self.assertNotEqual(settings.rotate_token(), first)

    def test_a_token_is_long_enough_to_be_worth_having(self):
        self.assertGreaterEqual(len(settings.access_token()), 24)


HUIHUI = "qwen3.5-9b-huihui-abliterated-Q4_K_M.gguf"
HAUHAU = "Qwen3.5-9B-Uncensored-HauhauCS-Aggressive-Q4_K_M.gguf"
HAUHAU_MMPROJ = "mmproj-Qwen3.5-9B-Uncensored-HauhauCS-Aggressive-BF16.gguf"


class Sampling(TempStateTest):
    """
    Reasoning turns are sampled differently from instruct ones — unless the
    profile says otherwise.

    These name the model rather than asking which one is picked: that used to
    read the real choice file, so the result depended on the machine.
    """

    def test_thinking_and_instruct_differ(self):
        cfg = settings.load_config()
        self.assertNotEqual(settings.sampling_for(cfg, True, HUIHUI),
                            settings.sampling_for(cfg, False, HUIHUI))

    def test_every_sampling_key_is_a_number(self):
        for path in (HUIHUI, HAUHAU, "something-new.gguf"):
            for thinking in (True, False):
                for key, value in settings.sampling_for(
                        settings.load_config(), thinking, path).items():
                    self.assertIsInstance(value, (int, float), f"{key}={value!r}")

    def test_a_fine_tune_is_not_tuned_as_its_base_model(self):
        # Both names start "qwen3.5-9b", and the first prefix to match wins.
        self.assertEqual(settings.profile_for(HAUHAU)["temperature"], 0.6)
        self.assertEqual(settings.profile_for(HUIHUI)["temperature"], 0.7)

    def test_min_p_is_sent_only_when_the_profile_asks_for_it(self):
        cfg = settings.load_config()
        self.assertEqual(settings.sampling_for(cfg, False, HAUHAU)["min_p"], 0.0)
        self.assertEqual(settings.sampling_for(cfg, True, HAUHAU)["min_p"], 0.0)
        self.assertNotIn("min_p", settings.sampling_for(cfg, False, HUIHUI))

    def test_a_profile_without_thinking_deltas_keeps_its_sampling(self):
        cfg = dict(settings.load_config(), temperature=0.6, top_p=0.95, top_k=20)
        self.assertEqual(settings.sampling_for(cfg, True, HAUHAU),
                         settings.sampling_for(cfg, False, HAUHAU))


class Models(TempStateTest):
    """What the model list offers, and what it says each one costs."""

    def place(self, name, size):
        path = os.path.join(settings.MODELS_DIR, name)
        with open(path, "wb") as handle:
            handle.truncate(size)
        return path

    def test_a_projector_is_not_offered_as_a_model(self):
        self.place(HAUHAU, 1000)
        self.place(HAUHAU_MMPROJ, 1000)
        self.assertEqual([m["name"] for m in settings.list_models()], [HAUHAU[:-5]])

    def test_a_projector_counts_toward_disk_and_memory(self):
        path = self.place(HAUHAU, 5_000_000_000)
        alone = settings.list_models()[0]
        self.assertFalse(alone["vision"])
        self.place(HAUHAU_MMPROJ, 1_000_000_000)
        paired = settings.list_models()[0]
        self.assertTrue(paired["vision"])
        self.assertEqual(settings.projector_for(path),
                         os.path.join(settings.MODELS_DIR, HAUHAU_MMPROJ))
        self.assertAlmostEqual(paired["gb"] - alone["gb"], 1.0, places=1)
        self.assertAlmostEqual(paired["est_gb"] - alone["est_gb"], 1.0, places=1)

    def test_another_models_projector_is_not_borrowed(self):
        path = self.place(HUIHUI, 1000)
        self.place(HAUHAU_MMPROJ, 1000)
        self.assertEqual(settings.projector_for(path), "")

    def test_a_projector_cannot_be_picked_as_the_model(self):
        self.assertFalse(settings.set_model(self.place(HAUHAU_MMPROJ, 1000)))
        self.assertTrue(settings.set_model(self.place(HAUHAU, 1000)))
        self.assertEqual(settings.load_config()["temperature"], 0.6)

    def test_model_sh_matches_the_profiles(self):
        # model.sh launches the server and config.py tunes the chat; the two
        # tables are kept by hand, and drifting apart is silent.
        script = os.path.join(os.path.dirname(settings.__file__), "model.sh")
        with open(script) as handle:
            text = handle.read()
        positions = [text.find(f"    {prefix}*)") for prefix in settings.MODEL_PROFILES]
        self.assertNotIn(-1, positions, "a profile with no case in model.sh")
        self.assertEqual(positions, sorted(positions), "listed in a different order")
        for profile in settings.MODEL_PROFILES.values():
            if profile.get("mmproj"):
                self.assertIn(f'MMPROJ_NAME="{profile["mmproj"]}"', text)



class NumbersAreNumbers(TempStateTest):
    """What a request is built from has to be usable, whatever the file says."""

    def test_a_blank_or_wrong_value_falls_back(self):
        settings._write(settings.CONFIG_FILE, {"max_tokens": None, "max_steps": "lots",
                                               "temperature": float("nan")})
        cfg = settings.load_config()
        self.assertEqual(cfg["max_tokens"], settings.DEFAULTS["max_tokens"])
        self.assertEqual(cfg["max_steps"], settings.DEFAULTS["max_steps"])
        self.assertEqual(cfg["temperature"], settings.DEFAULTS["temperature"])

    def test_a_number_written_as_text_is_read_as_one(self):
        settings._write(settings.CONFIG_FILE, {"max_tokens": "2000"})
        self.assertEqual(settings.load_config()["max_tokens"], 2000)

    def test_out_of_range_is_brought_into_range(self):
        settings._write(settings.CONFIG_FILE, {"max_steps": 0, "top_p": 7})
        cfg = settings.load_config()
        self.assertEqual(cfg["max_steps"], 1)
        self.assertEqual(cfg["top_p"], 1.0)

    def test_saving_answers_with_what_will_be_used(self):
        self.assertEqual(settings.save_config({"max_tokens": None})["max_tokens"],
                         settings.DEFAULTS["max_tokens"])

    def test_saving_leaves_no_temporary_files(self):
        for n in range(3):
            settings.save_config({"temperature": n / 10})
        self.assertEqual(sorted(os.listdir(settings.BASE)), ["config.json", "sessions"])


class BrokenSchedules(unittest.TestCase):

    def test_a_bad_interval_is_not_due_rather_than_an_exception(self):
        # Raised, it took every other automation down with it for that tick.
        entry = {"enabled": True, "schedule": {"kind": "interval", "minutes": "often"}}
        self.assertFalse(settings.is_due(entry))


if __name__ == "__main__":
    unittest.main()

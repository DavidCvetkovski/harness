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


class Sampling(unittest.TestCase):
    """Reasoning turns are sampled differently from instruct ones."""

    def test_thinking_and_instruct_differ(self):
        cfg = settings.load_config()
        self.assertNotEqual(settings.sampling_for(cfg, True),
                            settings.sampling_for(cfg, False))

    def test_every_sampling_key_is_a_number(self):
        for thinking in (True, False):
            for key, value in settings.sampling_for(settings.load_config(), thinking).items():
                self.assertIsInstance(value, (int, float), f"{key}={value!r}")


if __name__ == "__main__":
    unittest.main()

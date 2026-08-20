#!/usr/bin/env python3
"""Regression tests — `python3 scripts/test_reminders.py` (no deps, no remctl).

Every remctl call is stubbed, so this never touches real reminders and can
run anywhere. What's covered is deliberately skewed toward the things that
actually broke in review rather than toward line coverage: the date/all-day
handling, the Quick edit prefill/reparse round trip, and the config
defaults that decide whether a mutation gets confirmed at all.
"""
import datetime as dt
import sys
import unittest
from unittest import mock

import _remctl
from _remctl import (
    _normalize_all_day_due,
    normalize_date_phrase,
    split_implicit_due,
    today_reschedule_makes_sense,
)
from quick_add import escape_literal, notes_are_multiline, parse


class AllDayNormalization(unittest.TestCase):
    """remctl reports an all-day reminder as UTC midnight rendered as a
    naive *local* timestamp, i.e. dated a day early west of UTC. Values
    below are real ones captured from this machine and cross-checked
    against EventKit.
    """

    def test_edt_item_moves_to_true_date(self):
        item = {"allDay": True, "dueDate": "2026-08-20T20:00:00"}
        with mock.patch.object(_remctl.dt, "datetime", wraps=dt.datetime):
            _normalize_all_day_due(item)
        self.assertEqual(item["dueDate"][:10], "2026-08-21")

    def test_est_item_moves_to_true_date(self):
        item = {"allDay": True, "dueDate": "2026-11-25T19:00:00"}
        _normalize_all_day_due(item)
        self.assertEqual(item["dueDate"][:10], "2026-11-26")

    def test_normalized_to_local_midnight(self):
        item = {"allDay": True, "dueDate": "2026-08-20T20:00:00"}
        _normalize_all_day_due(item)
        self.assertTrue(item["dueDate"].endswith("T00:00:00"))

    def test_idempotent(self):
        item = {"allDay": True, "dueDate": "2026-08-20T20:00:00"}
        _normalize_all_day_due(item)
        once = item["dueDate"]
        _normalize_all_day_due(item)
        self.assertEqual(item["dueDate"], once)

    def test_timed_reminder_untouched(self):
        item = {"allDay": False, "dueDate": "2026-08-20T09:00:00"}
        _normalize_all_day_due(item)
        self.assertEqual(item["dueDate"], "2026-08-20T09:00:00")

    def test_non_string_due_does_not_raise(self):
        item = {"allDay": True, "dueDate": 1755}
        _normalize_all_day_due(item)
        self.assertEqual(item["dueDate"], 1755)

    def test_missing_due_does_not_raise(self):
        item = {"allDay": True}
        _normalize_all_day_due(item)
        self.assertIsNone(item.get("dueDate"))

    def test_payload_shapes(self):
        raw = "2026-08-20T20:00:00"
        as_list = _remctl._normalize_payload([{"allDay": True, "dueDate": raw}])
        as_wrapped = _remctl._normalize_payload({"items": [{"allDay": True, "dueDate": raw}]})
        as_single = _remctl._normalize_payload({"allDay": True, "dueDate": raw})
        for shape, payload in (("list", as_list[0]), ("wrapped", as_wrapped["items"][0]), ("single", as_single)):
            self.assertEqual(payload["dueDate"][:10], "2026-08-21", shape)

    def test_non_reminder_payloads_pass_through(self):
        lists = [{"title": "Work"}, {"title": "Home", "isGroup": True, "children": []}]
        self.assertEqual(_remctl._normalize_payload(lists), lists)


class TodayRescheduleEligibility(unittest.TestCase):
    def _at(self, delta_hours):
        return (dt.datetime.now() + dt.timedelta(hours=delta_hours)).replace(microsecond=0).isoformat()

    def test_future_time_allowed(self):
        self.assertTrue(today_reschedule_makes_sense({"dueDate": self._at(1), "allDay": False}))

    def test_past_time_refused(self):
        self.assertFalse(today_reschedule_makes_sense({"dueDate": self._at(-1), "allDay": False}))

    def test_all_day_always_allowed(self):
        self.assertTrue(today_reschedule_makes_sense({"dueDate": self._at(-5), "allDay": True}))

    def test_no_due_date_allowed(self):
        self.assertTrue(today_reschedule_makes_sense({"dueDate": None, "allDay": False}))

    def test_garbage_fails_open(self):
        self.assertTrue(today_reschedule_makes_sense({"dueDate": "not-a-date", "allDay": False}))

    def test_non_string_does_not_raise(self):
        self.assertTrue(today_reschedule_makes_sense({"dueDate": 1755, "allDay": False}))


class DatePhrases(unittest.TestCase):
    def test_out_of_range_times_rejected(self):
        for phrase in ("9/13 25:00", "9/13 99:99", "sep 9 13pm", "9/13 0:70"):
            self.assertNotIn(":", normalize_date_phrase(phrase).split(" ", 1)[-1], phrase)

    def test_valid_times_kept(self):
        self.assertTrue(normalize_date_phrase("9/13 9am").endswith(" 09:00"))
        self.assertTrue(normalize_date_phrase("9/13 14:30").endswith(" 14:30"))
        self.assertTrue(normalize_date_phrase("9/13 12am").endswith(" 00:00"))
        self.assertTrue(normalize_date_phrase("9/13 12pm").endswith(" 12:00"))

    def test_shorthand_expansion(self):
        self.assertEqual(normalize_date_phrase("tom 9am"), "tomorrow 9am")

    def test_empty_passthrough(self):
        self.assertEqual(normalize_date_phrase(""), "")


class ImplicitDueSplitting(unittest.TestCase):
    def test_trailing_anchor_split(self):
        self.assertEqual(split_implicit_due(["Buy", "milk", "tomorrow"]), (["Buy", "milk"], ["tomorrow"]))

    def test_bare_modifier_not_a_date(self):
        self.assertEqual(split_implicit_due(["Check", "in"]), (["Check", "in"], []))

    def test_bare_number_not_pulled_in(self):
        self.assertEqual(split_implicit_due(["Test", "2", "tomorrow"]), (["Test", "2"], ["tomorrow"]))

    def test_never_empties_the_title(self):
        title, _ = split_implicit_due(["tomorrow"])
        self.assertTrue(title)


class QuickEditRoundTrip(unittest.TestCase):
    """The Quick edit prefill is re-parsed with the same parser on confirm,
    so anything that doesn't survive the round trip is silently rewritten
    into the reminder — the source of most of this feature's bugs.
    """

    def _roundtrip(self, text):
        return parse(
            escape_literal(text),
            auto_detect_due=False,
            recognize_list=False,
            preserve_boundary_whitespace=True,
        )["title"]

    def test_marker_shaped_words_survive(self):
        for text in ("Email @alice", "Discuss #release", "Use !high", "Read notes:draft", "Run \\deploy"):
            self.assertEqual(self._roundtrip(text), text)

    def test_repeated_internal_whitespace_survives(self):
        self.assertEqual(self._roundtrip("Buy  milk   at store"), "Buy  milk   at store")

    def test_boundary_whitespace_survives(self):
        parsed = parse(
            "Title notes: " + escape_literal("  indented  "),
            auto_detect_due=False,
            recognize_list=False,
            preserve_boundary_whitespace=True,
        )
        self.assertEqual(parsed["notes"], "  indented  ")

    def test_tabs_and_newlines_agree_between_escape_and_parse(self):
        for sep in ("\t", "\n", " "):
            self.assertEqual(self._roundtrip(f"a{sep}#release"), "a #release")

    def test_trailing_day_word_not_stolen_as_due(self):
        parsed = parse("Review on Monday", auto_detect_due=False, recognize_list=False)
        self.assertEqual(parsed["title"], "Review on Monday")
        self.assertIsNone(parsed["due"])

    def test_freshly_typed_at_word_stays_literal(self):
        parsed = parse("Call @alice instead", auto_detect_due=False, recognize_list=False)
        self.assertEqual(parsed["title"], "Call @alice instead")

    def test_escaped_date_word_not_auto_detected(self):
        parsed = parse("Review \\Monday call bob next week")
        self.assertEqual(parsed["title"], "Review Monday call bob")
        self.assertEqual(parsed["due"], "next week")


class MultilineNotes(unittest.TestCase):
    def test_detection(self):
        self.assertTrue(notes_are_multiline("a\nb"))
        self.assertFalse(notes_are_multiline("a b"))
        self.assertFalse(notes_are_multiline(""))
        self.assertFalse(notes_are_multiline(None))

    def test_prefill_omits_multiline_notes(self):
        import list_reminders as lr
        info = {"title": "T", "tags": [], "priority": "none", "notes": "one\ntwo"}
        self.assertNotIn("notes:", lr._quick_edit_prefill(info))

    def test_prefill_keeps_single_line_notes(self):
        import list_reminders as lr
        info = {"title": "T", "tags": [], "priority": "none", "notes": "one line"}
        self.assertIn("notes:", lr._quick_edit_prefill(info))

    def test_execute_leaves_multiline_notes_alone(self):
        import reminder_action as ra
        calls = []

        def fake(args, json_output=True):
            calls.append(args)
            return {"notes": "a\nb\nc"} if args[0] == "info" else {}

        with mock.patch.object(ra, "run", fake):
            ra.execute_quick_edit("1", "Title")
        edit = [c for c in calls if c[0] == "edit"][0]
        self.assertNotIn("-n", edit)

    def test_execute_still_clears_single_line_notes(self):
        import reminder_action as ra
        calls = []

        def fake(args, json_output=True):
            calls.append(args)
            return {"notes": "one line"} if args[0] == "info" else {}

        with mock.patch.object(ra, "run", fake):
            ra.execute_quick_edit("1", "Title")
        edit = [c for c in calls if c[0] == "edit"][0]
        self.assertEqual(edit[edit.index("-n") + 1], "")


class ConfirmDefault(unittest.TestCase):
    """Confirmation is the guardrail in front of every mutation, so it has
    to fail *on*, including for a variable that exists but is blank.
    """

    def _enabled(self, value):
        import list_reminders as lr
        env = {} if value is None else {"CONFIRM_CHANGES": value}
        with mock.patch.dict("os.environ", env, clear=False):
            if value is None:
                sys.modules["os"].environ.pop("CONFIRM_CHANGES", None)
            return lr.confirm_enabled()

    def test_unset_defaults_on(self):
        self.assertTrue(self._enabled(None))

    def test_blank_stays_on(self):
        self.assertTrue(self._enabled(""))
        self.assertTrue(self._enabled("   "))

    def test_unrecognized_stays_on(self):
        self.assertTrue(self._enabled("maybe"))

    def test_explicit_off_values(self):
        for value in ("0", "false", "no", "FALSE", " No "):
            self.assertFalse(self._enabled(value), value)


class BinaryDiscovery(unittest.TestCase):
    def test_non_executable_path_rejected(self):
        with mock.patch.dict("os.environ", {"REMCTL_PATH": "/etc/hosts"}):
            self.assertNotEqual(_remctl.find_remctl(), "/etc/hosts")

    def test_run_reports_oserror_as_remctl_error(self):
        with mock.patch.object(_remctl, "find_remctl", lambda: "/etc/hosts"):
            with self.assertRaises(_remctl.RemctlError):
                _remctl.run(["today"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

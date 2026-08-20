#!/usr/bin/env python3
"""Run Script: executes the action chosen in list_reminders.py.

Reads `action` and `reminder_id` from the environment (set as Alfred
workflow variables by the triggering item/mod) and `{query}` as argv[1],
which is only meaningful for edit/reschedule/priority/quickedit (the text
typed, or the priority picked, in list_reminders.py's menu/text-entry/
picker modes). Clears the scope-fetch cache after any mutation so the
next `rem` keystroke reflects the change immediately.
"""
import datetime as dt
import glob
import os
import re
import sys

from _remctl import (
    CACHE_DIR,
    RemctlError,
    fetch_overdue_items,
    normalize_date_phrase,
    notify,
    run,
    today_reschedule_makes_sense,
)
from quick_add import notes_are_multiline, parse as parse_quick_add

_TAG_TOKEN_RE = re.compile(r"(?:(?<=\s)|^)#(\S+) ?")


def clear_cache():
    for path in glob.glob(os.path.join(CACHE_DIR, "*.json")):
        try:
            os.remove(path)
        except OSError:
            pass


def _due_preserving_time(item, target):
    """Only the *day* moves — a reminder due at 9am stays due at 9am, just
    on `target` ("today" or "tomorrow") instead of wherever it was
    scheduled before. An all-day reminder (no specific time) stays
    all-day. remctl accepts "today HH:MM" / "tomorrow HH:MM" directly
    (verified against `remctl add -d`), so the original time is read
    straight off dueDate and reattached rather than dropped — passing a
    bare "today"/"tomorrow" would silently strip any existing time and
    turn a timed reminder into an all-day one. Used both for the overdue
    bulk-reschedule action and the single-reminder "Reschedule to
    today"/"tomorrow" shortcuts on the View details screen.
    """
    if item.get("allDay"):
        return target
    due_iso = item.get("dueDate")
    if not due_iso:
        return target
    try:
        return f"{target} {dt.datetime.fromisoformat(due_iso).strftime('%H:%M')}"
    except ValueError:
        return target


def bulk_reschedule_overdue(target):
    """Fetches the overdue set fresh at execution time (not whatever was
    overdue when the confirm screen rendered — the two can drift by
    however long the user took to read and confirm) and reschedules every
    one of them to `target` ("today" or "tomorrow"), preserving each
    reminder's own time of day. One reminder failing doesn't stop the
    rest; failures are collected and reported together.
    """
    # cached=False for the same reason this was a bare run() before: the
    # set is re-derived at execution time, not inherited from whatever the
    # confirm screen happened to show. Includes reminders due earlier
    # today, which `remctl overdue` alone leaves out — see
    # fetch_overdue_items().
    items = fetch_overdue_items(cached=False)
    if not items:
        notify("Reminders", "No overdue reminders to reschedule.")
        return

    succeeded = 0
    failures = []
    for item in items:
        reminder_id = str(item.get("id"))
        try:
            run(["edit", reminder_id, "-d", _due_preserving_time(item, target)], json_output=False)
            succeeded += 1
        except RemctlError as exc:
            failures.append(f'{item.get("title") or reminder_id}: {exc}')

    if failures:
        detail = "; ".join(failures[:3])
        if len(failures) > 3:
            detail += f"; +{len(failures) - 3} more"
        notify(
            "Reminders — bulk reschedule",
            f"Rescheduled {succeeded} to {target}, {len(failures)} failed ({detail})",
        )
    else:
        plural = "s" if succeeded != 1 else ""
        notify("Reminders", f"Rescheduled {succeeded} overdue reminder{plural} to {target}")


def execute_quick_edit(reminder_id, typed_text):
    """Applies title/tags/priority/due/notes together from one line of
    remadd-style syntax, in a single `remctl edit` call. A marker's
    absence means that field is explicitly cleared (`-d clear`, `-p
    none`, `--clear-tags`, `-n ""`), not left alone — see
    render_quick_edit()'s docstring in list_reminders.py for why that's
    safe: the line starts pre-filled with the current state, so an
    absent marker here means it was deliberately deleted on screen.
    """
    # auto_detect_due=False, recognize_list=False, and
    # preserve_boundary_whitespace=True must all match
    # render_quick_edit()'s preview in list_reminders.py — otherwise what
    # the confirm step showed and what actually gets applied could
    # disagree. See that function's comments for why each is needed.
    parsed = parse_quick_add(
        typed_text, auto_detect_due=False, recognize_list=False, preserve_boundary_whitespace=True
    )
    if not parsed["title"]:
        fail("No title — quick edit cancelled.")

    args = [
        "edit", reminder_id,
        "--title", parsed["title"],
        "-d", normalize_date_phrase(parsed["due"]) if parsed["due"] else "clear",
        "-p", parsed["priority"] or "none",
        "--private",
    ]
    # A multi-line note is deliberately left out of the prefill (see
    # notes_are_multiline()), so "no notes: marker in the text" doesn't
    # mean "the user deleted it" here the way it does for every other
    # field — it means the note was never representable on this screen at
    # all. Omitting -n entirely leaves the stored note untouched; passing
    # -n "" (the normal clear-what's-absent behavior) would wipe it. The
    # check is recomputed from live state rather than trusted from render
    # time, so the two sides can't drift.
    #
    # A failed lookup aborts rather than assuming "no notes". Swallowing
    # the error and defaulting to "" would treat a multi-line note as
    # absent and send -n "", wiping it — reintroducing exactly the data
    # loss the multi-line rule exists to prevent, in the one path where
    # the user has no way to see it coming. Nothing has been written at
    # this point, so aborting leaves the reminder untouched and the user
    # can retry; every other field's semantics are unambiguous, but
    # applying four of five fields silently is still a partial edit
    # nobody asked for.
    try:
        current_notes = (run(["info", reminder_id], json_output=True) or {}).get("notes") or ""
    except RemctlError as exc:
        fail("Couldn't read the reminder's current notes — quick edit cancelled.", str(exc))
    if not notes_are_multiline(current_notes):
        args += ["-n", parsed["notes"] or ""]
    args += ["--set-tags", ",".join(parsed["tags"])] if parsed["tags"] else ["--clear-tags"]
    run(args, json_output=False)


def extract_tags(text):
    """Pull #tag tokens out of edited-title text so they become real synced
    tags (via --private -t) instead of literal "#tag" characters left in
    the title — plain `edit --title` never auto-converts hashtag-looking
    text into a tag, verified directly against a real reminder.

    Removes only the matched "#tag " spans from the original string rather
    than splitting on whitespace and rejoining with single spaces, which
    would silently collapse any repeated whitespace elsewhere in the title
    even when no tag is present at all.
    """
    tags = []

    def _consume(match):
        tags.append(match.group(1))
        return ""

    new_title = _TAG_TOKEN_RE.sub(_consume, text).strip()
    return new_title, tags


def fail(message, detail=""):
    """Abort with something the user can actually see.

    This workflow has no Post Notification object wired into either Run
    Script — the whole graph is two Script Filters feeding two Run Scripts
    — so writing to stderr and exiting non-zero produces *no* visible
    feedback at all: the action simply appears to do nothing, with the
    explanation buried in Alfred's debug console. Every abort therefore
    posts a notification itself (the same way quick_add.py already did for
    remadd's failures) and then still writes to stderr for the debugger.
    """
    notify("Reminders", message)
    print(f"{message}\n{detail}".strip(), file=sys.stderr)
    sys.exit(1)


BULK_TARGETS = ("today", "tomorrow")


def main():
    action = os.environ.get("action")
    reminder_id = os.environ.get("reminder_id")
    typed_text = sys.argv[1] if len(sys.argv) > 1 else ""

    # Bulk actions operate on a whole scope, not one reminder_id — handled
    # before the reminder_id check below, which every other action needs.
    if action == "bulk_reschedule_overdue":
        # Only the two values the UI can actually produce. This rides in
        # as a workflow variable, and it's interpolated straight into a
        # remctl date argument for *every* overdue reminder — the one
        # place in the workflow where a bad value would be applied in
        # bulk, so it's worth refusing outright rather than letting
        # remctl interpret whatever arrives.
        target = (os.environ.get("target") or "today").strip().lower()
        if target not in BULK_TARGETS:
            fail(f"Unknown reschedule target “{target}” — expected today or tomorrow.")
        try:
            bulk_reschedule_overdue(target)
        except RemctlError as exc:
            fail(f"Bulk reschedule failed: {exc}", exc.stderr)
        clear_cache()
        return

    if not reminder_id:
        fail("Missing reminder_id — action aborted.")

    try:
        if action == "open":
            run(["open", reminder_id], json_output=False)
        elif action == "done":
            run(["done", reminder_id], json_output=False)
        elif action == "edit":
            if not typed_text:
                fail("No title entered — edit cancelled.")
            new_title, tags = extract_tags(typed_text)
            if not new_title:
                fail("No title text (only tags) — edit cancelled.")
            args = ["edit", reminder_id, "--title", new_title]
            if tags:
                args += ["--private", "-t", ",".join(tags)]
            run(args, json_output=False)
        elif action == "reschedule":
            if not typed_text:
                fail("No date entered — reschedule cancelled.")
            run(["edit", reminder_id, "-d", normalize_date_phrase(typed_text)], json_output=False)
        elif action in ("reschedule_today", "reschedule_tomorrow"):
            target = "today" if action == "reschedule_today" else "tomorrow"
            info = run(["info", reminder_id], json_output=True)
            # Revalidated here, not just when list_reminders.py rendered
            # the "Today" option — CONFIRM_CHANGES adds a real review
            # step between picking it and executing, long enough for a
            # time-of-day that was still in the future at render time to
            # have passed by now. Executing anyway would immediately
            # bounce the reminder back to overdue, the exact trap this
            # was meant to prevent — so abort instead of silently
            # rescheduling to an already-elapsed time.
            if target == "today" and not today_reschedule_makes_sense(info):
                fail("That time has already passed today — reschedule to tomorrow instead.")
            run(["edit", reminder_id, "-d", _due_preserving_time(info, target)], json_output=False)
        elif action == "flag":
            # `remctl flag <id>` (AppleScript UI automation) needs
            # Reminders.app frontmost to respond at all — verified
            # directly: it times out entirely when the app is merely
            # running in the background, and only completes (in ~3.5s)
            # once activated to the front. `edit --private --flagged` uses
            # EventKit's private-metadata path instead, no app-frontmost
            # dependency, and is reliably fast (~0.2s).
            run(["edit", reminder_id, "--private", "--flagged"], json_output=False)
        elif action == "unflag":
            run(["edit", reminder_id, "--private", "--no-flagged"], json_output=False)
        elif action == "priority":
            if not typed_text:
                fail("No priority chosen — priority change cancelled.")
            run(["edit", reminder_id, "-p", typed_text], json_output=False)
        elif action == "quickedit":
            if not typed_text:
                fail("Nothing typed — quick edit cancelled.")
            execute_quick_edit(reminder_id, typed_text)
        else:
            fail(f"Unknown action: {action}")
    except RemctlError as exc:
        fail(f"{action} failed: {exc}", exc.stderr)

    clear_cache()


if __name__ == "__main__":
    main()

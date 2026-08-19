#!/usr/bin/env python3
"""Run Script: executes the action chosen in list_reminders.py.

Reads `action` and `reminder_id` from the environment (set as Alfred
workflow variables by the triggering item/mod) and `{query}` as argv[1],
which is only meaningful for edit/reschedule/move (the text typed, or the
list name picked, in list_reminders.py's menu/text-entry/picker modes).
Clears the scope-fetch cache after any mutation so the next `rem`
keystroke reflects the change immediately.
"""
import datetime as dt
import glob
import os
import re
import subprocess
import sys

from _remctl import CACHE_DIR, RemctlError, items_from, normalize_date_phrase, run

_TAG_TOKEN_RE = re.compile(r"(?:(?<=\s)|^)#(\S+) ?")


def clear_cache():
    for path in glob.glob(os.path.join(CACHE_DIR, "*.json")):
        try:
            os.remove(path)
        except OSError:
            pass


def notify(title, subtitle):
    script = f'display notification {subtitle!r} with title {title!r}'
    subprocess.run(["osascript", "-e", script], capture_output=True)


def _due_for_bulk_reschedule(item, target):
    """Only the *day* moves — a reminder due at 9am stays due at 9am, just
    on `target` instead of whenever it was overdue from. An all-day
    reminder (no specific time) stays all-day. remctl accepts "today
    HH:MM" / "tomorrow HH:MM" directly (verified against `remctl add -d`),
    so the original time is read straight off dueDate and reattached
    rather than dropped — passing a bare "today"/"tomorrow" would silently
    strip any existing time and turn a timed reminder into an all-day one.
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
    payload = run(["overdue"], json_output=True)
    items = items_from(payload)
    if not items:
        notify("Reminders", "No overdue reminders to reschedule.")
        return

    succeeded = 0
    failures = []
    for item in items:
        reminder_id = str(item.get("id"))
        try:
            run(["edit", reminder_id, "-d", _due_for_bulk_reschedule(item, target)], json_output=False)
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


def main():
    action = os.environ.get("action")
    reminder_id = os.environ.get("reminder_id")
    typed_text = sys.argv[1] if len(sys.argv) > 1 else ""

    # Bulk actions operate on a whole scope, not one reminder_id — handled
    # before the reminder_id check below, which every other action needs.
    if action == "bulk_reschedule_overdue":
        target = os.environ.get("target") or "today"
        try:
            bulk_reschedule_overdue(target)
        except RemctlError as exc:
            print(f"{exc}\n{exc.stderr}", file=sys.stderr)
            sys.exit(1)
        clear_cache()
        return

    if not reminder_id:
        print("Missing reminder_id — action aborted.", file=sys.stderr)
        sys.exit(1)

    try:
        if action == "open":
            run(["open", reminder_id], json_output=False)
        elif action == "done":
            run(["done", reminder_id], json_output=False)
        elif action == "edit":
            if not typed_text:
                print("No title entered — edit cancelled.", file=sys.stderr)
                sys.exit(1)
            new_title, tags = extract_tags(typed_text)
            if not new_title:
                print("No title text (only tags) — edit cancelled.", file=sys.stderr)
                sys.exit(1)
            args = ["edit", reminder_id, "--title", new_title]
            if tags:
                args += ["--private", "-t", ",".join(tags)]
            run(args, json_output=False)
        elif action == "reschedule":
            if not typed_text:
                print("No date entered — reschedule cancelled.", file=sys.stderr)
                sys.exit(1)
            run(["edit", reminder_id, "-d", normalize_date_phrase(typed_text)], json_output=False)
        elif action == "move":
            if not typed_text:
                print("No list chosen — move cancelled.", file=sys.stderr)
                sys.exit(1)
            run(["edit", reminder_id, "-l", typed_text], json_output=False)
        else:
            print(f"Unknown action: {action}", file=sys.stderr)
            sys.exit(1)
    except RemctlError as exc:
        print(f"{exc}\n{exc.stderr}", file=sys.stderr)
        sys.exit(1)

    clear_cache()


if __name__ == "__main__":
    main()

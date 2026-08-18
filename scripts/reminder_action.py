#!/usr/bin/env python3
"""Run Script: executes the action chosen in list_reminders.py.

Reads `action` and `reminder_id` from the environment (set as Alfred
workflow variables by the triggering item/mod) and `{query}` as argv[1],
which is only meaningful for edit/reschedule/move (the text typed, or the
list name picked, in list_reminders.py's menu/text-entry/picker modes).
Clears the scope-fetch cache after any mutation so the next `rem`
keystroke reflects the change immediately.
"""
import glob
import os
import sys

from _remctl import CACHE_DIR, RemctlError, normalize_date_phrase, run


def clear_cache():
    for path in glob.glob(os.path.join(CACHE_DIR, "*.json")):
        try:
            os.remove(path)
        except OSError:
            pass


def extract_tags(text):
    """Pull #tag tokens out of edited-title text so they become real synced
    tags (via --private -t) instead of literal "#tag" characters left in
    the title — plain `edit --title` never auto-converts hashtag-looking
    text into a tag, verified directly against a real reminder.
    """
    words = text.split()
    tags = [w[1:] for w in words if w.startswith("#") and len(w) > 1]
    title_words = [w for w in words if not (w.startswith("#") and len(w) > 1)]
    return " ".join(title_words).strip(), tags


def main():
    action = os.environ.get("action")
    reminder_id = os.environ.get("reminder_id")
    typed_text = sys.argv[1] if len(sys.argv) > 1 else ""

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

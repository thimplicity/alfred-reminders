#!/usr/bin/env python3
"""Run Script: executes the action chosen in list_reminders.py.

Reads `action` and `reminder_id` from the environment (set as Alfred
workflow variables by the triggering item/mod) and `{query}` as argv[1],
which is only meaningful for edit/reschedule (the text typed into
prompt_for_text.py). Clears the scope-fetch cache after any mutation so the
next `rem` keystroke reflects the change immediately.
"""
import glob
import os
import sys

from _remctl import CACHE_DIR, RemctlError, run


def clear_cache():
    for path in glob.glob(os.path.join(CACHE_DIR, "*.json")):
        try:
            os.remove(path)
        except OSError:
            pass


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
        elif action == "delete":
            run(["delete", reminder_id, "--force"], json_output=False)
        elif action == "edit":
            if not typed_text:
                print("No title entered — edit cancelled.", file=sys.stderr)
                sys.exit(1)
            run(["edit", reminder_id, "--title", typed_text], json_output=False)
        elif action == "reschedule":
            if not typed_text:
                print("No date entered — reschedule cancelled.", file=sys.stderr)
                sys.exit(1)
            run(["edit", reminder_id, "-d", typed_text], json_output=False)
        else:
            print(f"Unknown action: {action}", file=sys.stderr)
            sys.exit(1)
    except RemctlError as exc:
        print(f"{exc}\n{exc.stderr}", file=sys.stderr)
        sys.exit(1)

    clear_cache()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Second-stage Script Filter: free-text entry for edit/reschedule.

Reached only via the Option (edit) or Control (reschedule) connections out
of list_reminders.py, which set the `action` and `reminder_id`/
`reminder_title` workflow variables before routing here. This script does
not call remctl — it just echoes back what the user is typing as a single
confirmable item, and Return hands the typed text to reminder_action.py.
"""
import json
import os
import sys

ACTION_LABEL = {
    "edit": "new title",
    "reschedule": "new due date (e.g. tomorrow 9am, next friday, clear)",
}


def main():
    text = sys.argv[1] if len(sys.argv) > 1 else ""
    action = os.environ.get("action", "edit")
    title = os.environ.get("reminder_title", "this reminder")

    prompt = ACTION_LABEL.get(action, "new value")
    item = {
        "title": text if text else f"Type {prompt}…",
        "subtitle": f"↩ to confirm — {action} on “{title}”",
        "arg": text,
        "valid": bool(text),
        "variables": {
            "action": action,
            "reminder_id": os.environ.get("reminder_id", ""),
            "reminder_title": title,
        },
    }
    print(json.dumps({"items": [item]}))


if __name__ == "__main__":
    main()

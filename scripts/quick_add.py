#!/usr/bin/env python3
"""Run Script backend for the `remadd` keyword.

Syntax (tokens may appear in any order):
  remadd <title words...> [#List] [@tag ...] [!priority] [due:<phrase>] [notes:<text>]

Examples:
  remadd Buy milk #Groceries due:tomorrow 9am
  remadd Ship notes #Work !high due:friday 3pm @errand
  remadd Pay rent due:2026-06-01 notes:autopay is off this month

`due:`/`notes:` phrases run to the end of the query (or until the next
recognized token) rather than being NLP-guessed apart from the title, since
that's unambiguous to parse correctly. Priority accepts high/medium/low or
h/m/l. Tags are appended as plain #hashtags in the title unless the workflow
is later extended to pass --private (see README).
"""
import subprocess
import sys

from _remctl import RemctlError, run

PRIORITY_MAP = {
    "h": "high", "high": "high",
    "m": "medium", "medium": "medium",
    "l": "low", "low": "low",
}


def notify(title, subtitle):
    script = f'display notification {subtitle!r} with title {title!r}'
    subprocess.run(["osascript", "-e", script], capture_output=True)


def parse(query):
    title_words, due_words, notes_words, tags = [], [], [], []
    list_name = priority = None
    mode = None  # None | 'due' | 'notes'

    for tok in query.split():
        low = tok.lower()
        if tok.startswith("#") and len(tok) > 1:
            mode = None
            list_name = tok[1:]
            continue
        if tok.startswith("@") and len(tok) > 1:
            mode = None
            tags.append(tok[1:])
            continue
        if tok.startswith("!") and low[1:] in PRIORITY_MAP:
            mode = None
            priority = PRIORITY_MAP[low[1:]]
            continue
        if low.startswith("due:"):
            mode = "due"
            rest = tok[4:]
            if rest:
                due_words.append(rest)
            continue
        if low.startswith("notes:"):
            mode = "notes"
            rest = tok[6:]
            if rest:
                notes_words.append(rest)
            continue
        if mode == "due":
            due_words.append(tok)
        elif mode == "notes":
            notes_words.append(tok)
        else:
            title_words.append(tok)

    return {
        "title": " ".join(title_words).strip(),
        "list": list_name,
        "tags": tags,
        "priority": priority,
        "due": " ".join(due_words).strip() or None,
        "notes": " ".join(notes_words).strip() or None,
    }


def main():
    query = sys.argv[1] if len(sys.argv) > 1 else ""
    parsed = parse(query)

    if not parsed["title"]:
        notify("Reminders", "No title given — nothing added.")
        sys.exit(1)

    args = ["add", parsed["title"]]
    if parsed["list"]:
        args += ["-l", parsed["list"]]
    if parsed["due"]:
        args += ["-d", parsed["due"]]
    if parsed["priority"]:
        args += ["-p", parsed["priority"]]
    if parsed["tags"]:
        args += ["-t", ",".join(parsed["tags"])]
    if parsed["notes"]:
        args += ["-n", parsed["notes"]]

    try:
        run(args, json_output=False)
    except RemctlError as exc:
        notify("Reminders — failed to add", str(exc))
        print(f"{exc}\n{exc.stderr}", file=sys.stderr)
        sys.exit(1)

    where = f' in {parsed["list"]}' if parsed["list"] else ""
    when = f' ({parsed["due"]})' if parsed["due"] else ""
    notify("Reminder added", f'{parsed["title"]}{where}{when}')


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Run Script backend for the `remadd` keyword.

Syntax (tokens may appear in any order):
  remadd <title words...> [@List] [#tag ...] [!priority] [<due phrase>] [notes:<text>]

Examples:
  remadd Buy milk @Groceries tomorrow 9am
  remadd Buy milk @Groceries tom 9am
  remadd Ship notes @Work !high friday 3pm #errand
  remadd Pay rent /2026-06-01 notes:autopay is off this month

A trailing due-date phrase is auto-detected (tomorrow/tom/mon/next
friday/9am/2026-06-01/+3d/...) without needing any marker at all — see
`looks_like_due_token()` in _remctl.py for exactly what's recognized. This
is a heuristic: a title that happens to end in a word like "Monday" will
get parsed as a due date. Use an explicit `/<phrase>` prefix (or the
longer `due:<phrase>`, still supported — both run to the end of the
query) to disambiguate. `notes:` works the same way and is never
auto-detected, since free text can't be told apart from a due phrase by
pattern alone.
"""
import subprocess
import sys

from _remctl import RemctlError, normalize_date_phrase, run, split_implicit_due

PRIORITY_MAP = {
    "h": "high", "high": "high",
    "m": "medium", "medium": "medium",
    "l": "low", "low": "low",
}


def notify(title, subtitle):
    script = f'display notification {subtitle!r} with title {title!r}'
    subprocess.run(["osascript", "-e", script], capture_output=True)


def parse(query):
    plain_tokens, due_words, notes_words, tags = [], [], [], []
    list_name = priority = None
    mode = None  # None | 'due' | 'notes'
    explicit_due = False

    for tok in query.split():
        low = tok.lower()
        if tok.startswith("@") and len(tok) > 1:
            mode = None
            list_name = tok[1:]
            continue
        if tok.startswith("#") and len(tok) > 1:
            mode = None
            tags.append(tok[1:])
            continue
        if tok.startswith("!") and low[1:] in PRIORITY_MAP:
            mode = None
            priority = PRIORITY_MAP[low[1:]]
            continue
        if low.startswith("due:") or (tok.startswith("/") and len(tok) > 1):
            mode = "due"
            explicit_due = True
            rest = tok[4:] if low.startswith("due:") else tok[1:]
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
            plain_tokens.append(tok)

    if not explicit_due and plain_tokens:
        plain_tokens, implicit_due = split_implicit_due(plain_tokens)
        due_words = implicit_due + due_words

    return {
        "title": " ".join(plain_tokens).strip(),
        "list": list_name,
        "tags": tags,
        "priority": priority,
        "due": normalize_date_phrase(" ".join(due_words).strip()) or None,
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

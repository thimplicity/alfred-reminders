#!/usr/bin/env python3
"""Alfred Script Filter backend for the `remadd` keyword.

Alfred re-invokes this script on every keystroke, so it can offer live
completion for the last word being typed:

  ...#<partial>   -> matching known tags, Tab/Return picks one and keeps typing
  ...@<partial>   -> matching real lists (single-word names only, same
                      limitation as quick_add.py's -l shorthand), same deal
  ...!<partial>   -> high/medium/low, same deal
  (anything else) -> a live preview of what quick_add.py's parser sees so
                      far, valid so Return adds the reminder at any point —
                      same "just press Return" behavior as before this
                      script existed, now with the completions layered on

Completion only kicks in for the *last* whitespace-delimited token, and
only when the query doesn't end in a space (i.e. that token is still being
typed) — once a space closes it, it's just part of the title/tags/etc.
again and normal preview mode resumes. Each completion's `autocomplete`
value is the full query with only that last token replaced, so everything
typed before it is preserved and typing continues right after.

The actual reminder creation is unchanged: this Script Filter's one output
item in preview mode carries `arg` = the full query text, connected
straight through to quick_add.py exactly as when `remadd` was a plain
Keyword Input.
"""
import json
import sys

from _remctl import RemctlError, fetch_known_tags, fetch_list_and_smart_list_names
from quick_add import parse

PRIORITY_CHOICES = ["high", "medium", "low"]


def replace_last_token(query, new_token):
    """Swap the token currently being typed for `new_token`, keeping
    everything before it and adding a trailing space to continue typing.
    """
    if " " not in query:
        return f"{new_token} "
    prefix, _ = query.rsplit(" ", 1)
    return f"{prefix} {new_token} "


def current_partial_token(query):
    """Returns (kind, partial) for the token being typed right now, or
    (None, None) if the query is empty, ends in a space (last token is
    already closed), or the last token isn't a #/@/! shorthand.
    """
    if not query or query.endswith(" "):
        return None, None
    last = query.rsplit(" ", 1)[-1]
    if last.startswith("#"):
        return "tag", last[1:]
    if last.startswith("@"):
        return "list", last[1:]
    if last.startswith("!"):
        return "priority", last[1:]
    return None, None


def render_tag_completion(query, partial):
    tags = fetch_known_tags()
    needle = partial.lower()
    matches = sorted(t for t in tags if needle in t.lower())
    if not matches:
        return {"items": [{
            "title": f'No existing tag matches "{partial}"' if partial else "No existing tags yet",
            "subtitle": "Keep typing to use it as a new tag, or continue with the rest of the reminder",
            "valid": False,
        }]}
    return {"items": [
        {
            "title": f"#{tag}",
            "subtitle": "Tab or Return to use this tag and keep typing",
            "valid": False,
            "autocomplete": replace_last_token(query, f"#{tag}"),
        }
        for tag in matches
    ]}


def render_list_completion(query, partial):
    entries = fetch_list_and_smart_list_names()
    # Single-word real lists only — matches quick_add.py's own @List
    # limitation (multi-word list names aren't representable inline).
    real_lists = [name for name, kind in entries if kind == "List" and " " not in name]
    needle = partial.lower()
    matches = sorted(n for n in real_lists if needle in n.lower())
    if not matches:
        return {"items": [{
            "title": f'No list matches "{partial}"' if partial else "No single-word lists found",
            "subtitle": "Keep typing, or check the name in Reminders.app",
            "valid": False,
        }]}
    return {"items": [
        {
            "title": name,
            "subtitle": "Tab or Return to pick this list and keep typing",
            "valid": False,
            "autocomplete": replace_last_token(query, f"@{name}"),
        }
        for name in matches
    ]}


def render_priority_completion(query, partial):
    needle = partial.lower()
    matches = [p for p in PRIORITY_CHOICES if p.startswith(needle)] if needle else PRIORITY_CHOICES
    if not matches:
        return {"items": [{
            "title": f'No priority matches "{partial}"',
            "subtitle": "Try high, medium, or low",
            "valid": False,
        }]}
    return {"items": [
        {
            "title": f"!{p}",
            "subtitle": f"{p.capitalize()} priority — Tab or Return to pick and keep typing",
            "valid": False,
            "autocomplete": replace_last_token(query, f"!{p}"),
        }
        for p in matches
    ]}


def render_preview(query):
    if not query.strip():
        return {"items": [{
            "title": "Type a reminder title…",
            "subtitle": "Add @List, #tag, !priority, and a due date anywhere — e.g. Buy milk @Groceries tomorrow 9am",
            "valid": False,
        }]}

    parsed = parse(query)
    if not parsed["title"]:
        return {"items": [{
            "title": "No title yet",
            "subtitle": "Keep typing a title for the reminder",
            "valid": False,
        }]}

    meta = []
    if parsed["list"]:
        meta.append(f"@{parsed['list']}")
    if parsed["tags"]:
        meta.append(" ".join(f"#{t}" for t in parsed["tags"]))
    if parsed["priority"]:
        meta.append(f"!{parsed['priority']}")
    if parsed["due"]:
        meta.append(f"due {parsed['due']}")
    if parsed["notes"]:
        meta.append(f"notes: {parsed['notes']}")
    subtitle = "↩ to add" + (" — " + "  ·  ".join(meta) if meta else "")

    return {"items": [{
        "title": parsed["title"],
        "subtitle": subtitle,
        "arg": query,
        "valid": True,
    }]}


def main():
    query = sys.argv[1] if len(sys.argv) > 1 else ""

    kind, partial = current_partial_token(query)
    try:
        if kind == "tag":
            result = render_tag_completion(query, partial)
        elif kind == "list":
            result = render_list_completion(query, partial)
        elif kind == "priority":
            result = render_priority_completion(query, partial)
        else:
            result = render_preview(query)
    except RemctlError as exc:
        result = {"items": [{"title": "remctl error", "subtitle": str(exc), "valid": False}]}

    print(json.dumps(result))


if __name__ == "__main__":
    main()

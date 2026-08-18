#!/usr/bin/env python3
"""Alfred Script Filter backend for the `rem` keyword.

Query grammar (all optional, space separated):
  rem                     -> due today + overdue (remctl today)
  rem <free text>         -> full-text search across all lists (remctl search)
  rem #ListName           -> everything in one list (remctl show ListName)
  rem #ListName <text>    -> that list, locally filtered by <text>
  rem all                 -> every open reminder across every list
  rem all <text>          -> same, locally filtered by <text>
  rem upcoming [N]        -> due within N days (default 7)
  rem flagged             -> flagged reminders
  rem overdue             -> overdue only (no "due today" items)

Row modifiers are expressed as Alfred item "mods", not separate Script
Filter branches: Return=open, Shift=complete, Option=edit title,
Control=reschedule, Ctrl+Option+Cmd=delete. Edit/reschedule route (via the
workflow's connections, not this script) to prompt_for_text.py for the
follow-up text entry.
"""
import datetime as dt
import json
import sys

from _remctl import RemctlError, cached_run, items_from, run

CACHE_TTL = 5  # seconds; only applies to scope-level fetches, not free text


def flatten_lists(payload):
    """`remctl lists --json` returns group rows (isGroup: true) interleaved
    with plain list rows; both use "title" for the display name. Group rows
    carry a "children" array (possibly of child dicts or child names) —
    recurse into it when present, otherwise skip the group itself, since it
    isn't something `show` can target directly.
    """
    names = []
    for entry in payload if isinstance(payload, list) else []:
        if not isinstance(entry, dict):
            continue
        if entry.get("isGroup"):
            for child in entry.get("children") or []:
                if isinstance(child, dict) and child.get("title"):
                    names.append(child["title"])
                elif isinstance(child, str):
                    names.append(child)
            continue
        if entry.get("title"):
            names.append(entry["title"])
    return names


def fetch_scope(query_tokens):
    """Returns (items, remaining_free_text)."""
    if not query_tokens:
        payload = cached_run("scope:today", ["today"], ttl=CACHE_TTL)
        return items_from(payload), ""

    first = query_tokens[0]

    if first.startswith("#") and len(first) > 1:
        list_name = first[1:]
        payload = cached_run(f"scope:list:{list_name}", ["show", list_name], ttl=CACHE_TTL)
        return items_from(payload), " ".join(query_tokens[1:])

    if first == "all":
        lists_payload = cached_run("scope:lists", ["lists"], ttl=CACHE_TTL)
        all_items = []
        for name in flatten_lists(lists_payload):
            try:
                payload = cached_run(f"scope:list:{name}", ["show", name], ttl=CACHE_TTL)
            except RemctlError:
                continue
            all_items.extend(items_from(payload))
        return all_items, " ".join(query_tokens[1:])

    if first == "upcoming":
        rest = query_tokens[1:]
        days = "7"
        if rest and rest[0].isdigit():
            days = rest[0]
            rest = rest[1:]
        payload = cached_run(f"scope:upcoming:{days}", ["upcoming", days], ttl=CACHE_TTL)
        return items_from(payload), " ".join(rest)

    if first == "flagged":
        payload = cached_run("scope:flagged", ["flagged"], ttl=CACHE_TTL)
        return items_from(payload), " ".join(query_tokens[1:])

    if first == "overdue":
        payload = cached_run("scope:overdue", ["overdue"], ttl=CACHE_TTL)
        return items_from(payload), " ".join(query_tokens[1:])

    # No recognized scope keyword: treat the whole query as a full-text
    # search term. remctl does the searching remotely (title + notes,
    # across all lists), so no local filtering is layered on top.
    text = " ".join(query_tokens)
    payload = run(["search", text], json_output=True)
    return items_from(payload), ""


def local_filter(items, text):
    if not text:
        return items
    needle = text.lower()
    return [i for i in items if needle in (i.get("title") or "").lower()]


def humanize_due(item):
    raw = item.get("dueDate") or item.get("displayDate")
    if not raw:
        return "No due date"
    try:
        due = dt.datetime.fromisoformat(raw)
    except ValueError:
        return raw

    now = dt.datetime.now()
    today = now.date()
    due_date = due.date()
    has_time = not (due.hour == 0 and due.minute == 0 and due.second == 0)
    time_str = due.strftime("%-I:%M %p") if has_time else ""

    if due_date == today:
        label = "Today"
    elif due_date == today - dt.timedelta(days=1):
        label = "Yesterday"
    elif due_date == today + dt.timedelta(days=1):
        label = "Tomorrow"
    elif due_date < today:
        label = due.strftime("Overdue: %b %-d")
    else:
        label = due.strftime("%b %-d")

    return f"{label} {time_str}".strip()


PRIORITY_MARK = {"high": "!!!", "medium": "!!", "low": "!"}


def build_subtitle(item):
    parts = [item.get("list") or "?", humanize_due(item)]
    prio = PRIORITY_MARK.get((item.get("priority") or "").lower())
    if prio:
        parts.append(prio)
    if item.get("flagged"):
        parts.append("⚑")
    if item.get("notes"):
        parts.append("…")
    return "  ·  ".join(parts)


def build_item(item):
    reminder_id = str(item.get("id"))
    title = item.get("title") or "(untitled)"
    base_vars = {"reminder_id": reminder_id, "reminder_title": title}

    return {
        "uid": reminder_id,
        "title": title,
        "subtitle": build_subtitle(item),
        "arg": reminder_id,
        "variables": dict(base_vars, action="open"),
        "mods": {
            "shift": {
                "subtitle": f"Complete “{title}”",
                "variables": dict(base_vars, action="done"),
            },
            "alt": {
                "subtitle": f"Edit title of “{title}”…",
                "variables": dict(base_vars, action="edit"),
            },
            "ctrl": {
                "subtitle": f"Reschedule “{title}”…",
                "variables": dict(base_vars, action="reschedule"),
            },
            "cmd+alt+ctrl": {
                "subtitle": f"Delete “{title}” — cannot be undone",
                "variables": dict(base_vars, action="delete"),
                "icon": {"path": "icons/delete.png"},
            },
        },
    }


def main():
    query = sys.argv[1] if len(sys.argv) > 1 else ""
    tokens = query.split()

    try:
        items, free_text = fetch_scope(tokens)
    except RemctlError as exc:
        print(json.dumps({
            "items": [{
                "title": "remctl error",
                "subtitle": str(exc),
                "valid": False,
            }]
        }))
        return

    if free_text:
        items = local_filter(items, free_text)

    items = [i for i in items if not i.get("completed")]

    alfred_items = [build_item(i) for i in items]
    if not alfred_items:
        alfred_items = [{
            "title": "No matching reminders",
            "subtitle": "Try a different list (#Name), \"all\", or search text",
            "valid": False,
        }]

    print(json.dumps({"items": alfred_items}))


if __name__ == "__main__":
    main()

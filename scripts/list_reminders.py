#!/usr/bin/env python3
"""Alfred Script Filter backend for the `rem` keyword.

Three modes, all driven by a prefix on the query itself — Alfred re-invokes
this same script on every keystroke/navigation, so "mode" just means "what
does the current query string look like":

  browse (default)   rem <scope> <text>          -> reminder list
  menu:<id>           reached via Tab or Return     -> action menu for one item
  edit:<id>:<text>     reached via the menu         -> retitle, typing <text>
  due:<id>:<text>      reached via the menu         -> reschedule, typing <text>
  movelist:<id>:<text> reached via the menu         -> move, picking a list
  view:<id>            reached via the menu         -> read-only detail screen
  confirm:<action>:<id>:<value>  reached from a menu action or a filled-in
                       text entry/picker              -> one-line summary,
                       one more Return actually executes it (skip via the
                       CONFIRM_CHANGES=0 workflow variable)

Browse-mode scope grammar (all optional, space separated):
  rem                     -> due today + overdue (remctl today)
  rem <free text>         -> full-text search across all lists (remctl search)
  rem @<List or SmartList> -> everything in one list, or one smart list
                              (smart lists are matched client-side — remctl
                              can only inspect their filter definition, not
                              fetch contents, so see matches_smart_list())
  rem #<tag>              -> every reminder with that tag, across all lists
  rem all                 -> every open reminder across every list
  rem all <text>          -> same, locally filtered by <text>
  rem upcoming [N]        -> due within N days (default 7)
  rem flagged             -> flagged reminders
  rem overdue             -> overdue only

`@` and `#` both double as live pickers: if what follows doesn't exactly
match a real list/smart-list (for `@`) or a known tag (for `#`), instead of
erroring on the partial text, show every name that contains it so far —
selecting one (Return, since these are `valid: false` items with an
`autocomplete`) fills in the exact name and re-renders that scope
immediately. See render_list_picker()/render_tag_picker().

Row modifiers in browse mode: Return=menu, Shift=complete (a fast path
that needs no further input; there's no delete anywhere in this workflow
— use Reminders.app for that). Return used to open the reminder directly
in Reminders.app, which was too easy to trigger by accident when what you
actually wanted was more info — now both Tab and Return drill into the
action menu (reschedule/change-title/move/view-details also live there,
since those need a follow-up text entry, picker, or just more screen
space than a modifier+Return can provide, and "Open in Reminders.app" is
still one of the menu's actions, just no longer the default). Tab and
Return can't be made to land on two *different* screens here — Alfred
drives both keys off the same item, and the only way Return can diverge
from Tab at all is by firing a terminal action immediately (no room for a
list of choices), so the menu stays a single actions-first screen with
"View details" as one of its entries rather than a separate key-mapped
destination. Per Alfred's own docs, an item's "autocomplete" field is a
Tab-triggered behavior specifically — an earlier version of this workflow
incorrectly documented Right Arrow as equivalent, which it isn't for a
plain Script Filter item. Right Arrow itself is a fixed Alfred behavior
tied only to native file/folder results ("Show list of available
Actions... in File System Navigation" per Alfred's cheatsheet) — it isn't
available to hook into for a custom Script Filter's own results at all.
"""
import datetime as dt
import json
import os
import re
import sys

from _remctl import (
    RemctlError,
    cached_run,
    fetch_known_tags,
    fetch_list_and_smart_list_names,
    flatten_lists,
    items_from,
    matches_smart_list,
    normalize_date_phrase,
    run,
)

CACHE_TTL = 5  # seconds; only applies to scope-level fetches, not free text

MENU_RE = re.compile(r"^menu:(\d+)$")
EDIT_RE = re.compile(r"^edit:(\d+):(.*)$", re.DOTALL)
DUE_RE = re.compile(r"^due:(\d+):(.*)$", re.DOTALL)
MOVE_RE = re.compile(r"^movelist:(\d+):(.*)$", re.DOTALL)
VIEW_RE = re.compile(r"^view:(\d+)$")
CONFIRM_RE = re.compile(r"^confirm:(done|edit|reschedule|move):(\d+):(.*)$", re.DOTALL)


def confirm_enabled():
    """Controlled by the CONFIRM_CHANGES workflow variable (on by default)
    — set it to 0/false/no in the workflow's variables to skip straight to
    executing mutations instead of reviewing a one-line summary first.
    """
    return os.environ.get("CONFIRM_CHANGES", "1").strip().lower() not in ("0", "false", "no", "")


# ---------------------------------------------------------------------------
# List / smart-list resolution
# ---------------------------------------------------------------------------

def fetch_all_items():
    lists_payload = cached_run("scope:lists", ["lists"], ttl=CACHE_TTL)
    all_items = []
    for name in flatten_lists(lists_payload):
        try:
            payload = cached_run(f"scope:list:{name}", ["show", name], ttl=CACHE_TTL)
        except RemctlError:
            continue
        all_items.extend(items_from(payload))
    return all_items


BUILTIN_SMART_LIST_SCOPES = {
    "com.apple.reminders.smartlist.today": "today",
    "com.apple.reminders.smartlist.urgent": "urgent",
}


def fetch_smart_list_items(smart_list):
    smart_type = smart_list.get("smartListType", "")
    if smart_type == "com.apple.reminders.smartlist.all":
        return fetch_all_items()
    if smart_type in BUILTIN_SMART_LIST_SCOPES:
        payload = cached_run(
            f"scope:{BUILTIN_SMART_LIST_SCOPES[smart_type]}",
            [BUILTIN_SMART_LIST_SCOPES[smart_type]],
            ttl=CACHE_TTL,
        )
        return items_from(payload)
    if smart_type == "com.apple.reminders.smartlist.completed":
        lists_payload = cached_run("scope:lists", ["lists"], ttl=CACHE_TTL)
        completed = []
        for name in flatten_lists(lists_payload):
            try:
                payload = run(["show", name, "--completed"], json_output=True)
            except RemctlError:
                continue
            completed.extend(i for i in items_from(payload) if i.get("completed"))
        return completed
    # Custom smart list (or an unrecognized built-in): emulate the filter
    # client-side against every open reminder, since remctl can't fetch a
    # smart list's live contents directly.
    return [i for i in fetch_all_items() if matches_smart_list(i, smart_list)]


class PickerNeeded(Exception):
    """Raised from fetch_scope() to hand control to a name picker instead
    of erroring on partial/unresolved @list or #tag text."""

    def __init__(self, kind, partial, rest=""):
        super().__init__(kind)
        self.kind = kind  # "list" or "tag"
        self.partial = partial
        # Free text typed after the tag, e.g. "report" in "#ur report" —
        # carried through so completing the tag doesn't silently drop it.
        # Lists have no equivalent: `@` always claims the rest of the query
        # as the (possibly multi-word) name, by design.
        self.rest = rest


def render_list_picker(partial):
    needle = partial.lower()
    matches = sorted(
        (n, kind) for n, kind in fetch_list_and_smart_list_names() if needle in n.lower()
    )
    if not matches:
        return {"items": [{
            "title": f'No list matches "{partial}"' if partial else "No lists found",
            "subtitle": "Keep typing, or check the name in Reminders.app",
            "valid": False,
        }]}
    return {"items": [
        {"title": name, "subtitle": kind, "valid": False, "autocomplete": f"@{name}"}
        for name, kind in matches
    ]}


def render_tag_picker(partial, rest=""):
    needle = partial.lower()
    matches = sorted(t for t in fetch_known_tags() if needle in t.lower())
    if not matches:
        return {"items": [{
            "title": f'No tag matches "{partial}"' if partial else "No tags found",
            "subtitle": "Keep typing, or check remctl tags",
            "valid": False,
        }]}
    suffix = f" {rest}" if rest else ""
    return {"items": [
        {
            "title": f"#{tag}",
            "subtitle": f'Tag — keeps "{rest}" as a filter' if rest else "Tag",
            "valid": False,
            "autocomplete": f"#{tag}{suffix}",
        }
        for tag in matches
    ]}


def resolve_named_scope(name):
    """Try a real list first, then fall back to a smart list by name.

    Returns (items, skip_completed_filter, error). skip_completed_filter is
    derived from the resolved smart list's `smartListType`, not from the
    (possibly localized, e.g. "Terminé") display name the user typed, so
    the built-in Completed list works regardless of System Settings
    language.
    """
    try:
        payload = cached_run(f"scope:list:{name}", ["show", name], ttl=CACHE_TTL)
        return items_from(payload), False, None
    except RemctlError as list_error:
        smart_lists = cached_run("scope:smart-lists", ["smart-lists"], ttl=CACHE_TTL)
        for sl in smart_lists if isinstance(smart_lists, list) else []:
            if (sl.get("name") or "").lower() == name.lower():
                skip_completed_filter = (
                    sl.get("smartListType") == "com.apple.reminders.smartlist.completed"
                )
                return fetch_smart_list_items(sl), skip_completed_filter, None
        return None, False, list_error


def fetch_scope(query_tokens):
    """Returns (items, remaining_free_text, skip_completed_filter)."""
    if not query_tokens:
        # DEFAULT_SCOPE (a workflow variable, unset by default) lets `rem`
        # alone show something other than "today + overdue" — e.g. set it
        # to "@Tasks" or "upcoming 14" to make that the default landing
        # scope instead. Falling through to the rest of this function with
        # its tokens (rather than special-casing it here) means it gets
        # exactly the same scope grammar as if the user had typed it.
        default_scope = os.environ.get("DEFAULT_SCOPE", "").strip()
        if not default_scope:
            payload = cached_run("scope:today", ["today"], ttl=CACHE_TTL)
            return items_from(payload), "", False
        query_tokens = default_scope.split()

    first = query_tokens[0]

    if first.startswith("@"):
        # Smart-list and list names can contain spaces, so `@` claims the
        # rest of the query as the name rather than just the first token.
        name = " ".join(query_tokens)[1:].strip()
        if not name:
            raise PickerNeeded("list", "")
        items, skip_completed_filter, error = resolve_named_scope(name)
        if items is None:
            raise PickerNeeded("list", name)
        return items, "", skip_completed_filter

    if first.startswith("#"):
        tag = first[1:]
        rest = " ".join(query_tokens[1:])
        if not tag or tag.lower() not in {t.lower() for t in fetch_known_tags()}:
            raise PickerNeeded("tag", tag, rest)
        candidate_pool = fetch_all_items()
        items = [
            i for i in candidate_pool
            if tag.lower() in {t.lower() for t in (i.get("tags") or [])}
        ]
        return items, " ".join(query_tokens[1:]), False

    if first == "all":
        return fetch_all_items(), " ".join(query_tokens[1:]), False

    if first == "upcoming":
        rest = query_tokens[1:]
        days = "7"
        if rest and rest[0].isdigit():
            days = rest[0]
            rest = rest[1:]
        payload = cached_run(f"scope:upcoming:{days}", ["upcoming", days], ttl=CACHE_TTL)
        return items_from(payload), " ".join(rest), False

    if first == "flagged":
        payload = cached_run("scope:flagged", ["flagged"], ttl=CACHE_TTL)
        return items_from(payload), " ".join(query_tokens[1:]), False

    if first == "overdue":
        payload = cached_run("scope:overdue", ["overdue"], ttl=CACHE_TTL)
        return items_from(payload), " ".join(query_tokens[1:]), False

    # No recognized scope keyword: treat the whole query as a full-text
    # search term. remctl does the searching remotely (title + notes,
    # across all lists), so no local filtering is layered on top.
    text = " ".join(query_tokens)
    payload = run(["search", text], json_output=True)
    return items_from(payload), "", False


def local_filter(items, text):
    if not text:
        return items
    needle = text.lower()
    return [i for i in items if needle in (i.get("title") or "").lower()]


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

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


def build_browse_item(item):
    reminder_id = str(item.get("id"))
    title = item.get("title") or "(untitled)"
    base_vars = {"reminder_id": reminder_id, "reminder_title": title}

    # Return used to open the reminder directly in Reminders.app — easy to
    # trigger by accident when what you actually wanted was more info.
    # Return and Tab now both drill into the action menu (menu:<id>, itself
    # valid:false so both keys autocomplete into it) instead; "Open in
    # Reminders.app" is still there as one of that screen's actions, just
    # no longer the accidental default. Tab and Return can't be routed to
    # two different screens from this one item — see the module docstring.
    result = {
        "uid": reminder_id,
        "title": title,
        "subtitle": build_subtitle(item),
        "valid": False,
        "autocomplete": f"menu:{reminder_id}",
    }
    # A modifier fires its variables immediately on Return — there's no
    # autocomplete-style drill-in for mods, so Shift+Return can't be routed
    # through the confirm step the way the menu's "Mark as complete" can.
    # Omit the shortcut entirely while confirmation is on, rather than
    # silently bypass the "every mutation gets reviewed" guarantee; Tab (or
    # Return) into the menu instead.
    if not confirm_enabled():
        result["mods"] = {
            "shift": {
                "subtitle": f"Complete “{title}”",
                "arg": reminder_id,
                "valid": True,
                "variables": dict(base_vars, action="done"),
            },
        }
    return result


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

def render_browse(query):
    tokens = query.split()

    try:
        items, free_text, skip_completed_filter = fetch_scope(tokens)
    except PickerNeeded as pick:
        # A picker's own lists/smart-lists/tags lookup can itself fail
        # (e.g. no Reminders permission yet), so it needs the same
        # RemctlError handling as fetch_scope() — a bare except here
        # wouldn't catch an error raised while rendering the picker below.
        try:
            if pick.kind == "list":
                return render_list_picker(pick.partial)
            return render_tag_picker(pick.partial, pick.rest)
        except RemctlError as exc:
            return {"items": [{"title": "remctl error", "subtitle": str(exc), "valid": False}]}
    except RemctlError as exc:
        return {"items": [{"title": "remctl error", "subtitle": str(exc), "valid": False}]}

    if free_text:
        items = local_filter(items, free_text)
    if not skip_completed_filter:
        items = [i for i in items if not i.get("completed")]

    alfred_items = [build_browse_item(i) for i in items]
    if not alfred_items:
        alfred_items = [{
            "title": "No matching reminders",
            "subtitle": "Try a different list (@Name), tag (#tag), \"all\", or search text",
            "valid": False,
        }]
    return {"items": alfred_items}


MENU_ACTIONS = [
    ("Mark as complete", "done", None, None, False, True),
    ("Reschedule…", None, "due", "type a due date, e.g. tomorrow 9am", False, None),
    ("Change title…", None, "edit", "type a new title, or add #tag to tag it", True, None),
    ("Move to another list…", None, "movelist", "type or pick a list", False, None),
    ("View details", None, "view", "see notes, priority, flag, and tags", None, None),
    ("Open in Reminders.app", "open", None, None, False, False),
]


def render_menu(reminder_id):
    """Actions only — no read-only detail lines mixed in here, so this
    stays a pure "what do you want to do" list; "View details" below is
    its own drill-in to render_view()'s separate read-only screen instead.
    """
    try:
        info = run(["info", reminder_id], json_output=True)
    except RemctlError as exc:
        return {"items": [{"title": "remctl error", "subtitle": str(exc), "valid": False}]}

    title = info.get("title") or f"#{reminder_id}"
    items = []
    for label, action, drill_prefix, hint, prefill_title, needs_confirm in MENU_ACTIONS:
        if action and needs_confirm and confirm_enabled():
            # "Mark as complete" is the only menu entry that both mutates
            # and fires with no further typing, so it's the only one that
            # needs its own confirm drill-in here — edit/reschedule/move
            # route through confirm from render_text_input()/
            # render_move_picker() instead, once a value exists to show.
            items.append({
                "title": label,
                "subtitle": f"“{title}” — review before confirming",
                "valid": False,
                "autocomplete": f"confirm:{action}:{reminder_id}:",
            })
        elif action:
            items.append({
                "title": label,
                "subtitle": f"“{title}”",
                "arg": reminder_id,
                "valid": True,
                "variables": {
                    "action": action,
                    "reminder_id": reminder_id,
                    "reminder_title": title,
                },
            })
        elif prefill_title is None:
            # View details is direct navigation, not a text-entry drill —
            # no trailing ":text" slot to fill in.
            items.append({
                "title": label,
                "subtitle": f"“{title}” — {hint}",
                "valid": False,
                "autocomplete": f"{drill_prefix}:{reminder_id}",
            })
        else:
            # Change-title prefills the current title so adding a #tag (or
            # a small tweak) doesn't require retyping the whole thing —
            # reschedule/move don't prefill, since a stale due date or list
            # name isn't a useful starting point for either.
            prefill = title if prefill_title else ""
            items.append({
                "title": label,
                "subtitle": f"“{title}” — {hint}",
                "valid": False,
                "autocomplete": f"{drill_prefix}:{reminder_id}:{prefill}",
            })
    return {"items": items}


def render_view(reminder_id):
    try:
        info = run(["info", reminder_id], json_output=True)
    except RemctlError as exc:
        return {"items": [{"title": "remctl error", "subtitle": str(exc), "valid": False}]}

    title = info.get("title") or f"#{reminder_id}"
    base_vars = {"reminder_id": reminder_id, "reminder_title": title, "action": "open"}

    lines = [
        ("Title", title),
        ("List", info.get("list") or "—"),
        ("Due", humanize_due(info)),
        ("Priority", (info.get("priority") or "none").capitalize()),
        ("Flagged", "Yes" if info.get("flagged") else "No"),
        ("Tags", ", ".join(info.get("tags") or []) or "none"),
        ("Notes", info.get("notes") or "none"),
    ]
    return {"items": [
        {
            "title": f"{label}: {value}",
            "subtitle": "↩ to open in Reminders.app",
            "arg": reminder_id,
            "valid": True,
            "variables": dict(base_vars),
        }
        for label, value in lines
    ]}


def render_move_picker(reminder_id, partial):
    """Picking a list name from the (live-filtered) matches is the whole
    input needed for a move — no separate free-text step like edit/
    reschedule. Smart lists are excluded since they're filtered views, not
    real containers a reminder can be moved into. When confirmation is
    enabled, picking a match still drills one more step into confirm:...
    rather than firing immediately.
    """
    needle = partial.lower()
    matches = sorted(
        name for name, kind in fetch_list_and_smart_list_names()
        if kind == "List" and needle in name.lower()
    )
    if not matches:
        return {"items": [{
            "title": f'No list matches "{partial}"' if partial else "No lists found",
            "subtitle": "Keep typing, or check the name in Reminders.app",
            "valid": False,
        }]}
    if confirm_enabled():
        return {"items": [
            {
                "title": name,
                "subtitle": "Tab to review before confirming",
                "valid": False,
                "autocomplete": f"confirm:move:{reminder_id}:{name}",
            }
            for name in matches
        ]}
    return {"items": [
        {
            "title": name,
            "subtitle": "Move here",
            "arg": name,
            "valid": True,
            "variables": {"action": "move", "reminder_id": reminder_id},
        }
        for name in matches
    ]}


def render_confirm(action, reminder_id, value):
    try:
        info = run(["info", reminder_id], json_output=True)
    except RemctlError as exc:
        return {"items": [{"title": "remctl error", "subtitle": str(exc), "valid": False}]}

    title = info.get("title") or f"#{reminder_id}"
    summary_by_action = {
        "done": f"Mark “{title}” as complete",
        "edit": f"Change “{title}”'s title to “{value}”",
        "reschedule": f"Reschedule “{title}” to “{value}”",
        "move": f"Move “{title}” to “{value}”",
    }
    item = {
        "title": summary_by_action.get(action, f"{action} “{title}”"),
        "subtitle": "↩ to confirm, or backspace the query to cancel",
        "arg": value,
        "valid": True,
        "variables": {"action": action, "reminder_id": reminder_id, "reminder_title": title},
    }
    return {"items": [item]}


def render_text_input(reminder_id, action, typed_text, prompt_hint):
    if typed_text and confirm_enabled():
        item = {
            "title": typed_text,
            "subtitle": "Tab to review before confirming",
            "valid": False,
            "autocomplete": f"confirm:{action}:{reminder_id}:{typed_text}",
        }
    else:
        item = {
            "title": typed_text if typed_text else f"Type {prompt_hint}…",
            "subtitle": f"↩ to confirm ({action})",
            "arg": typed_text,
            "valid": bool(typed_text),
            "variables": {"action": action, "reminder_id": reminder_id},
        }
    return {"items": [item]}


def main():
    query = sys.argv[1] if len(sys.argv) > 1 else ""

    menu_match = MENU_RE.match(query)
    if menu_match:
        print(json.dumps(render_menu(menu_match.group(1))))
        return

    confirm_match = CONFIRM_RE.match(query)
    if confirm_match:
        action, reminder_id, value = confirm_match.group(1), confirm_match.group(2), confirm_match.group(3)
        print(json.dumps(render_confirm(action, reminder_id, value)))
        return

    edit_match = EDIT_RE.match(query)
    if edit_match:
        reminder_id, typed = edit_match.group(1), edit_match.group(2)
        print(json.dumps(render_text_input(reminder_id, "edit", typed, "a new title")))
        return

    due_match = DUE_RE.match(query)
    if due_match:
        reminder_id, typed = due_match.group(1), due_match.group(2)
        print(json.dumps(render_text_input(reminder_id, "reschedule", typed, "a due date, e.g. tomorrow 9am")))
        return

    move_match = MOVE_RE.match(query)
    if move_match:
        reminder_id, typed = move_match.group(1), move_match.group(2)
        try:
            print(json.dumps(render_move_picker(reminder_id, typed)))
        except RemctlError as exc:
            print(json.dumps({"items": [{"title": "remctl error", "subtitle": str(exc), "valid": False}]}))
        return

    view_match = VIEW_RE.match(query)
    if view_match:
        print(json.dumps(render_view(view_match.group(1))))
        return

    print(json.dumps(render_browse(query)))


if __name__ == "__main__":
    main()

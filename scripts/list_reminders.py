#!/usr/bin/env python3
"""Alfred Script Filter backend for the `rem` keyword.

Three modes, all driven by a prefix on the query itself — Alfred re-invokes
this same script on every keystroke/navigation, so "mode" just means "what
does the current query string look like":

  browse (default)   rem <scope> <text>              -> reminder list
  menu:<id>:<ret>     reached via Tab or Return         -> action menu for one item
  edit:<id>:<ret>:<text>    reached via the menu        -> retitle, typing <text>
  due:<id>:<ret>:<text>     reached via the menu        -> reschedule, typing <text>
  priority:<id>:<ret>:<text> reached via the menu       -> set priority, picking one
  quickedit:<id>:<ret>:<text> reached via the menu      -> title/tags/priority/due/
                              notes together, typing <text> (see
                              render_quick_edit()'s docstring for syntax)
  view:<id>:<ret>     reached via the menu              -> read-only detail
                              screen, plus two quick "reschedule to
                              today/tomorrow" actions below the read-only
                              lines (time-of-day preserving, see
                              reminder_action.py's _due_preserving_time())
  confirm:<action>:<id>:<value>  reached from a menu action or a filled-in
                       text entry/picker              -> one-line summary,
                       one more Return actually executes it (skip via the
                       CONFIRM_CHANGES=0 workflow variable)

There is deliberately no "move to another list" mode — see the "Move to
another list" note further down for why it was removed rather than kept
broken or worked around.

`<ret>` is the original browse query (percent-encoded via _encode_return()
in every mode string above) — it's how "← Back to results" on the menu
screen can re-render the exact scope/search you drilled in from, instead
of resetting to bare `rem`. It rides through every drill-down level so
that "← Back to actions" from a deeper screen (view/edit/due/priority)
still knows how to build the menu's own "← Back to results" afterward.
build_browse_item() is where it originates (the live `rem` query at
render time); every other producer of these mode strings just forwards
whatever it was given. Percent-encoding keeps it colon-free so it can sit
as one segment in an otherwise colon-delimited mode string without being
confused for a delimiter, even when the browse query itself contains a
literal colon.

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
  rem overdue today       -> bulk-reschedule every overdue reminder to
  rem overdue tomorrow       today/tomorrow — a confirm-style single item
                              (always shown, regardless of CONFIRM_CHANGES —
                              see render_bulk_reschedule_confirm()), not a
                              real browse list

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
action menu (reschedule/change-title/set-priority/view-details also live
there, since those need a follow-up text entry, picker, or just more screen
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
from urllib.parse import quote, unquote

from _remctl import (
    RemctlError,
    app_icon,
    cached_run,
    fetch_known_tags,
    fetch_list_and_smart_list_names,
    flatten_lists,
    items_from,
    matches_smart_list,
    normalize_date_phrase,
    reminders_app_icon,
    run,
)
from quick_add import escape_literal, parse as parse_quick_add

CACHE_TTL = 5  # seconds; only applies to scope-level fetches, not free text
# Computed once at import time rather than per-render — Reminders.app's
# location doesn't change mid-process. See reminders_app_icon()'s
# docstring for why every list-representing row (not reminder rows) uses
# this: it's a free, always-available "this is a list" glyph.
LIST_ICON = reminders_app_icon()


def _icon_kwargs():
    return {"icon": LIST_ICON} if LIST_ICON else {}


# icons/*.png are bundled assets, not borrowed from any installed app —
# each rendered once from an Apple SF Symbol (see the generation snippet
# in README.md's Extending section) rather than reaching for whatever
# third-party app happens to have a vaguely-matching icon (Mark as
# complete went through two rejected rounds of exactly that: Todoist,
# then TickTick, both "another app's icon" regardless of branding).
_WORKFLOW_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _bundled_icon(filename):
    return {"path": os.path.join(_WORKFLOW_ROOT, "icons", filename)}


MARK_COMPLETE_ICON = _bundled_icon("mark_complete.png")
PRIORITY_ICON = _bundled_icon("priority.png")
FLAG_ICON = _bundled_icon("flag.png")
TITLE_ICON = _bundled_icon("title.png")
TAGS_ICON = _bundled_icon("tags.png")
NOTES_ICON = _bundled_icon("notes.png")
DUE_ICON = app_icon("/System/Applications/Calendar.app")

# One icon per action-menu row, so the menu isn't a wall of identical
# default icons — each keyed by the MENU_ACTIONS `action` or `drill_prefix`
# (whichever is set). Beyond the bundled assets above, the rest borrow
# other installed apps' Finder icons: TextEdit for retitling, System
# Settings for "adjust several things at once" (Quick edit…), and System
# Information's icon for "more info about this." Those entries degrade to
# no icon (Alfred's default) if the app they point at isn't installed.
MENU_ICONS = {
    "done": MARK_COMPLETE_ICON,
    "due": DUE_ICON,
    "edit": app_icon("/System/Applications/TextEdit.app"),
    "quickedit": app_icon("/System/Applications/System Settings.app"),
    "priority": PRIORITY_ICON,
    "flag": FLAG_ICON,
    "unflag": FLAG_ICON,
    "view": app_icon("/System/Applications/Utilities/System Information.app"),
    "open": LIST_ICON,
}

# The same icons, reused on the read-only view:<id> detail lines, so e.g.
# "Priority" looks the same whether you're looking at it or acting on it.
VIEW_ICONS = {
    "Title": TITLE_ICON,
    "List": LIST_ICON,
    "Due": DUE_ICON,
    "Priority": PRIORITY_ICON,
    "Flagged": FLAG_ICON,
    "Tags": TAGS_ICON,
    "Notes": NOTES_ICON,
}


def _menu_icon_kwargs(key):
    icon = MENU_ICONS.get(key)
    return {"icon": icon} if icon else {}


def _encode_return(browse_query):
    """The original browse query (e.g. "@Groceries", "milk", "" for bare
    rem) travels alongside menu:<id> and every screen drilled into from
    there, so "← Back to results" can re-render exactly what was being
    browsed before — not just "today" or whatever DEFAULT_SCOPE is. Percent
    -encoded (colon-safe) so it can sit as one segment in an otherwise
    colon-delimited mode string without being confused for a delimiter,
    even if the query itself contains a literal colon.
    """
    return quote(browse_query, safe="")


def _back_item(reminder_id, return_q="", label="← Back to actions"):
    """A one-keypress way out of a drill-down screen (view/edit/due/
    priority) back to the action menu, instead of manually backspacing the
    query text. `return_q` (already percent-encoded, see _encode_return())
    is threaded straight through so the action menu reached from here can
    still offer its own "← Back to results" — going view -> back to
    actions -> back to results doesn't lose the original browse scope.
    """
    return {
        "title": label,
        "subtitle": "Tab to return to the action menu",
        "valid": False,
        "autocomplete": f"menu:{reminder_id}:{return_q}",
    }

MENU_RE = re.compile(r"^menu:(\d+):(.*)$", re.DOTALL)
EDIT_RE = re.compile(r"^edit:(\d+):([^:]*):(.*)$", re.DOTALL)
DUE_RE = re.compile(r"^due:(\d+):([^:]*):(.*)$", re.DOTALL)
PRIORITY_RE = re.compile(r"^priority:(\d+):([^:]*):(.*)$", re.DOTALL)
QUICKEDIT_RE = re.compile(r"^quickedit:(\d+):([^:]*):(.*)$", re.DOTALL)
VIEW_RE = re.compile(r"^view:(\d+):(.*)$", re.DOTALL)
CONFIRM_RE = re.compile(r"^confirm:(done|edit|reschedule|reschedule_today|reschedule_tomorrow|flag|unflag|priority|quickedit):(\d+):(.*)$", re.DOTALL)


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
        {"title": name, "subtitle": kind, "valid": False, "autocomplete": f"@{name}", **_icon_kwargs()}
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


def build_browse_item(item, browse_query=""):
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
    # browse_query (the exact `rem` scope/search text this row came from)
    # rides along so the menu can offer "← Back to results" pointing at
    # this same scope, not just whatever bare `rem` shows.
    result = {
        "uid": reminder_id,
        "title": title,
        "subtitle": build_subtitle(item),
        "valid": False,
        "autocomplete": f"menu:{reminder_id}:{_encode_return(browse_query)}",
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

def render_bulk_reschedule_confirm(target):
    """`rem overdue today` / `rem overdue tomorrow` — a one-click way to
    clear out the overdue pile without touching each reminder one at a
    time. Always shows this confirm-style summary before executing,
    regardless of CONFIRM_CHANGES — a bulk mutation across every overdue
    reminder is higher-stakes than a single edit, so it isn't worth
    letting that variable silently skip review here.
    """
    try:
        payload = cached_run("scope:overdue", ["overdue"], ttl=CACHE_TTL)
    except RemctlError as exc:
        return {"items": [{"title": "remctl error", "subtitle": str(exc), "valid": False}]}
    count = len(items_from(payload))
    if count == 0:
        return {"items": [{
            "title": "No overdue reminders",
            "subtitle": "Nothing to reschedule",
            "valid": False,
        }]}
    return {"items": [{
        "title": f"Reschedule all {count} overdue reminder{'s' if count != 1 else ''} to {target}",
        "subtitle": "↩ to confirm, or backspace the query to cancel",
        "arg": target,
        "valid": True,
        "variables": {"action": "bulk_reschedule_overdue", "target": target},
    }]}


def render_browse(query):
    tokens = query.split()

    if len(tokens) == 2 and tokens[0].lower() == "overdue" and tokens[1].lower() in ("today", "tomorrow"):
        return render_bulk_reschedule_confirm(tokens[1].lower())

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

    alfred_items = [build_browse_item(i, query) for i in items]
    if not alfred_items:
        alfred_items = [{
            "title": "No matching reminders",
            "subtitle": "Try a different list (@Name), tag (#tag), \"all\", or search text",
            "valid": False,
        }]
    return {"items": alfred_items}


# Split into three lists (rather than one, with Quick edit/flag appended
# at the end) purely so those two can be inserted at specific points in
# render_menu() — Quick edit's prefill is a custom multi-field string
# built from live info (not a simple boolean prefill_title flag like
# Change title's), and the flag toggle's label/action are computed
# per-reminder (Flag vs Unflag) — neither fits a static MENU_ACTIONS
# tuple, which has no way to know a specific reminder's current state at
# import time.
MENU_ACTIONS_MAIN = [
    ("Mark as complete", "done", None, None, False, True),
    ("Reschedule…", None, "due", "type a due date, e.g. tomorrow 9am", False, None),
    ("Change title…", None, "edit", "type a new title, or add #tag to tag it", True, None),
]
MENU_ACTIONS_PRIORITY = [
    ("Set priority…", None, "priority", "pick none, low, medium, or high", False, None),
]
MENU_ACTIONS_TAIL = [
    ("View details", None, "view", "see notes, priority, flag, and tags", None, None),
    ("Open in Reminders.app", "open", None, None, False, False),
]


def _menu_action_item(entry, reminder_id, return_q, title):
    label, action, drill_prefix, hint, prefill_title, needs_confirm = entry
    if action and needs_confirm and confirm_enabled():
        # "Mark as complete" is the only menu entry that both mutates
        # and fires with no further typing, so it's the only one that
        # needs its own confirm drill-in here — edit/reschedule/priority/quickedit
        # route through confirm from render_text_input()/
        # render_priority_picker() instead, once a value exists to show.
        return {
            "title": label,
            "subtitle": f"“{title}” — review before confirming",
            "valid": False,
            "autocomplete": f"confirm:{action}:{reminder_id}:",
            **_menu_icon_kwargs(action),
        }
    if action:
        return {
            "title": label,
            "subtitle": f"“{title}”",
            "arg": reminder_id,
            "valid": True,
            "variables": {
                "action": action,
                "reminder_id": reminder_id,
                "reminder_title": title,
            },
            **_menu_icon_kwargs(action),
        }
    if prefill_title is None:
        # View details is direct navigation, not a text-entry drill —
        # no trailing ":text" slot to fill in.
        return {
            "title": label,
            "subtitle": f"“{title}” — {hint}",
            "valid": False,
            "autocomplete": f"{drill_prefix}:{reminder_id}:{return_q}",
            **_menu_icon_kwargs(drill_prefix),
        }
    # Change-title prefills the current title so adding a #tag (or a small
    # tweak) doesn't require retyping the whole thing — reschedule/move/
    # priority don't prefill, since a stale due date, list name, or
    # priority isn't a useful starting point for any of them.
    prefill = title if prefill_title else ""
    return {
        "title": label,
        "subtitle": f"“{title}” — {hint}",
        "valid": False,
        "autocomplete": f"{drill_prefix}:{reminder_id}:{return_q}:{prefill}",
        **_menu_icon_kwargs(drill_prefix),
    }


def _due_prefill(info):
    """A due date re-typeable into quick_add's own parser — not
    humanize_due()'s "Overdue: Aug 1" style text, which isn't valid input
    for it. ISO "YYYY-MM-DD[ HH:MM]" round-trips cleanly either way: an
    all-day reminder gets the date only, matching how it was set.
    """
    due_iso = info.get("dueDate")
    if not due_iso:
        return None
    try:
        due_dt = dt.datetime.fromisoformat(due_iso)
    except ValueError:
        return None
    if info.get("allDay"):
        return due_dt.strftime("%Y-%m-%d")
    return due_dt.strftime("%Y-%m-%d %H:%M")


def _quick_edit_prefill(info):
    """Builds the same syntax remadd uses (title @List #tag !priority
    /due notes:text) from a reminder's current state, minus @List —
    deliberately excluded, since "move to another list" was removed as a
    feature after remctl's own -l move consistently failed on this
    machine (see README), and reintroducing list-changing through this
    back door would hit the identical bug.

    Title and notes go through escape_literal() — an *existing* title or
    notes can legitimately contain "@alice", "#release", "!high", or
    "notes:something" as ordinary words (someone else's reminder, or one
    created via Siri/Reminders.app directly, not composed with this
    grammar in mind), and without escaping, confirming this screen while
    changing some unrelated field would silently reinterpret those words
    as new metadata instead of leaving them alone — verified directly.
    """
    parts = [escape_literal(info.get("title") or "")]
    for tag in info.get("tags") or []:
        parts.append(f"#{tag}")
    priority = info.get("priority")
    if priority and priority != "none":
        parts.append(f"!{priority}")
    due_phrase = _due_prefill(info)
    if due_phrase:
        parts.append(f"/{due_phrase}")
    notes = info.get("notes")
    if notes:
        parts.append(f"notes:{escape_literal(notes)}")
    return " ".join(parts)


def render_menu(reminder_id, return_q=""):
    """Actions only — no read-only detail lines mixed in here, so this
    stays a pure "what do you want to do" list; "View details" below is
    its own drill-in to render_view()'s separate read-only screen instead.

    `return_q` (percent-encoded, from menu:<id>:<return_q>) is threaded
    into every drill-in below so a screen reached from here can still find
    its way back through the menu to the original browse results, and is
    used directly for this screen's own "← Back to results" row.
    """
    try:
        info = run(["info", reminder_id], json_output=True)
    except RemctlError as exc:
        return {"items": [{"title": "remctl error", "subtitle": str(exc), "valid": False}]}

    title = info.get("title") or f"#{reminder_id}"
    items = [_menu_action_item(entry, reminder_id, return_q, title) for entry in MENU_ACTIONS_MAIN]

    # Quick edit…: title/tags/priority/due/notes together in one editable
    # line, prefilled with the reminder's current state in the same
    # syntax remadd uses — for changing several fields at once without
    # repeating the menu -> field -> confirm -> re-navigate cycle for
    # each one individually.
    items.append({
        "title": "Quick edit…",
        "subtitle": f"“{title}” — title, #tags, !priority, /due, notes: together",
        "valid": False,
        "autocomplete": f"quickedit:{reminder_id}:{return_q}:{_quick_edit_prefill(info)}",
        **_menu_icon_kwargs("quickedit"),
    })

    items += [_menu_action_item(entry, reminder_id, return_q, title) for entry in MENU_ACTIONS_PRIORITY]

    # Flag/Unflag: same "action fires now, drilling into confirm first
    # when enabled" shape as Mark as complete, just with the label and the
    # actual action name depending on the reminder's current flagged
    # state — flip a flagged reminder back off, not just always on.
    flag_label, flag_action = ("Unflag", "unflag") if info.get("flagged") else ("Flag", "flag")
    if confirm_enabled():
        items.append({
            "title": flag_label,
            "subtitle": f"“{title}” — review before confirming",
            "valid": False,
            "autocomplete": f"confirm:{flag_action}:{reminder_id}:",
            **_menu_icon_kwargs(flag_action),
        })
    else:
        items.append({
            "title": flag_label,
            "subtitle": f"“{title}”",
            "arg": reminder_id,
            "valid": True,
            "variables": {"action": flag_action, "reminder_id": reminder_id, "reminder_title": title},
            **_menu_icon_kwargs(flag_action),
        })

    items += [_menu_action_item(entry, reminder_id, return_q, title) for entry in MENU_ACTIONS_TAIL]

    # Back goes last, same reasoning as elsewhere: Alfred selects the first
    # returned item by default, so a leading Back item would hijack a
    # quick Return meant for the top action (e.g. "Mark as complete" when
    # confirmation is off and it's valid:true).
    items.append({
        "title": "← Back to results",
        "subtitle": "Tab to return to your previous search",
        "valid": False,
        "autocomplete": unquote(return_q),
    })
    return {"items": items}


def render_view(reminder_id, return_q=""):
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
    # Back goes after the detail lines, not before — Alfred selects the
    # first returned item by default, and a leading Back item would hijack
    # a quick Return on this screen (meant to open the reminder) into
    # going back to the menu instead.
    items = [
        {
            "title": f"{label}: {value}",
            "subtitle": "↩ to open in Reminders.app",
            "arg": reminder_id,
            "valid": True,
            "variables": dict(base_vars),
            **({"icon": VIEW_ICONS[label]} if VIEW_ICONS.get(label) else {}),
        }
        for label, value in lines
    ]

    # Quick reschedule shortcuts — same time-of-day-preserving logic as
    # the overdue bulk-reschedule action (_due_preserving_time() in
    # reminder_action.py), just for this one reminder instead of every
    # currently-overdue one. Appended after the detail lines (not first),
    # same reasoning as Back below — these are real mutations, so they
    # go through the normal confirm step like any other single-item
    # action (unlike bulk reschedule, which always confirms regardless).
    for label, target in (("Reschedule to today", "today"), ("Reschedule to tomorrow", "tomorrow")):
        action = f"reschedule_{target}"
        if confirm_enabled():
            items.append({
                "title": label,
                "subtitle": f"“{title}” — review before confirming",
                "valid": False,
                "autocomplete": f"confirm:{action}:{reminder_id}:",
                **_menu_icon_kwargs("due"),
            })
        else:
            items.append({
                "title": label,
                "subtitle": f"“{title}”",
                "arg": reminder_id,
                "valid": True,
                "variables": {"action": action, "reminder_id": reminder_id, "reminder_title": title},
                **_menu_icon_kwargs("due"),
            })

    items.append(_back_item(reminder_id, return_q))
    return {"items": items}


PRIORITY_CHOICES = [("None", "none"), ("Low", "low"), ("Medium", "medium"), ("High", "high")]


def render_priority_picker(reminder_id, return_q, partial):
    """A fixed 4-choice picker rather than free-text entry — priority only
    has these values, so typing one out (and risking a typo remctl would
    just reject) buys nothing over picking from a short list. `partial`
    still filters it live, same shape as the list/tag pickers, so typing
    "h" narrows straight to High.
    """
    needle = partial.lower()
    matches = [(label, value) for label, value in PRIORITY_CHOICES if needle in label.lower()]
    if not matches:
        return {"items": [{
            "title": f'No priority matches "{partial}"',
            "subtitle": "Try none, low, medium, or high",
            "valid": False,
        }, _back_item(reminder_id, return_q)]}
    if confirm_enabled():
        return {"items": [
            {
                "title": label,
                "subtitle": "Tab to review before confirming",
                "valid": False,
                "autocomplete": f"confirm:priority:{reminder_id}:{value}",
                **_menu_icon_kwargs("priority"),
            }
            for label, value in matches
        ] + [_back_item(reminder_id, return_q)]}
    return {"items": [
        {
            "title": label,
            "subtitle": "Set this priority",
            "arg": value,
            "valid": True,
            "variables": {"action": "priority", "reminder_id": reminder_id},
            **_menu_icon_kwargs("priority"),
        }
        for label, value in matches
    ] + [_back_item(reminder_id, return_q)]}


def render_quick_edit(reminder_id, return_q, typed_text):
    """One editable line for title/tags/priority/due/notes together,
    parsed with the exact same parser remadd uses (quick_add.parse()) so
    the syntax is identical: #tag, !priority, /due (or due:), notes:. No
    @List — see _quick_edit_prefill()'s docstring for why.

    A marker's *absence* from the text means that field gets cleared on
    confirm, not "leave unchanged" — this only works because the field
    starts pre-filled with its current value (see _quick_edit_prefill()),
    so what's on screen when you Tab in already represents "no change";
    deleting a marker is a deliberate, visible act of removing it, mirrored
    by execute_quick_edit() in reminder_action.py explicitly clearing
    (`-d clear`, `-p none`, `--clear-tags`, `-n ""`) whatever's missing
    rather than omitting flags that would leave old values untouched.
    """
    # auto_detect_due=False: this text starts as an *existing* title
    # (from _quick_edit_prefill()), not fresh input — remadd's implicit
    # due-phrase heuristic would silently reinterpret a title ending in a
    # day-like word ("Review on Monday") as title="Review" due="on
    # Monday" on every confirm, even when only some other field was being
    # changed. Only an explicit /phrase or due: marker sets a due date
    # here. See parse()'s docstring in quick_add.py.
    parsed = parse_quick_add(typed_text, auto_detect_due=False)

    # Same "always show all the slots" treatment as remadd's own preview,
    # minus @list (not part of this screen's scope) — a slot switches
    # from its placeholder to the real value as soon as it's set.
    meta = [
        " ".join(f"#{t}" for t in parsed["tags"]) if parsed["tags"] else "#tag",
        f"!{parsed['priority']}" if parsed["priority"] else "!priority",
        f"/{parsed['due']}" if parsed["due"] else "/due",
        f"notes: {parsed['notes']}" if parsed["notes"] else "notes:",
    ]
    hint = "  ·  ".join(meta)

    if not parsed["title"]:
        return {"items": [{
            "title": "No title yet",
            "subtitle": f"Keep typing a title — {hint}",
            "valid": False,
        }, _back_item(reminder_id, return_q)]}

    if confirm_enabled():
        item = {
            "title": parsed["title"],
            "subtitle": f"Tab to review before confirming — {hint}",
            "valid": False,
            "autocomplete": f"confirm:quickedit:{reminder_id}:{typed_text}",
        }
    else:
        item = {
            "title": parsed["title"],
            "subtitle": f"↩ to confirm — {hint}",
            "arg": typed_text,
            "valid": True,
            "variables": {"action": "quickedit", "reminder_id": reminder_id},
        }
    return {"items": [item, _back_item(reminder_id, return_q)]}


def render_confirm(action, reminder_id, value):
    try:
        info = run(["info", reminder_id], json_output=True)
    except RemctlError as exc:
        return {"items": [{"title": "remctl error", "subtitle": str(exc), "valid": False}]}

    title = info.get("title") or f"#{reminder_id}"
    has_time = bool(info.get("dueDate")) and not info.get("allDay")
    time_note = " (keeping the time)" if has_time else ""
    summary_by_action = {
        "done": f"Mark “{title}” as complete",
        "edit": f"Change “{title}”'s title to “{value}”",
        "reschedule": f"Reschedule “{title}” to “{value}”",
        "reschedule_today": f"Reschedule “{title}” to today{time_note}",
        "reschedule_tomorrow": f"Reschedule “{title}” to tomorrow{time_note}",
        "flag": f"Flag “{title}”",
        "unflag": f"Unflag “{title}”",
        "priority": f"Set “{title}”'s priority to {value}",
        "quickedit": f"Update “{title}”: {value}",
    }
    item = {
        "title": summary_by_action.get(action, f"{action} “{title}”"),
        "subtitle": "↩ to confirm, or backspace the query to cancel",
        "arg": value,
        "valid": True,
        "variables": {"action": action, "reminder_id": reminder_id, "reminder_title": title},
    }
    return {"items": [item]}


def render_text_input(reminder_id, return_q, action, typed_text, prompt_hint):
    # For reschedule specifically, show what the reminder is currently due
    # so retyping isn't a guessing game — cached (not run()) since this
    # re-fetches on every keystroke while typing and the due date can't
    # have changed mid-typing; reminder_action.py clears the cache after
    # any real mutation.
    current_note = ""
    if action == "reschedule":
        try:
            info = cached_run(f"info:{reminder_id}", ["info", reminder_id], ttl=CACHE_TTL)
            current_note = f" — currently {humanize_due(info)}"
        except RemctlError:
            pass

    if typed_text and confirm_enabled():
        item = {
            "title": typed_text,
            "subtitle": f"Tab to review before confirming{current_note}",
            "valid": False,
            "autocomplete": f"confirm:{action}:{reminder_id}:{typed_text}",
        }
    else:
        item = {
            "title": typed_text if typed_text else f"Type {prompt_hint}…",
            "subtitle": f"↩ to confirm ({action}){current_note}",
            "arg": typed_text,
            "valid": bool(typed_text),
            "variables": {"action": action, "reminder_id": reminder_id},
        }
    # Back goes *after* the working item, not before — Alfred selects the
    # first returned item by default, so if Back were first, pressing
    # Return/Tab right after typing a value would activate Back instead of
    # submitting/reviewing what was just typed (caught in review: this
    # would have silently discarded typed input on every edit/reschedule).
    return {"items": [item, _back_item(reminder_id, return_q)]}


def main():
    query = sys.argv[1] if len(sys.argv) > 1 else ""

    menu_match = MENU_RE.match(query)
    if menu_match:
        print(json.dumps(render_menu(menu_match.group(1), menu_match.group(2))))
        return

    confirm_match = CONFIRM_RE.match(query)
    if confirm_match:
        action, reminder_id, value = confirm_match.group(1), confirm_match.group(2), confirm_match.group(3)
        print(json.dumps(render_confirm(action, reminder_id, value)))
        return

    edit_match = EDIT_RE.match(query)
    if edit_match:
        reminder_id, return_q, typed = edit_match.group(1), edit_match.group(2), edit_match.group(3)
        print(json.dumps(render_text_input(reminder_id, return_q, "edit", typed, "a new title")))
        return

    due_match = DUE_RE.match(query)
    if due_match:
        reminder_id, return_q, typed = due_match.group(1), due_match.group(2), due_match.group(3)
        print(json.dumps(render_text_input(reminder_id, return_q, "reschedule", typed, "a due date, e.g. tomorrow 9am")))
        return

    priority_match = PRIORITY_RE.match(query)
    if priority_match:
        reminder_id, return_q, typed = priority_match.group(1), priority_match.group(2), priority_match.group(3)
        print(json.dumps(render_priority_picker(reminder_id, return_q, typed)))
        return

    quickedit_match = QUICKEDIT_RE.match(query)
    if quickedit_match:
        reminder_id, return_q, typed = quickedit_match.group(1), quickedit_match.group(2), quickedit_match.group(3)
        print(json.dumps(render_quick_edit(reminder_id, return_q, typed)))
        return

    view_match = VIEW_RE.match(query)
    if view_match:
        print(json.dumps(render_view(view_match.group(1), view_match.group(2))))
        return

    print(json.dumps(render_browse(query)))


if __name__ == "__main__":
    main()

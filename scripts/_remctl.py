"""Shared helpers for talking to the remctl CLI from Alfred script objects."""
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import time

CANDIDATE_PATHS = [
    os.path.expanduser("~/bin/remctl"),
    "/opt/homebrew/bin/remctl",
    "/usr/local/bin/remctl",
]


def find_remctl():
    env_path = os.environ.get("REMCTL_PATH")
    if env_path and os.path.isfile(env_path):
        return env_path
    for path in CANDIDATE_PATHS:
        if os.path.isfile(path):
            return path
    found = shutil.which("remctl")
    if found:
        return found
    return None


class RemctlError(RuntimeError):
    def __init__(self, message, stderr=""):
        super().__init__(message)
        self.stderr = stderr


def run(args, json_output=True, timeout=10):
    """Run `remctl <args>` and return parsed JSON (or raw stdout text)."""
    binary = find_remctl()
    if binary is None:
        raise RemctlError(
            "remctl not found. Install it from https://github.com/viticci/remctl "
            "and/or set the REMCTL_PATH workflow variable."
        )
    cmd = [binary] + list(args)
    if json_output and "--json" not in cmd:
        cmd.append("--json")
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        raise RemctlError(f"remctl timed out: {' '.join(cmd)}") from exc

    if proc.returncode != 0:
        raise RemctlError(
            f"remctl exited {proc.returncode}: {' '.join(cmd)}",
            stderr=proc.stderr.strip(),
        )

    if not json_output:
        return proc.stdout

    stdout = proc.stdout.strip()
    if not stdout:
        return []
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RemctlError(f"Could not parse remctl JSON output: {exc}") from exc


CACHE_DIR = os.environ.get(
    "alfred_workflow_cache",
    os.path.expanduser("~/Library/Caches/com.alfredapp.reminders"),
)


def cached_run(cache_key, args, ttl=5, json_output=True):
    """Like run(), but reuses a recent result for the same scope.

    Scope-level caching only (e.g. "today", "all", "list:Work") — do not use
    this for per-keystroke free-text search, since the key would change on
    every character and the cache would never hit.
    """
    try:
        ttl = float(os.environ.get("REMCTL_CACHE_TTL", ttl))
    except ValueError:
        pass
    if ttl <= 0:
        return run(args, json_output=json_output)

    os.makedirs(CACHE_DIR, exist_ok=True)
    digest = hashlib.sha1(cache_key.encode("utf-8")).hexdigest()
    cache_file = os.path.join(CACHE_DIR, f"{digest}.json")

    try:
        age = time.time() - os.path.getmtime(cache_file)
        if age < ttl:
            with open(cache_file, "r") as fh:
                return json.load(fh)
    except (OSError, ValueError, json.JSONDecodeError):
        pass

    result = run(args, json_output=json_output)
    try:
        with open(cache_file, "w") as fh:
            json.dump(result, fh)
    except OSError:
        pass
    return result


DATE_WORD_NORMALIZE = {
    "tom": "tomorrow", "tmrw": "tomorrow", "tmw": "tomorrow",
    "yest": "yesterday",
    "mon": "monday", "tue": "tuesday", "tues": "tuesday",
    "wed": "wednesday", "thu": "thursday", "thur": "thursday", "thurs": "thursday",
    "fri": "friday", "sat": "saturday", "sun": "sunday",
}


def normalize_date_phrase(text):
    """Expand shorthand (tom, tmrw, mon, ...) before handing text to remctl's
    date parser, which understands full words but not these abbreviations.
    """
    if not text:
        return text
    words = text.split()
    return " ".join(DATE_WORD_NORMALIZE.get(w.lower(), w) for w in words)


_DUE_WEEKDAYS = {
    "monday", "mon", "tuesday", "tue", "tues", "wednesday", "wed",
    "thursday", "thu", "thur", "thurs", "friday", "fri", "saturday", "sat",
    "sunday", "sun",
}
_DUE_DAY_WORDS = {"today", "tomorrow", "tom", "tmrw", "tmw", "tonight", "yesterday", "yest"}
_DUE_MODIFIER_WORDS = {"next", "this", "last", "in", "at", "by", "on"}
_DUE_UNIT_WORDS = {
    "day", "days", "week", "weeks", "month", "months",
    "hour", "hours", "minute", "minutes", "min", "mins",
}
_DUE_TIME_ANCHOR_WORDS = {"am", "pm", "noon", "midnight", "eod"}
# "at"/"by" specifically license a bare hour right after them ("at 9",
# "by 5") — the other modifiers ("in", "next", "on", ...) don't naturally
# precede a bare hour the same way, so they're deliberately excluded here.
_HOUR_MODIFIER_WORDS = {"at", "by"}
# Time-of-day words are only ever a *weak* signal on their own — "Movie
# night" is a perfectly ordinary title — but they still need to be
# recognized so the scan doesn't stop dead on them before reaching a real
# anchor earlier in the phrase, e.g. "tomorrow morning", "Friday evening".
_DUE_DAYPART_WORDS = {"morning", "afternoon", "evening", "night"}
_TIME_RE = re.compile(r"\d{1,2}:\d{2}(am|pm)?$")
_HOUR_RE = re.compile(r"\d{1,2}(am|pm)$")
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}$")
_RELATIVE_RE = re.compile(r"\+\d+[dwm]$")
_BARE_INT_RE = re.compile(r"\d{1,4}$")


def _due_token_kind(tok):
    """Classify a token for due-phrase detection, or None if it doesn't
    plausibly belong to a trailing date/time phrase at all.

    "anchor" tokens are concrete enough to stand alone (a weekday, "9am",
    an ISO date, ...). "modifier" ("in", "next", "at", ...), "unit"
    ("days", "week", ...), and "daypart" ("morning", "night", ...) tokens
    only count as part of a due phrase in combination with an anchor or
    each other — see split_implicit_due() — so that a title ending in a
    single stray preposition ("Check in", "Read on") or a bare time-of-day
    word ("Movie night") isn't misread as one.
    """
    low = tok.lower().strip(",.")
    if low in _DUE_WEEKDAYS or low in _DUE_DAY_WORDS or low in _DUE_TIME_ANCHOR_WORDS:
        return "anchor"
    if _TIME_RE.fullmatch(low) or _HOUR_RE.fullmatch(low) or _ISO_DATE_RE.fullmatch(low) or _RELATIVE_RE.fullmatch(low):
        return "anchor"
    if low in _DUE_UNIT_WORDS:
        return "unit"
    if low in _DUE_MODIFIER_WORDS:
        return "modifier"
    if low in _DUE_DAYPART_WORDS:
        return "daypart"
    if _BARE_INT_RE.fullmatch(low):
        return "number"
    return None


def looks_like_due_token(tok):
    return _due_token_kind(tok) is not None


def split_implicit_due(tokens):
    """Greedily peel a trailing due-date phrase off a token list.

    Returns (title_tokens, due_tokens). Only actually splits if the
    candidate suffix contains a concrete "anchor" (a weekday, "9am", an ISO
    date, ...) or a modifier/unit pairing that only makes sense together
    ("in 3 days", "next week") — a bare trailing modifier, unit, or
    time-of-day word alone ("Check in", "Read on", "Movie night") is left
    as part of the title instead of being misread as a due phrase. Daypart
    words ("morning", "evening", ...) still extend the scan so a real
    anchor earlier in the phrase isn't missed ("tomorrow morning", "Friday
    evening" both still work), they just don't count as an anchor by
    themselves. A bare number only extends the scan when it's immediately
    *followed* by a unit word ("in 3 days") or immediately *preceded* by
    "at"/"by" ("tomorrow at 9", "by 5") — otherwise it's almost always just
    part of the title ("Test 2", "Room 5") and shouldn't get pulled into a
    due phrase just because an unrelated anchor happens to follow it
    ("Test 2 tomorrow" must stay title="Test 2", due="tomorrow", not
    due="2 tomorrow"). Always leaves at least one token as the title even
    when the whole query looks date-like, so a title that's just "Tomorrow"
    doesn't turn into an empty-title reminder.
    """
    i = len(tokens)
    kinds = []
    while i > 1:
        kind = _due_token_kind(tokens[i - 1])
        if kind is None:
            break
        if kind == "number":
            followed_by_unit = bool(kinds) and kinds[-1] == "unit"
            preceded_by_hour_modifier = (
                tokens[i - 2].lower().strip(",.") in _HOUR_MODIFIER_WORDS
            )
            if not (followed_by_unit or preceded_by_hour_modifier):
                break
        kinds.append(kind)
        i -= 1
    kinds.reverse()

    has_anchor = "anchor" in kinds
    has_numeric_duration = "unit" in kinds and "number" in kinds
    has_modifier_unit = "modifier" in kinds and "unit" in kinds
    if not (has_anchor or has_numeric_duration or has_modifier_unit):
        return tokens, []

    return tokens[:i], tokens[i:]


def matches_smart_list(item, smart_list):
    """Best-effort client-side re-implementation of a custom smart list's
    filter, since remctl can inspect a smart list's definition but has no
    command to fetch its live contents directly.
    """
    filt = smart_list.get("filterJSON")
    if not filt:
        return True
    return _matches_filter_node(item, filt)


def _matches_filter_node(item, node):
    op = (node.get("operation") or "and").lower()
    checks = []
    if "hashtags" in node:
        checks.append(_matches_tags(item, node["hashtags"]))
    if "date" in node:
        checks.append(_matches_date(item, node["date"]))
    if "priorities" in node:
        checks.append(_matches_priority(item, node["priorities"]))
    if "flagged" in node:
        checks.append(bool(item.get("flagged")) == bool(node["flagged"]))
    if not checks:
        return True
    return all(checks) if op == "and" else any(checks)


def _matches_tags(item, hashtags):
    item_tags = {t.lower() for t in (item.get("tags") or [])}
    if "untagged" in hashtags:
        return len(item_tags) == 0
    h = hashtags.get("hashtags", hashtags)
    include = {t.lower() for t in h.get("include", [])}
    exclude = {t.lower() for t in h.get("exclude", [])}
    if exclude and item_tags & exclude:
        return False
    if not include:
        return True
    if (h.get("operation") or "or").lower() == "and":
        return include.issubset(item_tags)
    return bool(include & item_tags)


def _matches_date(item, date_spec):
    if "noDate" in date_spec:
        return not item.get("dueDate")
    if "relativeRange" in date_spec:
        rr = date_spec["relativeRange"]
        if isinstance(rr, list):
            # e.g. ["inNext", "1", "day"] — no includePastDue in this shape
            direction = rr[0] if len(rr) > 0 else "inNext"
            magnitude = rr[1] if len(rr) > 1 else "1"
            units = rr[2] if len(rr) > 2 else "day"
            include_past_due = False
        else:
            direction = rr.get("direction", "inNext")
            magnitude = rr.get("magnitude", "1")
            units = rr.get("units", "day")
            include_past_due = bool(rr.get("includePastDue"))
        if not item.get("dueDate"):
            return False
        try:
            due = dt.datetime.fromisoformat(item["dueDate"])
            magnitude = float(magnitude)
        except (ValueError, TypeError):
            return True
        now = dt.datetime.now()
        unit_days = {"day": 1, "week": 7, "month": 30}.get(units, 1)
        horizon = now + dt.timedelta(days=unit_days * magnitude)
        if direction != "inNext":
            return True  # unrecognized direction shape; don't exclude
        if include_past_due:
            return due <= horizon
        return now <= due <= horizon
    return True


def _matches_priority(item, priorities):
    if not priorities or priorities == ["none"]:
        return True
    return (item.get("priority") or "none") in priorities


def items_from(payload):
    """Normalize read-command JSON into a plain list of reminder dicts.

    Handles both the normal array shape and the `--via-eventkit` wrapper
    object (`{"items": [...]}`).
    """
    if isinstance(payload, dict) and "items" in payload:
        return payload["items"]
    if isinstance(payload, list):
        return payload
    return []

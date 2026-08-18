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
_DUE_TIME_WORDS = {"am", "pm", "noon", "midnight", "morning", "afternoon", "evening", "night", "eod"}
_TIME_RE = re.compile(r"\d{1,2}:\d{2}(am|pm)?$")
_HOUR_RE = re.compile(r"\d{1,2}(am|pm)$")
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}$")
_RELATIVE_RE = re.compile(r"\+\d+[dwm]$")


def looks_like_due_token(tok):
    """True if `tok` reads as part of a trailing date/time phrase.

    Deliberately conservative about bare numbers (requires am/pm or a colon)
    to avoid swallowing an ordinary trailing number in a title, e.g. "Buy 5"
    should stay a title, not get parsed as a due fragment.
    """
    low = tok.lower().strip(",.")
    if low in _DUE_WEEKDAYS or low in _DUE_DAY_WORDS:
        return True
    if low in _DUE_MODIFIER_WORDS or low in _DUE_UNIT_WORDS or low in _DUE_TIME_WORDS:
        return True
    if _TIME_RE.fullmatch(low) or _HOUR_RE.fullmatch(low):
        return True
    if _ISO_DATE_RE.fullmatch(low) or _RELATIVE_RE.fullmatch(low):
        return True
    return False


def split_implicit_due(tokens):
    """Greedily peel a trailing due-date phrase off a token list.

    Returns (title_tokens, due_tokens). Always leaves at least one token as
    the title, even if the whole query looks date-like, so a title that's
    just "Tomorrow" doesn't turn into an empty-title reminder.
    """
    i = len(tokens)
    while i > 1 and looks_like_due_token(tokens[i - 1]):
        i -= 1
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

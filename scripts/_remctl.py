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


def _is_runnable(path):
    """A regular file we actually have execute permission on. isfile()
    alone isn't enough — pointing REMCTL_PATH at a readable-but-not-
    executable file (or a directory) got that path selected here and then
    blew up as an uncaught PermissionError at subprocess time instead of
    falling through to the next candidate.
    """
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def find_remctl():
    env_path = os.environ.get("REMCTL_PATH")
    if _is_runnable(env_path):
        return env_path
    for path in CANDIDATE_PATHS:
        if _is_runnable(path):
            return path
    found = shutil.which("remctl")
    if found:
        return found
    return None


# macOS moved most built-in apps to /System/Applications/ starting with
# Big Sur — Reminders.app lives there now, but check the pre-Big-Sur
# /Applications/ location too in case this ever runs somewhere older.
REMINDERS_APP_PATHS = [
    "/System/Applications/Reminders.app",
    "/Applications/Reminders.app",
]


def app_icon(*candidate_paths):
    """Alfred `icon` dict borrowing the Finder icon of the first existing
    path among `candidate_paths` — no bundled icon assets needed, and it
    degrades gracefully (returns None, meaning Alfred's own default icon)
    if none of the candidates exist on this machine, e.g. a third-party
    app that isn't installed. Callers should omit the `icon` key entirely
    when this returns None rather than passing None through to Alfred.
    """
    for path in candidate_paths:
        if os.path.exists(path):
            return {"type": "fileicon", "path": path}
    return None


def reminders_app_icon():
    """Alfred `icon` dict pointing at Reminders.app itself — used for any
    Script Filter result that represents *a list* (not a reminder), so
    list-picking rows read as "this is a list" at a glance without needing
    a bundled icon asset.
    """
    return app_icon(*REMINDERS_APP_PATHS)


def _applescript_string(text):
    """Escape and quote `text` as an AppleScript string literal.

    AppleScript string literals are double-quoted, not single-quoted — the
    previous version of this helper used Python's repr() (!r), which
    produces Python-style single-quoted output. That's not merely
    stylistically wrong: osascript rejects it outright (verified directly:
    `osascript -e "display notification 'x' with title 'y'"` fails with
    "syntax error: Expected ... but found unknown token"), so every
    notify() call using it silently failed — the notification just never
    appeared, with no visible error since the caller doesn't check
    osascript's exit code. Double-quoted output only needs its own
    backslashes and double quotes escaped.
    """
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def notify(title, subtitle):
    script = f"display notification {_applescript_string(subtitle)} with title {_applescript_string(title)}"
    subprocess.run(["osascript", "-e", script], capture_output=True)


class RemctlError(RuntimeError):
    def __init__(self, message, stderr=""):
        super().__init__(message)
        self.stderr = stderr


class InvalidDatePhrase(RemctlError):
    """A due phrase that parsed structurally but can't be a real time.

    Subclasses RemctlError purely so the existing handlers — which already
    wrap every mutation and render a visible error — pick it up without
    each call site growing a second except clause.
    """


def _normalize_all_day_due(item):
    """Rewrite an all-day reminder's `dueDate` to local midnight on the day
    it's actually due.

    remctl serializes an all-day reminder as *UTC* midnight rendered as a
    naive local timestamp, so every all-day reminder arrives dated one day
    early (west of UTC) with a bogus time attached. Verified against
    EventKit ground truth on this machine: a reminder genuinely due
    all-day Aug 21 arrives as "2026-08-20T20:00:00" (EDT, UTC-4), and one
    due Nov 26 arrives as "2026-11-25T19:00:00" (EST, UTC-5) — the offset
    tracks DST, so this can't be corrected with a fixed shift. Confirmed
    that every all-day item on this machine lands on exactly 00:00:00 UTC
    once reinterpreted, which is what makes the round trip below safe.

    Interpreting the naive value as local time and converting to UTC
    recovers the true date; it's then rewritten as plain local midnight so
    everything downstream (humanize_due(), _due_prefill(),
    _due_preserving_time(), today_reschedule_makes_sense(),
    _matches_date()) can keep treating dueDate as naive local without
    knowing any of this happened. Doing it here, at the single choke point
    every remctl read passes through, avoids five separate consumers each
    having to remember the quirk — an earlier version of this workflow got
    it wrong in all five places at once, including silently moving an
    all-day reminder a day earlier on every Quick edit confirm.

    Left alone if the value doesn't reinterpret to an exact UTC midnight,
    so a future remctl that emits all-day dates correctly (or a machine
    already running in UTC) passes through untouched rather than being
    shifted a second time.
    """
    raw = item.get("dueDate")
    if not item.get("allDay") or not isinstance(raw, str):
        return
    try:
        naive = dt.datetime.fromisoformat(raw)
    except ValueError:
        return
    if naive.tzinfo is not None:
        return
    utc = naive.astimezone(dt.timezone.utc)
    if (utc.hour, utc.minute, utc.second) != (0, 0, 0):
        return
    item["dueDate"] = dt.datetime.combine(utc.date(), dt.time.min).isoformat()


def _normalize_payload(payload):
    """Apply _normalize_all_day_due() to every reminder dict in a decoded
    remctl JSON payload, whatever shape it arrived in — a bare list of
    reminders, the `--via-eventkit` {"items": [...]} wrapper, or the single
    dict that `remctl info <id>` returns. Payloads with no reminders in
    them at all (`lists`, `tags`, `smart-lists`) simply have nothing to
    match and pass through untouched.
    """
    if isinstance(payload, dict):
        if isinstance(payload.get("items"), list):
            for entry in payload["items"]:
                if isinstance(entry, dict):
                    _normalize_all_day_due(entry)
        else:
            _normalize_all_day_due(payload)
    elif isinstance(payload, list):
        for entry in payload:
            if isinstance(entry, dict):
                _normalize_all_day_due(entry)
    return payload


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
    except OSError as exc:
        # Covers the whole family of "couldn't even start it": the file
        # vanished between find_remctl()'s check and here, it isn't
        # executable, it's a directory, the architecture doesn't match.
        # Without this these surface as an uncaught traceback — every
        # caller already handles RemctlError and renders it as a normal
        # "remctl error" row, so route it there instead.
        raise RemctlError(f"Could not run remctl at {binary}: {exc}") from exc

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
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RemctlError(f"Could not parse remctl JSON output: {exc}") from exc
    # Every remctl read in the workflow funnels through here, which is
    # exactly why the all-day date correction lives at this point rather
    # than in each consumer — see _normalize_all_day_due().
    return _normalize_payload(payload)


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
    # Write to a unique temp file in the same directory, then rename over
    # the target — rename is atomic within a filesystem, so a reader never
    # observes a half-written file. Alfred spawns a fresh process per
    # keystroke, so concurrent readers and writers of the same cache key
    # are routine, not hypothetical; the plain write this replaces could
    # be interleaved into a torn read (survivable, since the read path
    # catches JSONDecodeError, but it silently cost a redundant remctl
    # call every time it happened). The pid suffix keeps two concurrent
    # writers from clobbering each other's temp file mid-write.
    tmp_file = f"{cache_file}.{os.getpid()}.tmp"
    try:
        with open(tmp_file, "w") as fh:
            json.dump(result, fh)
        os.replace(tmp_file, cache_file)
    except OSError:
        try:
            os.remove(tmp_file)
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

_MONTH_NAMES = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

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
_ORDINAL_DAY_RE = re.compile(r"\d{1,2}(?:st|nd|rd|th)$")
# M/D or M/D/Y, e.g. "9/13", "9/9/26" — deliberately month-first (US style)
# to match how the rest of this workflow's due-phrase examples read.
_SLASH_DATE_RE = re.compile(r"\d{1,2}/\d{1,2}(?:/\d{2,4})?$")

# Words that can be immediately glued to a time or day number with no
# space — "tom9am", "friday3:30pm", "sep9" — typed this way often enough
# (thumb-typing, autocomplete eating the space) that it's worth splitting
# back apart rather than failing outright. Longest names first so
# "tomorrow" matches before the shorter "tom" prefix would otherwise steal
# part of it.
_GLUE_DAYWORDS = sorted(set(DATE_WORD_NORMALIZE) | _DUE_WEEKDAYS | _DUE_DAY_WORDS, key=len, reverse=True)
_GLUE_MONTHWORDS = sorted(_MONTH_NAMES, key=len, reverse=True)
_DAYWORD_TIME_PATTERN = (
    r"(" + "|".join(re.escape(w) for w in _GLUE_DAYWORDS) + r")"
    r"(\d{1,2}(?::\d{2})?(?:am|pm))"
)
_MONTH_DAY_PATTERN = (
    r"(" + "|".join(re.escape(w) for w in _GLUE_MONTHWORDS) + r")"
    r"(\d{1,2})"
)
_GLUED_DAYWORD_TIME_RE = re.compile(r"^" + _DAYWORD_TIME_PATTERN + r"$")
_GLUED_MONTH_DAY_RE = re.compile(r"^" + _MONTH_DAY_PATTERN + r"$")
_DAYWORD_TIME_SPLIT_RE = re.compile(r"\b" + _DAYWORD_TIME_PATTERN + r"\b")
_MONTH_DAY_SPLIT_RE = re.compile(r"\b" + _MONTH_DAY_PATTERN + r"\b")


def _due_token_kind(tok):
    """Classify a token for due-phrase detection, or None if it doesn't
    plausibly belong to a trailing date/time phrase at all.

    "anchor" tokens are concrete enough to stand alone (a weekday, "9am",
    an ISO date, a slash date, a glued "tom9am"/"sep9", ...). "month"
    tokens (a bare month name) are anchor-like but tracked separately so a
    lone day number can be recognized as part of the phrase when it sits
    next to one ("sep 9", "9 sep"). "modifier" ("in", "next", "at", ...),
    "unit" ("days", "week", ...), and "daypart" ("morning", "night", ...)
    tokens only count as part of a due phrase in combination with an
    anchor/month or each other — see split_implicit_due() — so that a
    title ending in a single stray preposition ("Check in", "Read on") or
    a bare time-of-day word ("Movie night") isn't misread as one.
    """
    low = tok.lower().strip(",.")
    if low in _DUE_WEEKDAYS or low in _DUE_DAY_WORDS or low in _DUE_TIME_ANCHOR_WORDS:
        return "anchor"
    if low in _MONTH_NAMES:
        return "month"
    if (
        _TIME_RE.fullmatch(low)
        or _HOUR_RE.fullmatch(low)
        or _ISO_DATE_RE.fullmatch(low)
        or _RELATIVE_RE.fullmatch(low)
        or _SLASH_DATE_RE.fullmatch(low)
        or _GLUED_DAYWORD_TIME_RE.fullmatch(low)
        or _GLUED_MONTH_DAY_RE.fullmatch(low)
    ):
        return "anchor"
    if low in _DUE_UNIT_WORDS:
        return "unit"
    if low in _DUE_MODIFIER_WORDS:
        return "modifier"
    if low in _DUE_DAYPART_WORDS:
        return "daypart"
    if _BARE_INT_RE.fullmatch(low) or _ORDINAL_DAY_RE.fullmatch(low):
        return "number"
    return None


def looks_like_due_token(tok):
    return _due_token_kind(tok) is not None


def split_implicit_due(tokens):
    """Greedily peel a trailing due-date phrase off a token list.

    Returns (title_tokens, due_tokens). Only actually splits if the
    candidate suffix contains a concrete "anchor" (a weekday, "9am", an ISO
    date, a slash date like "9/13", a glued "tom9am"/"sep9", ...), a bare
    month name paired with an adjacent day number ("sep 9", "9 sep"), or a
    modifier/unit pairing that only makes sense together ("in 3 days",
    "next week") — a bare trailing modifier, unit, or time-of-day word
    alone ("Check in", "Read on", "Movie night") is left as part of the
    title instead of being misread as a due phrase. Daypart words
    ("morning", "evening", ...) still extend the scan so a real anchor
    earlier in the phrase isn't missed ("tomorrow morning", "Friday
    evening" both still work), they just don't count as an anchor by
    themselves. A bare number only extends the scan when it's immediately
    *followed* by a unit word ("in 3 days") or a month name ("9 sep"), or
    immediately *preceded* by "at"/"by" ("tomorrow at 9", "by 5") or a
    month name ("sep 9") — otherwise it's almost always just part of the
    title ("Test 2", "Room 5") and shouldn't get pulled into a due phrase
    just because an unrelated anchor happens to follow it ("Test 2
    tomorrow" must stay title="Test 2", due="tomorrow", not due="2
    tomorrow"). Always leaves at least one token as the title even when
    the whole query looks date-like, so a title that's just "Tomorrow"
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
            followed_by_month = bool(kinds) and kinds[-1] == "month"
            preceding = tokens[i - 2].lower().strip(",.")
            preceded_by_hour_modifier = preceding in _HOUR_MODIFIER_WORDS
            preceded_by_month = preceding in _MONTH_NAMES
            if not (followed_by_unit or followed_by_month or preceded_by_hour_modifier or preceded_by_month):
                break
        kinds.append(kind)
        i -= 1
    kinds.reverse()

    has_anchor = "anchor" in kinds or "month" in kinds
    has_numeric_duration = "unit" in kinds and "number" in kinds
    has_modifier_unit = "modifier" in kinds and "unit" in kinds
    if not (has_anchor or has_numeric_duration or has_modifier_unit):
        return tokens, []

    return tokens[:i], tokens[i:]


def _resolve_year(month, day, year=None):
    """A bare M/D (or month-name + day) is read as the next occurrence of
    that date — this year if it hasn't passed yet, otherwise next year —
    matching how most calendar/reminder apps read a bare date rather than
    always assuming "this year" (which would silently create a reminder in
    the past for a date already gone by). An explicit 2-digit year
    ("9/9/26") is read as 20XX; a 4-digit year is used as-is.
    """
    today = dt.date.today()
    if year is None:
        year = today.year
        try:
            if dt.date(year, month, day) < today:
                year += 1
        except ValueError:
            pass  # invalid day-of-month (e.g. Feb 30) — let the caller's own dt.date(...) raise
    elif year < 100:
        year += 2000
    return year


def _parse_day_number(word):
    m = re.match(r"\d{1,2}", word)
    return int(m.group(0)) if m else None


def _slash_date_to_iso(word):
    parts = word.split("/")
    try:
        month, day = int(parts[0]), int(parts[1])
        year = int(parts[2]) if len(parts) == 3 else None
        return dt.date(_resolve_year(month, day, year), month, day).isoformat()
    except ValueError:
        return None


def normalize_date_phrase(text):
    """Expand shorthand (tom, tmrw, mon, ...) and resolve concrete dates
    (9/13, 9/9/26, sep 9, 9 sep, and glued forms like tom9am/sep9) before
    handing text to remctl's own date parser.

    remctl's parser only understands full relative-day words/phrases
    ("tomorrow 09:30", "Friday at 15:00", "+3d") and ISO "YYYY-MM-DD[
    HH:MM]" — confirmed directly against `remctl add -d <input>`, including
    that it does *not* accept an ISO date next to a bare "9am" ("2026-09-13
    9am" fails; "2026-09-13 09:00" works). So once a concrete date is found
    here (from a slash date or a month/day pairing), it's fully resolved to
    "YYYY-MM-DD[ HH:MM]" ourselves rather than leaving fragments for remctl
    to parse. A phrase with no concrete date (the common case — "tomorrow
    9am", "next friday", "in 3 days") is left for remctl's own parser,
    which already handles those fine; this function only expands the
    shorthand words it doesn't know (tom -> tomorrow, etc).
    """
    if not text:
        return text

    text = _DAYWORD_TIME_SPLIT_RE.sub(lambda m: f"{m.group(1)} {m.group(2)}", text)
    text = _MONTH_DAY_SPLIT_RE.sub(lambda m: f"{m.group(1)} {m.group(2)}", text)
    words = text.split()

    resolved_date = None
    leftover = []
    i = 0
    while i < len(words):
        low = words[i].lower().strip(",.")
        nxt = words[i + 1].lower().strip(",.") if i + 1 < len(words) else None

        if _SLASH_DATE_RE.fullmatch(low):
            iso = _slash_date_to_iso(low)
            if iso:
                resolved_date = iso
                i += 1
                continue

        if low in _MONTH_NAMES and nxt and (_BARE_INT_RE.fullmatch(nxt) or _ORDINAL_DAY_RE.fullmatch(nxt)):
            day = _parse_day_number(nxt)
            try:
                resolved_date = dt.date(_resolve_year(_MONTH_NAMES[low], day), _MONTH_NAMES[low], day).isoformat()
                i += 2
                continue
            except (ValueError, TypeError):
                pass

        if nxt in _MONTH_NAMES and (_BARE_INT_RE.fullmatch(low) or _ORDINAL_DAY_RE.fullmatch(low)):
            day = _parse_day_number(low)
            try:
                resolved_date = dt.date(_resolve_year(_MONTH_NAMES[nxt], day), _MONTH_NAMES[nxt], day).isoformat()
                i += 2
                continue
            except (ValueError, TypeError):
                pass

        leftover.append(words[i])
        i += 1

    if resolved_date is None:
        return " ".join(DATE_WORD_NORMALIZE.get(w.lower(), w) for w in words)

    # A concrete date was found — pull a time out of whatever's left
    # (connector words like "at"/"by" are simply ignored) and assemble the
    # final string ourselves; see the docstring for why remctl can't be
    # trusted to combine an ISO date with a bare "9am" itself.
    time_24h = None
    for w in leftover:
        low = w.lower().strip(",.")
        if low == "noon":
            time_24h = "12:00"
            break
        if low == "midnight":
            time_24h = "00:00"
            break
        m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(am|pm)?", low)
        if m and (m.group(3) or m.group(2)):
            hour, minute, meridiem = int(m.group(1)), int(m.group(2) or 0), m.group(3)
            # The regex only bounds digit *count*, not range, so "25:00",
            # "0:70", "13am" and "0pm" all match it. Handing those to
            # remctl gets an obscure failure; silently dropping the time
            # and falling through to date-only is worse still, because the
            # add/reschedule then *succeeds* as an all-day reminder while
            # the confirmation screen showed the time the user typed. Both
            # quietly do something other than what was asked, so refuse
            # outright and let the caller surface it.
            #
            # Range-check *before* the meridiem conversion, not just
            # after: a 12-hour clock only has hours 1-12, and validating
            # the converted value lets nonsense through because it lands
            # back inside 0-23 — "13am" stays 13 and saves as 1pm, "0pm"
            # becomes 12 and saves as noon. Neither is what was typed.
            if minute > 59:
                raise InvalidDatePhrase(f"“{w}” isn't a valid time of day.")
            if meridiem:
                if not 1 <= hour <= 12:
                    raise InvalidDatePhrase(f"“{w}” isn't a valid time of day.")
                if meridiem == "pm" and hour != 12:
                    hour += 12
                elif meridiem == "am" and hour == 12:
                    hour = 0
            elif hour > 23:
                raise InvalidDatePhrase(f"“{w}” isn't a valid time of day.")
            time_24h = f"{hour:02d}:{minute:02d}"
            break

    return f"{resolved_date} {time_24h}" if time_24h else resolved_date


def today_reschedule_makes_sense(info):
    """False when offering/executing "reschedule to today" would be a
    trap: a reminder with a specific time whose time-of-day has already
    passed today would go straight back to overdue the moment it's
    rescheduled, since the today/tomorrow shortcuts preserve the existing
    time of day rather than dropping it — silently defeating the whole
    point of using them to clear an overdue reminder. Always True for an
    all-day reminder (no time to compare against) or one with no due date
    yet at all (nothing to preserve).

    Shared by list_reminders.py (deciding whether to show "Today" in the
    Reschedule… picker) and reminder_action.py (revalidating right before
    actually executing reschedule_today) — checking only at render time
    isn't enough: the picker can render, get confirmed, and get executed
    minutes apart (longer still with CONFIRM_CHANGES on, which adds a
    whole extra review step), enough time for a time-of-day that was
    still in the future when the picker rendered to have passed by
    execution.
    """
    if info.get("allDay") or not info.get("dueDate"):
        return True
    try:
        due_dt = dt.datetime.fromisoformat(info["dueDate"])
    except (ValueError, TypeError):
        # TypeError too, not just ValueError: fromisoformat() raises it for
        # a non-string dueDate, and a bare `except ValueError` let that
        # escape as an uncaught traceback. Same guard _matches_date()
        # already uses on the identical call.
        return True
    now = dt.datetime.now()
    return (due_dt.hour, due_dt.minute) > (now.hour, now.minute)


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
        # "inNext 1 day" reads as *calendar* days ("today and tomorrow"),
        # not a rolling 24-hour window from the current clock time —
        # verified directly against a real "Important" smart list (filter:
        # priority set + inNext 1 day/includePastDue) whose actual contents
        # in Reminders.app included items due tomorrow morning, which a
        # `now + 24h` window would have cut off whenever "now" was past
        # that same time today. So the horizon is the end of the day that
        # is `magnitude` calendar days out, and (when includePastDue is
        # unset) the lower bound is the start of *today* rather than the
        # exact current moment, so an already-passed reminder due earlier
        # today still counts as "today."
        today = dt.date.today()
        unit_days = {"day": 1, "week": 7, "month": 30}.get(units, 1)
        horizon_date = today + dt.timedelta(days=round(unit_days * magnitude))
        horizon = dt.datetime.combine(horizon_date, dt.time(23, 59, 59))
        if direction != "inNext":
            return True  # unrecognized direction shape; don't exclude
        if include_past_due:
            return due <= horizon
        start_of_today = dt.datetime.combine(today, dt.time.min)
        return start_of_today <= due <= horizon
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


def fetch_known_tags():
    payload = cached_run("scope:tags", ["tags"])
    return [t.get("name") for t in (payload if isinstance(payload, list) else []) if t.get("name")]


def fetch_list_and_smart_list_names():
    lists_payload = cached_run("scope:lists", ["lists"])
    smart_payload = cached_run("scope:smart-lists", ["smart-lists"])
    entries = [(name, "List") for name in flatten_lists(lists_payload)]
    entries += [
        (sl.get("name"), "Smart list")
        for sl in (smart_payload if isinstance(smart_payload, list) else [])
        if sl.get("name")
    ]
    return entries

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

A word can be forced to stay literal — never read as `@`/`#`/`!`/`due:`/
`notes:`/a slash-date, no matter its shape — by prefixing it with a
backslash (a literal backslash character before the word, e.g. an
escaped "@alice" or "#release"). Not something you'd normally type by
hand; it exists so list_reminders.py's Quick edit… screen can safely
prefill an *existing* reminder's title/notes (which might legitimately
contain "@alice" or "#release" as ordinary words) without those getting
silently reinterpreted as new metadata on the next confirm. See
escape_literal().
"""
import re
import sys

from _remctl import RemctlError, looks_like_due_token, normalize_date_phrase, notify, run, split_implicit_due

# Any run of whitespace — space(s), a tab, a newline — counts as one
# boundary; splitting with a capturing group keeps the separator text
# itself in the result, so "".join(...) on an unmodified split reproduces
# the input exactly. escape_literal() needs this (not just literal-space
# splitting) so a marker-shaped word separated from prior text only by a
# newline — a later line of a multi-line note — still gets detected and
# escaped, before Alfred flattens that newline into a space on its own.
_WS_RE = re.compile(r"(\s+)")

PRIORITY_MAP = {
    "h": "high", "high": "high",
    "m": "medium", "medium": "medium",
    "l": "low", "low": "low",
}


def _is_marker_token(tok):
    """True if `parse()` would read this token as something other than
    plain literal text: `@List`/`#tag`/`!priority`/`due:`/`notes:`/a
    slash-date, *or* a token that already starts with a literal backslash
    (parse()'s own escape character — an unescaped one would itself get
    silently stripped on reparse, e.g. a real title word like a Windows
    UNC-style path starting with a backslash, so it needs protecting same
    as any other marker-shaped word). The exact set of conditions escape_literal()
    needs to shield a word from; kept in sync with parse()'s own checks
    by construction (same conditions, just returning a bool instead of
    consuming the token).
    """
    low = tok.lower()
    if tok.startswith("\\"):
        return True
    if tok.startswith("@") and len(tok) > 1:
        return True
    if tok.startswith("#") and len(tok) > 1:
        return True
    if tok.startswith("!") and low[1:] in PRIORITY_MAP:
        return True
    if low.startswith("due:"):
        return True
    if low.startswith("notes:"):
        return True
    if tok.startswith("/") and len(tok) > 1 and looks_like_due_token(tok[1:]):
        return True
    return False


def escape_literal(text):
    """Backslash-prefixes any word in `text` that `parse()` would
    otherwise read as `@`/`#`/`!`/`due:`/`notes:`/slash-date syntax (or
    that already starts with a literal backslash — escaping the escape
    character, so *that* backslash survives too), so it survives an
    edit-and-reparse round trip as plain text. For list_reminders.py's
    Quick edit… screen, which prefills this same grammar from an
    *existing* reminder's title/notes — ordinary text can legitimately
    start with any of those characters ("Email @alice", "Discuss
    #release", "Use !high", "Read notes:first draft" are all real titles
    a person might actually have), and re-parsing that text
    unescaped would silently reinterpret those words as new metadata (or,
    for `@`, just discard them — Quick edit has no list-changing slot to
    put a parsed list name into) instead of leaving them alone. Only
    words that would actually be misread get a backslash — a title with
    none of them round-trips with no visible change.

    Splits on whitespace runs via `_WS_RE`, capturing each separator
    (spaces, tabs, newlines) verbatim rather than reducing it to a fixed
    delimiter, so the original spacing — including a marker-shaped word
    that starts a later line of a multi-line note — survives exactly.
    `_is_marker_token()` is always False for a whitespace-only or empty
    string, so separator segments just pass through `"".join(...)`
    unchanged alongside the (possibly escaped) word segments.
    """
    return "".join(("\\" + w if _is_marker_token(w) else w) for w in _WS_RE.split(text))


def parse(query, auto_detect_due=True, recognize_list=True, preserve_boundary_whitespace=False):
    """`auto_detect_due=False` disables the trailing-due-phrase heuristic
    entirely (only an explicit `/phrase` or `due:phrase` marker sets a due
    date) — used by list_reminders.py's Quick edit… screen, which
    prefills this same syntax from an *existing* reminder's title rather
    than fresh user input. There, a title that happens to end in a
    day-like word ("Review on Monday") would otherwise get silently
    split into title="Review", due="on Monday" on every confirm, even
    when the user only meant to change some other field — verified
    directly. remadd itself keeps the heuristic on (the default), since
    typing "buy milk tomorrow" without a marker is the whole point there
    and the title is fresh input the user is actively composing, not
    existing data being blindly re-parsed.

    `recognize_list=False` disables `@List` recognition entirely — also
    used by Quick edit…, which has no list-changing slot to put a parsed
    list name into (see _quick_edit_prefill()'s docstring for why `@List`
    was dropped from that screen). escape_literal() protects *pre-filled*
    `@word` text from being misread, but can't protect a brand new
    `@word` the user types fresh during editing — verified directly
    ("Call @alice instead" silently became title="Call instead" with the
    mention discarded, since Quick edit's execution never reads the
    parsed list value). With recognize_list=False, `@` is never treated
    as a marker at all in this context, escaped or not, typed fresh or
    not, so there's nothing left to protect against.

    Tokenizes on literal single spaces (`query.split(" ")`), not generic
    whitespace-run splitting, so that repeated spaces within a field
    (title or notes) survive a prefill/reparse round trip instead of
    silently collapsing to one — a run of N spaces yields N-1 empty-string
    tokens, each inert (no marker check matches an empty string, so it
    just falls through to whichever accumulator is active), and the final
    `" ".join(...)` calls below reproduce the original spacing exactly.
    Verified: 'Buy  milk   at store' round-trips as 'Buy  milk   at store',
    not 'Buy milk at store'. (Newlines don't need the same treatment here:
    by the time Quick edit's prefill reaches this function, Alfred has
    already flattened any newline in the query into a space — it's only
    escape_literal(), building that prefill *before* Alfred sees it, that
    needs whitespace-run-aware splitting to catch a marker on a later
    line; see its docstring.)

    `preserve_boundary_whitespace=True` skips the final `.strip()` on
    title/due/notes — used by Quick edit…, where an *existing* note can
    legitimately start or end with whitespace (an indented first line),
    and confirming some unrelated field shouldn't silently trim it away.
    remadd leaves this off (the default): its title is fresh typed input,
    where accidental leading/trailing spaces are just typos worth
    cleaning up, not data worth preserving.
    """
    plain_tokens, due_words, notes_words, tags = [], [], [], []
    plain_escaped = []  # parallel to plain_tokens: True if from a \escape
    list_name = priority = None
    mode = None  # None | 'due' | 'notes'
    explicit_due = False

    for tok in query.split(" "):
        # A leading backslash (see escape_literal()) forces this token to
        # stay literal — mode-appropriate plain text, never reinterpreted
        # as @/#/!/due:/notes:/slash-date syntax regardless of shape.
        # Checked before every marker rule below so escaping is absolute.
        if tok.startswith("\\") and len(tok) > 1:
            literal = tok[1:]
            if mode == "due":
                due_words.append(literal)
            elif mode == "notes":
                notes_words.append(literal)
            else:
                plain_tokens.append(literal)
                plain_escaped.append(True)
            continue
        low = tok.lower()
        if recognize_list and tok.startswith("@") and len(tok) > 1:
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
        if low.startswith("due:"):
            mode = "due"
            explicit_due = True
            rest = tok[4:]
            if rest:
                due_words.append(rest)
            continue
        # "/" is only recognized as the due-phrase marker when what
        # follows actually looks date-like — otherwise a genuine leading
        # slash in a title ("Check /health endpoint", a Unix path, a URL
        # route) would get silently swallowed into the due phrase instead
        # of staying part of the title, and the add would likely fail
        # remctl's date parsing entirely instead of creating what was
        # actually typed. Same token classifier the implicit auto-detect
        # heuristic already uses, so "/" and unmarked auto-detection agree
        # on what counts as date-like.
        if tok.startswith("/") and len(tok) > 1 and looks_like_due_token(tok[1:]):
            mode = "due"
            explicit_due = True
            due_words.append(tok[1:])
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
            plain_escaped.append(False)

    if auto_detect_due and not explicit_due and plain_tokens:
        # split_implicit_due() scans from the tail looking for a date-like
        # anchor and stops as soon as a token doesn't look date-like. Two
        # kinds of token need to be kept out of that scan entirely rather
        # than just stopping it:
        #   - an empty-string separator token (see the split(" ") note
        #     above) isn't date-like either, so it would stop the scan
        #     right there and hide any real due phrase behind it —
        #     matters in practice because quick_add_filter.py's own
        #     completions append a trailing space after the picked word.
        #   - a token that came from a \escape (see escape_literal()) is
        #     explicitly promised to "never [be] read... no matter its
        #     shape" — but that promise was only ever kept against the
        #     per-token marker checks above, not against this separate,
        #     later heuristic scan, so an escaped date-like word ("Review
        #     \Monday") still got silently swept into the due phrase.
        # Escaped tokens are protected by walling them (and everything
        # before them) off from the scan entirely — the scan only ever
        # looks at the tail *after* the last escaped token, so an escaped
        # word can never end up inside a detected due phrase, same as if
        # it were a genuinely non-date-like word the scan had stopped at.
        # This path is remadd-only (auto_detect_due is False for Quick
        # edit…, where escaping instead exists to protect *pre-filled*
        # existing text, and this heuristic never runs at all).
        last_escaped = -1
        for i, esc in enumerate(plain_escaped):
            if esc:
                last_escaped = i
        protected = plain_tokens[: last_escaped + 1]
        candidate = [t for t in plain_tokens[last_escaped + 1 :] if t]
        candidate, implicit_due = split_implicit_due(candidate)
        due_words = implicit_due + due_words
        plain_tokens = protected + candidate

    title = " ".join(plain_tokens)
    notes = " ".join(notes_words)
    if not preserve_boundary_whitespace:
        title = title.strip()
        notes = notes.strip()
    # A due phrase is never legitimately boundary-padded (Quick edit's own
    # prefill always builds it from a clean ISO date, see _due_prefill()),
    # so it's always stripped before handing it to normalize_date_phrase()
    # regardless of preserve_boundary_whitespace — untested, unneeded
    # territory for that function otherwise.
    due = normalize_date_phrase(" ".join(due_words).strip())

    return {
        "title": title,
        "list": list_name,
        "tags": tags,
        "priority": priority,
        "due": due or None,
        "notes": notes or None,
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

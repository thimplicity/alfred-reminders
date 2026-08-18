# Reminders — Alfred workflow

Create, search, complete, and edit Apple Reminders from Alfred, backed by
[remctl](https://github.com/viticci/remctl). Modeled on the UX of
[Alfredo](https://alfred.app/workflows/giovanni/alfredo/) for Todoist.

## Status

This is a hand-built scaffold: the Python scripts under `scripts/` are
tested standalone (see "Testing the scripts directly" below) against real
Reminders data, including full create/edit/reschedule/complete/move-list
cycles, and are the part to trust. `info.plist` is a best-effort,
hand-authored Alfred workflow definition — it has **not** been
round-tripped through the actual Alfred app, since this was built in a
headless session with no GUI access. Import it, then sanity-check each
piece against "If something's not wired right" below before relying on it.
The object graph is intentionally minimal now (two keywords, each with one
plain connection, no modifier-gated routing) specifically to keep that risk
small.

**Security note**: every script object embeds `"{query}"` directly into a
`/bin/bash`-interpreted command line, so without escaping configured, a
query containing shell metacharacters would have that command executed by
bash before Python ever starts, under Alfred's own permissions. Two
reachable paths on this branch, both requiring a selection, not just
typing — **Change title…** prefills a reminder's current title into
`autocomplete` (`render_menu()` in `scripts/list_reminders.py`), so a
title containing `$(command)` — plausible from a collaborator on a shared
list — would reach `{query}` once you select that menu entry (Tab); the
`@`/`#` picker's `autocomplete` values are similarly built from list/tag
names (`render_list_picker()`/`render_tag_picker()`), which a shared-list
collaborator can also rename — typing `rem @` alone only renders the
picker entry, it's selecting it (Tab) that puts the name into `{query}`
on the *next* invocation, the point bash actually evaluates it. All four
script objects (`rem` and `remadd` Script Filters, both Run Scripts) set
`escaping: 102` — verified against a deanishe benchmark plist built for
exactly this class of input, and against real third-party workflows on
this machine using the same value for the same
`"{query}"`-in-shell-argument pattern — to escape
backquotes/dollars/double-quotes/backslashes before substitution. If you
ever add a new script object with `"{query}"` in it, set this too.

## Setup

### 1. Install remctl

Already done on this machine (`remctl` v1.7.1 in `~/bin`). On a fresh Mac:

```bash
git clone https://github.com/viticci/remctl.git
cd remctl
./install.sh --bootstrap --doctor
```

### 2. Grant permissions — to Alfred.app specifically

`remctl` needs two grants, and they're per-*process*, not per-Mac — the
grant you made for a Terminal/interpreter while testing does **not** cover
Alfred, because Alfred spawns the scripts itself:

1. **Reminders access** — run `remctl onboard` once from within an Alfred
   Script Filter/Run Script (or just trigger the `rem` keyword once) and
   approve the macOS "would like to access your reminders" prompt.
2. **Full Disk Access** — System Settings → Privacy & Security → Full Disk
   Access → add:
   - **Alfred.app** (usually `/Applications/Alfred 5.app`)
   - The Python interpreter remctl runs under — check with `remctl doctor`;
     on this Mac it's
     `/opt/homebrew/Cellar/python@3.14/3.14.2_1/Frameworks/Python.framework/Versions/3.14/bin/python3.14`,
     but Alfred's own script objects call `/usr/bin/python3` (see below),
     so also add `/usr/bin/python3` if `remctl doctor` still fails once
     Alfred is granted access.

Run `remctl doctor` again after granting to confirm both checks pass.

### 3. Import the workflow

Double-click `info.plist`, or drag the `alfred-reminders` folder onto
Alfred's Workflows tab. Alfred will complain if the plist doesn't parse —
if so, rebuild the workflow by hand using the object list in "Workflow
structure" below; the scripts themselves don't change.

## Keyword syntax

### `rem` — search / browse

| Query | Scope |
|---|---|
| `rem` (empty) | Due today + overdue |
| `rem <text>` | Full-text search (title + notes) across all lists |
| `rem @List` | Everything in one list — or one **smart list** (see below) |
| `rem #tag` | Every reminder with that tag, across all lists |
| `rem all` | Every open reminder, across every list |
| `rem all <text>` | Same, filtered by `<text>` |
| `rem upcoming [N]` | Due within N days (default 7) |
| `rem flagged` | Flagged reminders |
| `rem overdue` | Overdue only |

**Changing what `rem` alone shows**: by default an empty query is "due
today + overdue," which is legitimately empty whenever nothing's due or
overdue — that's not a bug, just what the scope means. Set the
`DEFAULT_SCOPE` workflow variable to anything from the table above (with
the leading `rem` dropped) to change it — e.g. `@Tasks` to always land on
one list, `upcoming 14` to always show the next two weeks, `flagged` to
land on flagged items, `all` for everything. Empty (the default) keeps
the "today + overdue" behavior.

`@` claims the *rest* of the query as the name, so multi-word list/smart-list
names work: `rem @Sometime - AI`, `rem @Don't Forget Me`. `#tag` is a single
word (tags don't have spaces), and anything after it is a further free-text
filter, same as `flagged`/`overdue`.

**Live picker**: typing `@` or `#` with no exact match yet — rather than
erroring on partial text — shows every list/smart-list or tag whose name
contains what you've typed so far (`@` with nothing after it lists
everything). These are `valid: false` `autocomplete` items, so Tab (not
Return) fills in the exact name and immediately shows that scope — no
separate confirm step needed.

**Smart lists**: `remctl` can inspect a smart list's filter definition but
has no command to fetch its live contents, so `@Name` tries a real list
first and, if none matches, looks up a smart list by that name and
re-implements its filter (tags, date range, priority, flagged) client-side
against every reminder in every list — see `matches_smart_list()` in
`scripts/_remctl.py`. Apple's built-in smart lists (Today, Urgent, All,
Completed) map straight onto equivalent remctl commands instead of being
filter-emulated. This is best-effort: unusual filter shapes may not match
exactly what Reminders.app itself would show — verified against this
machine's actual smart lists (tag/priority/date/flagged combinations), but
not exhaustively against every filter kind Reminders supports.

### Row navigation

| Key | Action |
|---|---|
| Return | Open the action menu for that reminder |
| ⇧ Return | Mark complete — **only when `CONFIRM_CHANGES=0`**; otherwise Shift is unbound (falls back to the default Return/menu) and completing goes through the menu instead, since a modifier can't drill into the confirm step |
| ⇥ Tab | Open the action menu for that reminder (same destination as Return) |

Return used to open the reminder directly in Reminders.app, which turned
out too easy to trigger by accident when what you actually wanted was
more info — now Return and Tab both drill into the action menu instead.
Since one Alfred item can only carry a single `autocomplete` value, the
only way to make Return "enter" a screen rather than fire an action
immediately is to make the base item `valid: false` with `autocomplete`
pointing at that screen — which is also why Return and Tab can't be made
to lead to two *different* screens from this row: Alfred drives both keys
off the same item, and the only way Return can diverge from Tab at all is
by firing a one-shot terminal action, which has no room for a list of
choices. So the menu stays a single actions-first screen, with "View
details" as one of its entries rather than a separately key-mapped
destination. Per Alfred's own docs, `autocomplete` is a Tab-triggered
behavior for a `valid: true` item; here the item is `valid: false`, so
UI text sticks to advertising Tab, though in practice Return reaches it
too (see the caveat in "If something's not wired right"). Right Arrow is
unrelated either way — it's a *fixed* Alfred behavior tied only to native
file/folder results, not something a custom Script Filter can hook into.

The action menu, in order:

1. **Mark as complete**
2. **Reschedule…**
3. **Change title…**
4. **Move to another list…**
5. **View details**
6. **Open in Reminders.app**

Reschedule, change-title, move, and view-details all need a follow-up
value or more screen space than a modifier+Return can give (a modifier is
a one-shot fire-and-forget action, not an interactive prompt or a second
screen), which is why they live in the menu rather than on a modifier
key. Tab into the menu, then Tab again on "Reschedule…" / "Change
title…" / "Move to another list…" / "View details" drops you into a
text-entry prompt, picker, or read-only detail screen — these are
`valid: false` items, so only Tab reliably applies their `autocomplete`.
To back out of any of these without finishing, just backspace the query
text (it's plain editable text at that point, e.g. `menu:3724` or
`edit:3724:`) back down to `rem` and continue browsing.

**View details** shows title, list, due date, priority, flag, tags, and
notes as a read-only screen (Return on any line just opens the reminder
in Reminders.app, same as browsing normally) — the one place in the menu
that doesn't mutate anything.

There's no delete anywhere in this workflow — use Reminders.app directly
for that.

**Confirmation step**: every mutation (Mark as complete, Reschedule,
Change title, Move to another list) shows a one-line summary — "Mark
'Buy milk' as complete", "Move 'Buy milk' to 'Groceries'" — that needs one
more Return before it actually calls `remctl`. This is on by default; set
the `CONFIRM_CHANGES` workflow variable to `0` (Alfred's workflow
configuration sheet, or edit `info.plist`'s top-level `variables` dict) to
skip straight to executing instead. Open/View details are never confirmed
— they don't change anything.

Reschedule accepts `tomorrow`, `tom`, `next friday`, `2026-06-01`, `clear`,
etc. — same trailing-phrase parsing as `remadd`'s due-date detection below.

**Change title…** prefills the current title (so appending `#tag` or
making a small edit doesn't mean retyping the whole thing) and supports
adding real synced tags: any `#tag` word anywhere in what you type is
pulled out and added via `remctl edit --private -t`, rather than left as
literal `#tag` characters in the title — plain `--title` never
auto-converts hashtag-looking text into a tag (verified directly against
a real reminder: `tags` stayed `null` after creating one with `#word` in
the title). Reschedule and move don't prefill, since a stale due date or
list name isn't a useful starting point for either.

**Move to another list…** is a live-filtered picker over real lists only
(not smart lists, since those are filtered views, not containers) —
picking one completes the move immediately, no further typing needed.

**Known limitation**: moving a reminder between lists calls `remctl edit
ID -l LIST`, which is documented to fall back to a verified clone-delete
when EventKit rejects a plain move across a list/container boundary. On
at least one test machine this instead surfaces a raw
`com.apple.reminderkit error -3002` — reproduced identically calling
`remctl` directly (with and without `--private`), so it's a remctl/EventKit
behavior on that Mac, not a bug in this workflow's scripts. If you hit this,
it's worth checking whether it's specific to certain lists (e.g. Groceries)
or all moves on your machine, and reporting to the remctl project if it's
the latter.

### `remadd` — quick add

```
remadd <title words...> [@List] [#tag ...] [!priority] [<due phrase>] [notes:<text>]
```

- `@List` — target list (single word; lists with spaces in the name aren't
  supported by this shorthand — use `remctl add` directly for those). If
  omitted, no `-l` flag is passed to `remctl add` at all, so the reminder
  lands wherever `remctl`/EventKit puts an untargeted reminder — verified
  directly to be the same list configured as the system-wide Default List
  in Settings → Reminders → Default List (the same one Siri or a plain
  "Reminders, add ___" would use), not something this workflow controls.
- `#tag` — repeatable; without `--private` these land as inline `#hashtags`
  appended to the title (remctl limitation, not a synced tag) — see
  "Extending" below if you want real synced tags
- `!priority` — `!high` / `!medium` / `!low` (or `!h`/`!m`/`!l`)
- A trailing due-date phrase is **auto-detected** — no `due:` marker
  needed: `tomorrow`, `tom`, `next friday`, `9am`, `2026-06-01`, `+3d`,
  `9/13`, `9/13 9am`, `9/9/26`, `sep 9`, `9 sep`, and glued forms typed
  with no space (`tom9am`, `friday3:30pm`, `sep9`), etc. This is a
  heuristic (see `looks_like_due_token()` in `scripts/_remctl.py`) and
  conservative about bare numbers specifically to avoid misreading an
  ordinary trailing number in a title (`Buy 5 apples` stays a title). If a
  title genuinely ends in a date-like word and gets misparsed, use an
  explicit `due:<phrase>` prefix to disambiguate — it still works and
  always wins over the heuristic. Slash dates (`9/13`) are month-first
  (US style); a bare `M/D` or `sep 9` with no year is read as the next
  occurrence of that date (this year if it hasn't passed yet, otherwise
  next year), and a 2-digit year (`9/9/26`) is read as 20XX. remctl's own
  parser doesn't understand any of these formats directly (verified
  against `remctl add -d <input>` — it only takes ISO `YYYY-MM-DD[ HH:MM]`
  or its own relative-phrase parsing), so `normalize_date_phrase()`
  resolves them to a concrete ISO date(+time) itself before handing off,
  rather than leaving fragments for remctl to guess at.
- `notes:<text>` — everything after `notes:` through the end of the query
  (or up to the next recognized token) becomes the reminder's notes; never
  auto-detected, since free text can't be told apart from a due phrase by
  pattern alone

**Live completion**: `remadd` is a Script Filter, so it re-renders on every
keystroke. Whenever the word you're currently typing starts with `@`, `#`,
or `!` (and you haven't closed it with a space yet), it shows matching
lists, tags, or priorities instead of the usual preview — Tab on one fills
in just that word and puts the cursor back at the end so you keep typing
the rest (`@Groceries `, then continue with `tomorrow 9am`); these rows
are `valid: false`, so Return doesn't apply the completion.
The `@` picker only offers single-word real lists (smart lists aren't
valid add targets, and multi-word names aren't representable in this
inline shorthand anyway). Once a word is closed with a space, normal
preview mode resumes: the result becomes a live summary of everything
parsed so far, and Return adds the reminder at any point — you don't have
to use a suggestion, typing a brand-new tag or a due phrase by hand works
exactly as before.

The preview's subtitle always shows all five slots — `@list`, `#tag`,
`!priority`, `due`, `notes:` — as a standing reminder of the syntax, not
just the ones you've already filled in; a slot switches from its
placeholder to the real value as soon as it's set (e.g. `@list` becomes
`@Groceries`).

Examples:

```
remadd Buy milk @Groceries tomorrow 9am
remadd Buy milk @Groceries tom 9am
remadd Ship notes @Work !high friday 3pm #errand
remadd Pay rent due:2026-06-01 notes:autopay is off this month
```

A macOS notification confirms success or reports the failure.

## Workflow structure

```
[rem, keyword]  Script Filter          scripts/list_reminders.py
      │ (Return / ⇧ / ⌃⌥⌘, default connection — the only connection)
      └──────────────────────────────▶ Run Script   scripts/reminder_action.py

[remadd, keyword]  Script Filter        scripts/quick_add_filter.py
      │ (Return, default connection — the only connection)
      └──────────────────────────────▶ Run Script   scripts/quick_add.py
```

Two objects per keyword, one plain connection each — no modifier-gated
routing anywhere. `list_reminders.py` handles browse, the Tab/Return
action menu, the edit/reschedule/move text-entry prompts and picker, the
read-only details screen, *and* the confirm-step summary all in one
script, branching on a prefix in the query string itself (`menu:<id>`,
`edit:<id>:<text>`, `due:<id>:<text>`, `movelist:<id>:<text>`, `view:<id>`,
`confirm:<action>:<id>:<value>` — see the module docstring).
`quick_add_filter.py` similarly handles both the `@`/`#`/`!` live
completion and the running preview, but never calls `remctl` itself — its
one valid output item's `arg` is the full query text, unchanged, which is
what `quick_add_filter.py` was already parsing for the preview and is
exactly what `quick_add.py` re-parses to actually create the reminder.
Only the terminal actions (open/complete/edit/reschedule/move) reach
`reminder_action.py`, which is the only script that actually calls
`remctl` to mutate anything — `view:<id>` never does, it only reads.

## If something's not wired right

The scripts are correct and independently testable (below); if the
imported workflow misbehaves, it's almost certainly the hand-authored
`info.plist`, not the Python. In Alfred's workflow editor:

- **A keyword does nothing**: click the object, check its "Script" field
  points at `/usr/bin/python3 scripts/<name>.py "{query}"` and the
  language dropdown is set to `/bin/bash` (the script text itself invokes
  python3, so bash is just the outer shell).
- **Tab doesn't open the menu**: confirm the `rem` Script Filter item
  actually carries an `autocomplete` value (it should — check by running
  `python3 scripts/list_reminders.py ""` directly and confirming each item
  has `"autocomplete": "menu:<id>"`); if the JSON is right but Alfred still
  doesn't drill in on Tab, that's an Alfred-side quirk to report rather
  than a workflow bug. (Right Arrow is *not* the trigger — Alfred's own
  docs specify Tab for the `autocomplete` field on a `valid: true` item.)
- **"remctl not found"**: confirm `~/bin/remctl` exists, or set a
  `REMCTL_PATH` workflow variable to the correct path.
- **Permission errors**: see "Grant permissions" above — remember Alfred
  needs its own grant, separate from any terminal you tested in.
- **The Alfred window won't close / an action fires repeatedly**: this was
  a real bug in an earlier version (`vitoclose` was set on every
  connection, which vetoes Alfred's normal window-close behavior) — the
  current `info.plist` doesn't set it anywhere. If it recurs after you've
  edited the workflow in Alfred's GUI, check the connection's config
  popover for an accidentally-checked "This connection can veto Alfred's
  window closing" box.
- **`rem` alone (empty query) shows nothing**: this was also a real bug —
  the `rem` Script Filter's `argumenttype` was `0` ("Argument Required"),
  and per Alfred's own docs a Script Filter set to Required simply never
  runs until you've typed something past the keyword. It's `1`
  ("Optional") now. If this regresses after GUI edits, check the
  keyword's Argument setting in Alfred's Script Filter config panel —
  it should be "Argument Optional", not "Argument Required".

## Testing the scripts directly

No Alfred needed for this — they're plain argv/env scripts:

```bash
cd scripts
python3 list_reminders.py ""                        # today + overdue
python3 list_reminders.py "@Work"                    # one list or smart list
python3 list_reminders.py "@Wo"                        # list/smart-list picker
python3 list_reminders.py "#urgent"                     # tag filter, across all lists
python3 list_reminders.py "#ur"                          # tag picker
python3 list_reminders.py "milk"                          # search
python3 list_reminders.py "menu:23880"                     # action menu for one reminder
python3 list_reminders.py "edit:23880:New title"            # edit text-entry preview
python3 list_reminders.py "due:23880:tom 9am"                # reschedule text-entry preview
python3 list_reminders.py "movelist:23880:Gro"                 # move-to-list picker preview
python3 list_reminders.py "view:23880"                           # read-only detail screen
python3 list_reminders.py "confirm:done:23880:"                    # confirm-step preview
CONFIRM_CHANGES=0 python3 list_reminders.py "edit:23880:New title"   # with confirm disabled
action=done reminder_id=23880 python3 reminder_action.py
python3 quick_add.py "Buy milk @Groceries tomorrow 9am"
python3 quick_add_filter.py "Buy milk"                     # live preview
python3 quick_add_filter.py "Buy milk @Ta"                   # list completion
python3 quick_add_filter.py "Buy milk #wo"                    # tag completion
python3 quick_add_filter.py "Buy milk !h"                      # priority completion
```

## Extending

- **Real synced tags / sections / recurrence / subtasks**: remctl supports
  all of this behind `--private` (unsupported private ReminderKit writes).
  `quick_add.py` and `reminder_action.py` are the two places to add
  `--private` and the relevant flags.
- **Caching**: scope-level fetches (`today`, `all`, `@List`, `#tag`,
  `upcoming`, smart-list emulation's underlying "all" fetch, plus the
  `@`/`#` picker's list/smart-list/tag name lookups) are cached for
  `REMCTL_CACHE_TTL` seconds (default 5, set as a workflow variable) under
  `~/Library/Caches/com.alfredapp.reminders`. Free-text search and the
  menu/text-entry modes are never cached. `reminder_action.py` clears the
  cache after every mutation.
- **Smart-list filter coverage**: `matches_smart_list()` in
  `scripts/_remctl.py` handles tags (any/all/exclude/untagged), date
  (no-date, relative range with/without past-due), priority, and flagged
  filters, combined with `and`/`or`. Reminders' smart-list filter UI
  supports more filter kinds over time; unrecognized filter keys are
  currently ignored (treated as "always matches"), which can make a smart
  list look broader in Alfred than it does in Reminders.app.

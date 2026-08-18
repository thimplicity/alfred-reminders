# Reminders — Alfred workflow

Create, search, complete, and edit Apple Reminders from Alfred, backed by
[remctl](https://github.com/viticci/remctl). Modeled on the UX of
[Alfredo](https://alfred.app/workflows/giovanni/alfredo/) for Todoist.

## Status

This is a hand-built scaffold: the Python scripts under `scripts/` are
tested standalone (see "Testing the scripts directly" below) against real
Reminders data, including full create/edit/reschedule/complete/delete
cycles, and are the part to trust. `info.plist` is a best-effort,
hand-authored Alfred workflow definition — it has **not** been
round-tripped through the actual Alfred app, since this was built in a
headless session with no GUI access. Import it, then sanity-check each
piece against "If something's not wired right" below before relying on it.
The object graph is intentionally minimal now (two keywords, each with one
plain connection, no modifier-gated routing) specifically to keep that risk
small.

**Security note**: every script object that embeds `"{query}"` directly
into a `/bin/bash`-interpreted command line needs escaping configured,
otherwise a query containing shell metacharacters — e.g. a reminder title
with `$(command)` in it, plausible from a shared-list collaborator — would
have that command executed by bash before Python ever starts, under
Alfred's own permissions. All three script-bearing objects that do this
(the `rem` Script Filter and both Run Scripts — `remadd`'s Keyword Input
doesn't embed `{query}` in a script string at all, so it isn't affected)
now set `escaping: 102` (verified against a deanishe benchmark plist built
for exactly this class of input, and against real third-party workflows on
this machine using the same value for the same
`"{query}"`-in-shell-argument pattern) to escape
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

`@` claims the *rest* of the query as the name, so multi-word list/smart-list
names work: `rem @Sometime - AI`, `rem @Don't Forget Me`. `#tag` is a single
word (tags don't have spaces), and anything after it is a further free-text
filter, same as `flagged`/`overdue`.

**Live picker**: typing `@` or `#` with no exact match yet — rather than
erroring on partial text — shows every list/smart-list or tag whose name
contains what you've typed so far (`@` with nothing after it lists
everything). Selecting one (Return) fills in the exact name and immediately
shows that scope, since these are `autocomplete` items rather than an
action — no separate confirm step needed.

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
| Return | Open in Reminders.app |
| ⇧ Return | Mark complete |
| ⌃⌥⌘ Return | Delete (no undo) |
| ⇥ Tab | Open the action menu for that reminder |

Tab, not Right Arrow — per Alfred's own docs, an item's `autocomplete`
field is specifically Tab-triggered for a `valid: true` item (which browse
results are, since Return already does something else on them). An
earlier version of this README incorrectly said Right Arrow worked too;
it doesn't for a plain Script Filter result.

The action menu (Open / Complete / Edit title / Reschedule / Move to list /
Delete) is where editing, rescheduling, and moving live, rather than on a
modifier key — all three need you to type or pick a follow-up value, and a
modifier+Return is a one-shot fire-and-forget action with no way to open a
text box or picker afterward. Tab into the menu, then Tab or Return on
"Edit title…" / "Reschedule…" / "Move to list…" drops you into a
text-entry prompt or picker. To back out of any of these without
finishing, just backspace the query text (it's plain editable text at that
point, e.g. `menu:3724` or `edit:3724:`) back down to `rem` and continue
browsing.

Reschedule accepts `tomorrow`, `tom`, `next friday`, `2026-06-01`, `clear`,
etc. — same trailing-phrase parsing as `remadd`'s due-date detection below.
"Move to list…" is a live-filtered picker over real lists only (not smart
lists, since those are filtered views, not containers) — picking one
completes the move immediately, no further typing needed.

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
  supported by this shorthand — use `remctl add` directly for those)
- `#tag` — repeatable; without `--private` these land as inline `#hashtags`
  appended to the title (remctl limitation, not a synced tag) — see
  "Extending" below if you want real synced tags
- `!priority` — `!high` / `!medium` / `!low` (or `!h`/`!m`/`!l`)
- A trailing due-date phrase is **auto-detected** — no `due:` marker
  needed: `tomorrow`, `tom`, `next friday`, `9am`, `2026-06-01`, `+3d`, etc.
  This is a heuristic (see `looks_like_due_token()` in `scripts/_remctl.py`)
  and conservative about bare numbers specifically to avoid misreading an
  ordinary trailing number in a title (`Buy 5 apples` stays a title). If a
  title genuinely ends in a date-like word and gets misparsed, use an
  explicit `due:<phrase>` prefix to disambiguate — it still works and
  always wins over the heuristic.
- `notes:<text>` — everything after `notes:` through the end of the query
  (or up to the next recognized token) becomes the reminder's notes; never
  auto-detected, since free text can't be told apart from a due phrase by
  pattern alone

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

[remadd, keyword]  ──▶ Run Script       scripts/quick_add.py
```

Two objects per keyword, one plain connection each — no modifier-gated
routing anywhere. `list_reminders.py` handles browse, the Tab-triggered
action menu, *and* the edit/reschedule text-entry prompts all in one
script, branching on a prefix in the query string itself (`menu:<id>`,
`edit:<id>:<text>`, `due:<id>:<text>` — see the module docstring). Only the
terminal actions (open/complete/edit/reschedule/delete) reach
`reminder_action.py`, which is the only script that actually calls
`remctl` to mutate anything.

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
action=done reminder_id=23880 python3 reminder_action.py
python3 quick_add.py "Buy milk @Groceries tomorrow 9am"
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

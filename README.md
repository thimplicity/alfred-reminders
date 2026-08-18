# Reminders — Alfred workflow

Create, search, complete, and edit Apple Reminders from Alfred, backed by
[remctl](https://github.com/viticci/remctl). Modeled on the UX of
[Alfredo](https://alfred.app/workflows/giovanni/alfredo/) for Todoist.

## Status

This is a hand-built scaffold: the Python scripts under `scripts/` are
tested standalone (see "Testing the scripts directly" below) and are the
part to trust. `info.plist` is a best-effort, hand-authored Alfred workflow
definition — it has **not** been round-tripped through the actual Alfred
app, since this was built in a headless session with no GUI access. Import
it, then sanity-check each piece against "If something's not wired right"
below before relying on it.

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
| `rem #List` | Everything in one list |
| `rem #List <text>` | That list, filtered by `<text>` |
| `rem all` | Every open reminder, across every list |
| `rem all <text>` | Same, filtered by `<text>` |
| `rem upcoming [N]` | Due within N days (default 7) |
| `rem flagged` | Flagged reminders |
| `rem overdue` | Overdue only |

Row actions (modifier keys):

| Key | Action |
|---|---|
| Return | Open in Reminders.app |
| ⇧ Return | Mark complete |
| ⌥ Return | Edit title (opens a second prompt — type the new title, Return to confirm) |
| ⌃ Return | Reschedule (opens a second prompt — type a new due date, e.g. `tomorrow 9am`, `next friday`, or `clear`; Return to confirm) |
| ⌃⌥⌘ Return | Delete (no undo) |

### `remadd` — quick add

```
remadd <title words...> [#List] [@tag ...] [!priority] [due:<phrase>] [notes:<text>]
```

- `#List` — target list (single word; lists with spaces in the name aren't
  supported by this shorthand — use `remctl add` directly for those)
- `@tag` — repeatable; without `--private` these land as inline `#hashtags`
  appended to the title (remctl limitation, not a synced tag) — see
  "Extending" below if you want real synced tags
- `!priority` — `!high` / `!medium` / `!low` (or `!h`/`!m`/`!l`)
- `due:<phrase>` — everything after `due:` through the end of the query (or
  up to the next `#`/`@`/`!`/`notes:` token) is passed straight to remctl's
  date parser: `tomorrow 9am`, `next friday`, `2026-06-01`, `+3d`, etc.
- `notes:<text>` — same trailing-phrase rule, for the reminder's notes

Examples:

```
remadd Buy milk #Groceries due:tomorrow 9am
remadd Ship notes #Work !high due:friday 3pm @errand
remadd Pay rent due:2026-06-01 notes:autopay is off this month
```

A macOS notification confirms success or reports the failure.

## Workflow structure

```
[rem, keyword]  Script Filter          scripts/list_reminders.py
      │ (Return / ⇧ / ⌃⌥⌘, default connection)
      ├──────────────────────────────▶ Run Script   scripts/reminder_action.py
      │ (⌥ Edit)
      ├───▶ Script Filter (no keyword)  scripts/prompt_for_text.py
      │              │ (Return, default connection)
      │              └──────────────────────────────▶ (same Run Script above)
      │ (⌃ Reschedule)
      └───▶ (same prompt_for_text.py node, action=reschedule)

[remadd, keyword]  ──▶ Run Script       scripts/quick_add.py
```

`prompt_for_text.py` doesn't call remctl — it just echoes what you're
typing back as a confirmable item, carrying the `action` (`edit` or
`reschedule`) and `reminder_id` variables set by whichever modifier routed
you there. `reminder_action.py` is the only script that actually mutates
data; it branches on the `action` variable.

## If something's not wired right

The scripts are correct and independently testable (below); if the
imported workflow misbehaves, it's almost certainly the hand-authored
`info.plist`, not the Python. In Alfred's workflow editor:

- **A keyword does nothing**: click the object, check its "Script" field
  points at `/usr/bin/python3 scripts/<name>.py "{query}"` and the
  language dropdown is set to `/bin/bash` (the script text itself invokes
  python3, so bash is just the outer shell).
- **⌥/⌃ don't do anything special**: the alternate connections from the
  `rem` Script Filter to the "Edit / Reschedule" node need to be drawn
  while holding Option / Control respectively — redraw them in the
  connection view if they're missing or point at the wrong modifier.
- **"remctl not found"**: confirm `~/bin/remctl` exists, or set a
  `REMCTL_PATH` workflow variable to the correct path.
- **Permission errors**: see "Grant permissions" above — remember Alfred
  needs its own grant, separate from any terminal you tested in.

## Testing the scripts directly

No Alfred needed for this — they're plain argv/env scripts:

```bash
cd scripts
python3 list_reminders.py ""                     # today + overdue
python3 list_reminders.py "#Work"                 # one list
python3 list_reminders.py "milk"                  # search
action=done reminder_id=23880 python3 reminder_action.py
python3 quick_add.py "Buy milk #Groceries due:tomorrow 9am"
```

## Extending

- **Real synced tags / sections / recurrence / subtasks**: remctl supports
  all of this behind `--private` (unsupported private ReminderKit writes).
  `quick_add.py` and `reminder_action.py` are the two places to add
  `--private` and the relevant flags.
- **Caching**: scope-level fetches (`today`, `all`, `#List`, `upcoming`)
  are cached for `REMCTL_CACHE_TTL` seconds (default 5, set as a workflow
  variable) under `~/Library/Caches/com.alfredapp.reminders`. Free-text
  search is never cached since the query changes every keystroke.
  `reminder_action.py` clears the cache after every mutation.

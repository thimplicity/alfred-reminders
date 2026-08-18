"""Shared helpers for talking to the remctl CLI from Alfred script objects."""
import hashlib
import json
import os
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

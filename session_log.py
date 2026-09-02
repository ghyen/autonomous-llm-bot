"""Structured session logging: metadata by default, fixed modes, bounded life.

The previous sink appended a title and a body verbatim with `open(path, "a")`.
That is how full chains of thought, tool arguments, tool results, and user text
ended up on disk in files whose mode was whatever the umask happened to be, in
a tree with no rotation and no expiry - while the things needed to reason about
an incident (run identity, step identity, the revision that produced it) were
nowhere.

This module inverts that. One writer, one record shape:

    {"schema":1,"ts":...,"rev":...,"pid":...,"run":...,"step":...,"kind":...}

plus a small set of derived fields (tool name, status, duration, counts). Raw
content has no route into it. Content-level debugging is a separate sink behind
an explicit opt-in with its own much shorter retention, so the default
deployment never has content on disk at all.

The same line goes to the run log and to stdout, so a console line can always
be matched back to a run and a step.
"""

import json
import os
import re
import stat
import sys
import time
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path

SCHEMA = 1

# Guaranteed regardless of umask, and corrected on paths that predate this.
DIR_MODE = 0o700
FILE_MODE = 0o600

# Hard bound on any string field, applied where the write happens. Callers
# already summarise; the leak this module exists to stop came from trusting
# them, so the sink does not.
MAX_FIELD_CHARS = 200

UNKNOWN_REVISION = "unknown"
_HEX40 = re.compile(r"\A[0-9a-f]{40}\Z")

DEFAULT_MAX_BYTES = 1048576
DEFAULT_RETENTION_DAYS = 14.0
DEFAULT_CONTENT_RETENTION_HOURS = 24.0

# Policy, set once by configure() at process start. Module-level so a test can
# patch a single value without rebuilding the whole configuration.
LOG_ROOT = None
CONTENT_DEBUG = False
MAX_BYTES = DEFAULT_MAX_BYTES
RETENTION_SECONDS = DEFAULT_RETENTION_DAYS * 86400.0
CONTENT_RETENTION_SECONDS = DEFAULT_CONTENT_RETENTION_HOURS * 3600.0
REVISION = UNKNOWN_REVISION

CONTENT_DIR_NAME = "content-debug"
STARTUP_FILE_NAME = "startup.jsonl"


def _git_dir(root):
    marker = root / ".git"
    if marker.is_dir():
        return marker
    # In a worktree `.git` is a file pointing at the real git directory.
    try:
        text = marker.read_text(encoding="utf-8")
    except OSError:
        return None
    if text.startswith("gitdir:"):
        return Path(text.split(":", 1)[1].strip())
    return None


def _resolve_ref(git_dir, ref):
    common = git_dir
    try:
        common = (git_dir / (git_dir / "commondir").read_text(encoding="utf-8").strip()).resolve()
    except OSError:
        pass
    for candidate in (git_dir / ref, common / ref):
        try:
            return candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
    # A fresh clone has no loose ref file; the sha is in packed-refs.
    try:
        packed = (common / "packed-refs").read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in packed.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == ref:
            return parts[0]
    return ""


def execution_revision():
    """Short commit of the running tree, read from .git without a subprocess.

    Shelling out to `git` would depend on it being on PATH, which the minimal
    environment of a LaunchAgent does not provide - and identifying the exact
    deployment is the entire point of this field.
    """
    for root in Path(__file__).resolve().parents:
        git_dir = _git_dir(root)
        if git_dir is None:
            continue
        try:
            head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        except OSError:
            return UNKNOWN_REVISION
        if head.startswith("ref:"):
            head = _resolve_ref(git_dir, head.split(":", 1)[1].strip())
        return head[:12] if _HEX40.match(head or "") else UNKNOWN_REVISION
    return UNKNOWN_REVISION


def configure(
    log_root,
    content_debug=False,
    max_bytes=DEFAULT_MAX_BYTES,
    retention_days=DEFAULT_RETENTION_DAYS,
    content_retention_hours=DEFAULT_CONTENT_RETENTION_HOURS,
):
    """Install the logging policy. Called once, before any run can start."""
    global LOG_ROOT, CONTENT_DEBUG, MAX_BYTES
    global RETENTION_SECONDS, CONTENT_RETENTION_SECONDS, REVISION
    LOG_ROOT = Path(log_root)
    CONTENT_DEBUG = bool(content_debug)
    MAX_BYTES = int(max_bytes)
    RETENTION_SECONDS = float(retention_days) * 86400.0
    CONTENT_RETENTION_SECONDS = float(content_retention_hours) * 3600.0
    REVISION = execution_revision()


def secure_directory(path):
    """Create `path` at 0700, correcting it and anything created on the way.

    `mkdir(mode=...)` is masked by the umask and does nothing at all to a
    directory that already exists, which is how the log tree ended up readable
    by other local accounts. chmod after the fact is umask-independent.
    """
    path = Path(path)
    created = [candidate for candidate in (path, *path.parents) if not candidate.exists()]
    path.mkdir(parents=True, exist_ok=True)
    for candidate in created + [path]:
        try:
            os.chmod(candidate, DIR_MODE)
        except OSError:
            pass
    return path


def _open_secure(path):
    """Append-only descriptor whose mode is 0600 whatever the umask says."""
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, FILE_MODE)
    # The mode argument only applies when the file is created, so a file written
    # before this module existed keeps its old mode. fchmod acts through the
    # descriptor already held, so no swapped path can redirect the correction.
    if stat.S_IMODE(os.fstat(descriptor).st_mode) != FILE_MODE:
        os.fchmod(descriptor, FILE_MODE)
    return descriptor


def _rotate_if_needed(path):
    """Keep one previous generation so a run log cannot grow without bound.

    ponytail: one generation only - records older than that are dropped rather
    than archived. Upgrade path is numbered generations, if an operator ever
    needs more history than MAX_BYTES holds.
    """
    if MAX_BYTES <= 0:
        return
    try:
        if path.stat().st_size < MAX_BYTES:
            return
    except OSError:
        return
    rotated = path.with_name(path.stem + ".1" + path.suffix)
    try:
        os.replace(path, rotated)
        os.chmod(rotated, FILE_MODE)
    except OSError:
        pass


def _append(path, payload):
    path = Path(path)
    secure_directory(path.parent)
    _rotate_if_needed(path)
    descriptor = _open_secure(path)
    try:
        os.write(descriptor, (payload + "\n").encode("utf-8"))
    finally:
        os.close(descriptor)


def _bounded(value):
    if value is None:
        # Nested nulls stay null. Rendering them as the string "None" would make
        # a field's type depend on whether it happened to be set.
        return None
    if isinstance(value, str):
        return value[:MAX_FIELD_CHARS]
    if isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (list, tuple)):
        return [_bounded(item) for item in value[:20]]
    if isinstance(value, dict):
        return {str(key)[:64]: _bounded(item) for key, item in value.items()}
    return str(value)[:MAX_FIELD_CHARS]


def _record(kind, run_id, step, fields):
    record = {
        "schema": SCHEMA,
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "rev": REVISION,
        "pid": os.getpid(),
        "run": str(run_id or "-"),
        "step": int(step or 0),
        "kind": str(kind)[:64],
    }
    for key, value in fields.items():
        if value is None:
            continue
        record[str(key)[:32]] = _bounded(value)
    return record


def _dump(record):
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def log_session_event(workspace, kind, step=None, status="", duration_ms=None, **fields):
    """Append one structured record and echo the same line to stdout.

    `workspace` is None for process-level events, which have no run log to
    belong to and go to the console only.
    """
    run_id = getattr(workspace, "run_id", "") if workspace is not None else ""
    record = _record(
        kind, run_id, step, dict(fields, status=status or None, dur_ms=duration_ms)
    )
    payload = _dump(record)
    print(payload, flush=True)
    log_path = getattr(workspace, "log_path", None)
    if log_path is not None:
        try:
            _append(log_path, payload)
        except OSError as error:
            # A failed log write must never take the run down with it, but it
            # must not be silent either.
            print(
                _dump(_record("log_write_failed", run_id, step, {
                    "error": type(error).__name__, "for": str(kind),
                })),
                file=sys.stderr,
                flush=True,
            )
    return record


def log_content_debug(workspace, kind, text, step=None):
    """Write raw content, but only where an operator explicitly asked for it.

    Separate directory, separate and much shorter retention, same 0600/0700
    modes. Returns whether anything was written, so callers cannot quietly
    assume the content is available.
    """
    if not CONTENT_DEBUG or LOG_ROOT is None:
        return False
    run_id = getattr(workspace, "run_id", "") or "process"
    record = _record(kind, run_id, step, {})
    # Deliberately unbounded: a debug sink that clips is useless. This is why it
    # is opt-in, isolated, and expires in hours.
    record["content"] = str(text)
    try:
        _append(Path(LOG_ROOT) / CONTENT_DIR_NAME / (str(run_id) + ".jsonl"), _dump(record))
    except OSError:
        return False
    return True


def _dependency_versions():
    versions = {"python": sys.version.split()[0]}
    for name in ("discord.py", "openai", "httpx", "duckduckgo-search"):
        try:
            versions[name] = metadata.version(name)
        except Exception:
            # An absent dependency is itself worth recording; it explains a
            # startup failure better than a missing key would.
            versions[name] = "absent"
    return versions


def write_startup_record(config_lines):
    """Record the running commit, dependency versions, and effective config."""
    if LOG_ROOT is None:
        return None
    record = _record("startup", "", 0, {})
    record["deps"] = _dependency_versions()
    record["config"] = [_bounded(str(line)) for line in config_lines]
    payload = _dump(record)
    path = Path(LOG_ROOT) / STARTUP_FILE_NAME
    _append(path, payload)
    print(payload, flush=True)
    return path


def sweep_retention(now=None):
    """Delete expired run logs and content-debug files.

    `now` is injectable so expiry is testable without waiting days for it.
    """
    moment = time.time() if now is None else float(now)
    removed = {"logs": 0, "content": 0}
    if LOG_ROOT is None:
        return removed
    for key, directory, window in (
        ("logs", Path(LOG_ROOT) / "runs", RETENTION_SECONDS),
        ("content", Path(LOG_ROOT) / CONTENT_DIR_NAME, CONTENT_RETENTION_SECONDS),
    ):
        if window <= 0 or not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.jsonl")):
            try:
                expired = moment - path.stat().st_mtime >= window
            except OSError:
                continue
            if not expired:
                continue
            try:
                path.unlink()
            except OSError:
                continue
            removed[key] += 1
    return removed

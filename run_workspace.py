"""Opaque owner-bound run workspaces and canonical-file integrity."""

import asyncio
import json
import os
import re
import secrets
import shutil
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

from session_log import secure_directory
from workspace_io import (
    CANONICAL_NAMES,
    REVISION_PATTERN,
    atomic_write,
    is_canonical,
    read_bytes,
    resolve_path,
    revision,
    write_bytes,
)


READ_HASH_LIMIT = 128
TERMINAL_STATUSES = frozenset(
    ("completed", "stopped", "exhausted", "failed", "interrupted")
)
REVISION_PATTERN = re.compile(r"\Asha256:[0-9a-f]{64}\Z")


class RunWorkspaceError(Exception):
    """Base error for catalog lifecycle operations."""


class RunNotFoundError(RunWorkspaceError):
    """The caller cannot see the requested run."""


class RunActiveError(RunWorkspaceError):
    """The requested lifecycle operation conflicts with an active run."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _age_seconds(moment, stamp):
    """Age of an ISO timestamp. Unparseable stamps never expire, deliberately:
    retention deletes files, so an unreadable date must not authorise that."""
    try:
        return moment - datetime.fromisoformat(str(stamp)).timestamp()
    except (TypeError, ValueError):
        return float("-inf")


_revision = revision


class RunWorkspace:
    """One execution's filesystem, log, canonical locks, and read-hash LRU."""

    def __init__(
        self,
        run_id,
        owner_id,
        channel_id,
        root,
        log_path,
        status,
        created_at,
        updated_at,
    ):
        self.run_id = run_id
        self.owner_id = owner_id
        self.channel_id = channel_id
        self.root = Path(root)
        self.log_path = Path(log_path)
        self.status = status
        self.created_at = created_at
        self.updated_at = updated_at
        self._canonical_locks = {
            name: asyncio.Lock() for name in CANONICAL_NAMES
        }
        self._read_hashes = OrderedDict()

    @property
    def metadata_path(self):
        return self.root / "run.json"

    def _metadata(self):
        return {
            "run_id": self.run_id,
            "owner_id": self.owner_id,
            "channel_id": self.channel_id,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def persist(self):
        payload = json.dumps(
            self._metadata(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        atomic_write(self.metadata_path, payload)

    def clear_read_cache(self):
        self._read_hashes.clear()

    def resolve(self, path):
        return resolve_path(self.root, path)

    def _is_canonical(self, target):
        return is_canonical(self.root, target)

    def _remember(self, target, revision):
        key = str(target)
        self._read_hashes[key] = revision
        self._read_hashes.move_to_end(key)
        while len(self._read_hashes) > READ_HASH_LIMIT:
            self._read_hashes.popitem(last=False)

    def remember_worker_read(self, result):
        """Merge a worker read into the parent cache without touching the file."""
        if result.get("status") != "success":
            return result
        target = self.resolve(result["path"])
        file_revision = result["revision"]
        if self._read_hashes.get(str(target)) == file_revision:
            self._read_hashes.move_to_end(str(target))
            return {
                "status": "unchanged",
                "path": result["path"],
                "revision": file_revision,
                "reference": file_revision,
            }
        self._remember(target, file_revision)
        return result

    def remember_worker_write(self, result):
        """Update the parent read cache from a successful worker write."""
        if result.get("status") == "success":
            target = self.resolve(result["path"])
            self._remember(target, result["revision"])
        return result

    def write_lock(self, path):
        target = self.resolve(path)
        return self._canonical_locks.get(target.name) if self._is_canonical(target) else None

    def read(self, path):
        display_path = os.fspath(path)
        target = self.resolve(path)
        status, data = read_bytes(self.root, path)
        if status != "success":
            return {"status": "error", "path": display_path, "error": status}

        revision = _revision(data)
        key = str(target)
        if self._read_hashes.get(key) == revision:
            self._read_hashes.move_to_end(key)
            return {
                "status": "unchanged",
                "path": display_path,
                "revision": revision,
                "reference": revision,
            }

        self._remember(target, revision)
        content = data.decode("utf-8", errors="replace")
        truncated = False
        if not self._is_canonical(target) and len(content) > 4000:
            content = content[:4000]
            truncated = True
        return {
            "status": "success",
            "path": display_path,
            "revision": revision,
            "content": content,
            "truncated": truncated,
        }

    async def write(self, path, content, expected_revision):
        display_path = os.fspath(path)
        target = self.resolve(path)
        data = str(content).encode("utf-8")
        lock = self.write_lock(path)
        if lock is None:
            result = write_bytes(self.root, path, data, expected_revision)
            result["path"] = display_path
            if result.get("status") == "success":
                self._remember(target, result["revision"])
            return result

        async with lock:
            result = write_bytes(self.root, path, data, expected_revision)
            result["path"] = display_path
            if result.get("status") == "success":
                self._remember(target, result["revision"])
            return result


class RunCatalog:
    """Small filesystem catalog for run creation, selection, and lifecycle."""

    def __init__(self, workspace_root, log_root):
        self.workspace_root = Path(
            os.path.abspath(os.path.expanduser(os.fspath(workspace_root)))
        )
        self.log_root = Path(
            os.path.abspath(os.path.expanduser(os.fspath(log_root)))
        )
        self.runs_root = self.workspace_root / "runs"
        self.logs_root = self.log_root / "runs"
        # 0700 on every level: a run's collected data and its log are as
        # sensitive as each other, and neither is anyone else's business.
        secure_directory(self.runs_root)
        secure_directory(self.logs_root)
        self._lock = threading.RLock()
        self._runs = {}
        self._selected = {}
        self._reset_reservations = {}
        self._scan()

    def _scan(self):
        prepared = []
        for root in self.runs_root.iterdir():
            if not root.is_dir() or root.name.startswith("."):
                continue
            metadata_path = root / "run.json"
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                run_id = metadata["run_id"]
                owner_id = metadata["owner_id"]
                channel_id = metadata["channel_id"]
                status = metadata["status"]
                created_at = metadata["created_at"]
                updated_at = metadata["updated_at"]
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if (
                run_id != root.name
                or not re.fullmatch(r"[0-9a-f]{32}", str(run_id))
                or not isinstance(owner_id, int)
                or not isinstance(channel_id, int)
                or not isinstance(status, str)
            ):
                continue
            workspace = RunWorkspace(
                run_id,
                owner_id,
                channel_id,
                root,
                self.logs_root / (run_id + ".jsonl"),
                status,
                created_at,
                updated_at,
            )
            self._runs[run_id] = workspace
            if status == "active":
                workspace.status = "interrupted"
                workspace.updated_at = _now()
                workspace.persist()
            elif status == "prepared":
                prepared.append(workspace)

        for workspace in sorted(
            prepared,
            key=lambda item: (
                str(item.updated_at), str(item.created_at), item.run_id
            ),
        ):
            self._select_prepared(workspace)

    def _create(self, owner_id, channel_id, status):
        while True:
            run_id = secrets.token_hex(16)
            if run_id not in self._runs and not (self.runs_root / run_id).exists():
                break
        timestamp = _now()
        workspace = RunWorkspace(
            run_id,
            owner_id,
            channel_id,
            self.runs_root / run_id,
            self.logs_root / (run_id + ".jsonl"),
            status,
            timestamp,
            timestamp,
        )
        # mkdir without exist_ok on purpose: a colliding run id must fail loudly
        # rather than adopt someone else's directory. secure_directory then fixes
        # the mode, which mkdir's mode argument cannot do under a loose umask.
        workspace.root.mkdir()
        secure_directory(workspace.root)
        workspace.persist()
        self._runs[run_id] = workspace
        return workspace

    def _select_prepared(self, workspace):
        for key, selected_id in tuple(self._selected.items()):
            if selected_id == workspace.run_id:
                self._selected.pop(key, None)

        key = (workspace.owner_id, workspace.channel_id)
        displaced_id = self._selected.get(key)
        if displaced_id is not None and displaced_id != workspace.run_id:
            displaced = self._runs.get(displaced_id)
            if displaced is not None and displaced.status == "prepared":
                displaced.status = "interrupted"
                displaced.updated_at = _now()
                displaced.persist()
        self._selected[key] = workspace.run_id

    def lookup_owned(self, owner_id, run_id):
        with self._lock:
            workspace = self._runs.get(str(run_id))
            if workspace is None or workspace.owner_id != owner_id:
                raise RunNotFoundError("run not found")
            return workspace

    def workspaces(self, channel_id=None):
        """Known runs, optionally narrowed to one channel.

        A read accessor, not a lifecycle operation. Startup recovery and
        `!reset` both need to find a channel's runs to settle their durable
        state, and neither should be reaching into the catalog's internals.
        """
        with self._lock:
            return [
                workspace
                for workspace in self._runs.values()
                if channel_id is None or workspace.channel_id == channel_id
            ]

    def _reset_conflicts(self, owner_id, channel_id):
        return any(
            reserved_owner == owner_id or reserved_channel == channel_id
            for reserved_owner, reserved_channel in self._reset_reservations
        )

    def ensure_reset_allowed(self, owner_id, channel_id):
        with self._lock:
            if self._reset_conflicts(owner_id, channel_id):
                raise RunActiveError("reset/clear is already in progress")
            if any(
                workspace.status == "active"
                and (
                    workspace.owner_id == owner_id
                    or workspace.channel_id == channel_id
                )
                for workspace in self._runs.values()
            ):
                raise RunActiveError("an active run must stop before reset/new/clear")

    def reserve_reset(self, owner_id, channel_id):
        with self._lock:
            self.ensure_reset_allowed(owner_id, channel_id)
            token = object()
            self._reset_reservations[(owner_id, channel_id)] = token
            return token

    def cancel_reset(self, owner_id, channel_id, token):
        with self._lock:
            key = (owner_id, channel_id)
            if self._reset_reservations.get(key) is token:
                self._reset_reservations.pop(key, None)

    def prepare_reserved(self, owner_id, channel_id, token):
        with self._lock:
            key = (owner_id, channel_id)
            if self._reset_reservations.get(key) is not token:
                raise RunActiveError("clear reservation is not active")
            try:
                workspace = self._create(owner_id, channel_id, "prepared")
                self._select_prepared(workspace)
                return workspace
            finally:
                self._reset_reservations.pop(key, None)

    def acquire(self, owner_id, channel_id):
        with self._lock:
            if self._reset_conflicts(owner_id, channel_id):
                raise RunActiveError("reset/clear is in progress")
            selected_id = self._selected.pop((owner_id, channel_id), None)
            workspace = self._runs.get(selected_id) if selected_id else None
            if workspace is None or workspace.status != "prepared":
                workspace = self._create(owner_id, channel_id, "active")
            else:
                workspace.channel_id = channel_id
                workspace.status = "active"
                workspace.updated_at = _now()
                workspace.clear_read_cache()
                workspace.persist()
            return workspace

    def prepare(self, owner_id, channel_id):
        with self._lock:
            self.ensure_reset_allowed(owner_id, channel_id)
            workspace = self._create(owner_id, channel_id, "prepared")
            self._select_prepared(workspace)
            return workspace

    def resume(self, owner_id, channel_id, run_id):
        with self._lock:
            workspace = self.lookup_owned(owner_id, run_id)
            if workspace.status == "active":
                raise RunActiveError("run is active")
            workspace.channel_id = channel_id
            workspace.status = "prepared"
            workspace.updated_at = _now()
            workspace.clear_read_cache()
            workspace.persist()
            self._select_prepared(workspace)
            return workspace

    def finish(self, workspace, status):
        if status not in TERMINAL_STATUSES:
            raise ValueError("invalid run status: {0}".format(status))
        with self._lock:
            current = self._runs.get(workspace.run_id)
            if current is None or current is not workspace:
                raise RunNotFoundError("run not found")
            workspace.status = status
            workspace.updated_at = _now()
            workspace.persist()

    def delete(self, owner_id, run_id):
        with self._lock:
            workspace = self.lookup_owned(owner_id, run_id)
            self._purge(workspace)

    def sweep_retention(self, max_age_seconds, now=None):
        """Delete runs whose files outlived the retention window.

        Active and prepared runs are never swept: one is in use, and the other is
        the selection a caller is about to consume. Returns the number deleted.

        `now` is injectable so expiry is testable without waiting days for it.
        """
        max_age_seconds = float(max_age_seconds)
        if max_age_seconds <= 0:
            return 0
        moment = time.time() if now is None else float(now)
        with self._lock:
            expired = [
                workspace
                for workspace in self._runs.values()
                if workspace.status not in ("active", "prepared")
                and _age_seconds(moment, workspace.updated_at) >= max_age_seconds
            ]
        deleted = 0
        for workspace in expired:
            try:
                self._purge(workspace)
            except (RunWorkspaceError, OSError):
                continue
            deleted += 1
        return deleted

    def _purge(self, workspace):
        """Detach, then destroy: a run's directory and every log generation."""
        with self._lock:
            if workspace.status == "active":
                raise RunActiveError("run is active")
            selected_keys = [
                key for key, selected_id in self._selected.items()
                if selected_id == workspace.run_id
            ]
            for key in selected_keys:
                self._selected.pop(key, None)
            detached = self.runs_root / (
                ".deleting-{0}-{1}".format(workspace.run_id, secrets.token_hex(8))
            )
            try:
                os.replace(workspace.root, detached)
            except BaseException:
                for key in selected_keys:
                    self._selected[key] = workspace.run_id
                raise
            self._runs.pop(workspace.run_id, None)

        shutil.rmtree(detached)
        # Rotated generations are part of the run's log, so deletion covers them
        # too; unlinking only log_path would leave the older records behind.
        for path in sorted(workspace.log_path.parent.glob(workspace.run_id + "*")):
            try:
                path.unlink()
            except (FileNotFoundError, IsADirectoryError, PermissionError):
                pass

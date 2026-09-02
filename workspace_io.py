"""Filesystem primitives shared by the parent catalog and sandbox worker."""

import hashlib
import os
import re
import tempfile
from pathlib import Path


CANONICAL_NAMES = frozenset(("plan.md", "findings.md"))
REVISION_PATTERN = re.compile(r"\Asha256:[0-9a-f]{64}\Z")


def revision(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def atomic_write(path, data):
    """Write bytes through a flushed, synced temporary file and replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".{0}.".format(path.name), dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _workspace_root(root):
    root = Path(os.path.abspath(os.fspath(root)))
    if not root.is_dir():
        raise ValueError("workspace is not a directory")
    return root


def resolve_path(root, path):
    """Resolve a user path under the physical workspace root."""
    root = _workspace_root(root)
    real_root = os.path.realpath(str(root))
    raw_path = os.fspath(path)
    if isinstance(raw_path, bytes):
        raise ValueError("workspace paths must be text")
    if not os.path.isabs(raw_path):
        raw_path = os.path.join(str(root), raw_path)
    real_target = os.path.realpath(raw_path)
    try:
        inside = os.path.commonpath((real_root, real_target)) == real_root
    except ValueError:
        inside = False
    if not inside:
        raise ValueError("path escapes the current run workspace")
    relative = os.path.relpath(real_target, real_root)
    target = root if relative == os.curdir else root / relative
    if target.parent == root and target.name.casefold() in CANONICAL_NAMES:
        return root / target.name.casefold()
    return target


def is_canonical(root, target):
    root = _workspace_root(root)
    target = Path(target)
    return any(target == root / name for name in CANONICAL_NAMES)


def read_bytes(root, path, max_bytes=None):
    target = resolve_path(root, path)
    try:
        with target.open("rb") as handle:
            data = handle.read(int(max_bytes) + 1) if max_bytes is not None else handle.read()
        if max_bytes is not None and len(data) > int(max_bytes):
            return "file_bytes_limit", None
        return "success", data
    except FileNotFoundError:
        return "not_found", None
    except (IsADirectoryError, PermissionError, OSError) as error:
        return type(error).__name__, None


def read_file(root, path, max_bytes=None):
    """Return the parent-compatible read envelope without a cache."""
    display_path = os.fspath(path)
    try:
        target = resolve_path(root, path)
        status, data = read_bytes(root, path, max_bytes=max_bytes)
    except (TypeError, ValueError, OSError) as error:
        return {
            "status": "error",
            "path": display_path,
            "error": "path_escape" if isinstance(error, ValueError) else type(error).__name__,
        }
    if status == "file_bytes_limit":
        return {"status": "resource_limit", "path": display_path, "error": status}
    if status != "success":
        return {"status": "error", "path": display_path, "error": status}

    file_revision = revision(data)
    content = data.decode("utf-8", errors="replace")
    truncated = False
    if not is_canonical(root, target) and len(content) > 4000:
        content = content[:4000]
        truncated = True
    return {
        "status": "success",
        "path": display_path,
        "revision": file_revision,
        "content": content,
        "truncated": truncated,
    }


def write_bytes(root, path, data, expected_revision):
    """Write bytes and enforce canonical-file compare-and-swap semantics."""
    target = resolve_path(root, path)
    if not isinstance(data, bytes):
        raise TypeError("workspace data must be bytes")
    canonical = is_canonical(root, target)
    if canonical:
        if expected_revision is None:
            return {"status": "error", "error": "expected_revision_required"}
        if expected_revision != "absent" and not REVISION_PATTERN.fullmatch(
            str(expected_revision)
        ):
            return {"status": "error", "error": "invalid_expected_revision"}
        try:
            current_revision = revision(target.read_bytes())
        except FileNotFoundError:
            current_revision = "absent"
        if expected_revision != current_revision:
            return {
                "status": "conflict",
                "expected_revision": expected_revision,
                "current_revision": current_revision,
            }

    atomic_write(target, data)
    return {"status": "success", "revision": revision(data)}


def write_file(root, path, content, expected_revision):
    display_path = os.fspath(path)
    try:
        result = write_bytes(
            root,
            path,
            str(content).encode("utf-8"),
            expected_revision,
        )
    except (TypeError, ValueError, OSError) as error:
        return {
            "status": "error",
            "path": display_path,
            "error": "path_escape" if isinstance(error, ValueError) else type(error).__name__,
        }
    result["path"] = display_path
    return result

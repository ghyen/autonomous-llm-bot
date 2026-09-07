"""Append-only, run-local execution trajectory for tiered agent memory.

Unlike the privacy-scrubbed session log, this file is deliberately visible to
the agent: it is the backing store used to recover a URL, parameter, or result
that no longer fits in the live prompt. It never prints records to stdout.
"""

import fcntl
import hashlib
import json
import os
import stat
from collections import OrderedDict
from pathlib import Path


SCHEMA = 2
FILE_NAME = "traj.jsonl"
ARGUMENT_STRING_MAX_CHARS = 1000
RESULT_MAX_CHARS = 4000
MICRO_LINE_MAX_CHARS = 280
LOOKUP_MAX_RECORDS = 20
_COLLECTION_MAX_ITEMS = 50
_SOURCE_ARGUMENT_CHARS = 80
_SOURCE_RESULT_CHARS = 80


def trajectory_path(workspace):
    return Path(workspace.root) / FILE_NAME


def _canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _clip_middle(value, limit):
    text = str(value or "")
    limit = max(1, int(limit))
    if len(text) <= limit:
        return text
    marker = f"...[{len(text) - limit}자 생략]..."
    if len(marker) >= limit:
        return marker[:limit]
    available = limit - len(marker)
    head = (available * 3) // 4
    tail = available - head
    return text[:head] + marker + (text[-tail:] if tail else "")


def _bounded_value(value, depth=0):
    if isinstance(value, str):
        return _clip_middle(value, ARGUMENT_STRING_MAX_CHARS)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if depth >= 4:
        return _clip_middle(_canonical(value), ARGUMENT_STRING_MAX_CHARS)
    if isinstance(value, list):
        bounded = [
            _bounded_value(item, depth + 1)
            for item in value[:_COLLECTION_MAX_ITEMS]
        ]
        if len(value) > _COLLECTION_MAX_ITEMS:
            bounded.append(f"...[{len(value) - _COLLECTION_MAX_ITEMS}개 항목 생략]...")
        return bounded
    if isinstance(value, dict):
        keys = sorted(value, key=lambda item: str(item))
        bounded = {
            str(key): _bounded_value(value[key], depth + 1)
            for key in keys[:_COLLECTION_MAX_ITEMS]
        }
        if len(keys) > _COLLECTION_MAX_ITEMS:
            bounded["__omitted_items__"] = len(keys) - _COLLECTION_MAX_ITEMS
        return bounded
    return _clip_middle(str(value), ARGUMENT_STRING_MAX_CHARS)


def _decode_records(data, require_complete=False):
    records = []
    pending = []
    pending_step = None
    expected_parent = None
    previous_step = 0
    invalid = False
    text = data.decode("utf-8", errors="replace")
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except (TypeError, ValueError):
            invalid = True
            break
        if not isinstance(record, dict) or record.get("schema") != SCHEMA:
            invalid = True
            break
        body = dict(record)
        record_id = body.pop("id", None)
        step = record.get("step")
        group_end = record.get("group_end")
        if (
            not isinstance(record_id, str)
            or len(record_id) != 64
            or any(char not in "0123456789abcdef" for char in record_id)
            or record.get("parent") != expected_parent
            or hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()
            != record_id
            or not isinstance(step, int)
            or isinstance(step, bool)
            or step < 1
            or not isinstance(group_end, bool)
        ):
            invalid = True
            break
        if pending_step is None:
            if step <= previous_step:
                invalid = True
                break
            pending_step = step
        elif step != pending_step:
            invalid = True
            break
        pending.append(record)
        expected_parent = record_id
        if group_end:
            records.extend(pending)
            pending = []
            previous_step = step
            pending_step = None
    complete = not invalid and not pending
    if not complete and require_complete:
        raise ValueError("trajectory integrity check failed")
    return records, complete


def _read_descriptor(descriptor):
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks = []
    while True:
        chunk = os.read(descriptor, 65536)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _write_all(descriptor, data):
    remaining = memoryview(data)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("trajectory append made no progress")
        remaining = remaining[written:]


def _open_flags(access):
    flags = access
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def append_tool_group(workspace, step, tool_calls, results, executed_ids):
    """Append one complete assistant/tool group and return its records."""
    if not isinstance(step, int) or isinstance(step, bool) or step < 1:
        raise ValueError("trajectory step must be a positive integer")
    if len(tool_calls) != len(results):
        raise ValueError("trajectory calls and results must stay paired")

    root = Path(workspace.root)
    if not root.is_dir():
        raise ValueError("trajectory workspace does not exist")
    path = trajectory_path(workspace)
    flags = _open_flags(os.O_CREAT | os.O_APPEND | os.O_RDWR)
    descriptor = os.open(str(path), flags, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("trajectory path is not a regular file")
        os.fchmod(descriptor, 0o600)
        existing = _read_descriptor(descriptor)
        parent = None
        prior, _complete = _decode_records(existing, require_complete=True)
        if tool_calls and prior and step <= prior[-1]["step"]:
            raise ValueError("trajectory steps must increase")
        if prior:
            parent = prior[-1]["id"]

        executed_ids = {str(call_id) for call_id in executed_ids or ()}
        records = []
        for index, (call, raw_result) in enumerate(zip(tool_calls, results)):
            arguments = call.get("arguments")
            if not isinstance(arguments, dict):
                arguments = {}
            raw_arguments = _canonical(arguments)
            result = str(raw_result or "")
            body = {
                "schema": SCHEMA,
                "parent": parent,
                "step": step,
                "group_end": index == len(tool_calls) - 1,
                "call_id": str(call.get("id") or ""),
                "tool": str(call.get("name") or ""),
                "arguments": _bounded_value(arguments),
                "arguments_chars": len(raw_arguments),
                "result": _clip_middle(result, RESULT_MAX_CHARS),
                "result_chars": len(result),
                "failed": bool(call.get("failed")),
                "executed": str(call.get("id") or "") in executed_ids,
            }
            record_id = hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()
            record = dict(body)
            record["id"] = record_id
            records.append(record)
            parent = record_id

        if records:
            prefix = b"\n" if existing and not existing.endswith(b"\n") else b""
            payload = prefix + b"".join(
                (_canonical(record) + "\n").encode("utf-8")
                for record in records
            )
            _write_all(descriptor, payload)
            os.fsync(descriptor)
        return records
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def read_records(workspace):
    path = trajectory_path(workspace)
    try:
        descriptor = os.open(str(path), _open_flags(os.O_RDONLY))
    except FileNotFoundError:
        return [], False
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("trajectory path is not a regular file")
        return _decode_records(_read_descriptor(descriptor))
    finally:
        os.close(descriptor)


def lookup(workspace, step, call_id=""):
    if not isinstance(step, int) or isinstance(step, bool) or step < 1:
        return {"status": "not_found", "step": step, "call_id": str(call_id or "")}
    call_id = str(call_id or "")
    trusted, _complete = read_records(workspace)
    records = [
        record
        for record in trusted
        if record.get("step") == step
        and (not call_id or record.get("call_id") == call_id)
    ]
    if not records:
        return {"status": "not_found", "step": step, "call_id": call_id}
    truncated = len(records) > LOOKUP_MAX_RECORDS
    return {
        "status": "success",
        "step": step,
        "call_id": call_id,
        "records": records[:LOOKUP_MAX_RECORDS],
        "truncated": truncated,
        "total": len(records),
    }


def _one_line(value):
    return " ".join(str(value or "").split())


def _result_preview(record, limit):
    result = str(record.get("result") or "")
    try:
        envelope = json.loads(result)
    except (TypeError, ValueError):
        envelope = None
    if isinstance(envelope, dict):
        if envelope.get("blocked"):
            result = "blocked:" + str(envelope.get("reason") or "unknown")
        elif envelope.get("status"):
            result = str(envelope.get("status"))
            if envelope.get("error"):
                result += ":" + str(envelope["error"])
            elif envelope.get("content"):
                result += ":" + _one_line(envelope["content"])
    result = _one_line(result)
    return _clip_middle(result, limit)


def _arguments_preview(record, limit):
    return _clip_middle(_one_line(_canonical(record.get("arguments") or {})), limit)


def _clip_micro_line(line):
    if len(line) <= MICRO_LINE_MAX_CHARS:
        return line
    marker = "...[생략]]"
    return line[: MICRO_LINE_MAX_CHARS - len(marker)] + marker


def micro_index(workspace, start_step, end_step):
    if end_step < start_step:
        return []
    grouped = OrderedDict()
    records, _complete = read_records(workspace)
    for record in records:
        step = record.get("step")
        if not isinstance(step, int) or step < start_step or step > end_step:
            continue
        grouped.setdefault(step, []).append(record)

    lines = []
    for step, records in grouped.items():
        actions = []
        for record in records:
            action = (
                f"{record.get('tool') or 'unknown'}("
                f"{_arguments_preview(record, 55)}) -> "
                f"{_result_preview(record, 45)}"
            )
            actions.append(action)
        lines.append(_clip_micro_line(f"[Step {step}: {' | '.join(actions)}]"))
    return lines


def procedural_source(workspace, end_step, max_chars, start_step=1):
    max_chars = max(0, int(max_chars))
    start_step = max(1, int(start_step))
    if max_chars == 0 or end_step < start_step:
        return "", start_step - 1

    records, complete = read_records(workspace)
    if complete:
        trusted_through = end_step
    elif records:
        trusted_through = min(end_step, records[-1]["step"])
    else:
        trusted_through = start_step - 1

    grouped = OrderedDict()
    for record in records:
        step = record.get("step")
        if (
            not isinstance(step, int)
            or step < start_step
            or step > trusted_through
            or record.get("tool") in ("record_state", "finish_task")
        ):
            continue
        if not record.get("executed"):
            outcome = "blocked:" + _result_preview(record, _SOURCE_RESULT_CHARS)
        elif record.get("failed"):
            outcome = "failed:" + _result_preview(record, _SOURCE_RESULT_CHARS)
        else:
            outcome = "completed"
        grouped.setdefault(step, []).append(
            f"[Step {step}] {record.get('tool') or 'unknown'} "
            f"args={_arguments_preview(record, _SOURCE_ARGUMENT_CHARS)} -> "
            f"{outcome}"
        )

    selected = []
    used = 0
    through = max(start_step - 1, trusted_through)
    for step, lines in grouped.items():
        block = "\n".join(lines)
        cost = len(block) + (1 if selected else 0)
        if cost > max_chars - used:
            through = step - 1
            break
        selected.append(block)
        used += cost
    return "\n".join(selected), max(start_step - 1, through)

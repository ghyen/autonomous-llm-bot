"""One durable record per run: exactly what a restart needs to continue.

Every piece of run state used to live in module-global dicts, so a process
restart lost the step cursor, the cumulative summary, the authoritative ledger,
and the ids of the tool calls that had already been announced. The next message
then began the same goal again at Step 1 - and a Step 1 record on its own cannot
be told apart from a genuinely new request, so the loss was not even diagnosable
afterwards (issue #6).

This module is that missing layer and nothing more. It is not a log: it is the
program's own state, so it lives inside the run's own 0700 workspace next to
`run.json` and is written through the same atomic replace, which is what stops
an interrupt from leaving a truncated record behind.

Two rules are structural rather than documented:

* A record whose schema or shape does not match is DISCARDED, never migrated.
  Resuming from a half-understood record would put a run into a state no code
  path can produce, and the ledger it carries decides which hypotheses are
  still held true.
* `state` stays `running` for as long as the run is live. A record still
  reading `running` at startup is therefore a run that ended without a terminal
  event, which is precisely what recovery has to detect.
"""

import json
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from ledger import ResearchLedger
from run_workspace import atomic_write
from workspace_io import REVISION_PATTERN


SCHEMA = 4
SUMMARY_VERSION = 2
FILE_NAME = "state.json"
TASK_CONTRACT_VERSION = 1
ARTIFACT_MANIFEST_VERSION = 1
ARTIFACT_MANIFEST_MAX_ITEMS = 24
TASK_GOAL_MAX_CHARS = 4000
SOURCE_RUN_ID_MAX_CHARS = 64
KNOWN_BAD_CALLS_VERSION = 1
KNOWN_BAD_CALLS_MAX_ITEMS = 20


def _has_unsafe_path_chars(value):
    return any(unicodedata.category(char).startswith("C") for char in value)

# 살아 있는 런의 상태. 시작 시 이 값이 남아 있으면 종료 이벤트 없이 끝난 런이다.
RUNNING = "running"

_REQUIRED = (
    "run_id",
    "state",
    "next_step",
    "summary",
    "summary_version",
    "tail",
    "ledger",
    "interrupt",
    "announced_call_ids",
    "tool_fingerprints",
    "trajectory_gap_step",
)


def snapshot_path(workspace):
    return Path(workspace.root) / FILE_NAME


def normalize_artifact_chain(value):
    """artifact 연쇄 카운터. 깨진 값·없는 값은 0."""
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
    ):
        return 0
    return value


def normalize_known_bad_calls(value):
    """확정 실패 회피 목록을 관대하게 정규화한다. 깨진 값·없는 값은 {}.

    선택적 키라 구 레코드에 없어도 load가 실패하지 않는다.
    """
    normalized = {}
    if not isinstance(value, (dict, list)):
        return normalized
    items = value.items() if isinstance(value, dict) else value
    for item in items:
        if isinstance(value, dict):
            fingerprint, entry = item
        else:
            if (
                not isinstance(item, (list, tuple))
                or len(item) != 2
            ):
                continue
            fingerprint, entry = item
        if (
            not isinstance(fingerprint, str)
            or len(fingerprint) != 64
            or any(char not in "0123456789abcdef" for char in fingerprint)
            or not isinstance(entry, dict)
        ):
            continue
        tool = entry.get("tool")
        target = entry.get("target")
        error = entry.get("error")
        first_step = entry.get("first_step")
        count = entry.get("count", 1)
        if (
            not isinstance(tool, str)
            or not tool
            or not isinstance(target, str)
            or not target
            or not isinstance(error, str)
            or not error
            or not isinstance(first_step, int)
            or isinstance(first_step, bool)
            or first_step < 1
            or not isinstance(count, int)
            or isinstance(count, bool)
            or count < 1
        ):
            continue
        normalized[fingerprint] = {
            "tool": tool[:32],
            "target": target[:128],
            "error": error[:32],
            "first_step": first_step,
            "count": count,
        }
        if len(normalized) >= KNOWN_BAD_CALLS_MAX_ITEMS:
            break
    return normalized


def _dump(record):
    return json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")


def _normalize_task_contract(value):
    if not isinstance(value, dict):
        return None
    if value.get("version") != TASK_CONTRACT_VERSION:
        return None
    origin_message_id = value.get("origin_message_id")
    if (
        not isinstance(origin_message_id, int)
        or isinstance(origin_message_id, bool)
        or origin_message_id < 1
    ):
        return None
    goal = value.get("goal")
    if not isinstance(goal, str):
        return None
    goal = goal.strip()
    if not goal:
        return None
    return {
        "version": TASK_CONTRACT_VERSION,
        "origin_message_id": origin_message_id,
        "goal": goal[:TASK_GOAL_MAX_CHARS],
    }


def _normalize_artifact_manifest(value):
    empty = {"version": ARTIFACT_MANIFEST_VERSION, "items": []}
    if not isinstance(value, dict) or value.get("version") != ARTIFACT_MANIFEST_VERSION:
        return empty
    raw_items = value.get("items")
    if not isinstance(raw_items, list):
        return empty
    items = []
    seen = set()
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            continue
        path = raw_item.get("path")
        if not isinstance(path, str) or not path or len(path) > 512:
            continue
        path_parts = Path(path).parts
        if (
            Path(path).is_absolute()
            or ".." in path_parts
            or _has_unsafe_path_chars(path)
        ):
            continue
        kind = raw_item.get("kind")
        if kind not in ("workspace_file", "tool_output"):
            continue
        step = raw_item.get("step")
        if not isinstance(step, int) or isinstance(step, bool) or step < 1:
            continue
        item = {"path": path, "kind": kind, "step": step}
        if "revision" in raw_item:
            revision = raw_item.get("revision")
            if not isinstance(revision, str) or not REVISION_PATTERN.fullmatch(revision):
                continue
            item["revision"] = revision
        if path in seen:
            continue
        seen.add(path)
        items.append(item)
        if len(items) >= ARTIFACT_MANIFEST_MAX_ITEMS:
            break
    return {"version": ARTIFACT_MANIFEST_VERSION, "items": items}


def _normalize_source_run_id(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > SOURCE_RUN_ID_MAX_CHARS or _has_unsafe_path_chars(value):
        return None
    return value


def _normalize_source_step(value):
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        return None
    return value


def save(
    workspace,
    message_id,
    next_step,
    summary,
    tail,
    ledger,
    interrupt,
    announced_call_ids,
    tool_fingerprints,
    trajectory_gap_step,
    state=RUNNING,
    task_contract=None,
    artifact_manifest=None,
    source_run_id=None,
    source_step=None,
    known_bad_calls=None,
    artifact_chain=0,
):
    """Replace the run's record atomically.

    Call this only with a complete assistant/tool tail. A record saved with a
    partial group would restore a payload whose tool calls have no results and
    could replay already-executed side effects.
    """
    if trajectory_gap_step is not None and (
        not isinstance(trajectory_gap_step, int)
        or isinstance(trajectory_gap_step, bool)
        or trajectory_gap_step < 1
    ):
        raise ValueError("trajectory gap step must be a positive integer or None")
    record = {
        "schema": SCHEMA,
        "summary_version": SUMMARY_VERSION,
        "run_id": str(workspace.run_id),
        "owner_id": int(workspace.owner_id),
        "channel_id": int(workspace.channel_id),
        "message_id": message_id,
        "state": str(state),
        "next_step": int(next_step),
        "summary": str(summary or ""),
        "tail": list(tail or []),
        "ledger": ledger.to_dict(),
        "interrupt": dict(interrupt or {}),
        "announced_call_ids": list(announced_call_ids or ()),
        "tool_fingerprints": [
            [str(fingerprint), int(step)]
            for fingerprint, step in tool_fingerprints or ()
        ],
        "trajectory_gap_step": trajectory_gap_step,
        "task_contract": _normalize_task_contract(task_contract),
        "artifact_manifest": _normalize_artifact_manifest(artifact_manifest),
        "source_run_id": _normalize_source_run_id(source_run_id),
        "source_step": _normalize_source_step(source_step),
        "known_bad_calls": [
            [fingerprint, entry]
            for fingerprint, entry in normalize_known_bad_calls(
                known_bad_calls
            ).items()
        ],
        "artifact_chain": normalize_artifact_chain(artifact_chain),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    atomic_write(snapshot_path(workspace), _dump(record))
    return record


def load(workspace):
    """The run's record with its ledger rebuilt, or None when none is usable."""
    try:
        payload = json.loads(snapshot_path(workspace).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        return None
    if any(key not in payload for key in _REQUIRED):
        return None
    if (
        not isinstance(payload["summary_version"], int)
        or isinstance(payload["summary_version"], bool)
        or payload["summary_version"] != SUMMARY_VERSION
    ):
        return None
    if payload["run_id"] != str(workspace.run_id):
        # 다른 런의 레코드다(디렉터리를 복사한 경우). 남의 상태로 이 런을
        # 이어갈 수는 없다.
        return None
    next_step = payload["next_step"]
    if not isinstance(next_step, int) or isinstance(next_step, bool) or next_step < 1:
        return None
    if not isinstance(payload["tail"], list):
        return None
    if not isinstance(payload["interrupt"], dict):
        return None
    announced_call_ids = payload["announced_call_ids"]
    if (
        not isinstance(announced_call_ids, list)
        or any(not isinstance(item, str) or not item for item in announced_call_ids)
        or len(set(announced_call_ids)) != len(announced_call_ids)
    ):
        return None
    if not isinstance(payload["tool_fingerprints"], list):
        return None
    normalized_fingerprints = []
    for item in payload["tool_fingerprints"]:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not isinstance(item[0], str)
            or len(item[0]) != 64
            or any(char not in "0123456789abcdef" for char in item[0])
            or not isinstance(item[1], int)
            or isinstance(item[1], bool)
            or item[1] < 1
        ):
            return None
        normalized_fingerprints.append([item[0], item[1]])
    trajectory_gap_step = payload["trajectory_gap_step"]
    if trajectory_gap_step is not None and (
        not isinstance(trajectory_gap_step, int)
        or isinstance(trajectory_gap_step, bool)
        or trajectory_gap_step < 1
    ):
        return None
    try:
        payload["ledger"] = ResearchLedger.from_dict(payload["ledger"])
    except ValueError:
        return None
    payload["summary"] = str(payload["summary"] or "")
    payload["tail"] = [item for item in payload["tail"] if isinstance(item, dict)]
    payload["tool_fingerprints"] = normalized_fingerprints
    payload["task_contract"] = _normalize_task_contract(payload.get("task_contract"))
    payload["artifact_manifest"] = _normalize_artifact_manifest(
        payload.get("artifact_manifest")
    )
    payload["source_run_id"] = _normalize_source_run_id(payload.get("source_run_id"))
    payload["source_step"] = _normalize_source_step(payload.get("source_step"))
    payload["known_bad_calls"] = normalize_known_bad_calls(
        payload.get("known_bad_calls")
    )
    payload["artifact_chain"] = normalize_artifact_chain(
        payload.get("artifact_chain")
    )
    return payload


def discard(workspace):
    """Delete the record. True when there was one to delete."""
    try:
        snapshot_path(workspace).unlink()
    except OSError:
        return False
    return True


def terminate(workspace, state):
    """Mark the record as ended so startup does not treat it as unfinished.

    The record itself stays: a stopped run is still resumable on request, and
    `!reset` / `!clear` / `!delete` are what remove it. Only the live `running`
    marker is what recovery acts on.
    """
    path = snapshot_path(workspace)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    payload["state"] = str(state or "terminated")
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        atomic_write(path, _dump(payload))
    except OSError:
        return False
    return True

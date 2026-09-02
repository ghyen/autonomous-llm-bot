"""One durable record per run: exactly what a restart needs to continue.

Every piece of run state used to live in module-global dicts, so a process
restart lost the step cursor, the cumulative summary, the authoritative ledger,
and the ids of the tool calls that had already run. The next message then began
the same goal again at Step 1 - and a Step 1 record on its own cannot be told
apart from a genuinely new request, so the loss was not even diagnosable
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
from datetime import datetime, timezone
from pathlib import Path

from ledger import ResearchLedger
from run_workspace import atomic_write

SCHEMA = 1
FILE_NAME = "state.json"

# 살아 있는 런의 상태. 시작 시 이 값이 남아 있으면 종료 이벤트 없이 끝난 런이다.
RUNNING = "running"

_REQUIRED = (
    "run_id",
    "state",
    "next_step",
    "summary",
    "tail",
    "ledger",
    "interrupt",
    "executed_call_ids",
)


def snapshot_path(workspace):
    return Path(workspace.root) / FILE_NAME


def _dump(record):
    return json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")


def save(
    workspace,
    message_id,
    next_step,
    summary,
    tail,
    ledger,
    interrupt,
    executed_call_ids,
    state=RUNNING,
):
    """Replace the run's record atomically.

    Call this only on a completed assistant/tool group boundary. A record saved
    mid-group would restore a payload whose tool calls have no results, which
    both breaks the next request and invites the already-executed side effects
    to run a second time.
    """
    record = {
        "schema": SCHEMA,
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
        "executed_call_ids": [str(call_id) for call_id in executed_call_ids or ()],
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
    if not isinstance(payload["executed_call_ids"], list):
        return None
    try:
        payload["ledger"] = ResearchLedger.from_dict(payload["ledger"])
    except ValueError:
        return None
    payload["summary"] = str(payload["summary"] or "")
    payload["tail"] = [item for item in payload["tail"] if isinstance(item, dict)]
    payload["executed_call_ids"] = [str(item) for item in payload["executed_call_ids"]]
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

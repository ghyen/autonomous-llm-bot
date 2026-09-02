# ADR: macOS Seatbelt tool workers

- Status: accepted
- Date: 2026-09-02
- Scope: issues #10 and #27
- Deployment target: macOS LaunchAgent

## Context

The bot's `bash_exec`, file tools, and `web_search` previously ran in the bot
process. Path checks protected the file-tool API, but a shell could still use
the bot user's filesystem, inherited environment, network, and resources. The
remaining requirement is a low-privilege, disposable boundary around every
OS-facing call without moving the live ledger or terminal-state owner.

## Decision

`bot.py` remains the policy and lifecycle owner. Each `bash_exec`, `read_file`,
`write_file`, and `web_search` request starts one `tool_worker.py` process under
`/usr/bin/sandbox-exec` with a generated SBPL profile:

```text
sandbox-exec -p <deny-by-default-profile> <python> tool_worker.py
```

The worker accepts one JSON request, performs one operation, returns one bounded
JSON response, and exits. It has no import path to `bot.py`, `config.py`, or
`session_log.py`. The profile permits the current physical run workspace and
only the Python/system files needed to start the worker. The shared workspace
I/O module preserves realpath containment, canonical-file CAS, and atomic
writes. The system log and the parent process's environment are outside the
worker boundary.

The worker environment is an explicit allowlist. `HOME` and `TMPDIR` point to
the run workspace, and the shell receives only the fixed system/virtualenv
`PATH`, locale values, and Python no-user-site flags. A worker and its shell
descendants use dedicated process groups. Timeout, cancellation, output
overflow, resource violations, and normal shell exit all trigger descendant
cleanup before the worker response is retained.

Network access is empty by default. Operator configuration accepts only HTTP(S)
origins without credentials, query strings, fragments, or non-root paths. Direct
`bash_exec` networking is limited to loopback destinations that the macOS
Seatbelt profile can express; external direct destinations fail closed. The
`web_search` exception is narrowly fixed to `https://html.duckduckgo.com:443`:
the parent resolves that host and runs a per-call loopback CONNECT broker whose
only accepted authority and upstream addresses are that target. The worker can
see only the broker's loopback port, not arbitrary network destinations.

The default worker ceilings are:

| Ceiling | Default | Enforcement |
| --- | ---: | --- |
| CPU | 30 s | `RLIMIT_CPU` |
| Memory | 256 MiB RSS | worker + child-tree RSS monitor |
| Processes | 32 | child-tree monitor |
| Threads | 64 | worker + child-tree monitor |
| Open files | 64 | `RLIMIT_NOFILE` |
| Individual file size | 10 MiB | `RLIMIT_FSIZE` and file API bound |
| Response output | 65,536 bytes | worker and parent byte caps |
| Workspace bytes | 50 MiB | 50 ms worker monitor |

Core dumps are disabled. macOS rejects lowering its `RLIMIT_AS` address-space
ceiling, so the worker does not pretend that rlimit is enforceable there; it
uses the RSS monitor and fails closed when process information is unavailable.
The 50 ms workspace check is intentionally documented as a monitoring ceiling,
not a filesystem quota.

`record_state` and `finish_task` remain in the parent process. They mutate the
live ledger and own terminal completion, but they do not execute shell commands
or open arbitrary files; moving them would break those ownership guarantees.

## Alternatives considered

1. Docker Desktop would provide documented Linux namespaces and cgroup quotas,
   but it adds a daemon, image lifecycle, and a second macOS deployment
   contract. It remains a future option if stronger portability is required.
2. In-process environment/path checks and `resource.setrlimit` are smaller, but
   they do not create a privilege or filesystem boundary and cannot satisfy
   issue #27.

## Consequences and limitations

- `sandbox-exec` and SBPL are deprecated/undocumented macOS interfaces. The
  deployment is intentionally macOS-only, has no direct-execution fallback, and
  runs real Seatbelt integration tests on macOS CI.
- Arbitrary external `bash_exec` networking is not supported by this profile;
  operators must use the fixed web-search capability or add a future, tested
  broker for another destination.
- Resource monitors are fail-closed but sampled. A write can briefly exceed a
  monitored aggregate ceiling before the next sample; Seatbelt still prevents
  it from escaping the run root.
- The fixed broker has no general proxy capability: it accepts only the exact
  DuckDuckGo CONNECT authority and connects only to addresses resolved for that
  origin.

## Verification

The acceptance suite includes profile parameter-injection checks, workspace and
symlink escape attempts, session-log denial, environment scrubbing, network
deny/allow tests, fixed-broker authority checks, output/disk/CPU/memory/process/
thread limits, worker timeout, cancellation, and process-tree cleanup:

```bash
python -m unittest discover -v
python -m compileall -q bot.py config.py authz.py outcome.py deadlines.py ledger.py run_state.py run_workspace.py workspace_io.py session_log.py steering.py tool_sandbox.py tool_worker.py tools
```

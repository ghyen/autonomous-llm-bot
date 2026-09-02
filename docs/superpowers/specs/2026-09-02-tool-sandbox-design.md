# macOS Tool Sandbox Design

**Date:** 2026-09-02
**Issues:** #10, #27
**Target:** macOS LaunchAgent deployment described by `README.md`

## Goal

Move operating-system-facing tool work out of the Discord bot process and run
each call in a disposable macOS Seatbelt worker. The worker must expose only
the current run workspace, deny network access unless an operator allowlists a
destination, apply resource limits, and terminate as a whole on timeout,
cancellation, or a limit violation.

The stateful `record_state` and `finish_task` tools remain in the bot process.
They do not execute host commands or open arbitrary files, and moving them
would break the in-memory ledger and terminal-state ownership model. This
exception is explicit in the ADR and does not weaken the OS-facing tool
boundary.

## Options considered

1. **macOS Seatbelt worker (selected):** launch a one-shot Python worker under
   `sandbox-exec` with a generated deny-by-default SBPL profile. It uses only
   the standard library and the existing dependency set. This matches the
   selected deployment environment and preserves the current tool protocol.
2. **Docker Desktop worker:** run each call in a short-lived Linux container.
   This gives stronger, documented isolation and cgroup quotas, but requires a
   running Docker daemon, image lifecycle, and a separate container deployment
   contract for a macOS LaunchAgent.
3. **In-process restrictions:** retain the bot process and add path checks,
   environment scrubbing, and `resource.setrlimit`. This is the smallest diff
   but does not create a privilege or filesystem boundary and therefore cannot
   satisfy #27.

## Architecture

`bot.py` remains the policy and lifecycle owner. For `bash_exec`, `read_file`,
`write_file`, and `web_search`, it sends one JSON request to a new
`tool_worker.py` process launched as:

```text
sandbox-exec -p <generated-profile> <python> tool_worker.py
```

The worker reads exactly one request from stdin, performs exactly one
operation, writes one bounded JSON response to stdout, and exits. It reuses a
small shared workspace-I/O module for the canonical path, revision, and
atomic-write behavior; it never imports `bot.py`, reads the Discord/LLM
configuration, or opens the system log.

The parent creates a new process group for every worker. The worker creates a
second process group for its shell child and installs a signal handler that
terminates that active group before the worker exits; the parent terminates the
worker group and awaits the worker. The parent reads stdout/stderr with a hard
byte cap and kills the worker group before retaining an oversized result.

`record_state` and `finish_task` continue through the existing in-process
dispatcher. A worker failure is returned as an explicit structured tool error
and is classified by the existing tool-failure policy.

## Seatbelt policy

The generated profile has `(deny default (with no-callout))` and allows only:

- read access to the current run root;
- write, create, rename, and unlink access below the current run root;
- read access to the Python executable, its standard-library/site-package
  roots, the worker and shared workspace-I/O modules, and the fixed system
  runtime paths required to start it;
- the worker's IPC pipes and `/dev/null`;
- process fork/exec needed for the requested shell command;
- outbound sockets matching resolved IP/port pairs from the explicit network
  allowlist.

The system log root, the user's home credential files, the bot repository
outside the explicitly allowlisted worker and shared workspace-I/O modules,
and every other filesystem path remain denied. A
shell command can still execute any binary visible in the approved executable
paths, but that binary inherits the same Seatbelt restrictions.

The allowlist is empty by default. An operator supplies URL origins through
`TOOL_NETWORK_ALLOWLIST`; the loader validates scheme, host, and port. Direct
`bash_exec` networking is supported only for loopback destinations because the
macOS Seatbelt profile cannot safely express the external host/IP rules needed
for arbitrary shell traffic; non-local direct destinations fail closed. For
`web_search`, the parent resolves the exact configured
`https://html.duckduckgo.com:443` origin and exposes only a per-call loopback
CONNECT broker. The broker accepts that exact authority and connects only to
the resolved target addresses. DNS resolver access is limited to the system
resolver socket. Search reports an actionable denial when its fixed origin is
absent.

No fallback executes a tool directly when `sandbox-exec` is missing, the
profile fails to compile, a destination cannot be resolved, or the runtime
paths cannot be represented safely. The tool fails closed.

## Resource policy

The operator-visible configuration gets bounded positive defaults for:

- CPU seconds (`RLIMIT_CPU`);
- memory RSS (worker and child tree; macOS rejects lowering `RLIMIT_AS`);
- open file descriptors (`RLIMIT_NOFILE`);
- maximum individual file size (`RLIMIT_FSIZE`);
- process creation budget using a per-worker process-tree monitor;
- output bytes read by the parent;
- aggregate bytes below the run workspace, sampled by the worker monitor;
- the existing wall-clock shell/tool deadlines.

The worker also disables core dumps and fails closed when process information
needed by the monitors is unavailable. A limit violation terminates the worker
group, returns a deterministic error, and leaves the run observable as a
failed tool call. Limits are applied before starting the shell or network
client. Aggregate workspace accounting is bounded by the monitor interval;
the Seatbelt write boundary prevents any overrun from reaching outside the
run. That known sampling ceiling is recorded in the ADR rather than presented
as a filesystem quota.

## Error and cleanup behavior

- Worker startup or JSON protocol failure: structured `worker_unavailable`
  error; no direct execution fallback.
- Seatbelt or network denial: a deterministic structured error without the
  command or secret-bearing path.
- CPU, memory, process, file, output, or workspace limit: structured
  `resource_limit` error.
- Timeout/cancellation: kill the worker process group, await the child, and
  preserve the existing timeout/cancellation semantics.
- Successful file reads/writes keep the existing revision and CAS response
  shapes. The parent updates its per-run read-hash cache from the worker's
  returned revision so unchanged reads remain references.

## Testing and documentation

Tests will cover configuration parsing, generated-profile validation, worker
JSON protocol, successful workspace operations, path/symlink escape attempts,
log and home credential denial, network deny/allow behavior, every resource
limit, output truncation, cancellation, and process-tree cleanup. macOS CI is
required for real Seatbelt integration tests; non-macOS jobs may run only
platform-neutral unit tests and must never enable a direct-execution fallback.

The README will document macOS as the supported tool-execution platform,
required `sandbox-exec` availability, the allowlist and resource settings, and
the deprecated/undocumented SBPL limitation. An ADR will record why Seatbelt
was selected for this macOS-only deployment and the aggregate-disk sampling
ceiling. #10 remains open until the #27 acceptance checks and the existing
#10 path/log checks pass together; then both issues can be closed without
claiming a partial fix.

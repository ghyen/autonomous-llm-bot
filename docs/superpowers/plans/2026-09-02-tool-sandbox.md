# macOS Tool Sandbox Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run every OS-facing tool call in a one-shot macOS Seatbelt worker with deny-by-default filesystem/network access, bounded resources, and complete timeout cleanup.

**Architecture:** `bot.py` remains the stateful dispatcher. `tool_sandbox.py` builds a per-call SBPL profile and supervises a disposable `tool_worker.py` process through JSON stdin/stdout. `workspace_io.py` contains the path, revision, and atomic-write primitives shared by the parent workspace object and the worker; `record_state` and `finish_task` remain in-process because they own live ledger/terminal state.

**Tech Stack:** Python 3.10/3.12, standard-library `asyncio`/`subprocess`/`resource`/`urllib.parse`, macOS `sandbox-exec` Seatbelt, existing `duckduckgo-search`, `unittest`, GitHub Actions on macOS.

## Global Constraints

- The supported tool-execution platform is macOS; Linux and Windows must never fall back to unsandboxed execution.
- `sandbox-exec` absence, SBPL compilation failure, unresolved allowlist destinations, malformed worker output, and unsupported resource limits fail closed.
- The network allowlist is empty by default; `web_search` uses the deterministic HTML DuckDuckGo backend and requires its host to be configured.
- Worker environment contains only `PATH`, `LANG`, `HOME`, `TMPDIR`, `PYTHONDONTWRITEBYTECODE`, and `PYTHONNOUSERSITE`.
- Worker output is capped at 65,536 bytes, each file at 10 MiB, each workspace at 50 MiB, address space at 256 MiB, CPU at 30 seconds, processes at 32, threads at 64, and open files at 64 by default.
- Existing `BASH_TIMEOUT_SECONDS=60` remains the wall-clock ceiling for a shell worker; tool-stage deadlines remain the outer batch ceiling.
- No new runtime dependency is added.
- `record_state` and `finish_task` remain parent-process exceptions and are documented as such.

---

### Task 1: Add validated tool-sandbox configuration

**Files:**
- Modify: `config.py:1-320`
- Modify: `.env.example`
- Test: `test_config.py`

**Interfaces:**
- Produces `NetworkAllowlistEntry = tuple[str, str, int]` and `parse_network_allowlist(raw, field) -> tuple[NetworkAllowlistEntry, ...]`.
- Extends `BotConfig` with `tool_cpu_seconds: float`, `tool_memory_bytes: int`, `tool_process_limit: int`, `tool_thread_limit: int`, `tool_open_files: int`, `tool_file_bytes: int`, `tool_output_bytes: int`, `tool_disk_bytes: int`, and `tool_network_allowlist: tuple[NetworkAllowlistEntry, ...]`.

- [ ] **Step 1: Write failing configuration tests**

```python
def test_tool_limits_have_bounded_defaults(self):
    config = load_config(env=env(), env_file=None)
    self.assertEqual(config.tool_cpu_seconds, 30.0)
    self.assertEqual(config.tool_memory_bytes, 268435456)
    self.assertEqual(config.tool_process_limit, 32)
    self.assertEqual(config.tool_thread_limit, 64)
    self.assertEqual(config.tool_open_files, 64)
    self.assertEqual(config.tool_file_bytes, 10485760)
    self.assertEqual(config.tool_output_bytes, 65536)
    self.assertEqual(config.tool_disk_bytes, 52428800)
    self.assertEqual(config.tool_network_allowlist, ())

def test_network_allowlist_accepts_origins_and_rejects_ambiguous_values(self):
    config = load_config(
        env=env(TOOL_NETWORK_ALLOWLIST="https://example.com:443,http://127.0.0.1:8080"),
        env_file=None,
    )
    self.assertEqual(
        config.tool_network_allowlist,
        (("https", "example.com", 443), ("http", "127.0.0.1", 8080)),
    )
    for raw in ("example.com:443", "ftp://example.com", "https://user@example.com", "https://example.com/path"):
        with self.subTest(raw=raw):
            with self.assertRaises(ConfigError):
                load_config(env=env(TOOL_NETWORK_ALLOWLIST=raw), env_file=None)
```

- [ ] **Step 2: Run the focused tests and confirm they fail because the fields/parser are absent**

Run: `.venv/bin/python -m unittest test_config.LoadConfigTest test_config.DeadlineConfigTest -v`

Expected: FAIL with missing `BotConfig` attributes or missing network parser behavior.

- [ ] **Step 3: Implement parsing and immutable config fields**

Add these defaults and parse values with the existing strict helpers:

```python
DEFAULT_TOOL_CPU_SECONDS = 30.0
DEFAULT_TOOL_MEMORY_BYTES = 268435456
DEFAULT_TOOL_PROCESS_LIMIT = 32
DEFAULT_TOOL_THREAD_LIMIT = 64
DEFAULT_TOOL_OPEN_FILES = 64
DEFAULT_TOOL_FILE_BYTES = 10485760
DEFAULT_TOOL_OUTPUT_BYTES = 65536
DEFAULT_TOOL_DISK_BYTES = 52428800
```

`parse_network_allowlist` must split commas, require `http` or `https`, reject credentials/query/fragment/non-root paths, normalize the hostname to lowercase, choose port 80/443 when omitted, and return entries in input order without duplicates. Empty input returns `()`.

- [ ] **Step 4: Add `.env.example` entries and startup diagnostics**

Document the exact keys and values:

```env
TOOL_CPU_SECONDS=30
TOOL_MEMORY_BYTES=268435456
TOOL_PROCESS_LIMIT=32
TOOL_THREAD_LIMIT=64
TOOL_OPEN_FILES=64
TOOL_FILE_BYTES=10485760
TOOL_OUTPUT_BYTES=65536
TOOL_DISK_BYTES=52428800
TOOL_NETWORK_ALLOWLIST=https://html.duckduckgo.com:443
```

`TOOL_NETWORK_ALLOWLIST` is an example, not an implicit default; the effective default remains empty.

- [ ] **Step 5: Run focused tests and commit**

Run: `.venv/bin/python -m unittest test_config -v`

Expected: all configuration tests pass.

```bash
git add config.py .env.example test_config.py
git commit -m "feat: configure macOS tool sandbox limits"
```

### Task 2: Extract shared workspace I/O and add the one-shot worker protocol

**Files:**
- Create: `workspace_io.py`
- Create: `tool_worker.py`
- Modify: `run_workspace.py:1-230`
- Test: `test_tool_worker.py`
- Test: `test_workspace_integrity.py`

**Interfaces:**
- `workspace_io.resolve_path(root: str | Path, path: str) -> Path`
- `workspace_io.read_bytes(root: str | Path, path: str) -> tuple[str, bytes | None]`
- `workspace_io.write_bytes(root: str | Path, path: str, data: bytes, expected_revision: str | None) -> dict`
- `tool_worker.handle_request(request: dict) -> dict`
- Worker request operations: `{"operation": "read_file", "workspace": str, "path": str}`, `{"operation": "write_file", "workspace": str, "path": str, "content": str, "expected_revision": str | None}`, `{"operation": "bash_exec", "workspace": str, "command": str, "env": dict, "limits": dict, "timeout": float}`, and `{"operation": "web_search", "query": str, "timeout": float}`.
- Worker response is one JSON object with `status` plus operation-specific fields; it never includes the raw command in an error.

- [ ] **Step 1: Write failing worker protocol tests**

```python
class WorkerProtocolTest(unittest.TestCase):
    def test_read_and_write_requests_return_existing_revision_shapes(self):
        with tempfile.TemporaryDirectory() as root:
            result = handle_request({"operation": "write_file", "workspace": root, "path": "note.txt", "content": "inside", "expected_revision": None})
            self.assertEqual(result["status"], "success")
            read = handle_request({"operation": "read_file", "workspace": root, "path": "note.txt"})
            self.assertEqual(read["content"], "inside")
            self.assertTrue(read["revision"].startswith("sha256:"))

    def test_outside_paths_are_refused_before_access(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            result = handle_request({"operation": "read_file", "workspace": root, "path": outside})
            self.assertEqual(result["status"], "error")
            self.assertNotIn("content", result)
```

- [ ] **Step 2: Run the focused tests and confirm they fail**

Run: `.venv/bin/python -m unittest test_tool_worker -v`

Expected: FAIL because the worker module and shared primitives do not exist.

- [ ] **Step 3: Move path/revision/atomic-write primitives into `workspace_io.py`**

Keep `RunWorkspace.resolve`, `read`, and `write` response formats unchanged by delegating to the shared functions. Preserve the existing `realpath` containment and canonical-file CAS rules. Add `RunWorkspace.remember_worker_read(path, revision)` so a parent-side worker read updates the existing LRU without reading bytes in the bot process.

- [ ] **Step 4: Implement `tool_worker.handle_request` and its stdin/stdout entrypoint**

The entrypoint must read one line, parse one JSON object, call `handle_request`, emit one compact JSON line, flush, and exit. It must reject a second non-empty line. File operations use `workspace_io`; no import of `bot.py`, `config.py`, or `session_log.py` is permitted.

- [ ] **Step 5: Run worker and existing workspace tests, then commit**

Run: `.venv/bin/python -m unittest test_tool_worker test_workspace_integrity.CanonicalIntegrityTest test_workspace_integrity.CatalogIsolationTest -v`

Expected: all focused tests pass and existing direct `RunWorkspace` callers retain their response shapes.

```bash
git add workspace_io.py tool_worker.py run_workspace.py test_tool_worker.py test_workspace_integrity.py
git commit -m "feat: add one-shot tool worker protocol"
```

### Task 3: Build the macOS Seatbelt profile and resource supervisor

**Files:**
- Create: `tool_sandbox.py`
- Modify: `tool_worker.py:1-260`
- Test: `test_tool_sandbox.py`

**Interfaces:**
- `tool_sandbox.build_profile(workspace: Path, runtime_paths: tuple[Path, ...], network_addresses: tuple[tuple[str, int], ...]) -> str`
- `tool_sandbox.resolve_network_addresses(entries: tuple[NetworkAllowlistEntry, ...]) -> tuple[tuple[str, int], ...]`
- `tool_sandbox.run_worker(workspace, request: dict, timeout: float, token=None) -> dict`
- `tool_worker.apply_limits(limits: dict) -> None`
- `tool_worker.workspace_usage(root: str | Path) -> int`
- `tool_worker.process_tree_counts(root_pid: int) -> tuple[int, int]`

- [ ] **Step 1: Write failing profile and supervisor tests**

```python
class ProfileTest(unittest.TestCase):
    def test_profile_denies_by_default_and_contains_only_parameterized_workspace(self):
        profile = build_profile(Path("/tmp/run root"), (Path("/usr/bin/python3"),), ())
        self.assertIn("(deny default", profile)
        self.assertIn("(subpath (param \"WORKSPACE\"))", profile)
        self.assertNotIn("allow network-outbound)\n", profile)
        self.assertNotIn("/Users/edwin", profile)

    def test_network_addresses_are_deduplicated_and_resolved(self):
        self.assertEqual(
            resolve_network_addresses((("https", "localhost", 443),)),
            (("127.0.0.1", 443),),
        )

    @unittest.skipUnless(sys.platform == "darwin", "macOS Seatbelt integration")
    async def test_sandboxed_worker_can_read_workspace_but_not_home(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "inside.txt").write_text("inside", encoding="utf-8")
            inside = await run_worker(
                root,
                {"operation": "read_file", "workspace": root, "path": "inside.txt"},
                timeout=5,
            )
            outside = await run_worker(
                root,
                {"operation": "bash_exec", "workspace": root, "command": "cat $HOME/.ssh/id_rsa"},
                timeout=5,
            )
        self.assertEqual(inside["status"], "success")
        self.assertEqual(inside["content"], "inside")
        self.assertIn(outside["status"], ("error", "resource_limit"))
        self.assertNotIn("PRIVATE KEY", json.dumps(outside))
```

The test must create a concrete temporary file, issue a `read_file` request, and issue a `bash_exec` request for `$HOME/.ssh`; the former must succeed and the latter must return `sandbox_denied` or a nonzero exit without exposing file bytes.

- [ ] **Step 2: Run the focused tests and confirm they fail for missing profile/supervisor behavior**

Run: `.venv/bin/python -m unittest test_tool_sandbox -v`

Expected: FAIL because the profile builder, resolver, and supervisor are absent.

- [ ] **Step 3: Implement strict macOS capability checks and SBPL generation**

`tool_sandbox` must require `sys.platform == "darwin"` and an executable `/usr/bin/sandbox-exec`. The profile must use `(deny default (with no-callout))`, parameterized `WORKSPACE`, `WORKER`, `PYTHON_PREFIX`, `PYTHON_STDLIB`, `PYTHON_SITE`, and `WORKER_DIR`, allow only the current workspace plus runtime files, allow `process-fork`, `process-exec*`, and same-sandbox signalling, and add one `(allow network-outbound (remote tcp "IP:PORT"))` rule per resolved allowlisted address. It must allow only the resolver socket and standard resolver files needed for allowlisted DNS.

Escape SBPL string parameters by replacing `\\` with `\\\\`, `"` with `\\"`, and newline/carriage-return with `\\n`/`\\r`. Do not interpolate model-provided command text into SBPL.

- [ ] **Step 4: Implement resource limits and monitors in the worker**

`apply_limits` must set `RLIMIT_CPU`, `RLIMIT_AS`, `RLIMIT_NOFILE`, `RLIMIT_FSIZE`, and `RLIMIT_CORE=(0, 0)` before the shell/network operation. Absence or failure of a required limit raises a deterministic `resource_limit` error. A monitor samples workspace logical bytes, descendant process count, and `ps -M` thread count every 50 ms; on a limit breach it kills the child process group and returns `resource_limit`.

For `bash_exec`, use `/bin/bash -c <command>` with `HOME` and `TMPDIR` equal to the run root, a fixed executable `PATH`, `start_new_session=True`, and bounded concurrent stdout/stderr readers. The child group is killed on timeout, output overflow, workspace overflow, or monitor violation. The worker installs a SIGTERM handler that kills the active child group before exiting.

For `web_search`, use `DDGS(timeout=...)` with `backend="html"` so the only intended HTTP host is `html.duckduckgo.com`; no proxy environment variable is inherited.

- [ ] **Step 5: Implement the async parent supervisor**

`run_worker` launches:

```python
await asyncio.create_subprocess_exec(
    "/usr/bin/sandbox-exec", "-p", profile,
    sys.executable, "-B", str(WORKER_PATH),
    stdin=PIPE, stdout=PIPE, stderr=PIPE,
    start_new_session=True, env=sanitized_worker_env,
)
```

It writes one JSON line, reads at most `tool_output_bytes + 8192` response bytes, waits under the supplied timeout, sends SIGTERM then SIGKILL to the worker process group when needed, awaits reaping, and never invokes the old direct shell path. Invalid/empty responses become `worker_unavailable`.

- [ ] **Step 6: Run profile, resource, and cleanup tests, then commit**

Run: `.venv/bin/python -m unittest test_tool_sandbox -v`

Expected: all macOS integration tests pass, including network denial, allowlisted local TCP, output cap, disk cap, process/thread cap, timeout, and descendant cleanup.

```bash
git add tool_sandbox.py tool_worker.py test_tool_sandbox.py
git commit -m "feat: enforce macOS Seatbelt tool limits"
```

### Task 4: Route bot tools through the worker without changing tool contracts

**Files:**
- Modify: `bot.py:1-760`
- Modify: `run_workspace.py:80-230`
- Modify: `test_workspace_integrity.py:439-630`
- Modify: `test_cancellation_flow.py:925-1090`
- Modify: `test_duplicate_tool_policy.py:510-560`

**Interfaces:**
- Preserve `async def tool_bash_exec(workspace, command) -> str`, `async def tool_read_file(workspace, path) -> str`, `async def tool_write_file(workspace, path, content, expected_revision) -> str`, and `async def tool_web_search(query) -> str`.
- `tool_bash_exec`, `tool_read_file`, and `tool_write_file` call `tool_sandbox.run_worker`; `tool_web_search` supplies a worker request with the configured network policy.

- [ ] **Step 1: Add failing routing assertions**

```python
async def test_os_facing_tools_use_the_sandbox_supervisor(self):
    workspace = self.catalog().acquire(TEST_USER_ID, CHANNEL_A)
    with patch.object(bot.tool_sandbox, "run_worker", AsyncMock(return_value={"status": "success", "stdout": "ok", "stderr": "", "exit_code": 0})) as run:
        await bot.tool_bash_exec(workspace, "printf ok")
    run.assert_awaited_once()
    self.assertEqual(run.await_args.args[0], workspace)
```

Add equivalent assertions for file read/write and ensure `record_state` remains local and receives the live ledger object.

- [ ] **Step 2: Run the focused routing tests and confirm they fail because bot tools still execute locally**

Run: `.venv/bin/python -m unittest test_workspace_integrity.ToolIntegrationTest -v`

Expected: FAIL because `bot.py` still calls `asyncio.create_subprocess_shell` or `RunWorkspace` directly.

- [ ] **Step 3: Replace the direct tool implementations with worker calls**

Remove the `DDGS` import and the direct subprocess/file code from `bot.py`. Convert worker responses to the existing tool result strings/JSON envelopes. Update the parent read-hash cache from worker revisions and guard canonical writes with the existing per-file asyncio lock while the worker CAS operation runs. Keep `_terminate_process_tree` as the worker-group cleanup primitive and extend it to SIGTERM-then-SIGKILL.

- [ ] **Step 4: Preserve failure classification and cancellation semantics**

Worker errors for `read_file`/`write_file` retain `status="error"` or `status="conflict"`; bash failures retain a terminal `[exit code: N]`; network/resource/sandbox failures start with `[Error` or a structured envelope recognized by `_tool_result_failed`. `RunCancelled` must cancel the worker and re-raise after the group is reaped.

- [ ] **Step 5: Run tool integration and cancellation tests, then commit**

Run: `.venv/bin/python -m unittest test_workspace_integrity test_cancellation_flow test_duplicate_tool_policy -v`

Expected: all existing path, log, environment, process-tree, duplicate-call, and cancellation tests pass through the worker.

```bash
git add bot.py run_workspace.py test_workspace_integrity.py test_cancellation_flow.py test_duplicate_tool_policy.py
git commit -m "feat: route OS tools through sandbox workers"
```

### Task 5: Document the deployment contract and verify real macOS CI

**Files:**
- Create: `docs/adr/2026-09-02-macos-seatbelt-tool-sandbox.md`
- Modify: `README.md:35-330`
- Modify: `.github/workflows/ci.yml`
- Modify: `test_tool_sandbox.py`

**Interfaces:**
- CI runs the full suite on `macos-latest` for Python 3.10 and 3.12.
- README exposes the exact environment variables, default-deny behavior, worker exception for state tools, and fail-closed behavior.

- [ ] **Step 1: Write failing documentation/CI assertions**

```python
class DocumentationContractTest(unittest.TestCase):
    def test_readme_describes_fail_closed_macos_sandbox(self):
        text = Path("README.md").read_text(encoding="utf-8")
        self.assertIn("sandbox-exec", text)
        self.assertIn("TOOL_NETWORK_ALLOWLIST", text)
        self.assertIn("직접 실행 fallback", text)
```

- [ ] **Step 2: Run the documentation test and confirm the new contract is absent**

Run: `.venv/bin/python -m unittest test_tool_sandbox.DocumentationContractTest -v`

Expected: FAIL until README and ADR are updated.

- [ ] **Step 3: Add the ADR and README deployment sections**

The ADR must record: macOS LaunchAgent as target, Seatbelt selection, Docker and in-process alternatives, `sandbox-exec` deprecation/undocumented SBPL risk, empty network default, resource defaults, monitor sampling ceiling for aggregate disk, and the explicit parent-process state-tool exception. README must show a valid `.env` snippet with `TOOL_NETWORK_ALLOWLIST=https://html.duckduckgo.com:443` when search is desired.

- [ ] **Step 4: Change CI to macOS and keep both supported Python versions**

Use:

```yaml
runs-on: macos-latest
strategy:
  fail-fast: false
  matrix:
    python-version: ["3.10", "3.12"]
```

Retain credential scan, compile, and full unittest steps. Add an explicit `sandbox-exec -p '(version 1) (deny default)' /usr/bin/true` preflight that is allowed to fail only with a clear CI error.

- [ ] **Step 5: Run the full local verification and commit documentation**

Run:

```bash
.venv/bin/python -m unittest discover -v
.venv/bin/python -m compileall -q bot.py config.py authz.py outcome.py deadlines.py ledger.py run_state.py run_workspace.py workspace_io.py session_log.py steering.py tool_sandbox.py tool_worker.py tools
.venv/bin/python tools/check_no_credential_defaults.py bot.py config.py authz.py outcome.py deadlines.py ledger.py run_state.py run_workspace.py workspace_io.py session_log.py steering.py tool_sandbox.py tool_worker.py tools
git diff --check
```

Expected: every unittest passes, compile and credential scan exit 0, and no whitespace errors remain.

```bash
git add docs/adr README.md .github/workflows/ci.yml test_tool_sandbox.py
git commit -m "docs: document macOS tool sandbox deployment"
```

### Task 6: Review, merge, and close the linked issues only after acceptance checks

**Files:**
- No source changes unless review finds a defect.
- GitHub: PR for `fix/tool-sandbox-worker`, issues #10 and #27.

- [ ] **Step 1: Inspect the complete diff against `origin/main`**

Run: `git diff --stat origin/main...HEAD && git diff --check origin/main...HEAD`

Confirm the diff contains no direct-execution fallback, no credential-bearing environment propagation, no unrelated refactor, and no PR-body structure changes.

- [ ] **Step 2: Dispatch the mandatory code review**

Review base SHA is `b6e74cef1b170481669cc8a025a30cf016eb42f3`; review head SHA is `git rev-parse HEAD`. Fix every Critical/Important finding before creating the PR.

- [ ] **Step 3: Create a PR preserving the repository’s existing required sections**

Use the existing PR template sections in their original order, link `Fixes #27` and `Related #10`, and include exact local test commands/results. Do not replace the body with a new structure.

- [ ] **Step 4: Wait for GitHub CI and inspect the diff/checks**

Require the macOS Python 3.10 and 3.12 jobs to pass. If a job fails, reproduce it locally in the worktree, fix the root cause with a failing regression test, and rerun the complete suite.

- [ ] **Step 5: Merge only after all #27 and #10 criteria pass**

Verify the following against fresh test output: worker privilege/process boundary, no outside/home/log access, network deny and allowlist behavior, CPU/memory/process/thread/file/output/disk limits, process-tree cleanup, TOCTOU neutralized by the worker boundary, ADR/README accuracy. Merge the PR into `main` only then.

- [ ] **Step 6: Close both issues with evidence**

Post a concise Korean comment to #10 and #27 containing the merged PR, merge SHA, CI run, and acceptance-test summary. Close both issues with completed state only after the comments are visible and the GitHub API reports `merged=true` and `state=closed`.

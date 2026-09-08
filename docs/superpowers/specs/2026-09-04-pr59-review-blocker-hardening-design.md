# PR #59 Review-Blocker Hardening Design

**Date:** 2026-09-04
**Issue:** #49
**Target:** staged merge result for PR #59 (`feat/tiered-context-compaction-issue49`)

## Goal

Close the staged merge's one Critical and two Important semantic-review findings
without adding an operating-system sandbox, an LLM request, a directory scan, a
dependency, or a compatibility layer. Preserve playbook inheritance and useful
artifact discovery while making their trust levels explicit.

The deployment is already treated as an isolated environment. Preventing direct
host access from `bash_exec` is therefore out of scope. This change instead
prevents parent-process features from following a Bash-planted playbook symlink,
prevents model-authored playbook text from becoming system-authority content,
and prevents arbitrary tool-result text from claiming artifact provenance.

## Performance budget

The implementation must satisfy all of these constraints:

- no additional LLM calls;
- no increase in playbook text or artifact-discovery limits;
- no recursive or whole-directory scan;
- no persistent artifact manifest or new process;
- one descriptor-relative playbook open and regular-file check only when an
  automatic successor run is created;
- one artifact-directory descriptor plus at most ten candidate file checks per
  rollover;
- no new dependency.

LLM and tool execution remain the dominant costs. The new work is bounded local
metadata handling and filesystem checks outside the per-token path.

## Options considered

1. **Targeted trust-boundary hardening (selected):** reject linked inherited
   playbooks, move playbook text to a lower-trust message in the existing LLM
   request, and persist host-captured artifact provenance in trajectory records.
   This closes the review blockers with bounded constant work.
2. **Call-site-only checks:** use `Path.is_symlink()` for inheritance and require
   discovered paths to exist. This is a smaller diff, but both checks are
   check/use-racy and file existence does not prove that the host artifact writer
   created the pointer.
3. **Full Bash isolation and artifact manifest:** add an OS-level filesystem
   sandbox and a durable artifact registry. This provides a broader boundary but
   is unnecessary for the deployment, adds operational complexity, and exceeds
   the performance and PR scope.

## Architecture

### No-follow playbook inheritance

Add a small descriptor-relative reader in `workspace_io.py` for an exact
root-level filename. It opens the run root as a directory, opens the leaf with
`O_NOFOLLOW`, and accepts it only when `fstat()` reports a regular file. The file
is read from the accepted descriptor, so no separate symlink check can race with
the read.

`RunCatalog._inherit_canonical()` uses this reader for `playbook.md`. A missing,
linked, or non-regular source is treated as no inheritable playbook. Automatic
run creation continues instead of failing. An unexpected I/O error from an
otherwise regular source still aborts and rolls back creation, preserving the
existing retry/data-loss contract. A valid regular file is copied with the
existing atomic destination write. The candidate remains only the newest
same-owner, same-channel run; absence remains a durable deletion boundary; and
`plan.md` and `findings.md` remain fresh.

This is deliberately not a generic Bash sandbox or a rewrite of every workspace
operation. It closes the parent-process confused-deputy path reported for
inheritance with one bounded read at run creation.

### Lower-trust playbook context

`build_system_content()` no longer embeds `playbook.md`. Message zero therefore
contains only application-owned policy, summary framing, and authoritative
ledger state.

At the final autonomous-agent request boundary, a helper reads the playbook
cache-free and, when non-empty, inserts one application-labelled
`role="assistant"` context message immediately after message zero and before
history/current user content. The label states that the body is model-authored
procedural context from this or a prior run and must yield to system policy and
current user instructions.

The helper returns a derived request list. It does not append the context message
to `messages_payload`, trajectory data, durable tail state, or Discord history.
Normal requests and correlation retries use the same finalized request, so the
playbook appears exactly once. Same-run updates are visible on the next request,
and automatically inherited bytes remain useful without acquiring system-role
authority.

Delimiters and labels are explanatory only; the protocol role is the trust
boundary. No phrase filter is introduced because arbitrary natural-language
filtering is incomplete and unnecessary once the content is structurally below
system and user messages.

### Host-captured artifact provenance

Create a dispatch-group-local ordered artifact slot list aligned with the tool-call
occurrences. A slot is populated only after `_store_tool_artifact()` successfully
writes an oversized `bash_exec`, `web_search`, or `lookup_trajectory` result for
that exact dispatched occurrence. Tool output text remains unchanged for the
model.

When the tool group is appended to `traj.jsonl`, copy each optional slot into the
matching ordered call record as `artifact_path`. Do not join provenance by
protocol call ID: duplicate IDs remain distinct occurrences, and only the
executed occurrence may own an artifact. The existing trajectory hash covers
the new field. Increment the strict trajectory schema version; old versions
remain rejected with no migration or compatibility branch, as required by the
workspace policy and current strict-state design.

Tier-2 rollover no longer regex-scans arbitrary `record["result"]` text for
artifact-shaped strings. It considers only `artifact_path` metadata and accepts
a candidate when all of these are true:

- the record says that the ordered call occurrence executed;
- the path exactly equals the deterministic current call-ID artifact name;
- the path is below the current run's `artifacts/` directory;
- descriptor-relative `O_NOFOLLOW` opening succeeds; and
- `fstat()` reports a regular file.

The artifact directory is opened once. One shared budget permits at most ten
unique candidate file checks across newly transitioning and retained artifact
discoveries; repeated paths reuse the first validation result. Rejected
candidates consume validation work but no discovery slot. Once the check budget
or the ten-line discovery capacity is exhausted, artifact validation stops.
Valid newly transitioning artifacts remain ahead of prior discoveries. Ordinary
non-artifact discoveries keep their existing behavior.

The ten-check ceiling deliberately favors predictable rollover cost over
exhaustive recovery after many deleted artifacts. If future use requires deeper
recovery, it can move to a bounded paginated artifact index rather than an
unbounded scan.

## Data flow

### Automatic successor

```text
new automatic run
  -> choose newest same-owner/channel prior run
  -> no-follow open prior playbook.md
  -> regular file: atomic copy
  -> missing/link/non-regular: inherit nothing
  -> publish new run
```

Explicitly prepared `!new` and `!reset` runs do not enter this inheritance path
and remain blank.

### Agent request

```text
base payload
  -> rebuild application-owned system message
  -> read current playbook cache-free
  -> insert one assistant provenance/context message when non-empty
  -> validate payload
  -> existing completion call (and existing retry, if needed)
```

No additional model request is introduced.

### Artifact discovery

```text
oversized tool result
  -> host writes deterministic artifact
  -> ordered occurrence slot records the host-created path
  -> matching trajectory record persists and hashes artifact_path
  -> rollover reads trusted metadata, not result-text matches
  -> no-follow regular-file validation, at most ten checks
  -> valid new artifacts + prior discoveries + generic discoveries
```

## Failure behavior

- A linked, missing, FIFO, socket, directory, or otherwise non-regular inherited
  `playbook.md` is skipped; run creation succeeds.
- An unexpected read error on an otherwise regular inherited playbook keeps the
  existing acquisition rollback and retry behavior; it is never converted into
  a durable blank successor.
- A blank playbook produces no assistant context message.
- Missing, wrong-call, wrong-run, malformed, linked, or non-regular artifact
  metadata is ignored and cannot displace a valid or prior discovery.
- Artifact storage failure keeps the existing bounded live-output fallback and
  records no trusted artifact path.
- A trajectory schema mismatch keeps the existing fail-closed behavior. No
  migration is added.

## Tests

Add focused RED regressions before implementation:

1. Plant `playbook.md` as an external symlink through the real `bash_exec`, finish
   the run, and automatically acquire its successor. The successor must start,
   inherit no linked bytes, and expose no canary in any model message.
2. Put useful and hostile text in a normal playbook. It must be absent from the
   sole system message and present exactly once in an assistant context message
   before the current user goal. Cover same-run refresh, automatic inheritance,
   rollover, and correlation retry without adding a completion call.
3. Put ten exact but nonexistent artifact strings in arbitrary trajectory result
   text and one real host-produced artifact in the transition window. The real
   artifact must remain first, the forgeries must be absent, and nine prior
   discoveries must remain.
4. Reject nonexistent, wrong-call, wrong-run, final-entry symlink, and mere-text
   artifact candidates. Preserve oversized `lookup_trajectory` ownership by its
   outer request call ID.
5. Verify at most ten candidate file validations occur during rollover.
6. Preserve canonical CAS, duplicate ordered-call marking, playbook-only
   automatic inheritance, explicit reset freshness, strict schema rejection,
   parser boundaries, cancellation, and process cleanup through the existing
   focused and full suites.

## Documentation

Update `README.md` to say that `playbook.md` is inherited only by an automatic
successor created when no prepared/resumed run is selected. Explicit `!new` and
`!reset` runs remain fresh. Describe playbook content as lower-trust inherited
procedural context rather than system-prompt rules. Keep the documented artifact
path and lookup behavior, but state that only host-recorded current-run
artifacts enter rollover discovery.

## Acceptance criteria

- The three reproduced security regressions fail before and pass after the
  implementation.
- The final semantic review reports Critical=0 and Important=0.
- The focused suites and full test suite pass, along with compile, credential,
  diff, and unresolved-index checks.
- Validation confirms no additional LLM call, unbounded filesystem operation,
  dependency, compatibility path, or production background process.

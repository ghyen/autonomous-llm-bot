# Legacy Run Context Recovery Plan

## Scope

Implement bounded recovery for legacy runs and an explicit fresh-run import
path. Preserve `!resume` as same-run resume, and make rollover telemetry
reflect actual context changes.

## Tasks

1. Add durable metadata normalization.
   - Keep old state records readable.
   - Preserve optional source run id and source step for imported runs.
   - Add tests for round-trip and backward-compatible loading.

2. Add deterministic legacy recovery helpers in `bot.py`.
   - Read trusted trajectory records through existing `trajectory` helpers.
   - Recover the goal from the durable contract/ledger/trajectory state in that
     order, without fabricating original user text.
   - Populate an empty ledger with bounded evidence-only entries from successful
     tool results.
   - Rebuild a bounded tiered summary when missing or behind the trusted step.
   - Reconcile `next_step` and persist the rebuilt state before the request.
   - Add focused unit tests first, then implementation.

3. Add bounded fresh-run import.
   - Add `!fork <run-id>` and `/fork <run-id>`.
   - Require the source run to be owned and inactive.
   - Create a prepared run without copying old files or full history.
   - Save only the recovered contract, summary, ledger, and source metadata.
   - Test source isolation and imported state.

4. Fix rollover reporting.
   - Compare the returned summary with its input.
   - Persist/log `rollover` only when the summary actually changed.
   - Add a no-op regression test plus keep existing compaction tests passing.

5. Verify and publish.
   - Run targeted tests, then the complete test suite and `git diff --check`.
   - Review the diff for secrets and unrelated changes.
   - Push the `ghyen` remote branch and open a PR against `main`.

## Test-first order

Write failing tests for recovery, fork state, next-step reconciliation, and
no-op rollover before changing production code. Use temporary catalogs and
synthetic trajectory records only; never use the production Mac data in tests.

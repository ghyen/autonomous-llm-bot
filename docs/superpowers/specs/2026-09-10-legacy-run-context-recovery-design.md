# Legacy Run Context Recovery

## Goal

Make a stopped or legacy run resumable without injecting its full history into
the next request. The recovered context is bounded to the goal, durable ledger,
investigation summary, source step, and the next saved step.

## Design

1. Recover legacy state before building a resume request.
   - Prefer the durable task contract and ledger goal.
   - Reconstruct missing evidence from successful trajectory tool results.
   - Rebuild a bounded summary when the stored summary is empty or behind the
     trusted trajectory step.
   - Reconcile `next_step` to the last trusted trajectory step plus one.
   - Do not invent hypotheses or conclusions from tool output.

2. Add an explicit `!fork <run-id>`/`/fork <run-id>` path for a fresh run.
   - The source run remains unchanged.
   - Only the recovered goal, summary, ledger, source run id, and source step
     are written to the new run state.
   - Old trajectory and artifact files are not copied or exposed to the model.
   - `!resume` keeps its existing same-run semantics.

3. Make rollover telemetry truthful.
   - Set `rollover:true` only when the durable summary actually changes.
   - A compaction attempt that returns identical context is recorded as
     `rollover:false`.

4. Keep all imported data bounded and deterministic.
   - Use existing trajectory readers and ledger normalization.
   - Cap reconstructed evidence and summary input at the existing context
     limits.
   - Missing original user text is reported as best-effort recovery; it is not
     fabricated from a tool call.

## Verification

- Unit tests cover legacy goal/evidence/summary recovery, next-step
  reconciliation, fork state contents, and no-op rollover telemetry.
- Existing run-state and bot tests must continue to pass.
- A clean `ghyen` branch and pull request are created after verification.

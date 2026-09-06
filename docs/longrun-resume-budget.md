# Renew execution budget while preserving cumulative resume cursor

Each new invocation, including a resumed run, receives MAX_AGENT_LOOPS additional steps. The saved step number remains cumulative and is used for checkpoint cadence and diagnostics. A run resuming at step 2001 can advance, and it still stops after its new bounded allowance; this does not introduce an unbounded autonomous loop.

## Verification

Regression starts beyond the old global limit and confirms exactly two tools run with a two-step allowance, followed by step-budget exhaustion.

Unit tests use synthetic model responses and Discord doubles. No 24-hour soak test or production deployment was performed for this change.


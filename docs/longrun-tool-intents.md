# Persist tool dispatch intent and block ambiguous replay

Tool batches write pending_tools before dispatch. A completed group snapshot clears the intent; interruption or failed result persistence keeps it and prevents automatic or ordinary manual replay. The owner must inspect the effects and choose !resume <run-id> retry or skip (also available as the slash resume pending option). Retry permits re-execution; skip records unknown effects, not success. This prevents silent replay across the batch commit gap; it does not promise exactly-once external effects.

## Verification

Tests cover an effect followed by interruption, explicit skip, intent-write failure before dispatch, and result-commit failure before another tool batch.

Unit tests use synthetic model responses and Discord doubles. No 24-hour soak test or production deployment was performed for this change.


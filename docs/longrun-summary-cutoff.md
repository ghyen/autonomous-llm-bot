# Keep original context when a rollover summary is truncated

A rollover response ending with finish_reason=length or the backend cutoff sentinel no longer replaces original history. Original messages and the prior summary remain available for a later compaction attempt. Normal summaries and timeout fallback keep their existing behavior.

## Verification

Regression cases exercise both cutoff forms; the full standalone suite passes.

Unit tests use synthetic model responses and Discord doubles. No 24-hour soak test or production deployment was performed for this change.


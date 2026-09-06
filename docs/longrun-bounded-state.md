# Bound model-facing ledger state without deleting durable evidence

Model-facing ledger views and record_state feedback have a bounded state block. Full evidence, hypotheses and conclusions stay in state.json, and the partial view directs the agent to query omitted entries before using them. Whole records are selected, with hypotheses prioritized; rollover correction cannot re-expand the summary beyond its limit. This bounds the ledger contribution, not every possible tool result or the tokenizer's total context.

## Verification

Large-ledger tests cover 2,000 evidence entries, durable-state preservation and rollover summary bounds.

Unit tests use synthetic model responses and Discord doubles. No 24-hour soak test or production deployment was performed for this change.


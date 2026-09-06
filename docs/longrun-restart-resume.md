# Resume interrupted runs once after Discord becomes ready

Recovered unfinished runs resume from their saved cursor after Discord on_ready, without a new user message. Repeated ready events cannot dispatch the same startup queue twice. Authorization is rechecked, and newer channel activity or a changed prepared selection wins. Stopped and uncertain-tool runs are not resumed. The original Discord message must remain fetchable; fetch failures are logged and leave manual resume available.

## Verification

Tests cover ready-triggered dispatch without new input, duplicate-ready suppression, revoked access, stopped/uncertain state and a new selection arriving during message fetch. Merge the tool-intents PR before enabling automatic recovery in production.

Unit tests use synthetic model responses and Discord doubles. No 24-hour soak test or production deployment was performed for this change.


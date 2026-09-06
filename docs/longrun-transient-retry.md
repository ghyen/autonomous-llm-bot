# Retry transient model failures within one deadline and tolerate checkpoint timeout

Completion calls retry connection/transport failures, HTTP 429 and server errors up to three attempts, with 1s/2s backoff inside the existing total stage deadline and cancellation scope. Non-transient API errors are not retried; insufficient backoff budget preserves the original error. A checkpoint timeout now reports checkpoint failure internally and continues research, while user cancellation still terminates immediately.

## Verification

Tests cover recovery, non-retriable errors, attempt limits, deadline/backoff cancellation and continuing after checkpoint timeout.

Unit tests use synthetic model responses and Discord doubles. No 24-hour soak test or production deployment was performed for this change.


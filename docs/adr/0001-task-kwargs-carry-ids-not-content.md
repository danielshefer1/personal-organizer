# ADR 0001 — Task kwargs carry identifiers, never user content

**Status:** accepted (Iteration 01)

## Context

Procrastinate builds `Job.call_string` as `task_name[id](kwarg=<repr>, ...)` and interpolates
it directly into its own log messages, at INFO on job start/end and at ERROR on failure
(`procrastinate/worker.py`, `jobs.py`). Those are stdlib log records, so they bypass
structlog's key allowlist entirely.

From Iteration 02 the ingress path defers a job per inbound WhatsApp message. If the message
body were passed as a task kwarg, **every processed job would log the user's message
verbatim**, at INFO, in production — defeating Iteration 01's "logs contain no raw PII" and
Iteration 12's redaction audit.

No regex can fix this in general: a calendar title or a note is ordinary prose and is not
detectable by pattern.

## Decision

1. **Task kwargs carry identifiers only** — a message id, a tenant id, a job id. The worker
   loads content from the database inside the task. This aligns with the spec's own ingress
   rule ("verify signature → persist (unique message ID) → defer job"), so the body is
   already persisted by the time the job is deferred.
2. **Defence in depth:** `observability/redaction.py` strips the argument list from any
   `name[id](...)` construct before rendering, so a future kwarg that does carry content is
   still not logged. See `_CALL_STRING`.

## Consequences

- Iteration 02's `defer` call passes `message_id`, not `body`.
- A task signature that accepts free text is a review failure, not a style preference.
- `tests/api/test_no_pii_in_logs.py::test_procrastinate_job_arguments_are_redacted` fails if
  the call-string rule is removed.

## What this does not guarantee

Redaction of our own structured logs is an allowlist and is therefore strong. Redaction of
third-party log *messages* is pattern-based and is therefore best-effort: identifiers
(phone, email, IBAN, PAN, JWT) and call strings are removed, arbitrary prose is not. The
structural rule above is what actually keeps content out of third-party messages.

# ADR 0002 — Ingress stores a message and defers its job in one transaction

**Status:** accepted (Iteration 02)

## Context

Meta redelivers a webhook it does not see acknowledged for up to seven days, and sends
duplicates even when nothing failed. The v7 plan's Done-When for Iteration 02 is "replaying
the same webhook ten times creates one job", and its ingress rule is "verify signature →
persist (unique message ID) → defer job → 200".

Read naively, "persist, then defer" is two writes, on two drivers: the application talks to
Postgres through SQLAlchemy on asyncpg, and Procrastinate (which ships no asyncpg connector)
talks to it through psycopg. Two drivers cannot share a transaction, so there is a window
after the message commits and before its job does. A deploy, an OOM kill or a dropped
connection in that window leaves a stored message with no job. Worse, Meta's redelivery
then hits the unique constraint, is treated as a duplicate, and defers nothing either: the
message is silently never processed.

## Decision

1. **One transaction.** `PgIngressStore` borrows a psycopg connection from Procrastinate's
   own pool and, for each message, runs
   `INSERT … ON CONFLICT (channel, provider_message_id) DO NOTHING RETURNING id` and — only
   when a row came back — `configure_task(..., connection=conn).defer_async(inbox_id=…)`.
   Procrastinate 3.10's `connection=` option writes the job row on that same connection.
   Both commit or neither does: *a row exists if and only if its job exists*.
2. **The ledgers are not tenant tables.** `channel_inbox` and `channel_outbox` carry no
   `tenant_id` and get no RLS: the tenant is not known at ingress, since resolving a sender
   to a tenant is the worker's first step. They are the "webhook event log" that
   `Database.system_session` was reserved for.
3. **Ordering by sender.** Each job is deferred with `lock=inbox:<channel>:<hash of sender>`,
   so one sender's messages run one at a time, in arrival order. The lock is hashed with the
   log pepper because `procrastinate_jobs.lock` is a plain column that outlives the job.

## Alternatives rejected

- **Insert on asyncpg, defer on psycopg.** Leaves the window described above.
- **`queueing_lock=<message id>`.** Unique only while the job is still `todo`; once it has
  run, a redelivery enqueues it again. It deduplicates the queue, not the history.
- **An `enqueued_at` column, re-defer on duplicate, plus a sweeper.** Closes the window by
  repairing it after the fact. More moving parts for the same guarantee, and the repair path
  is the one that never runs in tests.

## Consequences

- One write path speaks raw SQL to a psycopg connection instead of using the ORM models.
  `tests/db/test_models_match_migrations.py` keeps the models, the migration and (through
  the ingress tests) that SQL describing one schema.
- `tests/db/test_ingress_dedupe.py` pins the guarantee: ten sequential and ten concurrent
  replays give one row and one job, and a defer that fails after the insert leaves neither.
- The worker must still be idempotent (`processed_at`), because a job can run twice after
  a crash mid-task — that is Procrastinate's at-least-once, not ingress.
- **Content from Iteration 03.** The worker copies inbound content into the tenant-scoped
  `messages` table (RLS) and nulls `body` and `raw` here, so long-lived personal content
  sits under RLS and this table stays a transit and deduplication ledger. Until then,
  allowlisted users' messages are kept here in the clear; strangers' are purged on
  processing. Retention of `raw` is on Iteration 12's audit list.

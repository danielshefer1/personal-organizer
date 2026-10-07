# ADR 0006: Invites in the database

Status: accepted, 2026-10-07.

## Context

Until now the invite list was `WHATSAPP__ALLOWED_PHONES`. Inviting one person meant editing
a Railway variable and redeploying the api and the worker. The circle is ≤ ~50 people, and
docs/plan-iteration-03.md deferred "DB-backed invites" until it grew past a handful.

## Decision

- **An `invites` table, outside RLS.** An invite exists before its tenant does, so — like
  `channel_inbox` — it has no `tenant_id` and no policy, and is read through
  `system_session`. One open invite per number (a partial unique index on `phone` where
  neither `used_at` nor `revoked_at` is set); used and revoked rows stay as history.
  `app_user` has no `DELETE` on it.
- **The env list stays, as the fallback.** `is_invited` is true for a number on
  `WHATSAPP__ALLOWED_PHONES` or with an open invite, the env list checked first. The owner's
  number lives in the env list, so no state of the table can lock them out.
- **Identity still wins.** Only a sender with no tenant is asked. `enrol` marks the invite
  used in the transaction that creates the tenant.
- **Revoke and suspend are separate.** `po-admin revoke` cancels an unused invite and
  refuses a used one. Cutting off a member is `po-admin suspend` (`tenants.status =
  'suspended'`, which the gate already turns away); `unsuspend` restores `active` or
  `onboarding` from `onboarding_step`. Env-listed numbers cannot be suspended.
- **The bot never writes first.** `po-admin invite` prints `https://wa.me/<bot number>`,
  with no prefilled text, for the owner to forward. An unsolicited first message is what
  gets a QR-gateway number banned, Meta would need a template, and a prefilled "Hi" would
  make every invitee English (D11).
- **`po-admin` runs as `app_user`.** No new role and no new definer function: members are
  found through `resolve_tenant` and changed in their own `tenant_session`.

## Consequences

- Inviting, revoking and suspending need no deploy.
- An invite revoked in the milliseconds between the gate's check and `enrol` still enrols
  that person; `suspend` covers it.
- A first message through Meta that carries no phone number (a user hiding theirs behind a
  username) is not matched to an invite, which is keyed by phone.
- Invitations from chat need only a caller of `create_invite` that passes
  `invited_by_tenant_id`; the column is there.
- Not done: invite expiry, a cap on invites, deleting members.

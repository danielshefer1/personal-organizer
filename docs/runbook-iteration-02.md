# Runbook — Iteration 02: connecting WhatsApp

Iteration 02 is done when **replaying the same webhook ten times creates one job, an unsigned
or wrongly signed POST is rejected, and an unregistered sender uses no LLM budget**. All
three are mechanised in the test suite (below). What remains is connecting a real Meta app,
which needs a Meta developer account.

The code merges and deploys **with WhatsApp switched off** (`WHATSAPP__ENABLED` unset). In
that state `/webhooks/whatsapp` is not mounted — a 404, not a route that rejects — and no
WhatsApp variable is required. Nothing here has to happen before merging.

| Phase | Needs | Proves |
|---|---|---|
| A. Staging, simulated | nothing from Meta | the deployed path: signature → inbox → queue → worker → reply attempt |
| B. Staging, live | a Meta app and its test number | Meta can reach us, and we can reach a phone |

Production stays `WHATSAPP__ENABLED=false` until Phase B has passed on staging.

---

## The variables

| Variable | Service | Value |
|---|---|---|
| `WHATSAPP__ENABLED` | api, worker | `true` |
| `WHATSAPP__APP_SECRET` | api | App settings → Basic → **App secret** |
| `WHATSAPP__VERIFY_TOKEN` | api | generate: `uv run python -c "import secrets; print(secrets.token_hex(32))"` |
| `WHATSAPP__ACCESS_TOKEN` | worker | a **System User** token (step B3) |
| `WHATSAPP__PHONE_NUMBER_ID` | api, worker | WhatsApp → API Setup → **Phone number ID** (not the number) |
| `WHATSAPP__ALLOWED_PHONES` | worker | comma-separated E.164: `+31612345678,+447700900123` |
| `WHATSAPP__GRAPH_API_VERSION` | worker | default `v24.0`; match what API Setup shows |

With `ENABLED=true`, a service refuses to boot unless all four credentials are set, and names
every missing one in one message. Set them on **both** services, even the ones only the
other service uses: settings are validated identically everywhere, so the simplest correct
thing is one shared set.

The verify token must be at least 32 characters. It travels in a query string, and at that
length the log and Sentry scrubbers recognise it as a token; a short one would be logged.

---

## Phase A — staging, simulated (optional, recommended)

Proves everything on our side on the deployed stack, before Meta is involved.

1. Set the variables on staging with **self-made** values: generate an app secret and a
   verify token, and use `x` for the access token and `1` for the phone number id. Allowlist
   your own number. Redeploy.
2. Check the startup lines: `api.started` and `worker.started` should both show
   `whatsapp_enabled: true`, and the worker should show your `allowlist_size`.
3. From your laptop, with the **same** `WHATSAPP__APP_SECRET` and `WHATSAPP__PHONE_NUMBER_ID`
   exported:

   ```sh
   uv run po-whatsapp simulate --url https://<staging-api>/webhooks/whatsapp \
     --from +<your number> --replay 10
   ```

   Expect ten `-> 200` lines. Then in the logs:
   - `ingress.recorded` with `message_count: 1` once, and `duplicate_count: 1` nine times
   - `inbound.handled` with `disposition: allowed`, once
   - `whatsapp.graph_error` with `error_code: 190` (bad token). **This is the expected
     result** with a fake token; the outbox row is marked `failed` and nothing retries.
4. Also try a forged signature (any other secret) and expect `-> 401` and
   `whatsapp.signature_rejected reason: mismatch`.

`uv run po-whatsapp hash +<your number>` (with staging's `LOGGING__PII_PEPPER` exported)
prints the `sender_hash` your lines are logged under.

---

## Phase B — staging, live

1. **Create the app.** developers.facebook.com → My Apps → Create app → type **Business** →
   add the **WhatsApp** product. Meta creates a test WhatsApp Business Account with a free
   **test number**.
2. **API Setup** (WhatsApp → API Setup):
   - note the **Phone number ID** of the test number
   - under *To*, add your own phone as a **recipient** and confirm the code WhatsApp sends.
     The test number can only message recipients listed here (up to five).
3. **A token that does not expire.** The token on the API Setup page lasts about 24 hours.
   For staging, Business settings → Users → **System users** → add one (Admin) → assign it
   the app and the WhatsApp account → **Generate token** with `whatsapp_business_messaging`
   and `whatsapp_business_management`. Store it as `WHATSAPP__ACCESS_TOKEN`.
4. **App secret**: App settings → Basic → App secret → `WHATSAPP__APP_SECRET`.
5. **Set the variables** on staging's api and worker (table above), with
   `WHATSAPP__ALLOWED_PHONES=+<your number>`. Redeploy and wait for both to be healthy.
6. **Check the domain first.** `curl https://<staging-api>/health`. The domain must target
   port **8080** — see the PORT trap in `docs/runbook-iteration-01.md`. Meta's verification
   in the next step fails with an unhelpful message if the domain answers 502.
7. `uv run po-whatsapp check` with the staging token and number id exported. Expect
   `ok: <name> (<number>)`. Error `190` means the token is wrong or expired.
8. **Register the webhook**: WhatsApp → Configuration → Webhook → Edit:
   - Callback URL: `https://<staging-api>/webhooks/whatsapp`
   - Verify token: the value of `WHATSAPP__VERIFY_TOKEN`
   - **Verify and save.** Meta sends the GET handshake now; the log shows `whatsapp.verified`.
   - Under *Webhook fields*, **subscribe to `messages`**. Nothing else is needed.
9. **Send "hi"** from your phone to the test number. Expect, within a couple of seconds, blue
   ticks and "Got it — I'm not smart yet." The logs show `ingress.recorded` →
   `inbound.handled disposition: allowed` → `outbox.accepted`, then more `ingress.recorded`
   lines with `status_count: 1` as Meta reports sent, delivered and read.
10. **A stranger.** From a phone that is a listed recipient but *not* allowlisted: one
    "Sorry — this assistant is invite-only.", then silence for 24 hours
    (`disposition: stranger_muted`).
11. **Capture real payloads.** Send one of each: text, a voice note, an image, a reaction.
    Then read them back:

    ```sql
    SELECT message_type, raw FROM channel_inbox WHERE disposition = 'allowed'
    ORDER BY received_at DESC LIMIT 10;
    ```

    Redact the phone numbers and names, and replace the matching files in
    `tests/fixtures/whatsapp/`. Check whether a user id field appears alongside `from` or
    `wa_id`: if it is named differently from what
    `providers/channel/whatsapp/parser.py:BSUID_KEYS` lists, that tuple is the one line to
    change.

### If it does not work

| Symptom | Cause |
|---|---|
| Meta: "The callback URL or verify token couldn't be validated" | Staging is down, the domain targets the wrong port, `WHATSAPP__ENABLED` is not true on the api (404), or the verify token differs. The log shows `whatsapp.verify_rejected` for the last one. |
| Burst of `whatsapp.signature_rejected reason: mismatch` | `WHATSAPP__APP_SECRET` is wrong. Nothing is lost: Meta keeps redelivering for up to seven days, and every message arrives once the secret is fixed. |
| No POSTs arrive at all | The `messages` field is not subscribed, or the app is not subscribed to the WhatsApp account: `GET /<WABA_ID>/subscribed_apps` with the token. |
| Your own number gets the invite-only reply | It is not in the allowlist as Meta reports it. Compare `po-whatsapp hash +<number>` with the `sender_hash` on the `inbound.handled` line. Some countries' numbers arrive in a different form than people write them — use the one the payload shows. |
| `whatsapp.graph_error error_code: 131047` | Outside the 24-hour window. Only happens for a message redelivered after a long outage; those are marked `stale` and not answered. |
| `outbox.outcome_unknown` | A send timed out after the request left. Not retried, by design (docs/adr/0003). Many of them: raise `WHATSAPP__SEND_TIMEOUT_S`. |
| One user's messages stop being processed | A job holding their lock stalled. `system:retry_stalled_jobs` re-queues it within about a minute of its worker's heartbeat stopping; check it is running (`job.stalled_retried` in the worker log). |

---

## Where the Done-When is proved

| Criterion | Test |
|---|---|
| Replay ×10 → one job | `tests/db/test_ingress_dedupe.py` — ten sequential and ten concurrent identical signed POSTs through the real router give one inbox row and one job; a defer that fails after the insert leaves neither |
| Unsigned or wrongly signed POST rejected | `tests/api/test_whatsapp_webhook.py::TestSignature` — every malformed and forged variant is 401 and reaches nothing |
| Unregistered sender uses no LLM budget | `tests/db/test_handle_inbound.py::TestStrangers` — a stranger never reaches `on_allowed`, the seam Iteration 04 turns into the agent |

## Deliberately not in this iteration

- **Budgets and rate limits** per tenant. There are no tenants until Iteration 03 and no LLM
  until 04; the allowlist plus the once-a-day stranger reply is the only exposure today.
- **Buttons, lists and templates.** `send_text` and `mark_read` only.
- **Replies to users identified only by a BSUID** (no phone number in the payload). Logged as
  `inbound.reply_unaddressable`; Iteration 03's tenant identities settle how to address them.
- **Retention.** Allowlisted users' messages stay in `channel_inbox` until Iteration 03 moves
  content under RLS; strangers' are purged on processing.

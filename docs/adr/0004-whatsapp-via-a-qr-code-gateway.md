# ADR 0004 — WhatsApp through a QR-code gateway, beside Meta's Cloud API

**Status:** accepted (Iteration 03)

## Context

On 2026-09-30 Meta disabled the WhatsApp Business Account, citing its Commerce Policy, the
moment Iteration 02 Phase B started. That is almost certainly an automated false positive
on a new, empty business portfolio, but lifting it means business verification, which takes
time. Until then the official channel is out of reach.

A self-hosted gateway can link to an ordinary WhatsApp account as a companion device, the
same way WhatsApp Web does, by scanning a QR code from the phone. It needs no Meta app, no
business verification and no message templates. Of the maintained options (WAHA, Evolution
API, GOWA, wuzapi), **GOWA** (`aldinokemal/go-whatsapp-web-multidevice`) fits best:

- MIT-licensed, actively released, written in Go on whatsmeow, with no headless browser.
- It signs webhooks exactly as Meta does: `X-Hub-Signature-256: sha256=<hex>`, an
  HMAC-SHA256 of the body.
- It reports the sender as a phone JID (`316…@s.whatsapp.net`) and keeps WhatsApp's linked
  id separately (`from_lid`). The invite list is keyed by phone; WAHA can deliver `@lid`
  senders that need resolving per engine.

## Decision

**Add GOWA as a second channel, `gowa`, beside Meta's `whatsapp`, and let any combination
run at once.**

- **Independent switches.** `WHATSAPP__ENABLED` and `GOWA__ENABLED` are separate; each
  mounts its own webhook route (`/webhooks/whatsapp`, `/webhooks/gowa`) with its own
  secret, and registers its own outbound channel in the worker.
- **Replies follow the inbound channel.** The worker keeps outbound channels in a registry
  keyed by name, and `handle_inbound` looks up the row's `channel` *before* claiming or
  sending anything. A row whose channel the worker does not serve fails loudly and stays
  unprocessed.
- **One person, one key.** GOWA's parser keys a sender by phone, using the LID only when no
  phone is known, so `SenderRef.key` is `tel:+…` on both channels. The invite-only mute is
  per person: a stranger who writes to both numbers is told once a day, not once per
  channel. `WHATSAPP__ALLOWED_PHONES` stays the one invite list for every channel.
- **A dedicated SIM.** The gateway links a number used only by the bot, never anyone's
  personal number. A linked device can read every chat on its account, and a ban costs the
  account.
- **At-most-once, as ADR 0003, mapped onto the gateway's errors** (from its source):
  - *transient:* unreachable; 401 (session not connected or not logged in, or a wrong
    basic-auth password); 500 `INVALID_WA_CLI`; 429 other than the timelock; 502 and 503.
  - *ambiguous:* a timeout or reset after the request left; 504 `GATEWAY_TIMEOUT` (it was
    waiting on WhatsApp); 408; any other 5xx.
  - *rejected:* 400 `INVALID_JID` (not on WhatsApp) and other 4xx; 429
    `WA_REACHOUT_TIMELOCK`, WhatsApp's anti-spam block on starting new chats, recorded under
    WhatsApp's own code, 463.

## Consequences

- **This breaches WhatsApp's terms of service.** Automated use of a consumer account can get
  the number banned without notice and without appeal. Mitigations: a dedicated SIM; a
  typing indicator and a pause before every reply (`GOWA__TYPING_DELAY_S`); reply-only
  traffic. Proactive messages (reminders, when they come) must be paced and must only go to
  people who have written first, or the reach-out timelock will refuse them.
- **The gateway holds a live login to the account, and its chat history.** Its volume
  (`/app/storages`) is as sensitive as a password plus the messages themselves. It is
  reachable only behind basic auth, media auto-download is off, and its MCP endpoint (on by
  default) is turned off.
- **Meta stays.** When business verification lands, Meta can be switched on beside the
  gateway, or instead of it, with no code change.
- **No 24-hour window.** A linked device can message anyone at any time, which reminders
  will need. The `stale` disposition stays all the same: after a reconnect the gateway can
  deliver an offline backlog, and replying to a day-old message out of the blue is worse
  than silence.
- **Proactive sends need a channel choice.** A reply knows its channel from the inbound
  row. A reminder has no inbound row, so it will need a per-user preferred channel,
  presumably the one they last wrote on. That is the next seam, not built here.
- Read receipts are not sent: GOWA's read endpoint needs the chat JID, which
  `OutboundChannel.mark_read` does not take. Blue ticks are cosmetic.

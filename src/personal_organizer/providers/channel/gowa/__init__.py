"""WhatsApp through the GOWA gateway (go-whatsapp-web-multidevice), as the ``gowa`` channel.

GOWA links to a WhatsApp account as a companion device -- a QR code scanned from the phone of
a dedicated SIM -- and exposes it as a REST API plus signed webhooks. It is unofficial; the
risks and why we accept them are in docs/adr/0004. This is the only package that knows what
a JID is.
"""

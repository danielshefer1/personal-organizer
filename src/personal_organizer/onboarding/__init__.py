"""The web side of onboarding: signed tokens and the single-use connect links built on them.

The chat side (the steps, their text) lives in ``personal_organizer.messaging``. This package
is what both the worker, which sends a link, and the api, which serves ``/connect/{token}``
and Composio's callback, need to agree on.
"""

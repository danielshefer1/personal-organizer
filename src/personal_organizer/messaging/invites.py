"""Is this number invited? The env list first, then the ``invites`` table (ADR 0006).

``WHATSAPP__ALLOWED_PHONES`` stays as the fallback list, so the owner's own number is invited
whatever the table says -- empty, or a mistaken revoke. It is checked first, so a listed
number costs no query.

Only a sender with no tenant is ever asked: identity wins over invites, as it did over the
env list. Cutting off a member is ``tenants.status`` (``po-admin suspend``).
"""

from __future__ import annotations

from personal_organizer.db.engine import Database
from personal_organizer.db.repositories.invites import has_open_invite


async def is_invited(db: Database, phone: str | None, env_list: frozenset[str]) -> bool:
    if phone is None:
        return False
    if phone in env_list:
        return True
    async with db.system_session() as session:
        return await has_open_invite(session, phone)


__all__ = ["is_invited"]

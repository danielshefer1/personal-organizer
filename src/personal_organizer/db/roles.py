"""Database roles.

Four privilege tiers, because the spec's two-role split is not self-sufficient:

* ``BOOTSTRAP`` -- Railway's superuser. The only role that may ``CREATE EXTENSION`` and
  ``CREATE ROLE``. Used by ``po-db bootstrap`` and by nothing else, ever.
* ``OWNER`` -- ``app_owner``. Owns the ``public`` schema and every table; runs Alembic.
  Deliberately *not* a superuser: superusers bypass RLS unconditionally, which would make
  the spec's own ``FORCE ROW LEVEL SECURITY`` "second guard" a no-op.
* ``APP`` -- ``app_user``. The runtime role for api and worker. Owns nothing, cannot create
  in ``public``, ``NOBYPASSRLS``, and is not granted ``app_owner`` (no ``SET ROLE`` escape).
* ``DEFINER`` -- ``app_definer``. ``NOLOGIN BYPASSRLS``: it owns the two ``SECURITY DEFINER``
  functions that map a sender to a tenant before any tenant is known (D1 in
  docs/plan-iteration-03.md), and nothing else. Nothing ever connects as it, so it has no
  DSN -- :meth:`Settings.dsn_for` refuses it by name -- and its name is a constant here
  rather than a username parsed out of a URL. It is granted to ``app_owner``, because on
  PostgreSQL 16 ``ALTER FUNCTION ... OWNER TO`` requires the caller to be able to become the
  new owner; it is never granted to ``app_user``.
"""

from enum import StrEnum
from typing import Final

#: The ``SECURITY DEFINER`` owner. Passed to ``bootstrap.sql`` as the ``po.definer_role`` GUC
#: and used by migration 0004 for ``ALTER FUNCTION ... OWNER TO`` and its table grants.
DEFINER_ROLE_NAME: Final = "app_definer"


class DatabaseRole(StrEnum):
    BOOTSTRAP = "bootstrap"
    OWNER = "owner"
    APP = "app"
    DEFINER = "definer"


__all__ = ["DEFINER_ROLE_NAME", "DatabaseRole"]

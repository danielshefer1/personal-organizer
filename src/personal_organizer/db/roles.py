"""Database roles.

Three privilege tiers, because the spec's two-role split is not self-sufficient:

* ``BOOTSTRAP`` -- Railway's superuser. The only role that may ``CREATE EXTENSION`` and
  ``CREATE ROLE``. Used by ``po-db bootstrap`` and by nothing else, ever.
* ``OWNER`` -- ``app_owner``. Owns the ``public`` schema and every table; runs Alembic.
  Deliberately *not* a superuser: superusers bypass RLS unconditionally, which would make
  the spec's own ``FORCE ROW LEVEL SECURITY`` "second guard" a no-op.
* ``APP`` -- ``app_user``. The runtime role for api and worker. Owns nothing, cannot create
  in ``public``, ``NOBYPASSRLS``, and is not granted ``app_owner`` (no ``SET ROLE`` escape).

This is an enum rather than two settings fields so that Iteration 21's ``SECURITY DEFINER``
owner is one new member plus one env var, with no call sites to change.
"""

from enum import StrEnum


class DatabaseRole(StrEnum):
    BOOTSTRAP = "bootstrap"
    OWNER = "owner"
    APP = "app"


__all__ = ["DatabaseRole"]

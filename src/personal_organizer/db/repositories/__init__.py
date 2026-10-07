"""Data access for tenant tables.

Every function takes an ``AsyncSession`` first and never commits: the caller owns the
transaction, and for tenant tables that is ``Database.tenant_session(tenant_id)``, whose GUC
is what RLS checks. Each tenant-scoped query *also* filters through
:func:`~personal_organizer.db.repositories.scope.scoped`, an app-level ``WHERE`` that is
deliberately redundant: ``tests/db/test_isolation.py`` runs once with it and once with it
patched to a no-op, which is how the suite proves RLS alone keeps tenants apart.

``tenants.resolve_tenant`` and ``tenants.create_tenant`` are the exceptions: they run in a
``system_session`` and go through the ``SECURITY DEFINER`` functions, because the tenant is
what they are finding out.
"""

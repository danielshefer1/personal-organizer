-- Database bootstrap. Runs as the superuser (Railway's `postgres`), exactly once per
-- environment, idempotently, before any Alembic migration.
--
-- It exists because the spec's two-role split is not self-sufficient: CREATE EXTENSION and
-- CREATE ROLE both require superuser, and `app_owner` is deliberately not one. Making
-- app_owner a superuser would be simpler and would also make FORCE ROW LEVEL SECURITY --
-- which the spec names as its second guard -- completely inert, since superusers bypass RLS
-- unconditionally.
--
-- Role names and passwords arrive as transaction-local GUCs set by po-db bootstrap, and are
-- interpolated with format(%I/%L) so they are correctly quoted.

-- 1. pgvector. Not a trusted extension, so this needs superuser.
CREATE EXTENSION IF NOT EXISTS vector;

DO $$
DECLARE
    found_version text;
BEGIN
    SELECT extversion INTO found_version FROM pg_extension WHERE extname = 'vector';
    IF found_version IS NULL THEN
        RAISE EXCEPTION 'pgvector is not installed on this server';
    END IF;
    -- Iteration 11 builds an HNSW index, which needs pgvector >= 0.5.0. Asserting it here
    -- turns the spec's manual Day-1 check into a deploy that fails loudly, months before
    -- the memory store would otherwise discover it.
    IF string_to_array(found_version, '.')::int[] < ARRAY[0, 5, 0] THEN
        RAISE EXCEPTION 'pgvector >= 0.5 required for HNSW indexes, found %', found_version;
    END IF;
END $$;

-- 2. Roles.
DO $$
DECLARE
    owner_role text := current_setting('po.owner_role');
    owner_pw   text := current_setting('po.owner_password');
    app_role   text := current_setting('po.app_role');
    app_pw     text := current_setting('po.app_password');
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = owner_role) THEN
        EXECUTE format(
            'CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS',
            owner_role
        );
    END IF;
    EXECUTE format('ALTER ROLE %I WITH PASSWORD %L', owner_role, owner_pw);

    -- NOINHERIT so app_user cannot pick up privileges from any role it is later granted.
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = app_role) THEN
        EXECUTE format(
            'CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT',
            app_role
        );
    END IF;
    EXECUTE format('ALTER ROLE %I WITH PASSWORD %L', app_role, app_pw);

    -- 3. Schema ownership. app_owner owns public; app_user may use it but not create in it.
    EXECUTE format('ALTER SCHEMA public OWNER TO %I', owner_role);
    EXECUTE 'REVOKE ALL ON SCHEMA public FROM PUBLIC';
    EXECUTE format('GRANT USAGE ON SCHEMA public TO %I', app_role);
    -- Deliberately NOT granted: app_owner to app_user. That would be a SET ROLE escape
    -- hatch straight out of RLS.

    -- 4. Default privileges. This is what stops the owner/app split becoming a permanent
    -- GRANT tax on every migration: objects app_owner creates from here on are
    -- automatically usable by app_user. It only affects objects created AFTER this runs
    -- and only those created BY app_owner, which is why bootstrap must precede Alembic.
    EXECUTE format(
        'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
        'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO %I',
        owner_role, app_role
    );
    EXECUTE format(
        'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
        'GRANT USAGE, SELECT ON SEQUENCES TO %I',
        owner_role, app_role
    );
    EXECUTE format(
        'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
        'GRANT EXECUTE ON FUNCTIONS TO %I',
        owner_role, app_role
    );
END $$;

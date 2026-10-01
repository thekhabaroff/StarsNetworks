#!/bin/sh
# Create/update the PostgreSQL role used by the bot.  This script runs in the
# short-lived db-init Compose service after Alembic finishes as the schema
# owner.
set -eu

: "${PGHOST:?PGHOST is required}"
: "${PGDATABASE:?PGDATABASE is required}"
: "${PGUSER:?PGUSER is required}"
: "${PGPASSWORD:?PGPASSWORD is required}"
: "${APP_DB_USER:?APP_DB_USER is required}"
: "${APP_DB_PASSWORD:?APP_DB_PASSWORD is required}"

if [ "$APP_DB_USER" = "$PGUSER" ]; then
    echo "APP_DB_USER must differ from the PostgreSQL bootstrap user." >&2
    exit 1
fi

# psql's identifier/literal interpolation together with PostgreSQL format()
# safely quotes the values supplied through the environment.  The role cannot
# create databases, roles or schema objects and has no superuser/replication
# privileges.  Alembic runs earlier under the bootstrap owner role.
psql \
    --no-psqlrc \
    --set=ON_ERROR_STOP=1 \
    --set=app_db_user="$APP_DB_USER" \
    --set=app_db_password="$APP_DB_PASSWORD" \
    --set=database_name="$PGDATABASE" <<'SQL'
SELECT format(
    'CREATE ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOINHERIT NOBYPASSRLS',
    :'app_db_user',
    :'app_db_password'
)
WHERE NOT EXISTS (
    SELECT 1 FROM pg_roles WHERE rolname = :'app_db_user'
)
\gexec

SELECT format(
    'ALTER ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOINHERIT NOBYPASSRLS',
    :'app_db_user',
    :'app_db_password'
)
\gexec

-- A role can explicitly SET ROLE into any role it is a member of even when
-- NOINHERIT is set, so remove all pre-existing memberships as well.
SELECT format('REVOKE %I FROM %I', parent_role.rolname, member_role.rolname)
FROM pg_auth_members membership
JOIN pg_roles parent_role ON parent_role.oid = membership.roleid
JOIN pg_roles member_role ON member_role.oid = membership.member
WHERE member_role.rolname = :'app_db_user'
\gexec

-- The bootstrap role remains the owner/migrator.  This also repairs an older
-- deployment where the application role may have owned public objects.
SELECT format('ALTER DATABASE %I OWNER TO %I', :'database_name', current_user)
\gexec

SELECT format('ALTER SCHEMA public OWNER TO %I', current_user)
\gexec

SELECT format('ALTER TABLE %I.%I OWNER TO %I', schemaname, tablename, current_user)
FROM pg_tables
WHERE schemaname = 'public'
\gexec

SELECT format('ALTER SEQUENCE %I.%I OWNER TO %I', sequence_schema, sequence_name, current_user)
FROM information_schema.sequences
WHERE sequence_schema = 'public'
\gexec

SELECT format('REVOKE ALL PRIVILEGES ON DATABASE %I FROM %I', :'database_name', :'app_db_user')
\gexec

-- Do not leave a broad PUBLIC connection path; the application gets its own
-- explicit CONNECT grant below.
SELECT format('REVOKE CONNECT, TEMPORARY ON DATABASE %I FROM PUBLIC', :'database_name')
\gexec

SELECT format('GRANT CONNECT ON DATABASE %I TO %I', :'database_name', :'app_db_user')
\gexec

REVOKE ALL PRIVILEGES ON SCHEMA public FROM PUBLIC;

SELECT format('REVOKE ALL PRIVILEGES ON SCHEMA public FROM %I', :'app_db_user')
\gexec

SELECT format('REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM %I', :'app_db_user')
\gexec

SELECT format('REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM %I', :'app_db_user')
\gexec

SELECT format('GRANT USAGE ON SCHEMA public TO %I', :'app_db_user')
\gexec

-- The application role has DML only.  It deliberately does not own tables,
-- sequences or the schema and therefore cannot change migrations/DDL. The
-- ledger is the one exception: it is append-only, so the role gets only
-- SELECT/INSERT there even if a broad historical grant existed.
SELECT format('GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE %I.%I TO %I', schemaname, tablename, :'app_db_user')
FROM pg_tables
WHERE schemaname = 'public'
  AND tablename NOT IN ('alembic_version', 'balance_ledger')
\gexec

SELECT format('GRANT SELECT, INSERT ON TABLE %I.%I TO %I', schemaname, tablename, :'app_db_user')
FROM pg_tables
WHERE schemaname = 'public' AND tablename = 'balance_ledger'
\gexec

SELECT format('REVOKE UPDATE, DELETE ON TABLE %I.%I FROM %I', schemaname, tablename, :'app_db_user')
FROM pg_tables
WHERE schemaname = 'public' AND tablename = 'balance_ledger'
\gexec

SELECT format('GRANT USAGE, SELECT ON SEQUENCE %I.%I TO %I', sequence_schema, sequence_name, :'app_db_user')
FROM information_schema.sequences
WHERE sequence_schema = 'public'
\gexec

-- Omitting FOR ROLE intentionally targets the current bootstrap/migration role.
-- This is clearer than interpolating current_user as an identifier and is the
-- role that Alembic uses to create all future schema objects.
SELECT format('ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO %I', :'app_db_user')
\gexec

SELECT format('ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO %I', :'app_db_user')
\gexec
SQL

echo "PostgreSQL application role is ready."

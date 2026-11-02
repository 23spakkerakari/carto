#!/bin/sh
# Runs once, when the postgres image initializes an empty data directory (docker-entrypoint
# -initdb.d). Creates the per-service database roles of spec 14.4 and 2.3 invariant 9 with the
# passwords from Docker secrets: carto_ingest (ingest-api and the bundle loader), carto_app
# (api), carto_worker (profiler and linker). `carto` stays the owner and runs the migrations,
# which grant each role exactly what it needs (migration 0004). Nothing here is logged with a
# password: psql reads the values from variables and the script never echoes them.
set -eu

read_secret() {
  tr -d '\r\n' < "$1"
}

create_role() {
  role="$1"
  password_file="$2"
  if [ ! -s "$password_file" ]; then
    echo "init-roles: $password_file missing or empty; skipping $role" >&2
    return 0
  fi
  PGPASSWORD_VALUE="$(read_secret "$password_file")"
  psql -v ON_ERROR_STOP=1 -v role="$role" -v pw="$PGPASSWORD_VALUE" \
    --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'role', :'pw')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'role') \gexec
SQL
  unset PGPASSWORD_VALUE
  echo "init-roles: $role ready"
}

create_role carto_ingest /run/secrets/pg_ingest_password
create_role carto_app /run/secrets/pg_app_password
create_role carto_worker /run/secrets/pg_worker_password

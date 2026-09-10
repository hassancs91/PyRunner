#!/bin/bash
# First-boot init for the Databases feature's data server (docker-compose.postgres.yml).
#
# Creates, on the SAME Postgres service but as SEPARATE objects from the core DB:
#   - a non-superuser provisioner role (LOGIN + CREATEROLE — enough to create
#     per-database schemas/roles, nothing more)
#   - the data database, owned by that role
#
# Runs only on a fresh data volume (postgres runs /docker-entrypoint-initdb.d/*
# at initdb time only). On an existing deployment, create the two objects by
# hand with the same statements — see .env.example.
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE ROLE pyrunner_provisioner LOGIN PASSWORD '${DATA_DB_PASSWORD:-pyrunner-data}' CREATEROLE;
    CREATE DATABASE ${DATA_DB_NAME:-pyrunner_data} OWNER pyrunner_provisioner;
    -- Postgres grants CONNECT+TEMP on every database to PUBLIC by default;
    -- without this revoke, provisioned script roles could connect to the CORE
    -- database (no data access, but catalog metadata is world-readable once
    -- connected). The core app user is the database's owner and is unaffected.
    REVOKE CONNECT, TEMPORARY ON DATABASE ${POSTGRES_DB} FROM PUBLIC;
    -- The Databases monitor reads pg_stat_activity / pg_stat_statements query
    -- text of the per-database roles; without this, non-own query text shows
    -- as <insufficient privilege>. (PyRun still filters every monitor query
    -- to the active workspace's roles.)
    GRANT pg_read_all_stats TO pyrunner_provisioner;
EOSQL

# Slow-query history for the Databases monitor (works because the compose file
# preloads pg_stat_statements). Must be created IN the data database.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "${DATA_DB_NAME:-pyrunner_data}" <<-EOSQL
    CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
EOSQL

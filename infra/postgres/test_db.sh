#!/bin/bash
# Postgres init hook, after init.sql built the stack's database: create clinsync_test from the same schema.
# The test suite uses only clinsync_test (tests/conftest.py); the stack's API and workers use only $POSTGRES_DB, so
# its sweepers can never touch rows a test commits. Runs once, when the data volume is first initialised.
set -euo pipefail

TEST_DB=clinsync_test

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -c "CREATE DATABASE ${TEST_DB}"
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "${TEST_DB}" -f /docker-entrypoint-initdb.d/init.sql

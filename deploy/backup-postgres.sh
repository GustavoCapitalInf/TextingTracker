#!/bin/sh
# Run as an operator with access to PostgreSQL and an encrypted backup volume.
# PGHOST, PGPORT, PGDATABASE, PGUSER and PGPASSFILE use libpq conventions.
# BACKUP_DIR must already exist; this script never removes existing backups.
set -eu
umask 077

: "${BACKUP_DIR:?Set BACKUP_DIR to an existing directory on encrypted storage}"
: "${PGDATABASE:?Set PGDATABASE (normally phonetracker)}"
: "${PGUSER:?Set PGUSER (normally phonetracker)}"
: "${PGHOST:?Set PGHOST (normally 127.0.0.1)}"

PG_BIN=${PG_BIN:-/opt/homebrew/opt/postgresql@18/bin}
if [ ! -d "$BACKUP_DIR" ] || [ ! -w "$BACKUP_DIR" ]; then
    printf '%s\n' 'BACKUP_DIR must exist and be writable.' >&2
    exit 1
fi
if [ ! -x "$PG_BIN/pg_dump" ] || [ ! -x "$PG_BIN/pg_restore" ]; then
    printf '%s\n' 'PG_BIN must point to PostgreSQL 18 client tools.' >&2
    exit 1
fi

# Each invocation uses a new directory, so simultaneous backups cannot overwrite.
backup_job=$(mktemp -d "$BACKUP_DIR/phonetracker-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")
backup_file="$backup_job/database.dump"
"$PG_BIN/pg_dump" --no-password --format=custom --no-owner --file="$backup_file"
"$PG_BIN/pg_restore" --list "$backup_file" > "$backup_job/contents.txt"
shasum -a 256 "$backup_file" > "$backup_job/SHA256.txt"
printf '%s\n' "Backup completed: $backup_file"
printf '%s\n' 'Archive readability checked; perform a separate restore drill to verify recovery.'

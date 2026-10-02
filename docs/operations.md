# Operations and recovery

Use this runbook after completing [Mac mini deployment](mac-mini-deployment.md). The commands below assume the fixed installation paths from that guide. Keep actual secrets out of tickets, screenshots, source control, and chat.

## Accounts and daily administration

Give every manager and rep a personal account. A manager account has `is_staff`; a rep account does not. Protect manager credentials carefully: managers can import lists, choose recipients, and clear lists. Reps find their lists in their own account; a list's address never replaces signing in.

Disable a rep's account when they leave or no longer need access. Do not reuse their account for the next employee because doing so would compromise attribution in historical audit entries. Manage texting lists and their reps under **Texting lists**. In **Accounts**, open a rep to manage their texting lists, generate a replacement password, and review successful sign-ins with exact timestamps. New and reset passwords are shown once to the admin; distribute them privately to the intended rep. Reps cannot create accounts or change/reset their own passwords. Admins can change their own password using Account. Five wrong passwords for a username from one device block that username on that device for 15 minutes; other devices are unaffected, so a rep cannot lock a coworker out. Twenty-five failures for one username from any mix of devices, or fifty failures from one address across usernames, also block for 15 minutes. Review manager access periodically.

An authorized deployment administrator can reset a password interactively with `sudo -u _phonetracker /Library/PhoneTracker/app/.venv/bin/python /Library/PhoneTracker/app/manage.py changepassword USERNAME`, replacing `USERNAME` with the actual account name. The password is entered at the prompt, not supplied as a command argument.

Phone numbers remain in the registry after assignment, including future dates and cleared batches. Reuploading the same spreadsheet flags its numbers and asks whether to upload them again. They are only reassigned when a manager answers Yes; the earlier assignment is kept in history as assigned again. Use the import report and retained history to understand a warning rather than uploading variants of the same number.

Reps have read-only access to phone numbers in their assigned lists for today and can review their own weekly schedule metadata. They must still belong to the upload's GFS or Ringcentral group to access its batches. The application does not track calls, interest, or outcomes. Clearing a batch removes all rep access, including any access previously associated with legacy in-progress records; clearing an upload removes access to every batch in that upload. Historical data remains. The privacy overlay and watermark do not guarantee screenshot prevention, and clearing cannot retract a number already seen by a user.

## Database backups

The included script makes a PostgreSQL custom-format dump and checks that `pg_restore` can read its index. It does not delete old backups, install a schedule, or prove that a complete restoration will work. PostgreSQL's custom format supports restore through `pg_restore`. [PostgreSQL backup documentation](https://www.postgresql.org/docs/18/backup-dump.html).

Choose an existing directory on an encrypted backup volume separate from the Mac's main storage. Restrict it to the operator who manages backups. A `.dump` file is **not encrypted** by `pg_dump`; storage encryption and authorized handling are required. Maintain at least one independently recoverable copy and document the company's retention policy. Do not automatically delete backups until that policy is established.

Create a private libpq password file outside the repository, with the real database password. The format is:

```text
127.0.0.1:5432:phonetracker:phonetracker:ACTUAL_DATABASE_PASSWORD
```

Use `chmod 600` on the file. Escape `:` and `\` in the password according to the [PostgreSQL password-file format](https://www.postgresql.org/docs/18/libpq-pgpass.html). Do not put the password in a command line or save it in the launchd templates.

Run a backup as an authorized operator. Replace both example filesystem paths first; the backup directory must already exist on the intended encrypted volume:

```sh
export PG_BIN="$(brew --prefix postgresql@18)/bin"
export PGHOST=127.0.0.1
export PGPORT=5432
export PGDATABASE=phonetracker
export PGUSER=phonetracker
export PGPASSFILE=/absolute/private/path/phonetracker.pgpass
export BACKUP_DIR=/Volumes/EncryptedBackup/PhoneTracker
sh /Library/PhoneTracker/bin/backup-postgres
```

The script creates a unique timestamped directory with `database.dump`, `contents.txt`, and `SHA256.txt`. A failed run may leave an incomplete directory; investigate it and retain the previous verified backup. Record successful completion and check free space. Arrange an OS or managed backup schedule separately after a successful manual backup and restore drill; this repository does not create a schedule.

Also back up the protected `.env`, the deployed revision identifier, installed package versions, and TLS renewal information to an encrypted administrative store. A database dump does not contain the Django secret or OS configuration. The application database role is recreated separately during host recovery; it is not restored as an unrestricted database superuser.

## Restore drill without changing production

Use a separate database name and preferably a separate test machine. A restored database contains real phone numbers and accounts; apply the same access restrictions and do not expose it to reps or upload it to public services.

The following commands create a **new** local drill database via the private administrator socket. Choose a unique drill name before running. A pre-existing database should cause creation to fail; do not drop or overwrite it just to make the command proceed. Replace the backup path with a verified archive:

```sh
PT_PG_BIN="$(brew --prefix postgresql@18)/bin"
sudo -u _phonetracker_db "$PT_PG_BIN/createdb" \
  -h /Library/PhoneTracker/postgres \
  --owner=phonetracker phonetracker_restore_drill_20261001
```

For the restore command, make the selected archive readable by the `_phonetracker_db` identity through a private staging directory. Do not make the whole backup volume world-readable. For example, the deployment administrator can copy one archive to `/Library/PhoneTracker/restore-stage/database.dump`, assign that directory and file to `_phonetracker_db`, and set directory mode `700` and file mode `600`.

```sh
sudo -u _phonetracker_db "$PT_PG_BIN/pg_restore" \
  -h /Library/PhoneTracker/postgres \
  --dbname=phonetracker_restore_drill_20261001 \
  --role=phonetracker --no-owner --exit-on-error \
  /Library/PhoneTracker/restore-stage/database.dump
```

Connect to the drill database through the administrator socket, confirm tables and row counts, and inspect a known batch and audit event without printing phone lists into shared logs. To test the application against the restored database, use an isolated app checkout and configuration, a new private test database connection rule, and a loopback-only server. Do not edit the live service's `DATABASE_URL` for a drill. Keep external integrations disabled if any are added later.

Record the archive used, date, restoration duration, and verification results. This establishes practical recovery time and exposes missing secrets or dependencies. Retain or remove drill data deliberately under the retention policy; none of the supplied scripts deletes it automatically.

For an actual recovery, stop app writes, preserve the failed database for diagnosis, restore to a new database, verify it, and only then change the live connection in a planned maintenance window. Do not run `pg_restore --clean` against the live database as an improvised recovery procedure.

## Service status and logs

```sh
sudo launchctl print system/com.capitalinfusion.phonetracker
sudo launchctl print system/com.capitalinfusion.phonetracker-db
sudo launchctl print system/com.capitalinfusion.phonetracker-httpd
sudo tail -n 80 /var/log/phonetracker/app.log
sudo tail -n 80 /var/log/phonetracker-db/postgres.log
sudo tail -n 80 /var/log/phonetracker-httpd/error.log
```

Protect logs as operational records. The Apache access format omits query strings, cookies, referers, and bodies. Do not enable SQL statement logging or request-body logging for ordinary diagnostics. Configure log rotation and disk-space monitoring through IT; the launchd files alone do not rotate logs. After rotation, ensure processes reopen their log destinations or restart them in a maintenance window.

If the app cannot connect to the database, verify the database daemon, loopback listener, role password, and `.env` file ownership. If redirects loop, verify the supplied Apache config still overwrites `X-Forwarded-Proto` and Django's secure-proxy configuration has not changed. Do not disable HTTPS enforcement to conceal a broken proxy setup.

## Updates and restarts

Record the deployed revision and make a verified backup before upgrading. Test the intended revision with PostgreSQL in a separate environment first. Schedule downtime for schema changes and check whether a rollback would require restoring a database backup; not every migration is reversible.

For a code-only reload after preparing compatible files and dependencies, Gunicorn supports a graceful master reload. The PID is in the app runtime directory:

```sh
sudo -u _phonetracker sh -c 'kill -HUP "$(cat /Library/PhoneTracker/data/gunicorn.pid)"'
```

Verify that the PID belongs to the running Gunicorn master before signaling it. Replacing the virtual environment or changing database schemas should be done with the application stopped, not during a graceful reload. To stop the app deliberately, unload its launchd service so `KeepAlive` does not immediately start it again:

```sh
sudo launchctl bootout system/com.capitalinfusion.phonetracker
```

Install the reviewed release without overwriting `.env` or database/runtime files. Recreate dependencies if needed, reapply the application ownership from the deployment guide, run `migrate` as `_phonetracker`, run `collectstatic` as the deployment administrator, and run `check --deploy`. Then start the app again:

```sh
sudo launchctl bootstrap system /Library/LaunchDaemons/com.capitalinfusion.phonetracker.plist
```

Validate Apache configuration before reloading it, including after certificate renewal:

```sh
sudo "$(brew --prefix httpd)/bin/httpd" -t -f /Library/PhoneTracker/httpd.conf
sudo "$(brew --prefix httpd)/bin/httpd" -k graceful -f /Library/PhoneTracker/httpd.conf
```

Never run a major PostgreSQL upgrade by pointing new binaries directly at the old data directory. Follow PostgreSQL's supported upgrade procedure, with a verified backup and tested recovery. Review Homebrew and Python updates in a maintenance window because launchd references their installed runtimes.

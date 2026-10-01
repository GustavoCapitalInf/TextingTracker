# Deploy on the dedicated Mac mini

This runbook installs a private office service. It does not publish a public website. Complete the network and certificate setup with the administrator of the office network before giving reps access.

These are deployment templates, not a completed server installation. The Mac-specific Apache configuration, service startup/shutdown, certificate trust, and reboot recovery must be verified on the actual Mac; development tests on Windows cannot establish those results.

The templates use these fixed locations:

| Location | Purpose |
|---|---|
| `/Library/PhoneTracker/app` | Application checkout and virtual environment |
| `/Library/PhoneTracker/app/.env` | Production secrets, readable only by root and the app group |
| `/Library/PhoneTracker/data` | Application runtime files |
| `/Library/PhoneTracker/postgres` | Dedicated PostgreSQL cluster |
| `/Library/PhoneTracker/tls` | Certificate chain and private key |
| `/var/log/phonetracker*` | Separate app, database, and Apache logs |
| `/Library/LaunchDaemons/com.capitalinfusion.phonetracker*.plist` | Services that run at system startup |

Do not place production data in OneDrive, iCloud, Dropbox, or a user's Desktop folder.

## 1. Establish the host and service identities

Have IT reserve a private IPv4 address, create a DNS record, and supply a certificate whose subject alternative name matches the DNS name. The certificate's issuing authority must be trusted by every rep's computer. Do not tell users to bypass a certificate warning.

The renderer requires the real hostname, reserved IP, office subnet, and Homebrew prefix. Example values such as `phones.office.example`, `192.168.10.20`, and `192.168.10.0/24` below must be replaced with the site's values. The provided template supports one IPv4 subnet; additional VLANs need an administrator-reviewed access rule.

Enable FileVault, configure the Mac to stay awake while connected to power, and arrange updates/reboots outside office hours. FileVault may require a local unlock after a cold boot; test this with IT. Do not disable disk encryption to obtain unattended startup. Do not forward router ports to this Mac. Allow HTTPS only from the intended office network; the database and application worker ports must remain loopback-only.

IT must create two non-admin service identities with matching private primary groups, no interactive login, and unique locally allocated UID/GID values:

- `_phonetracker` / `_phonetracker`: application process.
- `_phonetracker_db` / `_phonetracker_db`: PostgreSQL process and database administrator through a local peer-authenticated socket.

Do not copy guessed numeric UID/GID values from another machine. Confirm the identities exist:

```sh
id _phonetracker
id _phonetracker_db
```

Apache starts under launchd as root to bind ports 80/443, then runs request workers as macOS's `_www` user. It cannot read the application `.env` or the database files.

## 2. Install dependencies and application files

Install Homebrew from its [official instructions](https://brew.sh/) if it is not installed. Run Homebrew as the deployment operator, not as root:

```sh
brew install python@3.14 postgresql@18 httpd
brew --prefix
```

The usual prefix is `/opt/homebrew` on Apple Silicon and `/usr/local` on Intel. The deployment renderer supports both. This runbook creates a separate PostgreSQL cluster and standalone Apache configuration; do not also start the default `brew services` instances for these components. Check for existing listeners and coordinate with their owner before using ports 80, 443, 5432, or 8000.

From the application's source directory, prepare the installation directories. The two service accounts must exist before these commands run:

```sh
sudo install -d -o root -g wheel -m 755 /Library/PhoneTracker
sudo install -d -o "$(id -un)" -g _phonetracker -m 750 /Library/PhoneTracker/app
sudo install -d -o _phonetracker -g _phonetracker -m 700 /Library/PhoneTracker/data
sudo install -d -o _phonetracker_db -g _phonetracker_db -m 700 /Library/PhoneTracker/postgres
sudo install -d -o root -g wheel -m 700 /Library/PhoneTracker/tls
sudo install -d -o root -g wheel -m 755 /Library/PhoneTracker/bin
sudo install -d -o _phonetracker -g _phonetracker -m 700 /var/log/phonetracker
sudo install -d -o _phonetracker_db -g _phonetracker_db -m 700 /var/log/phonetracker-db
sudo install -d -o root -g wheel -m 700 /var/log/phonetracker-httpd
rsync -a \
  --exclude='.git/' --exclude='.venv/' --exclude='.local/' \
  --include='.env.example' --exclude='.env' --exclude='.env.*' \
  --exclude='__pycache__/' --exclude='*.py[cod]' \
  --exclude='.pytest_cache/' --exclude='.coverage*' --exclude='htmlcov/' \
  --exclude='*.sqlite3*' --exclude='*.dump' --exclude='*.backup' --exclude='*.log' \
  --exclude='demo-credentials.json' --exclude='browser-smoke/' \
  --exclude='data/' --exclude='staticfiles/' --exclude='deploy/generated/' \
  --exclude='artifacts/' --exclude='screenshots/' \
  ./ /Library/PhoneTracker/app/
cd /Library/PhoneTracker/app
sudo install -o root -g wheel -m 755 deploy/backup-postgres.sh /Library/PhoneTracker/bin/backup-postgres
"$(brew --prefix python@3.14)/bin/python3.14" -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Do not use an old virtual environment copied from Windows or another Mac. Install it at the final path.

The copy excludes local databases, generated demo passwords, browser screenshots, caches, and backups. Do not copy a populated development database or any other synthetic demo artifacts into the production installation. Keep production `PHONETRACKER_DEMO` unset.

Render the configurations. **Replace the three example network values first.** Rendering writes files locally and does not install services, overwrite existing generated files, or modify the machine configuration.

```sh
.venv/bin/python deploy/render_config.py \
  --hostname phones.office.example \
  --lan-ip 192.168.10.20 \
  --lan-cidr 192.168.10.0/24 \
  --brew-prefix "$(brew --prefix)"
```

For a revised configuration, use `--output` with a new review directory. Inspect the generated files before installing them. No `@@...@@` placeholders should remain.

## 3. Initialize the dedicated database

The following `initdb` command is for a new empty `/Library/PhoneTracker/postgres` directory. It refuses to initialize an existing cluster. Never delete or reinitialize a cluster to resolve a startup error.

```sh
PT_PG_BIN="$(brew --prefix postgresql@18)/bin"
sudo -u _phonetracker_db "$PT_PG_BIN/initdb" \
  -D /Library/PhoneTracker/postgres \
  --encoding=UTF8 --locale=C --auth-local=peer --auth-host=scram-sha-256
sudo sh -c 'cat /Library/PhoneTracker/app/deploy/generated/postgresql.conf.fragment >> /Library/PhoneTracker/postgres/postgresql.conf'
sudo install -o _phonetracker_db -g _phonetracker_db -m 600 deploy/generated/pg_hba.conf /Library/PhoneTracker/postgres/pg_hba.conf
sudo install -o root -g wheel -m 644 deploy/generated/com.capitalinfusion.phonetracker-db.plist /Library/LaunchDaemons/com.capitalinfusion.phonetracker-db.plist
sudo plutil -lint /Library/LaunchDaemons/com.capitalinfusion.phonetracker-db.plist
sudo launchctl bootstrap system /Library/LaunchDaemons/com.capitalinfusion.phonetracker-db.plist
```

If launchd reports that the service is already loaded, inspect it rather than bootstrapping it a second time. PostgreSQL should start on `127.0.0.1:5432`, with its Unix socket in the private cluster directory.

`--pwprompt` is not required for this `initdb` command: the bootstrap administrator connects through local peer authentication, and the installed HBA rules deny it TCP access. PostgreSQL requires an initial superuser password when **both** local and host authentication use a password method; this configuration uses `peer` locally. The application role receives its own password in the next step. [PostgreSQL initdb documentation](https://www.postgresql.org/docs/18/app-initdb.html), [PostgreSQL 18 password requirement check](https://github.com/postgres/postgres/blob/REL_18_STABLE/src/bin/initdb/initdb.c#L2457-L2467).

Create the application database owner. The password prompt keeps the password out of shell history:

```sh
sudo -u _phonetracker_db "$PT_PG_BIN/createuser" \
  -h /Library/PhoneTracker/postgres \
  --no-superuser --no-createdb --no-createrole --pwprompt phonetracker
sudo -u _phonetracker_db "$PT_PG_BIN/createdb" \
  -h /Library/PhoneTracker/postgres --owner=phonetracker phonetracker
```

The application role has no database-creation or role-management privileges. Do not grant these merely to run tests; use a separate development/test cluster and test role.

## 4. Set the production environment

Create `/Library/PhoneTracker/app/.env` with the deployment operator's editor. Use the actual hostname and a unique random secret. Generate a secret privately with `.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(64))'`; do not commit or send it in chat.

```dotenv
DJANGO_ENV=production
DJANGO_SECRET_KEY=REPLACE_WITH_A_UNIQUE_RANDOM_SECRET
DJANGO_ALLOWED_HOSTS=phones.office.example
DJANGO_CSRF_TRUSTED_ORIGINS=https://phones.office.example
DATABASE_URL=postgresql://phonetracker:URL_ENCODED_DATABASE_PASSWORD@127.0.0.1:5432/phonetracker
COMPANY_TIME_ZONE=America/New_York
PHONETRACKER_DATA_DIR=/Library/PhoneTracker/data
DJANGO_SECURE_SSL_REDIRECT=true
```

Replace both credential placeholders and the example hostname. URL-encode reserved characters in the database password. Do not add wildcard hosts. Keep the timezone identical to the office's scheduling policy. The app loads `.env` from the application directory; launchd does not read your terminal profile.

Protect the file, complete setup, and seal the application checkout against writes by service accounts:

```sh
sudo chown root:_phonetracker .env
sudo chmod 640 .env
sudo chown -R root:_phonetracker /Library/PhoneTracker/app
sudo chmod -R o-rwx /Library/PhoneTracker/app
sudo chmod o+x /Library/PhoneTracker/app
sudo -u _phonetracker .venv/bin/python manage.py migrate
sudo -u _phonetracker .venv/bin/python manage.py createsuperuser
sudo .venv/bin/python manage.py collectstatic --noinput
sudo chmod -R a+rX /Library/PhoneTracker/app/staticfiles
sudo -u _phonetracker .venv/bin/python manage.py check --deploy
```

Only `staticfiles` is readable by Apache. The application identity can read source and secrets and write to its runtime directory, but cannot edit its code. `collectstatic` runs as the deployment administrator because the checkout is read-only to the app identity. Investigate every deployment-check warning; do not work around an HTTPS warning by disabling cookie security.

Create the CEO's password through the interactive command. No credentials are built into the deployment. The manager UI can then create individual rep accounts. Avoid running a synthetic demo seed in production.

## 5. Install TLS and start the web services

Have IT install the trusted certificate chain at `/Library/PhoneTracker/tls/fullchain.pem` and private key at `/Library/PhoneTracker/tls/privkey.pem`. Both files should be owned by root; the private key must be mode `600`. The chain file must contain the leaf certificate plus necessary intermediate certificates. Arrange a renewal procedure and expiry monitoring with IT.

Install the reviewed Apache config and the two remaining launchd services:

```sh
sudo install -o root -g wheel -m 644 deploy/generated/httpd.conf /Library/PhoneTracker/httpd.conf
sudo install -o root -g wheel -m 644 deploy/generated/com.capitalinfusion.phonetracker.plist /Library/LaunchDaemons/com.capitalinfusion.phonetracker.plist
sudo install -o root -g wheel -m 644 deploy/generated/com.capitalinfusion.phonetracker-httpd.plist /Library/LaunchDaemons/com.capitalinfusion.phonetracker-httpd.plist
sudo plutil -lint /Library/LaunchDaemons/com.capitalinfusion.phonetracker.plist
sudo plutil -lint /Library/LaunchDaemons/com.capitalinfusion.phonetracker-httpd.plist
sudo "$(brew --prefix httpd)/bin/httpd" -t -f /Library/PhoneTracker/httpd.conf
sudo launchctl bootstrap system /Library/LaunchDaemons/com.capitalinfusion.phonetracker.plist
sudo launchctl bootstrap system /Library/LaunchDaemons/com.capitalinfusion.phonetracker-httpd.plist
```

These are LaunchDaemons, not login-scoped LaunchAgents. They run after system startup and restart if a process exits. After FileVault unlock, service availability does not depend on an operator remaining logged in. PostgreSQL and Gunicorn are separate processes; the application may return an error briefly if the database is still starting. Confirm recovery after a reboot.

## 6. Verify before uploading real lists

Perform these checks from the Mac and a rep's machine:

- Open the actual HTTPS hostname. Verify a trusted certificate, the login page, working static assets, and HTTP-to-HTTPS redirect. Use the actual hostname rather than an IP URL.
- Verify PostgreSQL and Gunicorn listen only on `127.0.0.1`. Verify Apache listens on the intended reserved LAN address. Use `sudo lsof -nP -iTCP -sTCP:LISTEN` to inspect listeners.
- Confirm that an unsigned-in browser cannot read a batch and that rep A cannot read rep B's batch, including through a pasted URL. Use synthetic phone data for this acceptance test.
- Verify reps can read only their own lists for today, future/past batches are inaccessible, and timezone release matches the office date. Confirm the rep interface has no call-start, interest, or outcome controls and that old call-action URLs cannot change data.
- Reupload existing numbers and verify they remain excluded from allocation with their history intact. Clear a batch while a rep has it open: the list must hide on its next status check, and a fresh request must deny access. Clearing an upload must remove rep access to all of its batches, including legacy in-progress assignments, without deleting history or enabling automatic reassignment.
- Confirm the PostgreSQL test suite passes in a separate test environment, including concurrent allocation checks. Development SQLite results alone do not establish locking behavior.
- Make and restore a backup using [the recovery runbook](operations.md). Verify a restored account, batch, and audit history.
- Reboot in an approved window, unlock FileVault if required, and confirm the services recover. Review all three log directories.

Do not replace this deployment with `manage.py runserver` for production. Django explicitly requires a production server and provides `check --deploy` for configuration review. [Django deployment checklist](https://docs.djangoproject.com/en/5.2/howto/deployment/checklist/). The launchd service approach follows Apple's distinction between system daemons and user agents. [Apple launchd guide](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html).

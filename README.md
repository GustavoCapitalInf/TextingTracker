# PhoneTracker

An internal phone-list assignment manager for Capital Infusion. Managers import a single-column Excel sheet, divide eligible numbers across dates and reps, and share a link to each batch. Reps sign in with their own accounts and view only their assigned lists for the current company date. Rep lists are read-only; the application does not track calls, interest, or outcomes.

Built with Django 5.2 LTS, server-rendered HTML, PostgreSQL for production, and local browser assets. The dedicated Mac mini deployment uses Apache with HTTPS in front of Gunicorn. No cloud hosting or external font/CDN dependency is required.

## Local development

Use Python 3.14. SQLite is convenient for a local preview; production must use PostgreSQL because concurrency guarantees depend on database locking.

Windows PowerShell, from this directory:

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py createsuperuser
.\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8000
```

macOS, from this directory with Homebrew installed:

```sh
brew install python@3.14
"$(brew --prefix python@3.14)/bin/python3.14" -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
.venv/bin/python manage.py migrate
.venv/bin/python manage.py createsuperuser
.venv/bin/python manage.py runserver 127.0.0.1:8000
```

If `.env` exists already, keep it instead of copying over it. Open [the local application](http://127.0.0.1:8000/) and sign in with the account you created. There are no default production credentials. An account with `is_staff` is a manager; ordinary active accounts are reps.

Run the tests:

```sh
.venv/bin/python manage.py test
.venv/bin/python manage.py check
```

On Windows use `.\.venv\Scripts\python.exe` instead. Run the same test suite against PostgreSQL before deployment; SQLite cannot establish concurrent locking behavior. Use a separate PostgreSQL test account that can create a test database, never production credentials against the live database.

Verified on October 1, 2026: all 93 tests passed on Windows with Python 3.14 and PostgreSQL 16.15, covering group eligibility, global duplicate prevention, calendar access, account provisioning, password resets, and login history. Browser checks confirmed the plain accounts table, exact login times, dual-group rep calendar, future entries without number links, and responsive calendar at desktop and 384 CSS pixels. Earlier plain-interface checks covered 320, 375, 414, and 768 CSS pixels. Calendar browser verification used a separate synthetic database. The Mac mini service and HTTPS configuration still require validation on the target machine.

## Local demo and browser regression

Use a separate development database with no users, numbers, or uploads. Before running the demo, configure the development checkout's `.env` as follows and use a terminal with no `DATABASE_URL` environment override:

```dotenv
DJANGO_ENV=development
PHONETRACKER_DEMO=1
PHONETRACKER_DATA_DIR=.local/preview
DATABASE_URL=
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1
```

Then run these commands from the checkout (use `.venv/bin/python` on macOS):

```powershell
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py seed_demo --confirm
.\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8765
```

Seeding requires `DEBUG`, `PHONETRACKER_DEMO=1`, explicit `--confirm`, and an empty database. It creates fictional phone numbers and writes generated credentials to the private, ignored `.local/preview/demo-credentials.json` file instead of printing passwords. Open [the local demo](http://127.0.0.1:8765/) and use those generated credentials. Keep the file and demo screenshots local; never copy them into production. If a demo exists already, use it rather than reseeding or deleting its database.

With the demo server running, install the browser-test dependencies and run the regression in a second terminal:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe scripts/smoke_browser.py --base-url http://127.0.0.1:8765 --credentials .local/preview/demo-credentials.json
```

The script accepts only loopback demo environments. It creates and closes a synthetic test upload, exercises the browser workflow, and stores screenshots beside the credentials under `browser-smoke/`. The updated script was syntax-checked; this revision's browser checks were performed interactively instead. Clearing and retained history were verified by the PostgreSQL test suite. Mac production startup, shutdown, HTTPS, and recovery still require the deployment checks below.

## Daily workflow

1. In **Accounts**, create individual rep accounts with GFS Texting, Ringcentral Texting, or both memberships. Passwords are generated automatically and shown once to the admin. Reps cannot create accounts or change/reset passwords themselves.
2. Give the upload a title and select **GFS Texting** or **Ringcentral Texting**. Upload one `.xlsx` worksheet containing a phone-number column, an optional header, and up to 10,000 data rows. Use text cells for phone numbers; spreadsheet formatting cannot restore digits Excel has already discarded. Blank rows are ignored.
3. Review the import report. Previously uploaded numbers stay visible as warnings. Invalid numbers, duplicates, and numbers already in the registry do not become new assignments.
4. Choose the availability dates and rep accounts. Only active members of the selected texting group can receive the list. Monday can be a single-day upload. Tuesday–Friday can share a separate upload split first among four dates and then among the selected reps.
5. Review the allocation and create the batches. Every eligible number is assigned once; future work is reserved immediately. Share each batch's link with its assigned rep.
6. Reps sign in to view their daily lists and weekly schedule. The calendar shows their own list titles, types, dates, and counts; phone numbers open only on the assigned day. Links contain batch identifiers, not passwords. The server checks ownership, current group membership, and company date for every protected request. Reps do not record calls or change a number's status.
7. Clear one batch or its whole upload to withdraw the lists. All rep access to the cleared batches ends, including access associated with legacy in-progress records. History remains, and the numbers do not become available for automatic reassignment.

`COMPANY_TIME_ZONE` controls date release and defaults to `America/New_York`. Reps can view numbers only in today's assigned lists; past and future numbers are inaccessible. Existing phone numbers are not automatically reassigned after clearing or reuploading, including across different texting groups. Historical records from an earlier version remain in the database without creating a call-tracking workflow. Legacy drafts without a type require an admin to select a type on their existing import page; no reupload is necessary.

The interface uses a fixed white and light-gray appearance, plain controls, and one navigation bar. There is no theme selector or appearance preference storage. Managers use Lead lists, Upload list, Accounts, and History; reps use Your leads. Only admins have an account-password change link. Navigation wraps on small screens. Yellow identifies previously uploaded numbers; red identifies errors.

The admin account table includes disabled accounts, memberships, and latest sign-in times. Open an account to see successful sign-in history with seconds and the company timezone, change a rep's memberships, disable access, or generate a replacement password. Passwords are hashed in the database. New/reset passwords appear only in that submission response, never in audit events or subsequent account views.

For a new deployment, provision the supplied 16-person roster using `manage.py provision_texting_reps --admin ADMIN_USERNAME --output ABSOLUTE_PRIVATE_TEXT_PATH`. The output directory must exist and the file must not exist. The command creates 12 GFS memberships and 5 Ringcentral memberships, with Emilio Arguello in both. Existing matching accounts keep their passwords and enabled/disabled state; unrelated accounts and uploads are retained. The initial credentials file is private and should be kept outside web-served or shared folders. Run this only against the intended database.

There is no list export feature. Privacy blur and watermarks reduce casual exposure; browsers cannot guarantee detection or prevention of screenshots, photography, or copying numbers that have already been displayed.

## Production and operations

- [Mac mini deployment](docs/mac-mini-deployment.md): database, HTTPS, service identities, launchd, and first boot checks.
- [Operations and recovery](docs/operations.md): backups, restore drills, updates, and access administration.
- [Architecture and access rules](docs/architecture.md): data flow, scheduling, history, and scope of protections.

Do not expose `runserver`, PostgreSQL, or Gunicorn to the LAN. The deployment templates bind Apache to the Mac's reserved private IP and restrict the allowed office subnet. Installation still requires the actual network address, DNS name, trusted TLS certificate, and administrator-created service accounts.

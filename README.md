# PhoneTracker

An internal phone-list assignment manager for Capital Infusion. Managers import a single-column Excel sheet, divide eligible numbers across dates and reps, and share a link to each batch. Reps sign in with their own accounts and view only their assigned lists for the current company date. Rep lists are read-only; the application does not track calls, interest, or outcomes.

Built with Django 5.2 LTS, server-rendered HTML, PostgreSQL for production, and local browser assets. It is hosted on Vercel with a Neon PostgreSQL database (see docs/vercel-deployment.md); the Mac mini deployment with Apache and Gunicorn remains an alternative. Pages load no external fonts or third-party scripts.

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

1. In **Texting lists**, keep the lists your team texts from (GFS Texting (Donut) and Ringcentral Texting (Clean) come built in). Add a list with a name, an optional nickname, and its reps; open a list to rename it or add and remove reps. A list that was never used can be deleted; a list with uploads or templates can be hidden from new uploads instead, so history keeps its name. Drafts already uploaded to a hidden list can still be split, and disabled reps stay on their lists when you save a list’s reps. In **Accounts**, create individual rep accounts and pick their texting lists. Passwords are generated automatically and shown once to the admin. Reps cannot create accounts or change/reset passwords themselves.
2. Give the upload a title and select **GFS Texting (Donut)** or **Ringcentral Texting (Clean)**. Upload one `.xlsx` worksheet containing a single phone-number column and up to 10,000 data rows. A header is optional: a first row without digits (such as "Phone" or "Cell Phone Number") is skipped, and a plain column of numbers is counted from its first row. Common formats are accepted, including dots, spaces, parentheses, +1, and dashes, full-width characters or invisible formatting marks copied from Word, Outlook, Teams or web pages. Use text cells for phone numbers; spreadsheet formatting cannot restore digits Excel has already discarded. Blank rows are ignored, including formatted empty rows, so only rows with values count toward the 10,000 limit.
3. Review the import report. Previously uploaded numbers are flagged in a yellow notice and in the results, which show when each was first uploaded (and in which list) and which rep it was sent to on which date, including whether that list was later cleared. When a file contains previously uploaded numbers, a popup asks whether to upload them again. **Yes** adds them to this list's split; **No** leaves them out. The answer can be changed until the lists are created. Invalid numbers, duplicates within the file, and legacy do-not-contact numbers are always left out.
4. Choose the availability dates and rep accounts. Only active members of the selected texting group can receive the list. Monday can be a single-day upload. Tuesday–Friday can share a separate upload split first among four dates and then among the selected reps.
5. Review the allocation and create the lists. Each rep's list is added to their account automatically for its assigned day; there is no link to share. When there are fewer numbers than rep-days, a rep who would receive none that day gets no list rather than an empty one. A previously uploaded number that is uploaded again keeps its earlier list and history, and also appears in the new list.
6. Managers use the weekly schedule on **Lead lists** to see every rep's lists by day, grouped by upload, marked Sent, Available today, or Upcoming. Select a day to see each rep's assignment, or a list name to review or clear that upload. Reps sign in to view their daily lists and weekly schedule. The calendar shows their own list titles, types, dates, and counts; phone numbers open only on the assigned day. The server checks ownership, current group membership, and company date for every protected request. Reps do not record calls or change a number's status.
7. Optional: in **Templates**, select **Draft from past weeks**. The app looks at the lists sent in the last 4 weeks and drafts a weekly template from them: each list's name, texting group, weekdays and reps, with a note such as "Used in 3 of the last 4 weeks". Edit or remove lists, add new ones, and turn the template on. Its lists then appear on the Lead lists calendar as **Planned · Needs a file** for upcoming days. Selecting **Upload** pre-fills the list name and group; after the upload, the split is pre-filled with that week's remaining days and the template's reps who are still in the group. Everything stays editable before the lists are created. Each planned day is checked on its own: it disappears once that day has a list with the same name and group (ignoring capitals, spacing and punctuation). If you create a Tuesday–Friday list for Tuesday only, Wednesday–Friday stay planned, and uploading another file for that list pre-fills only the missing days. **Skip this day** hides a planned day you don't need; skipped days are listed under the calendar with **Undo**. Clearing every list on a day brings its planned entry back. If a file was already uploaded for a planned list that week, its upload page warns before a second file is added. A list in several active templates appears once. Reps never see planned entries.
8. Clear one batch or its whole upload to withdraw the lists. All rep access to the cleared batches ends, including access associated with legacy in-progress records. History remains, and the numbers are not reassigned unless a manager uploads them again and answers Yes.

`COMPANY_TIME_ZONE` controls date release and defaults to `America/New_York`. Reps can view numbers only in today's assigned lists; past and future numbers are inaccessible. Existing phone numbers are never reassigned automatically after clearing or reuploading, including across texting groups; reuse always requires the manager's Yes. Historical records from an earlier version remain in the database without creating a call-tracking workflow. Legacy drafts without a type require an admin to select a type on their existing import page; no reupload is necessary.

The interface is a dark console (see design.md): a left sidebar with icon navigation, rounded cards, white pill buttons for main actions, and status chips where green means available or active, amber means needs attention (drafts, previously uploaded numbers), and red means errors. Dark is the default, and a Dark / Light / Auto switch at the bottom of the sidebar (and on the sign-in page) changes it per browser; Auto follows the device. The light appearance is cool white and gray. The choice is kept in a single cookie and never stores page data. Managers use Lead lists, Upload list, Templates, Accounts, and History; reps use Your leads. Only admins have an account-password change link. On phones the sidebar becomes a row of navigation pills, the calendar stacks by day, and wide tables scroll inside their card.

The admin account table includes disabled accounts, memberships, and latest sign-in times. Open an account to see successful sign-in history with seconds and the company timezone, change a rep's memberships, disable access, or generate a replacement password. Passwords are hashed in the database. New/reset passwords appear only in that submission response, never in audit events or subsequent account views.

For a new deployment, provision the supplied 16-person roster using `manage.py provision_texting_reps --admin ADMIN_USERNAME --output ABSOLUTE_PRIVATE_TEXT_PATH`. The output directory must exist and the file must not exist. The command creates 12 GFS memberships and 5 Ringcentral memberships, with Emilio Arguello in both. Existing matching accounts keep their passwords and enabled/disabled state; unrelated accounts and uploads are retained. The initial credentials file is private and should be kept outside web-served or shared folders. Run this only against the intended database.

There is no list export feature. Privacy blur and watermarks reduce casual exposure; browsers cannot guarantee detection or prevention of screenshots, photography, or copying numbers that have already been displayed.

## Production and operations

- [Hosting on Vercel](docs/vercel-deployment.md): Vercel function, Neon PostgreSQL, environment variables, moving accounts, and limits.
- [Mac mini deployment](docs/mac-mini-deployment.md): database, HTTPS, service identities, launchd, and first boot checks.
- [Operations and recovery](docs/operations.md): backups, restore drills, updates, and access administration.
- [Architecture and access rules](docs/architecture.md): data flow, scheduling, history, and scope of protections.

Do not expose `runserver`, PostgreSQL, or Gunicorn to the LAN. The deployment templates bind Apache to the Mac's reserved private IP and restrict the allowed office subnet. Installation still requires the actual network address, DNS name, trusted TLS certificate, and administrator-created service accounts.

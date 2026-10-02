# Hosting on Vercel

PhoneTracker runs on Vercel as a single Python function (Django, WSGI), with its stylesheets and scripts served from Vercel's CDN and its data in a Neon PostgreSQL database added through Vercel's Storage tab. Vercel detects `manage.py`, reads `WSGI_APPLICATION`, installs `requirements.txt`, runs `collectstatic`, and uses Python 3.14 from `.python-version`. There is no `vercel.json`; the defaults fit.

## Current deployment

- Vercel project `cap-inf/phonetracker`, production address https://phonetracker-theta.vercel.app (Hobby plan at setup; move to Pro for company use).
- Neon database `phonetracker-db` (free plan, US East `iad1`), connected to Production only. Preview deployments have no database and will not start.
- Production variables: `DJANGO_SECRET_KEY` (Secret), `COMPANY_TIME_ZONE`, and the Neon `DATABASE_URL` set.
- First deployed October 2, 2026 with the admin, 16 reps and both texting lists moved from the local database, passwords unchanged.

## Before you start

- **Plan.** Vercel's free Hobby plan is for non-commercial, personal use only. A company tool belongs on Pro ($20 per developer seat per month; reps don't need Vercel seats). Neon's free database tier is enough for this app.
- **Public address.** Unlike the Mac mini setup, the sign-in page is reachable from anywhere. Access still needs a PhoneTracker account; sign-in is throttled per account and per visitor address (Vercel's own `x-vercel-forwarded-for` header, which visitors can't forge). If the office has a fixed IP, a Vercel Firewall custom rule can allow only that address.
- **Upload size.** Vercel accepts request bodies up to 4.5 MB, so PhoneTracker caps spreadsheets at 4 MB there. A 10,000-number sheet is about 0.2 MB.

## One-time setup

1. Log in and link the project (creates it in your Vercel account):
   ```sh
   npx vercel login
   npx vercel link --yes --project phonetracker
   ```
2. In the Vercel dashboard, open the project, then **Storage → Create Database → Neon (Postgres)**. Pick the region **US East (N. Virginia)**, next to Vercel's default function region `iad1`, and connect it to the project for **Production**. This adds `DATABASE_URL` (pooled) and `DATABASE_URL_UNPOOLED`.
3. Add the remaining Production environment variables:

   | Variable | Value |
   | --- | --- |
   | `DJANGO_SECRET_KEY` | A random string of 50+ characters, kept private. |
   | `COMPANY_TIME_ZONE` | `America/New_York` (the office's scheduling time zone). |
   | `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS` | Only for a custom domain, e.g. `leads.example.com` and `https://leads.example.com`. Vercel's own `*.vercel.app` addresses are added automatically. |

   On Vercel the app always runs in production mode; `DJANGO_ENV` and `PHONETRACKER_DEMO` are ignored there.
4. Create the tables from your machine with the unpooled URL (migrations hold locks a pooler can't keep):
   ```sh
   npx vercel env pull .vercel/.env.production --environment=production
   DATABASE_URL="<DATABASE_URL_UNPOOLED from that file>" DJANGO_ENV=production DJANGO_SECRET_KEY="<same key>" \
     DJANGO_ALLOWED_HOSTS=localhost .venv/bin/python manage.py migrate
   ```
   Migration 0007 creates the GFS Texting (Donut) and Ringcentral Texting (Clean) lists.
5. Accounts: either run `manage.py createsuperuser` and add reps in **Accounts**, or move existing accounts (passwords included) from another PhoneTracker database with `dumpdata auth.user tracker.textinglist tracker.replistmembership` and `loaddata`. Keep that dump private; it contains password hashes.
6. Deploy: `npx vercel deploy --prod`. `.vercelignore` keeps `.env`, `.local/`, databases, tests and docs out of the upload.

## Updating

Run `npx vercel deploy --prod` from a clean checkout, or connect the GitHub repository under **Settings → Git** so pushes to `main` deploy automatically. When a change adds migrations, run step 4 before deploying the code that needs them.

## Checks after a deploy

- `https://<project>.vercel.app/login/` loads with the Dark / Light / Auto switch and no demo labels.
- Sign in as the admin; Lead lists, Texting lists and Accounts open.
- Runtime logs (Vercel dashboard → Logs) show no errors. Neon's dashboard shows the restore window for backups on your plan.

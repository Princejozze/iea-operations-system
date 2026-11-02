# IEA Central Operations & Procurement Management System

Internal tool for Imperial East Africa. It is being built to run the tendering loop end to
end — tender intake from the published PDF, extraction and verification, Managing Director
approvals, item sheet and costing, QC audit, NeST funds, submission, award — and to replace
the four hand-filled Smartsheets the company tendered on before. `CLAUDE.md` §13 says what
is in which phase.

**The specification is [`CLAUDE.md`](CLAUDE.md)**: roles, the status vocabulary, the hard
business rules (R1–R23), the data model and the build order. This README only gets a
machine running. If anything here disagrees with `CLAUDE.md`, `CLAUDE.md` wins.

Django 6.1 · Python 3.12 · PostgreSQL 16 · server-rendered templates, no SPA.
**PostgreSQL is required.** There is no SQLite fallback and `DATABASE_URL` has no default
(§1): much of the enforcement (the audit-trail trigger, CHECK constraints, row locking)
lives in the database, so any other engine proves nothing.

This sequence is for a **fresh Windows 10/11 machine**, in **PowerShell**. Every step ends
with a check; do not move on until it passes. It was rehearsed from a fresh clone against a
throwaway role and database.

---

## 1. Install Python 3.12

Install Python **3.12** (Django 6.1 needs 3.12 or newer) from <https://www.python.org/downloads/windows/>
("Windows installer (64-bit)"). On the first screen tick **Add python.exe to PATH**.
Or: `winget install Python.Python.3.12`.

Also install **Git for Windows** (<https://git-scm.com/download/win>).

Check:

```powershell
py -3.12 --version        # Python 3.12.x
git --version
```

## 2. Install PostgreSQL 16 as a Windows service

Download the **PostgreSQL 16** Windows x86-64 installer from
<https://www.postgresql.org/download/windows/> and run it as an administrator.
(`winget install PostgreSQL.PostgreSQL.16` also works, but on some networks the download
host answers `403 Forbidden` to command-line clients. If so, use the browser download.)

In the installer:

| Prompt | Choose |
|---|---|
| Components | **PostgreSQL Server** and **Command Line Tools**. pgAdmin and Stack Builder are optional. |
| Data directory | the default |
| Password for the `postgres` superuser | a strong one (12+ characters). **Write it down: you need it in step 3.** |
| Port | **5432**. If something already uses it, pick another and use that port in `DATABASE_URL` in step 5. |
| Locale | the default |

The installer registers a service named `postgresql-x64-16` that starts automatically.

Check:

```powershell
Get-Service postgresql-x64-16                         # Status: Running, StartType: Automatic
$env:Path += ";C:\Program Files\PostgreSQL\16\bin"    # puts psql on PATH for this window
psql --version                                        # psql (PostgreSQL) 16.x
```

To keep `psql` on PATH permanently, add `C:\Program Files\PostgreSQL\16\bin` under
System Properties → Environment Variables → Path.

Recommended hardening: the installer's default is `listen_addresses = '*'` (every network
interface; `pg_hba.conf` still admits only this machine). In
`C:\Program Files\PostgreSQL\16\data\postgresql.conf` set `listen_addresses = 'localhost'`,
then restart from an **administrator** PowerShell: `Restart-Service postgresql-x64-16`.

## 3. Create the `iea` role and database

The application connects as an ordinary role: **not a superuser**, with only `CREATEDB`.
`CREATEDB` is there because Django's test runner creates and destroys a database called
`test_iea`. Non-superuser matters because a superuser can switch off the audit-trail trigger
(R7, §4).

Choose a password for the role. It goes into a URL in step 5, so use **letters and digits
only** (no `@ : / ? # %`). For example:

```powershell
py -3.12 -c "import secrets; print(secrets.token_hex(16))"
```

Then create the role and the database (each command asks for the `postgres` password from
step 2). Replace `REPLACE-WITH-THE-PASSWORD`:

```powershell
psql -h localhost -U postgres -c "CREATE ROLE iea LOGIN NOSUPERUSER CREATEDB NOCREATEROLE NOREPLICATION PASSWORD 'REPLACE-WITH-THE-PASSWORD';"
psql -h localhost -U postgres -c "CREATE DATABASE iea OWNER iea;"
```

Check — `rolsuper` must be `f` and `rolcreatedb` must be `t`:

```powershell
psql -h localhost -U postgres -c "SELECT rolname, rolsuper, rolcreatedb FROM pg_roles WHERE rolname = 'iea';"
psql -h localhost -U iea -d iea -c "SELECT current_user, current_database();"     # asks for the iea password
```

> **Development versus production.** One role that both migrates and serves the app is a
> development stopgap. Before any real tender data is loaded, production needs the
> two-role split in `CLAUDE.md` §4 ("Production database role"): `iea_owner` runs
> migrations only; `iea_app` is what Django connects as and holds only `SELECT` and
> `INSERT` on `core_auditentry`. `docker-compose.yml` is an optional Docker route; its
> `iea` user is a superuser and strictly development-only.

## 4. Clone the repository and create the virtual environment

```powershell
git clone https://github.com/Princejozze/iea-operations-system.git
cd iea-operations-system
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If PowerShell refuses to run `Activate.ps1`, run once
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` and activate again. The prompt then
starts with `(.venv)`. Every later command assumes the virtual environment is active.

`pip install` takes a few minutes (PyMuPDF is large). **Do not "upgrade" `pymupdf4llm` to
2.x**: that release is an empty shim whose code moved into `pymupdf` 2.x, and it installs
cleanly and then fails on import. `requirements.txt` pins the working 1.x pair.

Check:

```powershell
python --version                       # Python 3.12.x
python -c "import django, pymupdf4llm, google.genai, openpyxl; print('imports ok')"
```

## 5. Configure `.env`

```powershell
Copy-Item .env.example .env
notepad .env
```

`.env` is gitignored; never commit it. Real environment variables override it. Generate the
secret key with:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(50))"
```

Every variable the code reads:

| Variable | Needed? | What it is |
|---|---|---|
| `SECRET_KEY` | **required** | Django's secret. Use the generated value above. Startup fails without it. |
| `DATABASE_URL` | **required, no default** | `postgres://iea:THE-PASSWORD@localhost:5432/iea`. Must be PostgreSQL; unset, empty or any other engine stops startup with an instruction. |
| `DEBUG` | optional (default `False`) | `True` for local development. **Never `True` in production.** |
| `ENVIRONMENT` | optional locally, **`production` on the server** | `development` or `production`. Production turns on the HTTPS proxy header, secure cookies, the HTTPS redirect and HSTS, and requires `DEBUG=False`, `CSRF_TRUSTED_ORIGINS` and `LOG_FILE`. Unset, `DEBUG=True` means development; `DEBUG=False` with no `ENVIRONMENT` stops startup. |
| `CSRF_TRUSTED_ORIGINS` | **required in production** | The site's origin, e.g. `https://ops.imperialeastafrica.tz` (comma-separate several). Must be `https://`. Without it, and the proxy header that production sets, every POST behind nginx is refused, sign-in included. |
| `SECURE_HSTS_SECONDS` | production only (default `3600`) | HSTS lifetime. Raise to `31536000` after the first stable week. Subdomains and preload are deliberately off. |
| `LOG_FILE` | **required in production** | e.g. `/var/log/iea/app.log`; its directory must exist. Optional in development. Every line also goes to stderr (the journal, under systemd). |
| `LOG_LEVEL` | optional (default `INFO`) | Level for the project's own loggers. Django and libraries log `WARNING` and up; 500 errors arrive with the request and traceback, with secrets and passwords scrubbed. |
| `ALLOWED_HOSTS` | optional (default `localhost,127.0.0.1`) | Comma-separated host names the site may be served on. |
| `EMAIL_BACKEND` | optional | Default `django.core.mail.backends.console.EmailBackend`: emails are **printed in the terminal running the server**, which is where new users' temporary passwords appear in development. For real mail use `django.core.mail.backends.smtp.EmailBackend`. |
| `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS` | only for SMTP | Google Workspace SMTP: `smtp.gmail.com`, `587`, the account, an **app password** (not the account password), `True`. |
| `DEFAULT_FROM_EMAIL` | optional | Sender address. Default `no-reply@imperialeastafrica.tz`. |
| `USE_S3` | optional (default `False`) | `True` sends uploads to S3 (production: uploads must never sit on the app server). |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_STORAGE_BUCKET_NAME` | only if `USE_S3=True` | A scoped IAM user, never the root account. |
| `AWS_S3_REGION_NAME` | optional | Default `af-south-1`. |
| `GEMINI_API_KEY`, `GEMINI_MODEL` | only for extraction | Used by `extract_tenders` (step 8). No model name is hardcoded: pick a current Flash-tier model from Google's documentation (in November 2026, `gemini-3.6-flash`; `gemini-2.5-flash` is retired for new keys). Without them the command exits with an instruction. After changing either, restart the poller. |
| `EXCHANGE_RATE_API_KEY` | currently unused | Reserved. Nothing reads it yet; exchange rates are entered in the admin (step 8). |
| `MEDIA_ROOT` | **required when `USE_S3=False`** | A directory **outside the repository**, e.g. `C:/Users/you/iea-media`. Tender PDFs carry bid pricing, so startup refuses an unset value or a path inside the repo. It is **per machine**: set it to a path on this PC. Uploaded files are in neither git nor the database, so on a second machine the folder starts empty. |
| `RUN_GEMINI_TESTS` | tests only | Set to `1` (with the two Gemini variables) to run the one opt-in test that makes a real, **billed** API call. Not an `.env` setting. |

Minimal development `.env`: set `SECRET_KEY`, `DATABASE_URL` and `MEDIA_ROOT`, leave the rest as shipped.

> **Where uploads go in development.** With `USE_S3=False`, uploads are written under
> `MEDIA_ROOT`, which must be **outside** the repository: startup refuses an unset value or a
> path inside it, so a real tender PDF can never be staged by `git add`. Even so, **never
> use `git add .` or `git add -A` in this repository**; stage explicit paths.

Check (needs `.env` filled in):

```powershell
python manage.py check          # System check identified no issues (0 silenced).
```

## 6. Migrate

```powershell
python manage.py migrate
```

**No database dump is needed.** Everything the system needs at the start is created by
migrations:

- the audit trail and the PostgreSQL trigger that makes it append-only (R7);
- the 17 tender categories (§8);
- the costing defaults (`CostingDefaults`: shipping 10%, duty 25%, C&F 5%, labour 15,000 TZS
  an hour, overhead 10%, margin 20%, maintenance 1%, VAT 18%, cost of capital 22.25%), editable
  in the admin by the Managing Director or a System Admin.

**Not** created by migrations, and added in the admin as needed: **users, clients, suppliers
and exchange rates.**

Check:

```powershell
python manage.py makemigrations --check --dry-run     # No changes detected
```

## 7. Create the first EXEC account

This is the one account created outside the system's own provisioning flow. Every other
account is created by an `EXEC` or `ADMIN` user in the admin (R21).

```powershell
python manage.py createsuperuser
```

It asks for **Email address**, **First name**, **Last name**, **Role** and the **password**
(twice). Sign-in is by email; there is no username.

- At **Role**, type exactly `EXEC` (Executive, Managing Director). The role codes are `EXEC`,
  `HOPS`, `PM`, `AUDIT`, `FIN`, `CLEAR`, `ADMIN`. Anything else is refused
  (`Value '…' is not a valid choice`).
- At first sign-in the system makes you choose a new password before it shows anything else.

## 8. Run it

```powershell
python manage.py runserver
```

Open <http://127.0.0.1:8000/>, sign in with the email and password from step 7, and set a
new password when asked. The admin is at <http://127.0.0.1:8000/admin/>.

Setting up, in the admin:

1. **Users → Add**: name, email, role. The system generates a temporary password and emails
   it (in development it appears in the `runserver` terminal). It expires after 7 days. Nobody
   self-registers.
2. **Exchange rates → Add**: a USD → TZS rate (and any other currency in use) **before costing
   any item priced in a foreign currency**. Without a rate on record the item cannot be costed.
3. **Clients** and **Suppliers**, as tenders need them.

### Optional: demo data, to look at every screen

```powershell
python manage.py seed_demo
```

Creates one demo account per role (sign-in emails and passwords are printed at the end;
the addresses are on the unroutable `demo.invalid` domain), the clients from `CLAUDE.md`
§3, a few suppliers, **illustrative** USD/EUR/GBP rates, and eight tenders spread across
the workflow, from awaiting verification to won and lost. Every state is reached through
the real rules. Safe to run again: it skips whatever already exists. It refuses to run
unless `DEBUG` is on. **Never run it against a database that will hold real tenders**: its
exchange rates are made up, and costing would use them.

### The extraction poller: a second terminal

Tender extraction runs outside the web request. `python manage.py extract_tenders` picks up
tenders in `EXTRACTING`, converts and reads them, and moves them to `PENDING_VERIFICATION`.
It needs `GEMINI_API_KEY` and `GEMINI_MODEL` in `.env`.

**Nothing runs it for you in development.** Windows has no cron, so an uploaded tender sits
in `EXTRACTING` until it is run. Open a **second terminal**, activate the venv, and leave
this running beside `runserver`:

```powershell
python manage.py extract_tenders --loop
```

It runs once a minute, the same cadence as production cron, until you press Ctrl+C. It
prints a line only when it works on a tender. Add `-v 2` to see every run.

- **Expect about two minutes from upload to the verification screen.** That breaks down as
  up to a minute until the next run, about 0.4 s a page to convert (about a minute for a
  170-page tender), and 10–20 s for Gemini.
- **After editing `.env`, restart the poller.** It reads `.env` once, at start. A missing
  key or model is reported on every run until you do.
- **If Gemini is busy** (a 503 "high demand" or a 429), the tender stays in `EXTRACTING`
  and the next run tries again. Only if it is still unread 30 minutes after upload is it
  finished with empty fields for the PM to type in (R12).
- To run it once instead, use `python manage.py extract_tenders` without `--loop`.

`--loop` refuses to start when `DEBUG` is off. **Production uses real cron**, one run a
minute, set up per [docs/DEPLOY.md](docs/DEPLOY.md) §7. Never use the loop there.

## 9. Run the test suite

```powershell
python manage.py test
```

It takes about a minute. It creates and destroys a database called `test_iea` (hence
`CREATEDB` in step 3; the first line it prints names it). Two `manage.py test` runs at
the same time share that one name and destroy each other's database; give each its own by
changing the database name in `DATABASE_URL` for one of them.

**What to expect on a fresh clone** (about 1,150 tests at the time of writing, PostgreSQL 16):

- **About 54 tests are skipped.** 53 need the three real tender PDFs, which are deliberately
  **not in git** (`tenders/tests/fixtures/pdfs/` is gitignored; they must never enter
  history). The skip message names whichever are missing. To run them, obtain the PDFs from
  the project owner and put them in that folder:
  `gasco_general_TR24_001_2026_2027_G_133.pdf`, `sample_general_goods_173pp.pdf`,
  `tanapa_minicomp_TR219_2026_2027_G_38.pdf`.
  The last skip is the opt-in live Gemini call (`RUN_GEMINI_TESTS=1`, see step 5).
- **No failures.** Any failure or error means a setup problem. The usual causes are a
  database role without `CREATEDB`, or `DATABASE_URL` pointing at the wrong database.

---

## Not in git, on purpose

| What | Why |
|---|---|
| `.env` | secrets |
| `.venv/` | per-machine |
| `tenders/tests/fixtures/pdfs/` | real client tender documents |
| uploaded files | real tender documents (see the note in step 5) |
| database dumps and backups | contain real data; keep them outside the repository |
| `scratch/extraction/out/` | extraction output from real tenders |

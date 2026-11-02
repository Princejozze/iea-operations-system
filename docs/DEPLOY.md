# Deployment plan: IEA Operations, Phase 1

Status: **plan only. Nothing here has been created or run.**
Written 2 November 2026. Phase 1 must be live by 30 November 2026.

One EC2 instance runs gunicorn behind nginx. PostgreSQL 16 is on RDS, uploads go
to S3, static files are served by whitenoise, and cron runs the management
commands. There are no containers and no orchestration. The app serves about 31
users.

---

## 0. What exists today

Checked on the development machine on 2 November 2026:

| Check | Result |
|---|---|
| AWS CLI | Not installed |
| `AWS_*` environment variables | None set |
| `~/.aws/config`, `~/.aws/credentials` | No `~/.aws` directory |
| Region | Unknown, because there is no AWS configuration. The only reference in the code is `af-south-1` (Cape Town), the default in `.env.example` and `config/settings.py`. |
| Can the IAM identity create EC2, RDS or S3? | Cannot be determined. There is no identity to test. |
| GitHub repository | `Princejozze/iea-operations-system` is **public**: an unauthenticated API request returns 200. |
| Secrets in git history | None found. A scan for AWS key IDs, Google API keys and filled-in password, key and secret variables matched only a placeholder. |
| `manage.py check --deploy` (`DEBUG=False`) | 5 warnings: W004, W008, W009, W012 and W016. See §2. |

When AWS access exists, run these read-only checks before creating anything:

```bash
aws sts get-caller-identity                    # who am I (should not be the root user)
aws configure get region
# Is Cape Town enabled? It is an opt-in region. Expect "opted-in".
aws ec2 describe-regions --all-regions \
  --query "Regions[?RegionName=='af-south-1'].OptInStatus"
# Can this identity create what we need? Expect "allowed" for each.
aws iam simulate-principal-policy \
  --policy-source-arn "$(aws sts get-caller-identity --query Arn --output text)" \
  --action-names ec2:RunInstances ec2:CreateSecurityGroup ec2:AllocateAddress \
                 rds:CreateDBInstance rds:CreateDBSubnetGroup \
                 s3:CreateBucket s3:PutBucketPolicy s3:PutBucketVersioning \
                 iam:CreateUser iam:PutUserPolicy iam:CreateAccessKey \
  --query "EvaluationResults[].[EvalActionName,EvalDecision]" --output table
# Cheap dry run that needs no simulate permission:
aws ec2 describe-instances --dry-run          # "DryRunOperation" means allowed
```

---

## 1. Things you must obtain or decide

Nothing in §3 onward can start until the first three rows are done.

| # | Item | Who | Why it matters |
|---|---|---|---|
| 1 | **AWS account access**: an IAM user or IAM Identity Center user for the developer with MFA. AdministratorAccess is fine for the setup weeks. Never use the root user. Lock the root user with MFA. | Account owner (Faraja, or whoever pays the AWS bill) | Nothing can be created without it |
| 2 | **Region decision.** Recommended: `af-south-1` (Cape Town), the closest AWS region to Dar es Salaam and already the code default. It is opt-in and must be enabled under *Account → AWS Regions* before use; enabling can take minutes to hours. Neither this nor any alternative such as `eu-west-1` is in Tanzania. Check whether Tanzania's Personal Data Protection Act cross-border rules apply to staff and supplier contact data. | Faraja, with advice | Sets latency, price and data location. Changing it later means moving everything. |
| 3 | **Subdomain.** Proposed: `ops.imperialeastafrica.tz`. This document uses that name. | Faraja | Needed for TLS, `ALLOWED_HOSTS` and email links |
| 4 | **DNS control for `imperialeastafrica.tz`**: someone who can add an `A` record at the registrar or DNS host. That host also holds the Google Workspace MX, SPF and DKIM records, so the person must be careful. | Whoever manages the domain | Let's Encrypt cannot issue a certificate until the name resolves to the server |
| 5 | **Sending mailbox and SMTP app password.** Pick the Workspace account the system sends as. The `DEFAULT_FROM_EMAIL` default is `no-reply@imperialeastafrica.tz`. It must be a real Workspace user or a verified *Send mail as* alias of the SMTP login, otherwise Gmail rewrites the From line. Turn on 2-Step Verification for that account, then create an app password. | Workspace admin | Every alert, MD decision notice and new-user password goes by email (R21). Without email, nobody can be given an account. |
| 6 | **Where supplier replies go.** RFQs go out from this address. Replies to `no-reply@` go unread. Decide whether to use a read mailbox, or `no-reply@` with a `Reply-To`. A `Reply-To` would be a code change in `items`. | Faraja | Supplier quotes must not vanish |
| 7 | **Gemini API key, on a paid (billing-enabled) Google Cloud project.** At the time of writing, Google's terms let it use free-tier prompts to improve its products, and the free tier's rate limits are low. Also pick `GEMINI_MODEL`, a current Flash-tier model from Google's documentation. Do not hardcode it. | Faraja (billing), developer (key) | Extraction and supplier discovery |
| 8 | **Repository visibility.** The repository is public today. It holds the full source and `CLAUDE.md`, which includes margins, staff names and clients. Recommendation: make it private before go-live. The server then needs a read-only GitHub **deploy key** to clone. | Developer, with Faraja's approval | Commercial information is public right now |
| 9 | **Password manager** for the RDS master password, the two database role passwords, the S3 access key, the app password and the Gemini key. At least one other person (Faraja) must have access. | Faraja | If only one person can reach the secrets, losing that person locks the company out |
| 10 | **Developer's fixed IP for SSH**, or accept that you update the security group whenever it changes | Developer | Only SSH from that address is allowed in |
| 11 | **Budget.** A rough estimate is US$80–150 a month. Confirm in the AWS Pricing Calculator for the chosen region. Set an AWS Budgets alert at the agreed figure. | Faraja | Avoids a surprise bill |
| 12 | **Multi-AZ RDS.** Recommended: **no** for launch, because it doubles the database cost. Daily snapshots and point-in-time recovery are the safety net (§9). Revisit once the system is in daily use. | Faraja | Cost against availability |

`EXCHANGE_RATE_API_KEY` is in `.env.example` and `settings.py`, but no code reads it. It is not needed for launch.

---

## 2. Code changes required before the first deploy

These are in `config/settings.py`, which is outside `docs/`. They are listed here
for whoever owns that file. **Item 1 blocks launch.**

1. **TLS proxy header. Without it, every POST is refused with 403, including sign-in.**
   nginx terminates HTTPS and passes plain HTTP to gunicorn. Django then believes
   the request is `http://`, while the browser's `Origin` header says `https://`.
   The CSRF origin check therefore fails every POST.
   ```python
   SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")   # nginx always overwrites it (§6)
   CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])
   ```
2. **Secure cookies, HTTPS redirect and HSTS** (check warnings W004, W008, W012, W016):
   ```python
   if not DEBUG:
       SESSION_COOKIE_SECURE = True
       CSRF_COOKIE_SECURE = True
       SECURE_SSL_REDIRECT = True
       SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=3600)
   ```
   Start HSTS at one hour and raise it to a year after the first stable week.
   **Do not set `SECURE_HSTS_INCLUDE_SUBDOMAINS`.** It would apply to every
   `*.imperialeastafrica.tz` host, not just this app.
3. **Logging.** There is no `LOGGING` setting today. Django's default only prints
   errors when `DEBUG=True`, so a production 500 error leaves no trace anywhere.
   Add a handler that writes WARNING and above to stderr. Under systemd that goes
   to the journal, and under cron to the log files. Optionally, set `ADMINS` so
   500 errors are emailed to the developer.
4. W009 (weak `SECRET_KEY`) appeared only because the check ran with a dummy key.
   Production generates a real one (§5).

The deploy script (§8) runs `check --deploy --fail-level WARNING`, so it refuses
to deploy until items 1–3 are done.

**Optional, no code change needed:** the 101-item sheet page is about 500 KB
uncompressed. nginx compresses it on the way out (§6), so `GZipMiddleware` is not
needed.

---

## 3. AWS resources

Everything is in one region (§1, item 2). Names below are suggestions.

### 3.1 Network
- Use the region's **default VPC**. This plan does not need a custom VPC.
- Security group **`iea-web`**, for EC2:
  - Inbound 443 and 80 from `0.0.0.0/0` and `::/0`. Port 80 is used only for the
    Let's Encrypt check and the redirect to HTTPS.
  - Inbound 22 from the developer's IP only.
- Security group **`iea-db`**, for RDS: inbound 5432 **from the `iea-web` security
  group only**. No public access.
- An **Elastic IP** attached to the instance, so the DNS record never has to change.

### 3.2 EC2
- **Ubuntu Server 24.04 LTS**, x86_64. It ships Python 3.12, which this project
  requires, and `postgresql-client-16`.
- **`t3.medium`** (2 vCPU, 4 GB). The extraction poller loads 170-page PDFs into
  PyMuPDF while gunicorn keeps serving pages. `t3.small` (2 GB) would probably
  work but leaves no headroom.
- 30 GB gp3 root volume, **encrypted**. The disk holds no uploads; they go to S3.
- An SSH key pair. Keep the private key in the password manager.
- No instance profile is needed. S3 is reached with the scoped IAM user's keys
  (§3.4), as `settings.py` expects.

### 3.3 RDS PostgreSQL
- Engine **PostgreSQL 16**, latest 16.x minor. **`db.t4g.small`**.
- 20 GB gp3, with storage autoscaling up to 100 GB. **Encryption at rest on.**
- **Public access: No.** Security group `iea-db`.
- Master username `iea_master`. Its password goes in the password manager only,
  **never on the server** (§5). It is used once, to create the two roles (§4).
- Automated backups kept for **14 days**. **Deletion protection on.** Auto minor
  version upgrade on, with a maintenance window on Sunday 01:00–02:00 EAT.
- Single-AZ (§1, item 12).
- RDS for PostgreSQL 15 and later requires SSL by default. Connect with
  `sslmode=verify-full` against the RDS CA bundle (§5).

### 3.4 S3 for media
- Bucket **`iea-ops-media-<random-suffix>`**. The name must be globally unique.
- **Block all public access: on.** Object ownership is "bucket owner enforced"
  (ACLs disabled). This matches `AWS_DEFAULT_ACL = None`.
- **Default encryption SSE-S3.** **Versioning on.** Tender PDFs and supplier
  quotations are audit evidence, so an overwrite or delete must be recoverable.
- Lifecycle rule: expire noncurrent versions after 365 days.
- No CORS rule. Uploads pass through Django, not straight from the browser.
- Bucket policy that refuses non-TLS access:
  ```json
  {
    "Version": "2012-10-17",
    "Statement": [{
      "Sid": "TLSOnly", "Effect": "Deny", "Principal": "*", "Action": "s3:*",
      "Resource": ["arn:aws:s3:::BUCKET", "arn:aws:s3:::BUCKET/*"],
      "Condition": {"Bool": {"aws:SecureTransport": "false"}}
    }]
  }
  ```
- **IAM user `iea-app-s3`** has no console access and one inline policy that
  reaches only this bucket:
  ```json
  {
    "Version": "2012-10-17",
    "Statement": [
      {"Sid": "List",    "Effect": "Allow", "Action": "s3:ListBucket",
       "Resource": "arn:aws:s3:::BUCKET"},
      {"Sid": "Objects", "Effect": "Allow",
       "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
       "Resource": "arn:aws:s3:::BUCKET/*"}
    ]
  }
  ```
  Create one access key for it. The key goes into `/etc/iea/iea.env` (§5) and
  the password manager.

### 3.5 Account hygiene
- An AWS Budgets alert at the agreed figure.
- CloudWatch alarms on the EC2 status check and on RDS `FreeStorageSpace` below
  4 GB, both emailed to the developer.

---

## 4. The two database roles (CLAUDE.md §4, "Production database role")

This is the production requirement that was deferred. `core_auditentry` has a
trigger that refuses UPDATE and DELETE (`core/migrations/0002`), but the trigger
can be bypassed in three ways:
- a superuser can disable it;
- `TRUNCATE` skips row triggers;
- the **owner** of the table can drop the trigger or grant itself privileges back.

So the role that runs the website must not be the role that owns the schema.

| Role | Owns | Used by | On `core_auditentry` |
|---|---|---|---|
| `iea_owner` | Database `iea`, schema `public`, every table | `manage.py migrate` only, run by the deploy script (§8) | Owner |
| `iea_app` | Nothing | gunicorn and every cron command | **SELECT and INSERT only.** No UPDATE, DELETE, TRUNCATE, TRIGGER or REFERENCES. Not the owner. |

Neither role is a superuser, a member of `rds_superuser`, or able to create roles
or databases.

### 4.1 One-time setup

Run as `iea_master` from the EC2 instance. Generate the passwords with
`openssl rand -base64 33` and store them in the password manager.

```sql
-- connected to database "postgres" as iea_master
CREATE ROLE iea_owner LOGIN PASSWORD '...' NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE ROLE iea_app   LOGIN PASSWORD '...' NOSUPERUSER NOCREATEDB NOCREATEROLE;
-- PostgreSQL 16: creating a database OWNED BY another role requires being able to SET ROLE to it.
GRANT iea_owner TO iea_master;
CREATE DATABASE iea OWNER iea_owner ENCODING 'UTF8';
REVOKE ALL ON DATABASE iea FROM PUBLIC;
GRANT CONNECT ON DATABASE iea TO iea_app;
```

Then **connect to database `iea` as `iea_owner`**:

```sql
-- PG 15+: schema public belongs to the database owner, and PUBLIC cannot create in it.
GRANT USAGE ON SCHEMA public TO iea_app;
-- Every table and sequence iea_owner creates from now on, i.e. every migration:
ALTER DEFAULT PRIVILEGES FOR ROLE iea_owner IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO iea_app;
ALTER DEFAULT PRIVILEGES FOR ROLE iea_owner IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO iea_app;
```

The default privileges deliberately leave out `TRUNCATE`, `TRIGGER` and
`REFERENCES`. They still grant UPDATE and DELETE on every new table, which is
correct everywhere except the audit table. That is closed by the next step.

### 4.2 After every `migrate`

Save this on the server as `/etc/iea/lock-audit-table.sql`. The deploy script
(§8) runs it as `iea_owner` after each migrate, so a migration that ever
recreates the table cannot silently restore UPDATE or DELETE:

```sql
\set ON_ERROR_STOP on
REVOKE ALL ON core_auditentry FROM iea_app;
GRANT SELECT, INSERT ON core_auditentry TO iea_app;

-- Verification: any line that does not read "ok" is a failed deploy.
SELECT CASE WHEN tableowner = 'iea_owner' THEN 'ok' ELSE 'FAIL owner=' || tableowner END
  FROM pg_tables WHERE tablename = 'core_auditentry';
SELECT CASE WHEN has_table_privilege('iea_app', 'core_auditentry', p) = (p IN ('SELECT','INSERT'))
            THEN 'ok' ELSE 'FAIL ' || p END
  FROM unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','TRIGGER','REFERENCES']) AS p;
SELECT CASE WHEN count(*) = 1 THEN 'ok' ELSE 'FAIL trigger missing or disabled' END
  FROM pg_trigger WHERE tgrelid = 'core_auditentry'::regclass
   AND tgname = 'core_auditentry_append_only' AND tgenabled = 'O';
SELECT CASE WHEN NOT rolsuper AND NOT rolcreaterole AND NOT rolcreatedb
             AND NOT pg_has_role('iea_app', 'iea_owner', 'MEMBER') THEN 'ok' ELSE 'FAIL iea_app too powerful' END
  FROM pg_roles WHERE rolname = 'iea_app';
```

The checks use `has_table_privilege` on purpose. **Never test the setup by
running `TRUNCATE core_auditentry` as `iea_app`.** If the setup were wrong, that
test would wipe the audit trail.

### 4.3 What the app needs, checked against the code

- `select_for_update()` needs UPDATE on the locked table. It is used on tenders,
  items, quotes, approvals, fund requests, audits, users and the reference
  counter, and **never on `AuditEntry`**.
- The alert commands use `pg_try_advisory_xact_lock`, which needs no grant.
- `clearsessions` needs DELETE on `django_session`, which the default privileges give.
- New content types and permissions are inserted by `migrate` as `iea_owner`.
- No application code runs `TRUNCATE`, DDL or anything else that needs ownership.

Never run the test suite against RDS. It needs `CREATEDB`, and it relies on
TRUNCATE.

---

## 5. Secrets and environment variables

**Never in git.** `.env` is already in `.gitignore`. The repository is public (§1, item 8).

| File on the server | Owner, mode | Holds | Read by |
|---|---|---|---|
| `/etc/iea/iea.env` | `root:iea 0640` | Every runtime variable below. `DATABASE_URL` is the **`iea_app`** URL. | gunicorn and cron, through a symlink `/srv/iea-system/.env`. `settings.py` reads `BASE_DIR/.env`. |
| `/etc/iea/migrate.env` | `root:root 0600` | One line: `DATABASE_URL=` for **`iea_owner`** | The deploy script only, run as root |
| `/etc/iea/rds-global-bundle.pem` | `root:iea 0644` | The RDS CA bundle, from `https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem` | psycopg2 |
| *(nowhere on the server)* | | The `iea_master` password | The password manager only |

The app user cannot read `migrate.env`, so a compromised web process never holds
the credentials that own the audit table. A `DATABASE_URL` already set in the
environment takes precedence over `.env`, because django-environ's `read_env`
does not overwrite existing variables. The deploy script relies on that.

Every value is also kept in the password manager, so a lost server can be rebuilt.

### `/etc/iea/iea.env`

```ini
SECRET_KEY=            # python -c "import secrets; print(secrets.token_urlsafe(50))"
DEBUG=False
ALLOWED_HOSTS=ops.imperialeastafrica.tz
CSRF_TRUSTED_ORIGINS=https://ops.imperialeastafrica.tz      # once §2 item 1 is in
SECURE_HSTS_SECONDS=3600                                     # raise to 31536000 after week 1

DATABASE_URL=postgres://iea_app:PASSWORD@RDS-ENDPOINT:5432/iea?sslmode=verify-full&sslrootcert=/etc/iea/rds-global-bundle.pem

USE_S3=True
AWS_ACCESS_KEY_ID=                 # iea-app-s3's key
AWS_SECRET_ACCESS_KEY=
AWS_STORAGE_BUCKET_NAME=iea-ops-media-...
AWS_S3_REGION_NAME=af-south-1      # the region chosen in §1

EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
EMAIL_HOST=smtp.gmail.com
EMAIL_PORT=587
EMAIL_USE_TLS=True
EMAIL_HOST_USER=no-reply@imperialeastafrica.tz   # the mailbox from §1 item 5
EMAIL_HOST_PASSWORD=               # its 16-character app password, not the account password
DEFAULT_FROM_EMAIL=no-reply@imperialeastafrica.tz

GEMINI_API_KEY=
GEMINI_MODEL=                      # a current Flash-tier model id from Google's docs
```

`MEDIA_ROOT` is not set. With `USE_S3=True`, `settings.py` does not use it, and
no upload ever touches the server's disk.

---

## 6. Server setup (one time)

```bash
# as the ubuntu user, then sudo
sudo timedatectl set-timezone Africa/Dar_es_Salaam   # cron times below are EAT; Tanzania has no DST
sudo apt update && sudo apt -y upgrade
sudo apt -y install python3.12-venv python3-dev build-essential git nginx \
                    postgresql-client-16 certbot python3-certbot-nginx unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades

sudo adduser --system --group --home /srv/iea-system --shell /usr/sbin/nologin iea
sudo mkdir -p /etc/iea /var/log/iea && sudo chown iea:iea /var/log/iea
sudo -u iea git clone https://github.com/Princejozze/iea-operations-system.git /srv/iea-system
#   (once the repository is private: clone over SSH with a read-only deploy key)
sudo -u iea python3.12 -m venv /srv/iea-system/.venv
sudo -u iea /srv/iea-system/.venv/bin/pip install -r /srv/iea-system/requirements.txt

# secrets (§5), then:
sudo chown root:iea /etc/iea/iea.env && sudo chmod 0640 /etc/iea/iea.env
sudo chmod 0600 /etc/iea/migrate.env
sudo -u iea ln -s /etc/iea/iea.env /srv/iea-system/.env
```

### gunicorn: `/etc/systemd/system/iea-gunicorn.service`

```ini
[Unit]
Description=IEA Operations (gunicorn)
After=network.target

[Service]
User=iea
Group=www-data
WorkingDirectory=/srv/iea-system
RuntimeDirectory=iea
UMask=0007
ExecStart=/srv/iea-system/.venv/bin/gunicorn config.wsgi:application \
          --bind unix:/run/iea/gunicorn.sock --workers 3 --timeout 60 \
          --access-logfile - --error-logfile -
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Three sync workers are plenty for 31 users. nginx buffers request bodies, so a
slow 50 MB upload over a phone connection does not hold a worker.

### nginx: `/etc/nginx/sites-available/iea`

```nginx
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;
    return 444;                       # anything not addressed to our name: drop
}

server {
    listen 80;
    listen [::]:80;
    server_name ops.imperialeastafrica.tz;

    client_max_body_size 55m;         # tender PDFs are capped at 50 MB in tenders/forms.py

    gzip on;
    gzip_proxied any;
    gzip_types text/plain text/css application/javascript application/json image/svg+xml;
    # text/html is always compressed when gzip is on. whitenoise already serves pre-compressed static files.

    location / {
        proxy_pass http://unix:/run/iea/gunicorn.sock;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;   # overwrites anything the client sent (§2 item 1)
        proxy_read_timeout 120s;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/iea /etc/nginx/sites-enabled/iea
sudo rm /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

### TLS: Let's Encrypt

Once the `A` record for `ops.imperialeastafrica.tz` points at the Elastic IP
(§1 item 4; check with `dig +short ops.imperialeastafrica.tz`):

```bash
sudo certbot --nginx -d ops.imperialeastafrica.tz --redirect -m <developer email> --agree-tos
sudo certbot renew --dry-run          # the certbot package installs its own renewal timer
```

certbot adds the 443 server block and the HTTP→HTTPS redirect to the file above.

---

## 7. Cron: `/etc/cron.d/iea`

These entries are the ones already documented in each command's docstring, with
the user field that `cron.d` requires. Times are EAT, because the server clock is
set to `Africa/Dar_es_Salaam` in §6.

```cron
SHELL=/bin/bash
MAILTO=""
# Extraction poller (CLAUDE.md §6): every minute. Overlap-safe; claims each stage under a row lock.
*  *  * * *  iea  cd /srv/iea-system && .venv/bin/python manage.py extract_tenders      >> /var/log/iea/extract.log 2>&1
# Alerts (CLAUDE.md §10): daily, idempotent per tender per day, overlap-safe.
0  7  * * *  iea  cd /srv/iea-system && .venv/bin/python manage.py alert_unpriced_items >> /var/log/iea/alerts.log 2>&1
5  7  * * *  iea  cd /srv/iea-system && .venv/bin/python manage.py alert_nest_funds     >> /var/log/iea/alerts.log 2>&1
10 7  * * *  iea  cd /srv/iea-system && .venv/bin/python manage.py alert_award_capture  >> /var/log/iea/alerts.log 2>&1
# Housekeeping: expired sessions.
30 3  * * *  iea  cd /srv/iea-system && .venv/bin/python manage.py clearsessions        >> /var/log/iea/housekeeping.log 2>&1
```

The file must be `root:root 0644` and end with a newline, or cron ignores it.

`extract_tenders` exits non-zero with an instruction if `GEMINI_API_KEY` or
`GEMINI_MODEL` is missing. Tenders then wait in `EXTRACTING`, and after 30 minutes
the command's own sweep finishes them with empty fields for the PM to type in
(R12). A missing key is noticed when PMs see empty forms, so check `extract.log`
on day one.

Task-overdue notices are not in this list: there is no `Task` model until Phase 3.

### Log rotation: `/etc/logrotate.d/iea`

```
/var/log/iea/*.log {
    weekly
    rotate 12
    compress
    missingok
    notifempty
    copytruncate
}
```

gunicorn's own output goes to the journal: `journalctl -u iea-gunicorn`.

---

## 8. Deploying a release

`/usr/local/sbin/iea-deploy` is root-owned, mode 0750. Usage:
`sudo iea-deploy [commit]`, defaulting to `origin/main`.

```bash
#!/bin/bash
set -euo pipefail
APP=/srv/iea-system
REF="${1:-origin/main}"
cd "$APP"

sudo -u iea git fetch --quiet origin
sudo -u iea git checkout --quiet --detach "$REF"
sudo -u iea .venv/bin/pip install --quiet -r requirements.txt
sudo -u iea .venv/bin/python manage.py check --deploy --fail-level WARNING
sudo -u iea .venv/bin/python manage.py collectstatic --noinput

# Migrate as the OWNER role. Its URL is readable by root only, and an
# environment DATABASE_URL beats .env.
set -a; . /etc/iea/migrate.env; set +a
sudo -u iea --preserve-env=DATABASE_URL .venv/bin/python manage.py migrate --noinput
LOCK_OUT=$(psql "$DATABASE_URL" -X -q -t -v ON_ERROR_STOP=1 -f /etc/iea/lock-audit-table.sql)  # a psql error stops here (set -e)
unset DATABASE_URL
echo "$LOCK_OUT"
if grep -q FAIL <<<"$LOCK_OUT"; then
  echo "AUDIT TABLE PRIVILEGES WRONG - not restarting" >&2; exit 1
fi

systemctl restart iea-gunicorn
echo "$(date -Is) $(git rev-parse HEAD)" >> /var/log/iea/deploys.log
```

- Deploy outside working hours (EAT). Between `migrate` and the restart, the old
  code runs against the new schema for a few seconds.
- **Before any release that contains a migration, take a manual RDS snapshot**
  (`aws rds create-db-snapshot`, or from the console).
- **Rollback:** run `sudo iea-deploy <previous sha>`; the previous SHAs are in
  `deploys.log`. Migrations are not reversed automatically. If the release
  included one, restore from the pre-deploy snapshot rather than migrating
  backwards by hand.
- These scripts live only on the server for now. Moving them into the repository,
  for example under `deploy/`, means a new top-level directory that no current
  work stream owns. Agree on that first.

---

## 9. Backups and recovery

- **Database:** RDS automated backups with 14 days of point-in-time recovery,
  plus a manual snapshot before each release that contains a migration and one
  immediately before go-live. Manual snapshots are kept until deleted.
- **Media:** S3 versioning (§3.4). A deleted or overwritten file stays
  recoverable for 365 days.
- **Server:** nothing on it is unique. It is rebuilt from §6, the password
  manager and git.
- **Restore test before go-live:** restore a snapshot to a temporary instance,
  point a shell at it, confirm the tenders and audit entries are there, then
  delete the instance. A backup that has never been restored is not yet a backup.

---

## 10. First run and smoke test

1. `sudo iea-deploy`. The first run creates every table as `iea_owner`, and the
   lock script must print only `ok`.
2. Bootstrap one account: `sudo -u iea .venv/bin/python manage.py createsuperuser`,
   with role **`ADMIN`**, for the developer. **Every other account, starting with
   Faraja's `EXEC`, is created in the app**, so the R21 flow (generated temporary
   password, emailed once, 7-day expiry, forced change) is exercised from day one.
3. Email: `sudo -u iea .venv/bin/python manage.py sendtestemail <your address>`.
   Check that it arrives, that the From line is right, and that it is not in spam.
4. In a browser at `https://ops.imperialeastafrica.tz`, signed in as the admin,
   with a rehearsal user created in the app:
   - Sign-in works. This proves §2 item 1.
   - Uploading a real tender PDF reaches `PENDING_VERIFICATION` within about two
     minutes, and the object appears in the S3 bucket.
   - The item sheet loads on a phone, and the response is gzip-compressed
     (`curl -sI --compressed …` shows `content-encoding: gzip`).
   - Run each alert command by hand once and read `alerts.log`.
5. **Rehearse on a separate database, not on `iea`.** Every row written to `iea`,
   including test data, leaves `AuditEntry` rows that can never be deleted (R7).
   Do all rehearsal on a database `iea_rehearsal` (§4 again, with its own owner
   and app roles, or the same roles granted on it). Then point `DATABASE_URL` at
   a fresh, empty `iea` for go-live.

Legacy Smartsheet import and cutover are not covered here.

---

## 11. Timeline to 30 November

| Week | Goal |
|---|---|
| **2–6 Nov** | §1 items 1–9 obtained or decided. §2 settings changes merged. `af-south-1` enabled if chosen. |
| **9–13 Nov** | AWS resources (§3), database roles (§4), server (§6), first deploy against `iea_rehearsal`, TLS, email, cron. Smoke test (§10). |
| **16–20 Nov** | Rehearsal with real PMs on real tenders. Fix what they find. Snapshot restore test (§9). |
| **23–27 Nov** | Freeze. Fresh `iea` database. Accounts provisioned through the app. Go-live snapshot. |
| **≤ 30 Nov** | Live. HSTS raised the following week. |

The early weeks are front-loaded on purpose. Region opt-in, DNS changes,
Workspace admin settings and Google billing all wait on other people, and that is
where the surprises will be.

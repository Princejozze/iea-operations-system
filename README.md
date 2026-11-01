# IEA Central Operations & Procurement Management System

Phase 1 scope (Modules 1–3: Tender Management, BOM, Pre-Bid QC & Audit).
Personal target: working MVP by Nov 30, 2026. Company hard launch: Dec 7, 2026.

## What's in this scaffold

- `config/` — Django project settings, wired for env-based config (dev now, AWS later with zero code changes — just flip env vars)
- `accounts/` — custom User model with the 7 IEA role tiers (Executive, Head of Operations, PM, Auditor, Clearing Agent, Finance, System Admin) and the PM/Auditor segregation-of-duty check (`User.can_audit()`)
- `tenders/` — Tender model skeleton with the pipeline status field the original spec didn't define (Drafting → Costing → Pending Audit → Pending Executive Approval → Submitted → Won/Lost), and the exchange-rate lock field
- `requirements.txt`, `.env.example`, `docker-compose.yml` — everything needed to run this locally today

This is deliberately just the foundation (auth, roles, schema skeleton) —
matches the Oct 31–Nov 6 "Foundation" week. The full Module 1 field set,
BOM pricing engine, and QC Audit workflow are built in the following weeks,
not here.

## Local setup (do this today)

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # fill in SECRET_KEY at minimum
docker compose up -d            # starts local Postgres
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

No AWS account needed to start coding — `USE_S3=False` and the
console email backend mean everything runs fully offline until you're
ready to deploy.

## Preliminary steps before/alongside coding

### Blocking externally — chase these first
1. **Budget sign-off from Faraja** (Claude Ultra $200/mo + AWS ~$35–45/mo) — your Monday document asks for approval by Nov 3. Nothing else here is blocked by it, but deployment to a real server is.
2. **AWS access** — a scoped IAM user for you (not the root account), with permissions for EC2, RDS, and S3 only. Whoever holds IEA's AWS root login needs to create this.
3. **A subdomain** off IEA's existing domain (e.g. `tenders.imperialeastafrica.tz`) pointed at this system, plus SSL — can likely reuse however the existing domain/SSL is already managed.
4. **Google Workspace app password** (or decide now to use a transactional email provider instead of raw SMTP) — every SRS automation (T-minus alerts, overdue-task emails, NeST liquidation alerts) depends on outbound email actually working.

### Quick, no approval needed
5. Free API key from [exchangerate-api.com](https://www.exchangerate-api.com/) (free tier, 1,500 requests/month) — 2 minutes.
6. Decide the real people behind each role for seed data: who is Head of Operations? Who is Auditor (must be a different person from whoever is PM on a given tender — this is enforced in code, so it needs a real second person)? Is there a Finance person, or does that role go unused for now?

### Already resolved in this scaffold, flag if you disagree
7. Tender outcome/status field — added (`TenderStatus`), since the SRS never defined the Module 1→4 handoff trigger.
8. Exchange-rate lock point — locks when the BOM is submitted to QC Audit (`exchange_rate_locked_at`), so the price the Auditor/CEO approve doesn't drift after the fact.
9. "Idle Item Protocol" (unpriced BOM items) — will be enforced as a hard block on submission to QC Audit, not just a warning, when Module 2 is built next week.
10. "Negative Compliance" from the Auditor — will route back to the PM for correction rather than silently continuing to the CEO, when Module 3 is built.

### Ongoing cadence
11. Stick to the build order already set: Foundation (this week) → Tender Mgmt (Nov 7–13) → BOM Pricing (Nov 14–20) → QC Audit (Nov 21–27) → Harden (Nov 28–30) → buffer → Dec 7 launch.

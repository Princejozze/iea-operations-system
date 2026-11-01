# IEA Central Operations & Procurement Management System

Internal tool for Imperial East Africa. Replaces four hand-filled Smartsheets that
the company runs its tendering on today.

**Phase 1 must be live and usable by 30 November 2026.**
Company hard launch 7 December 2026. One developer. Treat scope creep as the
main risk.

---

## 1. How to work in this repo

- Python 3.12, Django 6.1, **PostgreSQL — required, not a preference.**
  Server-rendered templates, **no SPA**.

> **SQLite is not a supported fallback and must never be the default.** A large
> part of this system's enforcement lives in database constraints, triggers and
> raw SQL. On SQLite those tests skip or error, so a suite that "passes" there
> proves almost nothing about R7, the status constraints or the rate-lock window.
> `DATABASE_URL` has no default: if it is unset, startup fails loudly with an
> instruction to set it. Never report a test result without naming the database
> it ran against, and never compare two runs on different databases.
- htmx for interactivity. Bootstrap 5 and htmx are **self-hosted** in `static/vendor/` — never add a CDN link.
- Config via environment variables (`django-environ`), see `.env.example`.
- Files go to S3 in production via the `STORAGES` setting (Django 5.1 removed
  `DEFAULT_FILE_STORAGE` and `STATICFILES_STORAGE` — do not use them).
- Prefer boring, well-documented Django patterns over clever ones. This codebase is
  written largely with AI assistance and must stay easy to verify.
- Every model holding business data inherits `core.Auditable`. See R7.

Run locally:
```bash
python manage.py migrate && python manage.py runserver
```

### Committing during parallel work

Several sessions work in this tree at once, in different app directories.

- **Commit only your own paths.** `git add approvals/ tenders/` — never
  `git add -A` or `git add .`, which sweeps up another session's half-finished
  work and makes both streams unreviewable.
- Commit before handing back, so each stream's work is one reviewable diff.
- If a path you need is already modified and is not yours, stop and say so
  rather than committing it or reverting it.

### Application structure

Twelve apps, in dependency order. **An app may only import from apps above it.**

| # | App | Holds | Phase |
|---|---|---|---|
| 1 | `core` | `AuditEntry`, abstract bases (`TimeStamped`, `Auditable`), reference generation, shared permission mixins, the `Client` table | 1 |
| 2 | `accounts` | `User`, roles, rank, `EmailAlias` | 1 |
| 3 | `documents` | Letterhead template rendering, the `Document` registry. **Renders from a plain context dict and never imports a domain model or enum** — that is what keeps it independent of everything below it | 1 |
| 4 | `suppliers` | `Supplier` only — master data, no tender or item dependency | 1 |
| 5 | `tenders` | `Tender`, `Lot`, `TenderDocument`, `TenderExtraction`, `TenderAssignment`, `TenderOutcome`, PDF→Markdown, Gemini extraction, **all status transition methods** | 1 |
| 6 | `items` | `Item`, `ItemAttachment`, `Quote`, `SupplierSuggestion`, the costing chain, currency conversion, bulk specification import, **AI-assisted OEM discovery**, **RFQ dispatch and chasing** | 1 |
| 7 | `audits` | `QCAudit`, `QCAuditLine`, specification variance flagging | 1 |
| 8 | `approvals` | `Approval`, `EligibilityReview`, `FundRequest` | 1 |
| 9 | `postaward` | `PostAward`, `PaymentRecord`, `ShipmentRecord`, clearing agent views | 2 |
| 10 | `comms` | `EmailThread`, `EmailMessage`, `ChatChannel`, `ChatMessage`, `Announcement`, `Meeting` | 2–3 |
| 11 | `tasks` | `Task`, rank-based assignment (R11) | 3 |
| 12 | `dashboards` | Views and templates only, **no models** | 3 |

**Reverse accessors are how `tenders` reaches downward.** `Tender` lives in app 5
but its transition methods must check items, audits and approvals, which live
below it. Use reverse relations — `tender.items.filter(unit_price__isnull=True)`,
`tender.approvals.filter(...)` — which require **no import** and therefore do not
break the dependency rule. Never `from items.models import Item` inside `tenders`.

**Build `core` first.** R7 touches every model. Retrofitting the audit trail after
other apps have migrations means rewriting those migrations.

Do not create apps outside this list, and do not move a model to a different app.

---

## 2. The organisation

**This is a single company.** Every tender, item, task and document belongs to
Imperial East Africa. There is no company or tenant dimension on any model.

(Legacy Smartsheet data carries codes `IEA`, `KOME`, `KEA` and `PRESTIGE` from an
earlier arrangement. On import these are historical metadata only. Never model
them as a live field.)

### User identity
**One human maps to exactly one `User` account.** Staff hold addresses on
`imperialeastafrica.tz`, and historically also on `komeeastafrica.tz` and
`keapharma.co.tz`; the extra addresses become `EmailAlias` rows used for import
matching. Email and all aliases are unique across the whole system.

A person who genuinely performs two functions still gets **one** account. Never
issue a second account to work around a permission — that would let a PM audit
their own tender from their other login and silently defeat R1.

### Roles

Seven roles. One role per user.

| Role | Code | Rank | Can do |
|---|---|---|---|
| Executive (Managing Director) | `EXEC` | 1 | Approve at registration and submission. Decide eligibility. Approve fund requests. Withdraw tenders. Reassign tenders. Broadcast announcements. Read everything. |
| Head of Operations | `HOPS` | 2 | **Read-only on tenders.** Receives escalations and alerts. Creates and assigns tasks. |
| Project Manager | `PM` | 3 | Register tenders, build item sheets, price items, source suppliers, submit to audit, raise fund requests. |
| Auditor | `AUDIT` | 3 | Review item sheets for specification compliance. |
| Finance | `FIN` | 3 | Mark fund requests paid, record payments, view budget figures. **Never writes to `Tender`.** |
| Subcontractor / Clearing Agent | `CLEAR` | 4 | Post-award records only. Sees nothing else. |
| System Admin | `ADMIN` | — | User and system configuration. Outside the business hierarchy. Cannot alter the audit trail. |

Rank governs task assignment (R11) and nothing else. Lower number is senior.
Only a user with role `PM` may hold `Tender.project_manager`.

`role` is mandatory, has no default, and is in `REQUIRED_FIELDS` so
`createsuperuser` cannot produce a roleless account.

**Sign-in is by email address. There is no `username` field.** Accounts are
provisioned with name, email and role (R21), so a username would be a third
identifier nobody uses. Matching is case-insensitive. `EmailAlias` rows exist for
legacy import matching only and can never be used to sign in.

### User provisioning

**Only `EXEC` and `ADMIN` can create user accounts.** Nobody self-registers; there
is no public sign-up page and no invite link anyone else can send.

The flow, enforced by R21:

1. The MD or admin creates the account with name, email and **role** — role is
   required at creation, never left blank.
2. The system generates a random temporary password. The creator never chooses it
   and is never shown it.
3. The system emails the new user their email address and that temporary password,
   once. It is not stored in readable form anywhere and cannot be re-displayed — if
   it is lost, the account is reissued a fresh one.
4. The temporary password expires **7 days** after issue. After that it is dead and
   must be reissued.
5. On first sign-in the user can reach **nothing** but the change-password page.
   Middleware redirects every other URL while `must_change_password` is true.
6. Setting a new password clears the flag and the account becomes usable.

`User` carries `must_change_password` (default `True` on creation) and
`temporary_password_expires_at`. Creating a user and completing the first password
change both write `AuditEntry` rows.

Known people: Faraja Massawe (Managing Director, `EXEC`), Mariam Rweyemamu
(`HOPS`), Gift Minja (`PM`), Alice Joseph, Sylvia Kaboyoka, Anna Matiku,
Janeth Makori, Dinna Masagasi and others. ~31 active accounts in the legacy data.

---

## 3. Glossary

Use these terms exactly. Do not invent synonyms in code or UI.

| Term | Meaning |
|---|---|
| **Tender** | A procurement opportunity published by a client. The top-level record. |
| **Client** | The government body or institution issuing the tender. A real table in `core`, not free text. |
| **Tender document** | The PDF the client publishes on NeST. Source of every tender detail. |
| **Technical specification** | The section of the tender document listing what must be supplied. Split out as the RFQ reference. |
| **Special / minimum requirements** | Eligibility conditions a bidder must meet — turnover, experience, certifications, authorisations, local content. Failing one disqualifies the bid. |
| **Joint venture (JV)** | Partnering with a firm holding a qualification IEA lacks, so the bid can still be made. |
| **Required forms** | Forms the tender obliges the bidder to complete and submit. |
| **Lot** | A sub-division of a tender. Large tenders split items into lots bid separately. |
| **Item** | One line in a tender's technical specification. |
| **Item sheet / BOM** | The full set of items for a tender, with prices. |
| **NeST** | Tanzania's national e-procurement portal. **The portal account must hold funds before a bid can be submitted.** |
| **Application fund** | Money that must sit in the NeST account to cover tender fees at submission. PM requests, MD approves, Finance pays. |
| **RFQ** | Request for Quotation — sent to suppliers to price items. |
| **OEM** | Original Equipment Manufacturer. Preferred source where one exists. |
| **Complete unit** | One manufacturer supplying an entire assembled system rather than separate parts. |
| **MAF** | Manufacturer's Authorisation Form. |
| **Bid security** | A bank guarantee or insurance bond lodged with a bid. |
| **PVOC** | Pre-Export Verification of Conformity. |
| **EOT** | Extension of Time on a contract. |
| **Incoterm** | Delivery terms governing what the quoted price includes. |
| **Bid opening** | Public event where bids are opened and ranked. |
| **QC audit** | Internal pre-bid compliance review. The firewall before submission. |
| **Post-award** | Everything after a contract is won: import, logistics, payment, clearing. The only area `CLEAR` can see. |

### Real clients (seed data)
Wizara ya Kilimo (Ministry of Agriculture), MSD, TBS, JKCI, MUHAS, University of
Dar es Salaam, Mt. Meru Regional Referral Hospital, GASCO, MNH, Government Chemist
Laboratory Authority, Mkwawa University, DIT, NIMR, TANAPA, TCAA.

**This is predominantly a medical, laboratory, scientific and university supply
business.** Not construction. Not aviation.

---

## 4. Conventions fixed by decision

These were previously unspecified. They are now settled — do not invent alternatives.

### Reference formats
- **Tender:** `IEA-{YYYY}-{NNNN}` — e.g. `IEA-2026-0042`. Sequence restarts each
  calendar year. Generated inside the same transaction as the tender, using
  `select_for_update()` on a counter row so concurrent uploads cannot collide.
- **Item:** `{tender_reference}-{NNN}` — e.g. `IEA-2026-0042-007`, numbered within
  its tender.
- Legacy `KEA`-prefixed references are preserved on imported records in a separate
  `legacy_reference` field. New records never use that format.

### Prices and the costing chain
Two distinct figures, never conflated:
- `Item.unit_cost` — what the supplier charges, in `currency`, on the stated
  `incoterm`. This is what a quote gives you.
- `Item.unit_selling_price_tzs` — the output of the chain below.

**The chain is taken from IMPERIAL EAST AFRICA LIMITED MASTER TEMPLATE 2026.**
Implement it exactly.

Company defaults live in a **`CostingDefaults` singleton model in `core`** — not
in `settings.py` and not hardcoded. **Only `EXEC` may edit it**; `ADMIN` may view.
A gross operating margin is Faraja's commercial lever, not an IT setting. Three reasons: Faraja can change a margin
without a deploy, every change is audited under R7 so an old bid can be explained
with the rates that applied at the time, and `items` never has to touch a shared
file during parallel work. Each `Item` stores the values it was costed with, so
changing a default never silently reprices a past bid.

| Constant | Value |
|---|---|
| Shipping | 10% of FOB material cost |
| Duty | 25% of CIF cost |
| C&F | 5% of CIF cost |
| Labour rate | 15,000 TZS per hour |
| Overhead | 10% |
| Gross operating margin | 20% |
| Maintenance & guarantee | 1% |
| VAT | 18% |

Per-item inputs: FOB unit price and currency, local material cost per unit,
quantity, `shipping_applies` (imported vs locally sourced), risk-management
percentage, labour hours per unit.

All figures in TZS, converted at the rate locked by R4:

```
import_material  = fob_unit_price × fx_rate × quantity
shipping         = import_material × 10%          ┐
cif              = import_material + shipping     │ only when
duty             = cif × 25%                      │ shipping_applies
cnf              = cif × 5%                       ┘  otherwise all zero
total_material   = (local_unit_cost × quantity) + import_material
                   + shipping + duty + cnf
risk_management  = total_material × risk_pct
overhead         = (total_material + risk_management) × 10%
margin           = (total_material + risk_management + overhead) × 20%
material_selling = total_material + risk_management + overhead + margin

labour_hours     = hours_per_unit × quantity
labour_cost      = labour_hours × 15,000
labour_selling   = labour_cost + (labour_cost × 10%) + (labour_cost × 20%)
                 = labour_cost × 1.30
```
**Labour markup is additive, not compounding.** Confirmed from the template
formula `AB + ($O$2 × AB) + ($O$3 × AB)` — overhead and margin are each taken on
the labour cost, giving 1.30, not 1.32. Derive it as `1 + overhead + margin` so it
follows edits to either rate.
```

maintenance      = (material_selling + labour_selling) × 1%
total_selling    = material_selling + labour_selling + maintenance
unit_rate        = floor(total_selling / quantity)
```

Store every intermediate value, not just the result, so a bid submitted months ago
can still be explained line by line.

**Two open questions on this chain — ask Faraja, do not guess:**
1. The template computes a cost-of-capital column (`total_material × 22.25%`) and
   then never adds it to the total. Is it meant to be in the selling price?
2. That column is labelled "annual interest rate 20%, t=3 months" but the formula
   applies the full 20%, not a quarter of it. Which is right?

Until both are answered, compute cost of capital, store it, and **exclude it from
`total_selling`** — matching the template's actual behaviour rather than its label.

VAT (18%) is applied at bid-summary level, not per item.

`Item.total_cost = unit_cost × quantity`. All of these are **computed, never
entered** (R10). A tender's bid value is the sum of its items' `total_selling`.

### Signatures
`Approval.signature` is the approver's typed full name plus a re-entered password,
verified at the moment of signing. Store the typed name, the timestamp, and the
user — never the password.

### Background work
T-minus alerts, supplier chasing and the award prompt run as Django management
commands invoked by cron. No Celery, no Redis, no broker in Phase 1. If a real
queue is needed later, add `django-q2` — do not add Celery.

### Notifications
**Email only in Phase 1**, through Django's email backend on Google Workspace
SMTP. Every alert and every MD decision notice is an email. In-app notifications
arrive with `comms` in Phase 3; do not build a notification model before then.

### External packages
Add to `requirements.txt` when first needed, pinned:
- `pymupdf4llm` for PDF→Markdown (AGPL via PyMuPDF; substitute `docling`, MIT, if
  that matters), behind a `DocumentConverter` interface.
  **Pin `pymupdf4llm==1.28.2` with `pymupdf==1.28.2`.** Version 2.0 is an empty
  shim that installs cleanly and then fails on import, and the `pymupdf` pin stops
  pip resolving 2.x underneath it. Do not "upgrade" these.
- `google-genai` — the unified Google Gen AI SDK. The older `google-generativeai`
  is superseded; verify the package name at install time.
- **Do not hardcode a Gemini model identifier.** Put it in `GEMINI_MODEL` in the
  environment and pick a current Flash-tier model from Google's docs at build
  time. Model names change often enough that a pinned one in source will rot.

### Testing
`CompressedManifestStaticFilesStorage` fails any test that renders a template,
because tests never run `collectstatic`. Settings must fall back to plain
`StaticFilesStorage` when `"test" in sys.argv`. Do not scatter
`@override_settings` across test classes to work around it.

### Production database role
The audit trail's PostgreSQL trigger can be disabled by a database superuser, and
row triggers do not fire on `TRUNCATE`. **In production the application must
connect as an ordinary role with no superuser rights and no `TRUNCATE` privilege
on the audit table.** The `iea` user in `docker-compose.yml` is a superuser and is
development-only.

**`REVOKE TRUNCATE` is not sufficient on its own.** A role that *owns*
`core_auditentry` can re-grant itself the privilege or drop the trigger, so a
single role that both migrates and serves the app defeats R7 in principle.

**Required before production deploy — two roles:**
- `iea_owner` owns the schema and runs migrations. Used by `manage.py migrate`
  only, never by the running application.
- `iea_app` is what Django connects as at runtime. On `core_auditentry` it holds
  `SELECT` and `INSERT` only — no `UPDATE`, `DELETE`, `TRUNCATE`, and no ownership.

A single role with `REVOKE TRUNCATE` is acceptable in development. It is a known
gap, not a solution, and it must be closed before real tender data is loaded.

### Enforcement
"Enforce in the model layer" means: validation in `clean()`, the same check
repeated in `save()`, and a database constraint where the rule can be expressed as
one. Status is **never** changed by `queryset.update()` or admin — only by the
transition methods in R19.

---

## 5. How a tender actually starts

1. **PM finds a tender on NeST** and downloads the published PDF.
2. **PM uploads that PDF.** This is the only way a tender is created; there is no
   blank "new tender" form.
3. **System creates the `Tender` and its reference immediately**, status
   `EXTRACTING`.
4. **System converts the PDF to Markdown** before any AI call (§6).
5. **System sends the Markdown to Gemini** and extracts: tender number, title,
   client name and address, submission deadline, bid security requirement and
   type, special requirements, required forms, and the technical specification
   section.

   **The submission deadline is reliably present** (Tender Data Sheet item 36, or
   the Invitation clause on a mini-competition). **The clarification deadline and
   site visit requirement usually are not** — in all three test tenders they
   appeared only in generic boilerplate, never as a stated value. Return null and
   let the PM fill them. Never infer a date from boilerplate (R12).
6. **Status becomes `PENDING_VERIFICATION`** whether extraction succeeded, partly
   succeeded, or failed. A failed extraction leaves the fields empty for the PM to
   type; it never traps the tender (R12).
7. **PM verifies and confirms every field.** Confirmation copies verified values
   onto `Tender` and moves it to `PENDING_REGISTRATION_APPROVAL`.
8. **Registration gate (R15):** the MD approves → `DRAFTING`, or declines →
   `WITHDRAWN`. No item, costing or RFQ work may begin before approval.
9. **System splits the technical specification** into its own document and parses
   line items into draft `Item` rows.
10. **Eligibility (R16):** if IEA does not meet a special requirement, the PM raises
    an `EligibilityReview`; the tender moves to `PENDING_ELIGIBILITY_DECISION`.
    Only the MD resolves it: proceed, joint venture, remediate, or withdraw.
11. **PM sources suppliers, AI-assisted.** This is a real step, not a manual hunt:
    - The PM selects an item, a group of items, or a whole lot, and asks the system
      to find manufacturers.
    - The system sends that technical specification to **Gemini with Google Search
      grounding** and asks for **about five OEM or supplier candidates** that can
      meet it, each with name, country, website, and a short reason it fits.
    - Scope matters and is chosen by the PM: a **complete unit** — one manufacturer
      supplying a whole assembled system such as a pressure reduction and metering
      station with its pipes, meter and valves — or **separate parts**, several
      suppliers for individual items.
    - Candidates land in `SupplierSuggestion`, never as `Supplier` rows. The PM
      accepts or rejects each one (R12 applies here exactly as it does to
      extraction). Accepting creates a `Supplier`, or links an existing one after
      deduplication.
    - RFQs then go to the accepted suppliers.

    Store the model's stated reason and its source URLs on every suggestion. A PM
    cannot judge an unfamiliar manufacturer without seeing why it was proposed and
    where the claim came from.
12. **PM starts costing**, which moves the tender to `COSTING`. RFQs go out from
    here, always cc `sales@` (R8).
13. **Quotes come back, and the PM enters them.** Two things happen together and
    neither substitutes for the other:
    - **Attach the supplier's original** — the quotation exactly as they sent it.
      This is the evidence, and it is one of the three mandatory item attachments.
    - **Enter the figures** that drive the costing chain: unit price, currency,
      Incoterm, whether shipping applies, country of origin, make and model as
      actually offered, lead time, validity, and the **specification offered**.

    The typed figures are the working data; the attachment is the proof. Keep both
    — a price with no document behind it cannot be defended at audit, and a
    document nobody transcribed cannot be costed.

    **The specification offered flows into the QC audit.** `QCAuditLine.
    specification_offered` is populated from the selected quote, so the auditor
    reviews what the supplier actually committed to rather than retyping it.

    **Selecting a quote** copies its figures onto the `Item` as costing inputs and
    sets `Item.selected_quote`. An item may hold several quotes; exactly one is
    selected.

`start_costing` requires only that the tender is in `DRAFTING` and has at least
one `Item`. It does **not** require the specification split to have succeeded —
the split can fail or return nothing and the PM adds items by hand, exactly as
with extraction (R12). Never gate work on an AI step having worked.

Then: completeness (R2) → QC audit → submission approval (R3) → funds (R17) →
submitted → outcome.

---

## 6. Document processing and AI extraction

### PDF to Markdown first
Never send a raw PDF to the model. Tables and headings survive conversion, token
cost drops sharply, the Markdown is reused for the specification split, and page
provenance stays deterministic.

- Library: `pymupdf4llm`. It is AGPL via PyMuPDF — acceptable for an internal tool,
  but if that is a concern substitute `docling` (MIT). Put either behind a
  `DocumentConverter` interface.
- Store the Markdown on `TenderDocument.markdown` with page markers retained.

### Tender documents are standard PPRA templates
Real tenders are 70–175 pages of Tanzanian government boilerplate with a
**table of contents carrying page numbers**. Two variants seen so far:

| | Mini-competition (e.g. TANAPA, 73pp) | General goods (e.g. GASCO, 173pp) |
|---|---|---|
| Metadata | `SECTION I: INVITATION` | **`TENDER DATA SHEET`** |
| Eligibility | `QUALIFICATION AND EVALUATION CRITERIA` | `QUALIFICATION AND EVALUATION CRITERIA` |
| Items | `SCHEDULE OF REQUIREMENTS` → `List of Goods and Related Services` | `PROCURING ENTITY'S REQUIREMENTS` → `Schedule of Goods and Related Services` |
| Specs | `Specifications` | `Technical Specifications` |
| Forms | `TENDERING FORMS` | `TENDERING FORMS` |

**Parse the table of contents first, then extract section by section.**

Validated against three real tenders (TANAPA mini-competition, GASCO general
goods, Geological Survey of Tanzania). Every section was located in all three.

**The reason is accuracy, not cost.** Whole documents are only 26k–58k tokens, so
a full pass would fit in context comfortably. Sectioning wins because it gives the
model focused context and because page provenance falls out of it for free. Token
saving (6–10×) is a bonus, not the justification.

**Locate by TOC page range, then confirm by content signature — not by heading
name.** TOC names do not match body headings: the TOC says "Schedule of Goods",
the body says "List of Goods"; TANAPA's Specifications section has no heading at
all and was confirmed only because a Technical Requirements table sits in that
page range. Only about half of TOC headings can be confirmed by name on their
stated page. **Page numbers, however, were correct every time.** Keep an alias
list, but treat it as a hint — a list tuned on three documents will miss on a
fourth.

**The whole-document fallback is first-class, not an edge case.** When a section
cannot be located confidently, send the whole document with the TOC as a hint. It
fits. Never fail an upload because a heading was unfamiliar.

The extractor should:

1. Convert to Markdown and locate the TOC.
2. Pull metadata from the Tender Data Sheet or Invitation section.
3. Pull special requirements from `QUALIFICATION AND EVALUATION CRITERIA`.
4. Read `TENDERING FORMS` for `required_forms` — the TOC lists them by name
   (Manufacturer's Authorization Form, Tender Securing Declaration, Power of
   Attorney, Declaration Against Fraud and Corruption, and so on).
5. Split the specification pages into the `TECHNICAL_SPEC` document.
6. Parse item rows from the goods schedule table: `S/N | Description |
   Unit of measure | Quantity`.

This is cheaper, far more accurate, and page provenance falls out of it for free.
Section names vary between variants, so match on a list of known headings and fall
back to a whole-document pass only when none is found.

Two details from real documents:
- Goods schedules carry a **`GFS Code`** (e.g. `31122220 - Gardening equipment and
  machinery`), a government classification. Capture it on the lot as `gfs_code`.
- `Unit of measure: Lot, Quantity: 1` means a bundle of machines priced as one
  line, not a single machine. Do not assume one row is one physical item.
- Item descriptions are dense bullet blobs of `attribute: value` pairs
  (`• Engine Output: 23 kW • Working Width: 2,540 mm`). Extract these into
  structured attributes — that is what makes the numeric variance flagging in R9
  possible later.
- The specification section is **sometimes** a two-column `Technical Requirements
  | Description` table that maps onto `QCAuditLine.specification_required` and
  `specification_offered` — but only **one of three** real tenders was like that.
  In the second the right column held only a quantity; in the third the left
  column was the same text re-wrapped, so pairing rows was meaningless. Treat a
  clean two-column pairing as a lucky case to exploit when detected, never as the
  shape to assume.
- **Some tables shred on conversion.** In both general-goods tenders, Tender Data
  Sheet page 73 came out as seven misaligned columns — and that page carries the
  Manufacturer's Authorization requirement, which matters to IEA. When a table's
  columns look incoherent, pass that page's **raw text** to the model instead of
  the parsed table.
- **A scanned tender produces nothing.** All three test documents had a real text
  layer and converted at a 1.00 match. Detect an empty or near-empty text layer on
  upload and send the tender straight to manual entry with a clear message. Never
  let a scan silently extract to blank fields.

### Extraction runs asynchronously
Conversion alone takes about **0.4 seconds per page** — 66 seconds for a
173-page tender, before Gemini is even called. **Never do this in the request.**

The upload returns immediately with the tender in `EXTRACTING`. A management
command invoked by cron **every minute** picks up tenders in that status,
converts, extracts, and moves them to `PENDING_VERIFICATION`. The page polls.
Expect roughly one to two minutes end to end, and say so in the interface so the
PM is not left wondering.

This keeps the no-broker decision in §4. If upload volume ever makes a
once-a-minute poll feel slow, add `django-q2` — not Celery.

### Extraction
- Provider: **Gemini API**, behind a `DocumentExtractor` interface.
- Input the Markdown; output structured JSON plus a confidence value and source
  page per field.
- Extracted values land in `TenderExtraction`, **never** directly on `Tender`.
- Show the source page beside each field so the PM can check it quickly.
- Low confidence is flagged, never blocking.
- API cost is per document and belongs in the monthly budget line.

---

## 7. Data model

Which app each model belongs to is fixed by §1. Follow it.

```
Client  (core)
Tender  (tenders)
  ├── TenderDocument      kind: TENDER_PDF | TECHNICAL_SPEC | SUPPORTING
  │                       (the TENDER_PDF row also carries `markdown`)
  ├── TenderExtraction    (raw AI output + confidence + verification state)
  ├── TenderAssignment    (history of which PM holds it, and who moved it)
  ├── TenderOutcome       (position at opening, award result)
  ├── Lot
  ├── Item                (items) ── ItemAttachment (brochure | quotation | datasheet)
  ├── QCAudit             (audits) ── QCAuditLine
  ├── Approval            (approvals)  kind: REGISTRATION | SUBMISSION
  ├── EligibilityReview   (approvals)
  ├── FundRequest         (approvals)
  └── PostAward           (postaward) ── PaymentRecord, ShipmentRecord

Supplier (suppliers)
Quote    (items) ── FK to Supplier (upward, fine) and to Item (same app)
Document (documents)
Task (tasks) · ChatChannel/ChatMessage/Announcement/Meeting (comms)
AuditEntry (core) — immutable, written for every change
```

### Tender
System-set: `reference`, `status`, `previous_status`, `exchange_rate_locked_at`,
`created_at`, `created_by`, `legacy_reference`.
PM-confirmed: `title`, `client` (FK), `client_address`, `official_reference`,
`submission_deadline`, `clarification_deadline`, `bid_security_required`,
`bid_security_type`, `site_visit_required`, `site_visit_date`,
`special_requirements`, `required_forms`, `category`.
Assignment: `project_manager` — role `PM` only, changed only by `EXEC` (R13).

`previous_status` exists so the eligibility side state can return a tender to the
stage it came from.

**There is no `nest_funds_available` field.** Funding is derived:
`Tender.has_nest_funds` is a property, true when this tender has a `FundRequest`
with purpose **`NEST_APPLICATION_FEE`** and status `PAID`. A paid `BID_SECURITY`
or `OTHER` request does **not** satisfy it. This is why Finance never writes to
`Tender` (R17).

### Approval
`tender`, `kind` (`REGISTRATION` | `SUBMISSION`), `decision` (`APPROVED` |
`DECLINED`), `decided_by`, `decided_at`, `comments`, `signature`.
Only `EXEC` may create one. **There is no `ELIGIBILITY` kind** — eligibility
decisions live on `EligibilityReview`, and fund approvals on `FundRequest`.

### EligibilityReview
`tender`, `requirement`, `raised_by`, `pm_notes`, `md_decision`
(`PROCEED` | `JOINT_VENTURE` | `REMEDIATE` | `WITHDRAW`), `jv_partner`
(FK to `Supplier`, nullable), `decided_by`, `decided_at`, `decision_notes`.

### FundRequest
`tender`, `purpose` (`NEST_APPLICATION_FEE` | `TENDER_DOCUMENT_FEE` |
`BID_SECURITY` | `OTHER`), `amount`, `currency`, `requested_by`, `requested_at`,
`status`, `decided_by`, `decided_at`, `decision_reason`, `paid_by`, `paid_at`,
`payment_reference`, `notes`. `EXEC` moves it to `APPROVED` or `DECLINED`; `FIN`
moves it to `PAID`.

Name the decision fields `decided_by` / `decided_at` / `decision_reason`, matching
`Approval` and `EligibilityReview` — **not** `approved_by` on a row that was
declined, which reads as a lie in the audit trail.

Raised only by the tender's own PM, after registration approval and before
submission.

### Item
Required to save: `tender`, `specification`, `quantity`.
Required **before QC audit**: `unit_cost`, `currency`, `incoterm`,
`unit_of_measure`, `category`, `make_brand`, `model_part_number`,
`country_of_origin`, `supplier`, and all three `ItemAttachment` kinds.
System-set: `item_reference`, `total_cost`, `unit_selling_price_tzs`,
`total_selling_price_tzs`, `status`.
`sourcing_type`: `COMPLETE_UNIT` | `SEPARATE_PART`.
A `Lot`, if set, must belong to the same `Tender` as the item.

### Supplier
`name`, `is_oem`, `contact_person`, `email`, `phone`, `country`, `website`,
`notes`. Legacy data holds 426 name strings for roughly 62 real companies —
deduplicate on import.

### Quote  (`items`)
What a supplier actually offered, per §5 step 13.
`item`, `supplier`, `original_document` (their quotation as sent — mandatory),
`quoted_unit_price`, `currency`, `incoterm`, `shipping_applies`,
`country_of_origin`, `make_brand`, `model_part_number`,
`specification_offered`, `lead_time_days`, `valid_until`, `notes`,
`received_at`, `entered_by`, `status` (`RECEIVED` | `SELECTED` | `REJECTED`).

An item may hold several quotes; exactly one may be `SELECTED`, and selecting it
copies its figures onto the `Item` as costing inputs. `Item.selected_quote` points
back. Changing the selection re-costs the item and writes an `AuditEntry` — a bid
price that changed must always be explainable.

**Entry at scale.** A grid with inline editing is right for a handful of items.
Tenders in the legacy data run to **101 items**, where row-by-row entry is
punishing. Offer an **xlsx round-trip**: download a sheet pre-filled with the
tender's items, fill the price columns, upload it back. The spreadsheet is a
transport format only — never a system of record, never the place a figure lives.
Validate every uploaded row and report what failed rather than importing
silently.

### SupplierSuggestion  (`items`)
AI-proposed manufacturers for a specification, per §5 step 11.
`tender`, `items` (many-to-many — one suggestion may cover a whole unit),
`scope` (`COMPLETE_UNIT` | `SEPARATE_PART`), `suggested_name`, `country`,
`website`, `rationale` (why the model says it fits), `source_urls`, `confidence`,
`status` (`SUGGESTED` | `ACCEPTED` | `REJECTED`), `linked_supplier` (set on
accept), `requested_by`, `created_at`.

A suggestion **never** becomes a `Supplier` without the PM accepting it. Store the
rationale and sources — an unverifiable suggestion is worse than none, because it
looks authoritative.

---

## 8. Status vocabularies

Status is a **stored field that only the system writes.** No dropdown, no admin
edit, no `queryset.update()`. It changes only through the transition methods in
R19, each of which writes an `AuditEntry`. `max_length` must fit
`PENDING_REGISTRATION_APPROVAL` (29 characters) — use 32.

### `Tender.status`
```
EXTRACTING
  → PENDING_VERIFICATION              (PM checks extracted fields)
  → PENDING_REGISTRATION_APPROVAL     (MD decides whether to pursue)
  → DRAFTING
  → COSTING
  → PENDING_AUDIT
  → PENDING_EXECUTIVE_APPROVAL
  → READY_TO_SUBMIT                   (approved; waiting on NeST funds)
  → SUBMITTED
  → WON | LOST
```
`WITHDRAWN` is reachable from any state before `SUBMITTED`, by `EXEC` only.
`PENDING_ELIGIBILITY_DECISION` is a side state enterable from `DRAFTING` or
`COSTING`; resolving it returns the tender to `previous_status`, or to `WITHDRAWN`.

### `Item.status`
`DRAFT`, `QUOTED_COMPLIED`, `NEEDS_RESOLUTION`, `CLARIFICATION_PENDING`
*Legacy maps:* `QUOTED & COMPLIED` and `QUOTED COMPLIED` → `QUOTED_COMPLIED`;
`NEED RESOLVE` → `NEEDS_RESOLUTION`; `LOOPING` → `CLARIFICATION_PENDING`.

### `Currency`
`USD`, `TZS`, `GBP`, `EUR`, `ZAR`
*Legacy junk:* `EURO` → `EUR`; `P/1000`, `P/960`, `P/100` are pack sizes wrongly
entered as currency — discard.

### `Incoterm`
`CIF`, `EXW`, `FOB`, `CFR`, `FCA` — **default `CIF`**.

### `BidSecurityType`
Taken from the real distribution across 336 legacy tenders. **Not free text.**
`NOT_APPLICABLE` (142 historic), `TENDER_SECURING_DECLARATION` (77),
`BANK_LETTER_PROOF_OF_FUNDS` (62), `BANK_GUARANTEE` (34), `INSURANCE_BOND`.

There is **no separate `bid_security_required` boolean** — `NOT_APPLICABLE` is a
value of this field, and a second flag would only let the two disagree.

### Tender category
A lookup table (`TenderCategory` in `core`), **not an enum**. The legacy data shows
a long tail of genuine one-offs (leather, textile, PPE, metallurgy), so new
categories must be addable without a migration. Seed from real usage:
`ANALYTICAL` (99 historic), `METROLOGY` (17), `ENERGY AND RENEWABLE` (16),
`MECHANICAL` (15), `CLINICAL LAB`, `METEOROLOGY`, `WATER TREATMENT`, `AVIATION`,
`CHEMICAL/HYDROCARBON LAB`, `INDUSTRIAL`, `TEXTILE`, `LEATHER`, `PPE`,
`SPARE PARTS`, `METALLURGY`, `TESTING EQUIPMENT`, `CLEANING SUPPLIES`.

### Others
`FundRequestStatus`: `PENDING`, `APPROVED`, `DECLINED`, `PAID`
`EligibilityDecision`: `PROCEED`, `JOINT_VENTURE`, `REMEDIATE`, `WITHDRAW`
`PaymentStatus`: `PENDING_ORDER`, `PENDING_PAYMENT`, `PAID_ADVANCE`, `PAID_FULL`
`ShippingStatus`: `NOT_SHIPPED`, `ON_BOARD_TRANSIT`, `CUSTOMS`, `COMPLETED`
`BidOpeningPosition`: `LOWEST`, `SECOND_LOWEST`, `THIRD_LOWEST`, `FOURTH_LOWEST`,
`FIFTH_LOWEST`, `ABOVE_FIFTH`, `HIGHEST`, `NOT_SUBMITTED`
*(legacy spellings `THIRDLOWEST`, `FITH LOWEST`, `FORTH LOWEST` map in)*
`AwardStatus`: `PENDING`, `AWARDED`, `NOT_AWARDED`, `CANCELLED` — on
`TenderOutcome`. `AWARDED` drives `Tender.status = WON`; `NOT_AWARDED` and
`CANCELLED` drive `LOST`. `PENDING` leaves the tender in `SUBMITTED`.
`TaskStatus`: `ON_TRACK`, `IN_PROGRESS`, `COMPLETED`, `OVERDUE` (derived)

---

## 9. Hard business rules

Enforce per §4 "Enforcement". Cite these by number when working on them.

- **R1 — A PM cannot audit their own tender.** `QCAudit.auditor` must differ from
  `Tender.project_manager` at the moment the audit is created, checked on the real
  foreign keys. Re-assigning the tender afterwards does not invalidate a completed
  audit, but a *new* audit must re-check.
- **R2 — No audit while any item is unpriced.** The `COSTING → PENDING_AUDIT`
  transition is refused while any item has a null `unit_cost` or is missing any
  required-before-audit field. This applies **only** to that transition; moving to
  `PENDING_ELIGIBILITY_DECISION` or `WITHDRAWN` is unaffected.
- **R3 — No submission without MD submission approval.** A tender reaches
  `READY_TO_SUBMIT` only with an `Approval` of kind `SUBMISSION`, decision
  `APPROVED`, by an `EXEC`.
- **R4 — Exchange rates freeze at audit.** The `COSTING → PENDING_AUDIT`
  transition stamps `exchange_rate_locked_at`. R9 returning a tender to `COSTING`
  clears it; re-entering `PENDING_AUDIT` stamps it afresh.
- **R5 — Clearing agents see post-award only.** Enforce in querysets, not templates.
- **R6 — Every item belongs to a tender.** Mandatory FK; orphans impossible.
- **R7 — The audit trail is immutable.** Every create, update, status change and
  attachment delete writes an `AuditEntry`. No code path may update or delete one,
  including admin and superuser.
- **R8 — RFQs must cc `sales@imperialeastafrica.tz`.** The SOP voids an offer not
  copied there. The system adds it; the user cannot remove it.
- **R9 — Rejection always returns the tender to `COSTING`.** This covers two
  cases: a negative QC compliance verdict, and the MD **declining** a submission
  approval. Both return to `COSTING`, clear `exchange_rate_locked_at`, notify the
  PM, and record the reason. A decline is never a dead end and never skips the PM.
- **R9a — A tender in `PENDING_ELIGIBILITY_DECISION` is frozen.** No item edits,
  no pricing, no RFQs, no audit. Only the MD resolving the review moves it.
  Withdrawing a frozen tender **closes any open `EligibilityReview` with decision
  `WITHDRAW`** — never leave a dangling open review on a terminal tender.
- **R10 — Money is computed, never entered.** `total_cost`,
  `unit_selling_price_tzs` and `total_selling_price_tzs` are all derived (§4).
- **R11 — Task assignment follows rank.** A user may create a task for themselves
  or for anyone of strictly lower rank. `EXEC` may assign to anyone. Nobody assigns
  upward. `ADMIN` assigns to nobody.
- **R12 — AI extraction is a draft, never truth.** No extracted value reaches
  `Tender` without explicit PM confirmation. Extraction failure always leaves a
  completable form.
- **R13 — Only `EXEC` reassigns a tender**, and only to a user with role `PM`.
  The change writes a `TenderAssignment`; both PMs are notified.
- **R14 — Announcements are one-way, `EXEC` only.** Not a chat channel.
- **R15 — Every tender needs MD registration approval.** No item, costing or RFQ
  work may begin before it. Declining sends the tender to `WITHDRAWN`.
- **R16 — An unmet special requirement escalates to the MD.** A PM may neither
  silently proceed nor silently abandon. Only `EXEC` resolves an
  `EligibilityReview`.
- **R17 — No submission without NeST funds.** `READY_TO_SUBMIT → SUBMITTED` is
  refused while `Tender.has_nest_funds` is false. That property is true only when a
  `FundRequest` for the tender with purpose `NEST_APPLICATION_FEE` has status
  `PAID`. Finance never writes to `Tender`.
- **R18 — Every tender is findable.** Search matches the system reference, the
  legacy reference, the official tender number, the client name and the title. The
  reference appears on every tender view and is copyable in one click.
- **R19 — Status changes only through transition methods.** Each lives on `Tender`,
  is named for the event (`finish_extraction`, `confirm_extraction`,
  `approve_registration`,
  `start_costing`, `submit_for_audit`, `record_audit_result`, `approve_submission`,
  `mark_submitted`, `record_outcome`, `raise_eligibility`, `resolve_eligibility`,
  `withdraw`), takes the acting user, checks the rules above, writes an
  `AuditEntry`, and is the only writer of `status`.
- **R20 — References follow §4 exactly** and are generated transactionally so
  concurrent uploads cannot collide.
- **R21 — Accounts are created only by `EXEC` or `ADMIN`, and the first sign-in
  forces a password change.** No self-registration. The temporary password is
  system-generated, emailed once, never shown to its creator, and expires after
  7 days. While `must_change_password` is true, middleware blocks the user from
  every page except the change-password form, the Django admin included.
  Role is mandatory at creation.

---

## 10. What the system automates

**Tender intake** — reference on upload, PDF→Markdown, table-of-contents parsing,
section-targeted AI extraction, splitting the technical specification, parsing the
goods schedule into draft item rows.

**Supplier discovery** — proposing roughly five OEM candidates from a technical
specification, with the reason and sources for each, for the PM to accept or
reject.

**Identity and linking** — references, tender↔item linkage, created/modified by,
timestamps, audit entries.

**Calculations** — the costing chain, currency conversion, rate freezing, bid
roll-up, items-priced counters, budget vs actual, win rate, workload.

**Status and gates** — every transition in R19, and the R2, R15, R3 and R17 refusals.

**Routing** — eligibility escalations to the MD, fund requests to the MD then
Finance, negative QC results back to the PM.

**Alerts** (cron-driven management commands) — T-minus 3 / 2 / 1 day unpriced-item
escalation, NeST funds-not-loaded warning, supplier follow-up on the twice-weekly
protocol, task overdue notices, contract expiry and EOT reminders, award-capture
prompt after bid opening, reassignment notices, and notification to the PM on every
MD decision.

**Progress visibility** — every tender exposes its stage, items priced, days to
deadline and current blocking gate, so the MD sees progress without asking.

**Correspondence** — system-sent RFQs and reminders link to their tender by
message id.

**Meetings** — Google Workspace calendar events mirrored onto the dashboard.

**Document generation** — onto letterhead from live records: RFQ, MAF, board
resolutions, bid security request, bank reference and financing letters,
performance securing declaration, supplier follow-ups, clearing and forwarding
appointment, final payment and completion certificate. Each creates a `Document`
registry entry automatically.

---

## 11. What a person must enter

**PM** — upload the PDF; verify every extracted field; correct the parsed item
rows; quantity, unit, category, make/brand, model/part number, country of origin,
supplier, unit cost, currency, Incoterm, sourcing type, lot; three attachments per
item; supplier sourcing and compliance judgement; fund requests; eligibility
escalations.

**MD** — registration approval, eligibility outcome and JV partner, fund request
approval, submission approval and signature, withdrawals, reassignments.

**Finance** — payment reference, marking a fund request paid, post-award payments.

**Auditor** — specifications offered, compliance verdict, comments.

**After opening** — position at opening, award outcome, contract sum.

**Post-award** — payment amounts and dates, EOT, PVOC, manufacturer ETA, clearing
agent, bill of lading, receipts.

**Everyone** — tasks, chat, announcements, inbound documents received.

### Assisted — system drafts, person confirms
Tender field extraction; specification split and parse; numeric variance flagging;
supplier quote capture by per-RFQ link; supplier deduplication on import.

---

## 12. Realities of the legacy data

Four Smartsheet exports: 336 tenders, 5,568 items, 604 import records, 273
documents, 2022 to October 2026. **Still in live use** — plan a cutover.

- **55% of items cannot be traced to a registered tender.** Quarantine unmatched
  items on import; never discard or guess.
- **596 distinct tender references in the item sheet, only 225 tenders** exist.
- **Award outcomes almost never recorded** — 183 submitted, 6 with any award
  status. Do not display a win rate until the new award flow has data.
- **Statuses are free text with typos** — `QUOTED & COMPLIED` (818) and
  `QUOTED COMPLIED` (635) are one state.
- **Colour names stored as values** — `TENDER PAID` holds `Yellow`, `Green`, `Red`
  beside `Yes`/`No`/`Hold`. Map to null and flag for review.
- **Fields the old spec called mandatory are mostly empty** — make/brand 3.8%,
  model 4.4%, Incoterm 7.2%, catalogue 14.7%. This is why R2 gates at audit rather
  than at entry.
- **Arithmetic is sound** — of 3,203 testable rows, zero mismatches on
  price × quantity. Trust the numbers, distrust the structure.
- Tender size runs from 1 item (median) to 101. The item sheet must handle 100+ rows.
- All legacy references are `KEA`-prefixed; they go to `legacy_reference`.

---

## 13. Build order

**Foundation — REOPENED, not done.** The existing `accounts` and `tenders` apps
predate most of this spec and conflict with it: wrong status values and lengths,
wrong field names, a reference that is never generated, no `created_by`, no audit
trail, a roleless superuser, and a docstring that contradicts the one-account rule.
There is **no live data**, so the correct fix is to build `core`, correct these two
apps, and **regenerate their migrations** rather than patch forward.

**Phase 1 — by 30 November 2026.** Tender intake by upload, PDF→Markdown, Gemini
extraction and verification, registration approval, eligibility escalation,
specification split, lots, item sheet with attachments, supplier master, costing
chain and currency conversion with rate lock, QC audit, submission approval, fund
request and funds gate, search, reassignment, audit trail, cron alerts, RFQ
generation and sending, award capture, MD progress visibility.

**Phase 2 — 4 January 2027.** Post-award logistics, payments, shipping, EOT,
clearing agent access, remaining document templates, registry automation,
per-tender email history.

**Phase 3 — 8 February 2027.** Tasks with rank-based assignment, chat,
announcements, meeting mirroring, dashboards, workload analytics, budget vs
actual, win-rate reporting.

> Chat, meetings and tasks are Phase 3. Nothing in them may be pulled forward at
> the cost of the 30 November date.

---

## 14. Do not

- Do not add a company or tenant dimension. This is one company.
- Do not give one person two accounts for any reason (§2).
- Do not build self-registration, public sign-up, or an invite link. Accounts come
  from the MD or an admin only (R21).
- Do not let a user with `must_change_password` reach any page but the
  change-password form (R21).
- Do not send a raw PDF to the model. Convert to Markdown first.
- Do not let AI-extracted values reach a tender unverified (R12).
- Do not let work start on a tender the MD has not registered (R15).
- Do not let a PM proceed past or abandon an unmet special requirement alone (R16).
- Do not allow submission while the NeST account is unfunded (R17).
- Do not write `Tender.status` anywhere except a transition method (R19).
- Do not import downward between apps; use reverse accessors (§1).
- Do not automate the QC verdict or either MD approval. They are the controls.
- Do not make low-fill fields mandatory at data entry. Gate them at audit.
- Do not build a reusable item catalogue — only 9.3% of historic specifications
  repeat, so it would sit empty.
- Do not add a JavaScript framework, a REST API layer, or a frontend build step.
- Do not add Celery or Redis in Phase 1; cron-invoked management commands only.
- Do not use `DEFAULT_FILE_STORAGE` or `STATICFILES_STORAGE`; they are removed in
  Django 5.1. Use `STORAGES`.
- Do not build chat or meetings before the tendering loop is in use.
- Do not store uploads on the app server.
- Do not allow any code path to edit or delete an `AuditEntry`.
- Do not invent client names, reference formats or status values.

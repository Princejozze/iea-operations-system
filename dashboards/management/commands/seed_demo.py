"""
seed_demo — a realistic walkthrough of the whole tendering loop, for looking at screens.

    python manage.py seed_demo

DEVELOPMENT ONLY: refuses to run unless DEBUG is True.

It lives in ``dashboards`` (app 12) because that is the one app allowed to import every
other app (CLAUDE.md §1).

Every state is reached the way production reaches it. Tenders are created by upload, and
status moves only through the transition methods (R19); the MD's two approvals are signed
with a typed name and password (§4); items are priced by entering a supplier's quotation
and selecting it (§5 step 13); the QC audit is opened and completed by the auditor. Nothing
here writes ``status`` directly or bypasses a rule, so what you see on screen is what the
rules produce.

Idempotent. Every demo tender carries a marker document (``demo-<key>.pdf``); a tender whose
marker exists is left exactly as it is, so you can keep working on one and re-run safely.
Users, clients, suppliers and rates are looked up before they are created.

Email is switched to the in-memory backend for the run, so the notices the transitions send
never reach a real mailbox. Everything seeded is labelled DEMO, and the supplier prices,
FX rates, fees and model numbers are illustrative, not quotations or live rates.
"""

import datetime
from dataclasses import dataclass
from decimal import Decimal

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models.functions import Lower
from django.test.utils import override_settings
from django.utils import timezone

from accounts.models import Role, User
from approvals.models import ApprovalDecision, ApprovalKind, Approval, FundRequest, FundRequestPurpose
from audits.models import QCAudit, Verdict
from core.audit import audit_actor
from core.models import Client, TenderCategory
from documents.pdf import html_to_pdf
from items.models import AttachmentKind, ExchangeRate, Item, ItemAttachment, SourcingType
from items.services.quotes import enter_quote
from suppliers.models import Supplier
from tenders.models import (
    AwardStatus,
    BidOpeningPosition,
    BidSecurityType,
    Tender,
    TenderDocument,
    TenderDocumentKind,
    TenderExtraction,
)

D = Decimal

# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------

EMAIL_DOMAIN = "demo.invalid"  # never routable: a notice sent by mistake goes nowhere


@dataclass(frozen=True)
class DemoUser:
    key: str
    role: str
    first_name: str
    last_name: str
    password: str

    @property
    def email(self):
        return f"{self.key}@{EMAIL_DOMAIN}"

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"


# Deliberately not the real staff names in CLAUDE.md §2: one human, one account (§2), and a
# demo account must never look like a second account for a real person.
DEMO_USERS = (
    DemoUser("md", Role.EXECUTIVE, "Demo", "MD", "Demo-md-2026"),
    DemoUser("hops", Role.HEAD_OF_OPERATIONS, "Demo", "Head of Ops", "Demo-hops-2026"),
    DemoUser("pm", Role.PROJECT_MANAGER, "Demo", "PM", "Demo-pm-2026"),
    DemoUser("pm2", Role.PROJECT_MANAGER, "Demo", "Second PM", "Demo-pm2-2026"),
    DemoUser("audit", Role.AUDITOR, "Demo", "Auditor", "Demo-audit-2026"),
    DemoUser("fin", Role.FINANCE, "Demo", "Finance", "Demo-fin-2026"),
    DemoUser("clear", Role.CLEARING_AGENT, "Demo", "Clearing Agent", "Demo-clear-2026"),
    DemoUser("admin", Role.ADMIN, "Demo", "Admin", "Demo-admin-2026"),
)

# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------

# CLAUDE.md §3 "Real clients (seed data)", named exactly as written there.
CLIENTS = (
    ("Wizara ya Kilimo (Ministry of Agriculture)", ""),
    ("MSD", "MSD"), ("TBS", "TBS"), ("JKCI", "JKCI"), ("MUHAS", "MUHAS"),
    ("University of Dar es Salaam", ""),
    ("Mt. Meru Regional Referral Hospital", ""),
    ("GASCO", "GASCO"), ("MNH", "MNH"),
    ("Government Chemist Laboratory Authority", ""),
    ("Mkwawa University", ""), ("DIT", "DIT"), ("NIMR", "NIMR"), ("TANAPA", "TANAPA"), ("TCAA", "TCAA"),
)

# Illustrative only. Written as such on the row; replace through the admin for a real run.
DEMO_RATES = (("USD", D("2600")), ("EUR", D("2850")), ("GBP", D("3300")))

# (name, is_oem, country). Real manufacturers that make this kind of equipment; the contact
# addresses are not real.
SUPPLIERS = {
    "Eppendorf SE": (True, "Germany"),
    "Mettler-Toledo": (True, "Switzerland"),
    "Memmert": (True, "Germany"),
    "Thermo Fisher Scientific": (True, "United States"),
    "IKA-Werke": (True, "Germany"),
    "Olympus": (True, "Japan"),
    "Merck Millipore": (True, "Germany"),
    "DWK Life Sciences": (True, "Germany"),
    "Demo Local Supplies Ltd": (False, "Tanzania"),
}

# The equipment catalogue the demo tenders draw from. Prices are illustrative.
# key: (specification, unit, qty, category, supplier, make, model, origin, currency, price,
#       labour hours per unit)
CATALOGUE = (
    ("Refrigerated benchtop centrifuge, max speed 15,000 rpm, capacity 24 x 1.5 ml, temperature range -10 to 40 °C, 230 V",
     "each", 2, "CLINICAL LAB", "Eppendorf SE", "Eppendorf", "5424 R", "Germany", "USD", "6200", "0"),
    ("Analytical balance, capacity 220 g, readability 0.1 mg, internal calibration",
     "each", 1, "ANALYTICAL", "Mettler-Toledo", "Mettler Toledo", "XPR226", "Switzerland", "USD", "4800", "0"),
    ("Laboratory oven, forced convection, volume 108 l, temperature range 5 °C above ambient to 300 °C",
     "each", 2, "ANALYTICAL", "Memmert", "Memmert", "UF110", "Germany", "EUR", "3900", "2"),
    ("Biological safety cabinet Class II Type A2, width 1.2 m, HEPA filtered, 230 V",
     "each", 1, "CLINICAL LAB", "Thermo Fisher Scientific", "Thermo Scientific", "1300 Series A2", "United States", "USD", "7400", "4"),
    ("pH meter, benchtop, range -2.000 to 20.000 pH, accuracy 0.002 pH, with ATC probe",
     "each", 4, "ANALYTICAL", "Mettler-Toledo", "Mettler Toledo", "S220", "Switzerland", "USD", "780", "0"),
    ("Magnetic stirrer with hotplate, plate 135 mm, up to 1,500 rpm, 350 °C",
     "each", 6, "ANALYTICAL", "IKA-Werke", "IKA", "C-MAG HS 7", "Germany", "EUR", "410", "0"),
    ("Micropipette set of 3: 10-100 µl, 100-1000 µl and 1-10 ml, autoclavable",
     "set", 8, "CLINICAL LAB", "Eppendorf SE", "Eppendorf", "Research plus", "Germany", "USD", "640", "0"),
    ("Vortex mixer, 3,000 rpm, orbit 4.5 mm",
     "each", 6, "CLINICAL LAB", "IKA-Werke", "IKA", "MS 3 basic", "Germany", "EUR", "260", "0"),
    ("Water bath, volume 28 l, temperature range ambient +5 to 100 °C",
     "each", 3, "CLINICAL LAB", "Memmert", "Memmert", "WNB 29", "Germany", "EUR", "1500", "0"),
    ("Ultra-low temperature freezer, upright, -86 °C, volume 700 l",
     "each", 1, "CLINICAL LAB", "Thermo Fisher Scientific", "Thermo Scientific", "TSX Series", "United States", "USD", "11000", "3"),
    ("UV-Vis spectrophotometer, wavelength range 190 to 1100 nm, bandwidth 1 nm",
     "each", 2, "ANALYTICAL", "Thermo Fisher Scientific", "Thermo Scientific", "GENESYS 50", "United States", "USD", "6800", "2"),
    ("Binocular microscope, magnification 40x to 1000x, LED illumination",
     "each", 10, "CLINICAL LAB", "Olympus", "Olympus", "CX23", "Japan", "USD", "1700", "0"),
    ("Ultrapure water system, output 10 l/h, resistivity 18.2 MΩ.cm",
     "each", 1, "ANALYTICAL", "Merck Millipore", "Merck Millipore", "Milli-Q IQ 7003", "Germany", "USD", "9200", "4"),
    ("Borosilicate glassware set: beakers 50 to 1000 ml, flasks and measuring cylinders",
     "set", 20, "CLINICAL LAB", "DWK Life Sciences", "DURAN", "Lab set A", "Germany", "EUR", "380", "0"),
    ("Digital thermometer with probe, range -50 to 300 °C",
     "each", 12, "METROLOGY", "Memmert", "Memmert", "DT-300", "Germany", "EUR", "650", "0"),
    ("Nitrile examination gloves, size M, box of 100",
     "box", 400, "PPE", "Demo Local Supplies Ltd", "Local", "NEG-100M", "Tanzania", "TZS", "18500", "0"),
    ("Laboratory coats, cotton, size L, with name tag",
     "each", 60, "PPE", "Demo Local Supplies Ltd", "Local", "LC-L", "Tanzania", "TZS", "32000", "0"),
)
(
    CENTRIFUGE, BALANCE, OVEN, BSC, PH_METER, STIRRER, PIPETTES, VORTEX, WATER_BATH, FREEZER,
    SPECTRO, MICROSCOPE, WATER_SYSTEM, GLASSWARE, THERMOMETER, GLOVES, LAB_COATS,
) = range(len(CATALOGUE))

REQUIREMENTS = (
    "Average annual turnover of at least TZS 500 million in the last three years",
    "At least three similar supply contracts completed in the last five years",
    "Manufacturer's Authorization for the major equipment offered",
    "Valid business licence and tax clearance certificate",
)
FORMS = (
    "Tender Submission Form",
    "Manufacturer's Authorization Form",
    "Tender Securing Declaration",
    "Power of Attorney",
    "Declaration Against Fraud and Corruption",
    "Price Schedule",
)

# Stages a plan may stop at, in order.
STAGES = ("verify", "registration", "costing", "audit", "exec", "ready", "outcome")


def _stage(name):
    return STAGES.index(name)


# Item treatment, applied after costing starts.
BARE, RECEIVED, SELECTED, HAND_PRICED = "bare", "received", "selected", "hand-priced"


@dataclass(frozen=True)
class Line:
    catalogue: int
    treatment: str = SELECTED
    qty: int | None = None
    offered: str | None = None  # the specification the supplier committed to, if it differs
    second_quote: bool = False  # also enter a competing quote from another supplier


@dataclass(frozen=True)
class Plan:
    key: str
    stop: str
    client: str
    title: str
    category: str
    reference: str
    days_to_deadline: int
    security: str
    lines: tuple = ()
    pm: str = "pm"
    outcome: str = ""  # "won" | "lost"
    noisy_extraction: bool = False  # low-confidence and missing fields, for the verification screen


def complete(*indexes, **kw):
    return tuple(Line(i, SELECTED, **kw) for i in indexes)


PLANS = (
    Plan(
        "verify", "verify", "MUHAS", "Supply of molecular biology laboratory equipment", "CLINICAL LAB",
        "DEMO/MUHAS/2026-27/G/012", 20, BidSecurityType.TENDER_SECURING_DECLARATION, noisy_extraction=True,
    ),
    Plan(
        "registration", "registration", "Mt. Meru Regional Referral Hospital",
        "Supply of hospital laboratory equipment", "CLINICAL LAB",
        "DEMO/MMRRH/2026-27/G/031", 18, BidSecurityType.NOT_APPLICABLE,
    ),
    Plan(
        "costing", "costing", "University of Dar es Salaam", "Supply of teaching laboratory equipment",
        "ANALYTICAL", "DEMO/UDSM/2026-27/G/044", 9, BidSecurityType.BANK_GUARANTEE,
        lines=(
            # priced from a selected supplier quotation
            Line(CENTRIFUGE), Line(BALANCE), Line(PH_METER), Line(STIRRER), Line(GLOVES),
            # quotation attached and entered, but not yet chosen: the item is still unpriced
            Line(OVEN, RECEIVED, second_quote=True), Line(SPECTRO, RECEIVED), Line(WATER_BATH, RECEIVED),
            # priced by hand, with no supplier document behind it
            Line(VORTEX, HAND_PRICED), Line(THERMOMETER, HAND_PRICED),
            # not started
            Line(BSC, BARE), Line(FREEZER, BARE), Line(MICROSCOPE, BARE),
            Line(WATER_SYSTEM, BARE), Line(GLASSWARE, BARE),
        ),
    ),
    Plan(
        "audit", "audit", "Government Chemist Laboratory Authority", "Supply of analytical laboratory instruments",
        "CHEMICAL/HYDROCARBON LAB", "DEMO/GCLA/2026-27/G/008", 6, BidSecurityType.BANK_GUARANTEE,
        lines=(
            # offered below what the tender requires: the variance flags show these
            Line(CENTRIFUGE, offered=CATALOGUE[CENTRIFUGE][0].replace("15,000 rpm", "13,000 rpm")),
            Line(BALANCE, qty=2, offered=CATALOGUE[BALANCE][0].replace("220 g", "120 g")),
            Line(OVEN, offered=CATALOGUE[OVEN][0].replace("300 °C", "250 °C")),
            # as required
            *complete(SPECTRO, PH_METER, WATER_SYSTEM),
        ),
    ),
    Plan(
        "exec", "exec", "NIMR", "Supply of laboratory equipment for medical research", "CLINICAL LAB",
        "DEMO/NIMR/2026-27/G/019", 5, BidSecurityType.TENDER_SECURING_DECLARATION,
        lines=(
            *complete(BSC, FREEZER, MICROSCOPE, PIPETTES),
            Line(STIRRER, offered=CATALOGUE[STIRRER][0].replace("350 °C", "280 °C")),
        ),
    ),
    Plan(
        "ready", "ready", "MSD", "Supply of microbiology laboratory equipment", "CLINICAL LAB",
        "DEMO/MSD/2026-27/G/102", 4, BidSecurityType.BANK_GUARANTEE,
        lines=complete(WATER_BATH, VORTEX, GLASSWARE, LAB_COATS),
    ),
    Plan(
        "won", "outcome", "TBS", "Supply of metrology and testing equipment", "METROLOGY",
        "DEMO/TBS/2026-27/G/057", -45, BidSecurityType.NOT_APPLICABLE,
        lines=complete(THERMOMETER, BALANCE, OVEN, PH_METER, GLOVES), outcome="won",
    ),
    Plan(
        "lost", "outcome", "Mkwawa University", "Supply of science laboratory equipment", "ANALYTICAL",
        "DEMO/MKWAWA/2026-27/G/023", -60, BidSecurityType.BANK_GUARANTEE,
        lines=complete(MICROSCOPE, SPECTRO, STIRRER, GLASSWARE), outcome="lost", pm="pm2",
    ),
)


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


class Command(BaseCommand):
    help = "Populate a realistic demo walkthrough (development only; refuses unless DEBUG is True)."

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError(
                "seed_demo is for development only and refuses to run with DEBUG off. "
                "It creates accounts with published passwords."
            )
        self.document_pdf = html_to_pdf(
            "<h2>DEMO document</h2><p>Placeholder written by seed_demo. It is not a real "
            "tender, brochure, datasheet or supplier quotation.</p>"
        )
        # The transitions email the PM and the MD. Keep every one of them in memory.
        with override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"):
            self.people = self.seed_users()
            self.seed_reference_data()
            built = [(plan, self.seed_tender(plan)) for plan in PLANS]
        self.report(built)

    # -- reference data ----------------------------------------------------------

    def seed_users(self):
        people = {}
        for demo in DEMO_USERS:
            user = User.objects.filter(email__iexact=demo.email).first()
            if user is None:
                user = User.objects.create_user(
                    demo.email, demo.password, role=demo.role, first_name=demo.first_name,
                    last_name=demo.last_name, must_change_password=False,
                    is_staff=demo.role == Role.ADMIN, is_superuser=demo.role == Role.ADMIN,
                )
            elif not user.check_password(demo.password):  # a hash is re-salted on every set: only when needed
                user.set_password(demo.password)
                user.must_change_password = False
                user.save()
            people[demo.key] = user
        return people

    def seed_reference_data(self):
        for name, short in CLIENTS:
            if not Client.objects.annotate(n=Lower("name")).filter(n=name.lower()).exists():
                Client.objects.create(name=name, short_name=short)
        now = timezone.now()
        for currency, rate in DEMO_RATES:
            if ExchangeRate.rate_for(currency, now) is None:
                ExchangeRate.objects.create(
                    currency=currency, rate_to_tzs=rate, source="demo seed: illustrative, not a live rate"
                )
        for name, (is_oem, country) in SUPPLIERS.items():
            if not Supplier.objects.filter(name__iexact=name).exists():
                slug = "".join(c for c in name.lower() if c.isalnum())
                Supplier.objects.create(
                    name=name, is_oem=is_oem, country=country, contact_person="Sales desk",
                    email=f"sales@{slug}.{EMAIL_DOMAIN}", notes="Demo supplier record.",
                )

    # -- one tender --------------------------------------------------------------

    def seed_tender(self, plan):
        """Build (or find) one demo tender. Returns (tender, created)."""
        marker = f"demo-{plan.key}.pdf"
        existing = TenderDocument.objects.filter(original_filename=marker).select_related("tender").first()
        if existing:
            return existing.tender, False
        with transaction.atomic():  # all or nothing: a failure leaves no half-built tender
            return self.build(plan, marker), True

    def build(self, plan, marker):
        people, last = self.people, _stage(plan.stop)
        pm, md, auditor, finance = people[plan.pm], people["md"], people["audit"], people["fin"]
        client = Client.objects.get(name__iexact=plan.client)
        category = TenderCategory.objects.get_or_create(name=plan.category)[0]
        deadline = (timezone.localtime() + datetime.timedelta(days=plan.days_to_deadline)).replace(
            hour=10, minute=0, second=0, microsecond=0
        )

        # §5 steps 2-6: upload, extraction (a draft, R12), then the PM verifies.
        tender = Tender.objects.create_from_upload(
            uploaded_by=pm, pdf=ContentFile(self.document_pdf, name=marker)
        )
        self.record_extraction(tender, plan, client, deadline)
        tender.finish_extraction()
        if last == _stage("verify"):
            return tender

        tender.confirm_extraction(
            pm, title=f"[DEMO] {plan.title}", client=client, official_reference=plan.reference,
            client_address="P.O. Box 1, Dar es Salaam", submission_deadline=deadline,
            clarification_deadline=deadline - datetime.timedelta(days=7),
            bid_security_type=plan.security, site_visit_required=False,
            special_requirements="\n".join(REQUIREMENTS), required_forms="\n".join(FORMS), category=category,
        )
        if last == _stage("registration"):
            return tender
        self.sign(tender, ApprovalKind.REGISTRATION, md)  # R15: DRAFTING

        # §5 steps 9-13: the item sheet, costing, quotes.
        items = [self.add_item(pm, tender, line) for line in plan.lines]
        tender.start_costing(pm)
        for item, line in zip(items, plan.lines):
            self.price(pm, item, line)
        if last == _stage("costing"):
            return tender

        # R2, R4: submit for audit, then the auditor opens the audit (R1).
        tender.submit_for_audit(pm)
        with audit_actor(auditor):
            audit = QCAudit.objects.start(tender, auditor)
        if last == _stage("audit"):
            return tender

        with audit_actor(auditor):
            for line in audit.lines.all():
                line.verdict = Verdict.POSITIVE
                if line.has_variance:
                    line.comments = "Variance noted against the required specification and accepted (demo)."
                line.save()
            audit.complete(auditor)
        if last == _stage("exec"):
            self.raise_fund_request(pm, tender)  # waits for the MD
            return tender

        self.sign(tender, ApprovalKind.SUBMISSION, md)  # R3: READY_TO_SUBMIT
        request = self.raise_fund_request(pm, tender)
        request.approve(md)
        if last == _stage("ready"):
            return tender  # approved but not paid: R17 still blocks submission

        request.mark_paid(finance, "DEMO-NEST-0001")
        tender.mark_submitted(pm)
        total = sum((i.total_selling_price_tzs for i in tender.items.all()), D(0)).quantize(D("0.01"))
        if plan.outcome == "won":
            tender.record_outcome(
                pm, award_status=AwardStatus.AWARDED, bid_opening_position=BidOpeningPosition.LOWEST,
                contract_sum=total, contract_currency="TZS",
            )
        else:
            tender.record_outcome(
                pm, award_status=AwardStatus.NOT_AWARDED, bid_opening_position=BidOpeningPosition.THIRD_LOWEST
            )
        return tender

    # -- extraction (§6) -----------------------------------------------------------

    def record_extraction(self, tender, plan, client, deadline):
        def rec(value, confidence, page=None, quote=None, flags=()):
            return {
                "value": value, "confidence": confidence, "source_page": page, "quote": quote,
                "flags": list(flags), "low_confidence": confidence is not None and confidence < 0.6,
                "source": "model",
            }

        def listed(texts, confidence, first_page, noisy_at=None):
            return {
                "value": [{"text": t, "source_page": first_page + n // 2, "quote": None} for n, t in enumerate(texts)],
                "confidence": confidence if noisy_at is None else 0.52,
                "source_page": None, "quote": None, "flags": [],
                "low_confidence": noisy_at is not None, "source": "model",
            }

        noisy = plan.noisy_extraction
        stated = deadline.strftime("%d %B %Y")
        fields = {
            "official_reference": rec(plan.reference, 0.97, 3, f"Tender No. {plan.reference}"),
            "title": rec(f"{plan.title}", 0.93, 3, plan.title),
            "client_name": rec(client.name, 0.88 if noisy else 0.95, 3, client.name),
            "client_address": rec("P.O. Box 1, Dar es Salaam", 0.55 if noisy else 0.9, 3),
            "submission_deadline": rec(
                deadline.strftime("%Y-%m-%dT%H:%M:00"), 0.96, 5,
                f"Deadline for submission of tenders: {stated}, 10:00 hours",
            ),
            # §5: usually NOT stated. Null, never inferred from boilerplate (R12).
            "clarification_deadline": rec(None, None),
            "bid_security_type": rec(plan.security, 0.82, 6, "Tender Securing Declaration required"),
            "site_visit_required": rec(None, None),
            "site_visit_date": rec(None, None),
            "special_requirements": listed(REQUIREMENTS, 0.9, 31, noisy_at=3 if noisy else None),
            "required_forms": listed(FORMS, 0.95, 52),
        }
        extraction = TenderExtraction(
            tender=tender, outcome="PARTIAL" if noisy else "EXTRACTED",
            message="DEMO DATA written by seed_demo. No PDF was read and no model was called.",
            converter="seed_demo", fields=fields, model_name="seed_demo (no model was called)",
            extracted_at=timezone.now(),
        )
        with audit_actor(None):
            extraction.save()

    # -- signatures, items, quotes, funds ----------------------------------------------

    def sign(self, tender, kind, md):
        Approval.objects.sign(
            tender, kind, ApprovalDecision.APPROVED, md,
            typed_name=md.get_full_name(), password=next(u.password for u in DEMO_USERS if u.key == "md"),
            comments="Approved (demo).",
        )

    def raise_fund_request(self, pm, tender):
        return FundRequest.objects.create(
            tender=tender, purpose=FundRequestPurpose.NEST_APPLICATION_FEE, amount=D("100000.00"),
            currency="TZS", requested_by=pm, notes="Demo amount, not a real fee.",
        )

    def add_item(self, pm, tender, line):
        spec, uom, qty, category, *_ = CATALOGUE[line.catalogue]
        labour = D(CATALOGUE[line.catalogue][-1])
        with audit_actor(pm):
            return Item.objects.create(
                tender=tender, specification=spec, quantity=line.qty or qty, unit_of_measure=uom,
                category=category, sourcing_type=SourcingType.SEPARATE_PART,
                risk_pct=D("5") if labour else D("0"), labour_hours_per_unit=labour,
            )

    def quote(self, pm, item, line, *, price_factor=D(1), supplier_name=None, offered=None):
        spec, _, _, _, default_supplier, make, model, origin, currency, price, _ = CATALOGUE[line.catalogue]
        local = currency == "TZS"
        return enter_quote(
            item, supplier=Supplier.objects.get(name__iexact=supplier_name or default_supplier), user=pm,
            original_document=ContentFile(self.document_pdf, name=f"quotation-{item.item_reference}.pdf"),
            quoted_unit_price=(D(price) * price_factor).quantize(D("0.01")), currency=currency,
            incoterm="EXW" if local else "CIF", shipping_applies=not local, country_of_origin=origin,
            make_brand=make, model_part_number=model,
            specification_offered=offered if offered is not None else spec,
            lead_time_days=14 if local else 60, valid_until=timezone.localdate() + datetime.timedelta(days=45),
        )

    def price(self, pm, item, line):
        _, _, _, _, _, _, model, _, currency, price, _ = CATALOGUE[line.catalogue]
        if line.treatment == BARE:
            return
        with audit_actor(pm):
            if line.treatment == HAND_PRICED:
                item = Item.objects.get(pk=item.pk)
                item.fob_unit_price = item.unit_cost = D(price)
                item.currency, item.shipping_applies = currency, currency != "TZS"
                item.save()
                return
            if line.second_quote:
                self.quote(pm, item, line, price_factor=D("1.12"), supplier_name="Thermo Fisher Scientific")
            quote = self.quote(pm, item, line, offered=line.offered)
            if line.treatment == SELECTED:
                quote.select(pm)
                for kind in (AttachmentKind.BROCHURE, AttachmentKind.DATASHEET):
                    ItemAttachment.objects.create(
                        item=item, kind=kind, uploaded_by=pm, original_filename=f"{model}-{kind.lower()}.pdf",
                        file=ContentFile(self.document_pdf, name=f"{model}-{kind.lower()}.pdf"),
                    )

    # -- report ----------------------------------------------------------------------------

    def report(self, built):
        out = self.stdout.write
        out("")
        out(self.style.SUCCESS("Demo tenders"))
        out(f"  {'':8}{'reference':15}{'status':32}{'items':>6}  url")
        for plan, (tender, created) in built:
            tender.refresh_from_db()
            out(
                f"  {'created' if created else 'exists':8}{tender.reference:15}{tender.status:32}"
                f"{tender.items.count():>6}  /tenders/{tender.pk}/   ({plan.key})"
            )
        out("")
        out(self.style.SUCCESS("Sign in at /login/  (the email is the username)"))
        out(f"  {'role':8}{'email':26}{'password':20}signs approvals as")
        for demo in DEMO_USERS:
            out(f"  {demo.role:8}{demo.email:26}{demo.password:20}{demo.full_name}")
        out("")
        out(
            "Prices, FX rates, fees and model numbers are illustrative. Mail from these actions "
            "is held in memory and never sent."
        )

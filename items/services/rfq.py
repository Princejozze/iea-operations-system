"""
RFQ dispatch (§5 step 12, R8).

``send_rfq`` renders the RFQ on letterhead through ``documents`` (which registers
a Document and adds the sales cc), records an ``RFQ`` row with the Message-ID
that links replies and reminders back to the tender (§10), and sends the email.

R8 — "RFQs must cc sales@imperialeastafrica.tz. The system adds it; the user
cannot remove it." It is enforced four times over: documents.create_document
adds it to the Document, documents.build_email re-derives it on the outgoing
message, RFQ.save() adds it to the RFQ row (with a database constraint behind
it), and this module refuses to send a message whose cc somehow lacks it.

The send happens inside the transaction: if the mail server refuses, no RFQ row
is left claiming the email went out.
"""

from email.utils import make_msgid

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from core.audit import audit_actor
from documents.rfq import SALES_CC
from documents.services import build_email, create_document
from tenders.models import TenderStatus, TransitionError

from ..models import RFQ, Item, _is_tenders_pm, _stored_tender_status, assert_item_work_allowed

MESSAGE_ID_DOMAIN = "imperialeastafrica.tz"


def rfq_context(tender, supplier, items, user, *, message="", reply_by=None):
    """The plain dict documents.render() takes. Flattens our records; documents never sees a model."""
    return {
        "supplier_name": supplier.name,
        "supplier_contact": supplier.contact_person,
        "tender_reference": tender.reference,
        "sender_name": user.get_full_name() or user.email,
        "sender_title": "Project Manager",
        "date": timezone.localdate().strftime("%d %B %Y"),
        "reply_by": reply_by.strftime("%d %B %Y") if reply_by else "",
        "message": message or "",
        "items": [
            {
                "reference": item.item_reference,
                "specification": item.specification,
                "quantity": item.quantity,
                "unit_of_measure": item.unit_of_measure,
            }
            for item in items
        ],
    }


def send_rfq(tender, supplier, items, user, *, to=None, cc=(), message="", reply_by=None, connection=None):
    """
    Send ``supplier`` an RFQ for ``items`` of ``tender`` and return the RFQ row.

    Only the tender's PM sends, and only while the tender is in COSTING ("RFQs go
    out from here", §5 step 12) — which also refuses a frozen tender (R9a) and an
    unregistered one (R15). ``to`` defaults to the supplier's email. ``cc`` may add
    addresses; the sales mailbox is always added and cannot be removed (R8).
    Replies go to the PM (Reply-To), still copied to sales because they reply-all.
    """
    if not _is_tenders_pm(user, tender):
        raise PermissionDenied("Only this tender's PM sends its RFQs.")
    assert_item_work_allowed(tender, "Sending an RFQ")  # R9a, R15
    status = _stored_tender_status(tender)
    if status != TenderStatus.COSTING:
        raise TransitionError(
            f"RFQs go out once costing has started; the tender is {status}. Start costing first.",
            code="rfq",
        )

    items = list(items)
    if not items:
        raise ValidationError({"items": "Choose at least one item to ask the supplier about."})
    ids = {item.pk for item in items}
    if Item._base_manager.filter(pk__in=ids, tender_id=tender.pk).count() != len(ids):
        raise ValidationError({"items": "An RFQ can cover only items of its own tender."})
    items = list(Item.objects.filter(pk__in=ids).order_by("item_reference"))

    recipients = list(to) if to else ([supplier.email] if supplier.email else [])
    if not recipients:
        raise ValidationError({"to": f"{supplier.name} has no email address on record. Add one, or give a recipient."})

    with transaction.atomic(), audit_actor(user):
        document = create_document(
            "RFQ", rfq_context(tender, supplier, items, user, message=message, reply_by=reply_by),
            to=recipients, cc=list(cc), user=user, related_reference=tender.reference,
        )
        email = build_email(document, from_email=settings.DEFAULT_FROM_EMAIL)
        if SALES_CC not in [address.lower() for address in email.cc]:  # R8, before anything leaves
            raise ValidationError("Refusing to send an RFQ without the sales mailbox in cc (R8).")
        message_id = make_msgid(domain=MESSAGE_ID_DOMAIN)
        email.extra_headers["Message-ID"] = message_id
        if user.email:
            email.reply_to = [user.email]
        if connection is not None:
            email.connection = connection

        rfq = RFQ.objects.create(
            tender=tender, supplier=supplier, document=document, to=document.to, cc=email.cc,
            message_id=message_id, reply_by=reply_by, sent_by=user,
        )
        rfq.items.set(items)
        email.send(fail_silently=False)
    return rfq

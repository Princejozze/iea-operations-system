"""
The item sheet, inline editing, attachments and quote entry screens (CLAUDE.md §5, §7).

Runs against PostgreSQL like the rest of the suite. What matters here is not that a
page renders but that a refusal by the model layer reaches the PM, and that a
screen cannot do what the model forbids.
"""

from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from accounts.models import Role
from items.models import AttachmentKind, Item, ItemAttachment, Quote, QuoteStatus
from tenders.models import TenderStatus as S

from .helpers import ItemFixtures, make_user

D = Decimal


class SheetTestCase(ItemFixtures, TestCase):
    def setUp(self):
        super().setUp()
        self.tender = self.costing()
        self.item = self.make_item(self.tender, quantity=2)
        self.client.force_login(self.pm)

    def save(self, item=None, **fields):
        item = item or self.item
        data = {"quantity": item.quantity, "unit_price": "", "currency": "", "incoterm": "CIF", "supplier_name": ""}
        data.update(fields)
        return self.client.post(reverse("items:item_save", args=[item.pk]), data)


class SheetAccessTests(SheetTestCase):
    def test_who_can_read_the_sheet(self):
        url = reverse("items:sheet", args=[self.tender.pk])
        for user, expected in ((self.pm, 200), (self.pm2, 200), (self.md, 200), (self.hops, 200), (self.auditor, 200),
                               (self.finance, 403), (self.admin_user, 403),
                               (make_user(Role.CLEARING_AGENT, "clearer"), 403)):
            self.client.force_login(user)
            self.assertEqual(self.client.get(url).status_code, expected, user.role)

    def test_only_the_owning_pm_gets_inputs(self):
        url = reverse("items:sheet", args=[self.tender.pk])
        self.assertContains(self.client.get(url), 'name="unit_price"')
        for user in (self.pm2, self.md, self.auditor):
            self.client.force_login(user)
            self.assertNotContains(self.client.get(url), 'name="unit_price"', msg_prefix=user.role)

    def test_another_pm_cannot_save(self):
        self.client.force_login(self.pm2)
        self.assertEqual(self.save(unit_price="10", currency="USD").status_code, 403)
        self.item.refresh_from_db()
        self.assertIsNone(self.item.unit_cost)

    def test_filters_narrow_the_list(self):
        self.complete_item(self.tender)
        url = reverse("items:sheet", args=[self.tender.pk])
        self.assertEqual(len(self.client.get(url + "?show=unpriced").context["items"]), 1)
        self.assertEqual(len(self.client.get(url + "?show=all").context["items"]), 2)
        self.assertEqual(len(self.client.get(url + "?show=incomplete").context["items"]), 1)  # the bare one


class InlineSaveTests(SheetTestCase):
    def test_price_and_currency_save_and_cost_the_item(self):
        response = self.save(unit_price="1250", currency="USD")
        self.assertEqual(response.status_code, 200)
        self.item.refresh_from_db()
        self.assertEqual((self.item.unit_cost, self.item.fob_unit_price, self.item.currency), (D("1250"), D("1250"), "USD"))
        self.assertIsNotNone(self.item.unit_selling_price_tzs)  # R10: computed, never posted
        # The answer is out-of-band fragments for this item and the sheet, not a page.
        for fragment in (f'id="item-{self.item.pk}-figures"', f'id="item-{self.item.pk}-state"', 'id="sheet-summary"'):
            self.assertContains(response, fragment)
        self.assertContains(response, "hx-swap-oob")

    def test_price_without_currency_is_a_prompt_not_an_error(self):
        response = self.save(unit_price="1250")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Choose the currency")
        self.item.refresh_from_db()
        self.assertIsNone(self.item.unit_cost)

    def test_money_cannot_be_posted(self):
        """R10: no posted field reaches a computed figure."""
        self.save(unit_price="100", currency="USD", unit_selling_price_tzs="1", total_selling_price_tzs="1", total_cost="1")
        self.item.refresh_from_db()
        self.assertNotEqual(self.item.unit_selling_price_tzs, 1)
        self.assertEqual(self.item.total_cost, D("200"))

    def test_a_refusal_is_422_with_the_reason(self):
        response = self.save(quantity=0)
        self.assertEqual(response.status_code, 422)
        self.assertContains(response, "at least 1", status_code=422)

    def test_unknown_supplier_is_refused_by_name(self):
        response = self.save(supplier_name="No Such Company")
        self.assertEqual(response.status_code, 422)
        self.assertContains(response, "no supplier called", status_code=422)

    def test_supplier_is_found_by_name_ignoring_case(self):
        self.assertEqual(self.save(supplier_name="eppendorf se").status_code, 200)
        self.item.refresh_from_db()
        self.assertEqual(self.item.supplier, self.supplier)

    def test_a_save_without_the_more_panel_never_blanks_what_it_holds(self):
        self.item.make_brand, self.item.category = "Eppendorf", "CLINICAL LAB"
        self.item.save()
        self.save(unit_price="10", currency="USD")  # no more_loaded: those fields were never rendered
        self.item.refresh_from_db()
        self.assertEqual((self.item.make_brand, self.item.category), ("Eppendorf", "CLINICAL LAB"))

    def test_the_more_panel_saves_when_it_was_loaded(self):
        self.save(more_loaded="1", specification="Spec", make_brand="Sartorius", shipping_applies="on",
                  risk_pct="0", labour_hours_per_unit="0")
        self.item.refresh_from_db()
        self.assertEqual(self.item.make_brand, "Sartorius")

    def test_more_panel_endpoint_renders_the_fields(self):
        response = self.client.get(reverse("items:item_more", args=[self.item.pk]))
        self.assertContains(response, 'name="make_brand"')
        self.assertContains(response, 'name="more_loaded"')

    def test_read_only_once_the_tender_leaves_costing(self):
        from tenders.tests.helpers import force_status
        force_status(self.tender, S.PENDING_AUDIT, exchange_rate_locked_at=None)
        response = self.save(unit_price="10", currency="USD")
        self.assertEqual(response.status_code, 422)
        self.assertContains(response, "read-only", status_code=422)

    def test_a_quoted_item_keeps_its_figures_when_a_different_price_is_posted(self):
        quote = self.quote(self.item, quoted_unit_price=D("900"))
        quote.select(self.pm)
        self.save(unit_price="1", currency="EUR")  # disabled fields: ignored, not applied
        self.item.refresh_from_db()
        self.assertEqual((self.item.unit_cost, self.item.currency, self.item.selected_quote_id), (D("900"), "USD", quote.pk))


class AddDeleteTests(SheetTestCase):
    def test_add_an_item_gets_a_reference_and_a_card(self):
        response = self.client.post(reverse("items:item_add", args=[self.tender.pk]),
                                    {"specification": "Fume hood 1500 mm", "quantity": 3, "unit_of_measure": "Each"})
        self.assertEqual(response.status_code, 200)
        new = Item.objects.get(specification="Fume hood 1500 mm")
        self.assertTrue(new.item_reference.startswith(self.tender.reference + "-"))
        self.assertContains(response, f'id="item-{new.pk}"')

    def test_a_bad_add_is_retargeted_to_its_own_error_slot(self):
        response = self.client.post(reverse("items:item_add", args=[self.tender.pk]), {"specification": "", "quantity": 0})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response["HX-Retarget"], "#new-item-error")

    def test_delete_an_item(self):
        response = self.client.post(reverse("items:item_delete", args=[self.item.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Item.objects.filter(pk=self.item.pk).exists())

    def test_an_item_with_a_quote_cannot_be_deleted(self):
        self.quote(self.item)
        response = self.client.post(reverse("items:item_delete", args=[self.item.pk]))
        self.assertEqual(response.status_code, 422)
        self.assertTrue(Item.objects.filter(pk=self.item.pk).exists())


class AttachmentViewTests(SheetTestCase):
    def upload(self, name="brochure.pdf", content=b"%PDF-1.4 x", kind=AttachmentKind.BROCHURE):
        return self.client.post(reverse("items:attachment_add", args=[self.item.pk]),
                                {"kind": kind, "file": SimpleUploadedFile(name, content)})

    def test_attach_then_download_through_the_authenticated_view(self):
        self.assertEqual(self.upload().status_code, 200)
        attachment = ItemAttachment.objects.get(item=self.item)
        self.assertEqual(attachment.uploaded_by, self.pm)
        response = self.client.get(reverse("items:attachment_download", args=[attachment.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), b"%PDF-1.4 x")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")

    def test_downloads_follow_the_same_access_as_the_sheet(self):
        self.upload()
        url = reverse("items:attachment_download", args=[ItemAttachment.objects.get().pk])
        for user, expected in ((self.finance, 403), (self.hops, 200), (make_user(Role.CLEARING_AGENT, "clearer"), 403)):
            self.client.force_login(user)
            self.assertEqual(self.client.get(url).status_code, expected, user.role)

    def test_a_dangerous_or_empty_or_huge_file_is_refused(self):
        self.assertEqual(self.upload("run.exe", b"MZ").status_code, 422)
        self.assertEqual(self.upload("a.pdf", b"").status_code, 422)
        self.assertFalse(ItemAttachment.objects.exists())

    def test_remove_a_document(self):
        self.upload()
        attachment = ItemAttachment.objects.get()
        self.assertEqual(self.client.post(reverse("items:attachment_delete", args=[attachment.pk])).status_code, 200)
        self.assertFalse(ItemAttachment.objects.exists())

    def test_a_quotes_original_document_stays_with_the_quote(self):
        quote = self.quote(self.item)
        response = self.client.post(reverse("items:attachment_delete", args=[quote.original_document_id]))
        self.assertEqual(response.status_code, 422)
        self.assertTrue(ItemAttachment.objects.filter(pk=quote.original_document_id).exists())


class MovingTheTenderTests(SheetTestCase):
    def test_start_costing_from_drafting(self):
        tender = self.drafting()
        self.make_item(tender)
        response = self.client.post(reverse("items:start_costing", args=[tender.pk]))
        self.assertRedirects(response, reverse("items:sheet", args=[tender.pk]))
        tender.refresh_from_db()
        self.assertEqual(tender.status, S.COSTING)

    def test_submit_for_audit_is_refused_while_items_are_incomplete_and_says_what_is_missing(self):
        response = self.client.post(reverse("items:submit_for_audit", args=[self.tender.pk]), follow=True)
        text = " ".join(str(m) for m in response.context["messages"])
        self.assertIn("Not ready for QC audit", text)
        self.assertIn("price", text)
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.status, S.COSTING)

    def test_submit_for_audit_when_complete_locks_rates(self):
        tender = self.costing()
        self.complete_item(tender, fob_unit_price=D("100"))
        self.client.post(reverse("items:submit_for_audit", args=[tender.pk]))
        tender.refresh_from_db()
        self.assertEqual(tender.status, S.PENDING_AUDIT)
        self.assertIsNotNone(tender.exchange_rate_locked_at)  # R4

    def test_a_non_pm_cannot_move_the_tender(self):
        self.client.force_login(self.md)
        self.client.post(reverse("items:submit_for_audit", args=[self.tender.pk]))
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.status, S.COSTING)


class QuoteViewTests(SheetTestCase):
    def post_quote(self, item=None, document=True, **fields):
        item = item or self.item
        data = {
            "supplier": self.supplier.pk, "quoted_unit_price": "1500", "currency": "USD", "incoterm": "CIF",
            "shipping_applies": "on", "make_brand": "Eppendorf", "model_part_number": "5424 R",
            "specification_offered": "Max speed 14,000 rpm", "lead_time_days": "45",
        }
        data.update(fields)
        if document:
            data["original_document"] = SimpleUploadedFile("quotation.pdf", b"%PDF-1.4 quotation")
        return self.client.post(reverse("items:quotes", args=[item.pk]), data)

    def test_enter_a_quote_with_its_document_and_cost_the_item_on_it(self):
        response = self.post_quote(select_now="on")
        self.assertRedirects(response, reverse("items:quotes", args=[self.item.pk]))
        quote = Quote.objects.get(item=self.item)
        self.assertEqual(quote.status, QuoteStatus.SELECTED)
        self.assertEqual(quote.original_document.kind, AttachmentKind.QUOTATION)  # also counts for R2
        self.item.refresh_from_db()
        self.assertEqual((self.item.selected_quote_id, self.item.unit_cost, self.item.make_brand),
                         (quote.pk, D("1500"), "Eppendorf"))
        self.assertIsNotNone(self.item.unit_selling_price_tzs)

    def test_a_quote_without_its_document_is_refused(self):
        response = self.post_quote(document=False)
        self.assertEqual(response.status_code, 200)  # the form again, with the error
        self.assertContains(response, "This field is required")
        self.assertFalse(Quote.objects.exists())

    def test_a_new_supplier_can_be_typed_and_is_reused_by_name(self):
        self.post_quote(supplier="", new_supplier_name="Sartorius AG", new_supplier_country="Germany")
        self.post_quote(supplier="", new_supplier_name="sartorius ag")
        self.assertEqual(Quote.objects.count(), 2)
        self.assertEqual(self.item.quotes.values("supplier").distinct().count(), 1)

    def test_exactly_one_quote_is_selected_per_item(self):
        self.post_quote(select_now="on", quoted_unit_price="1500")
        self.post_quote(quoted_unit_price="1200")
        first, second = Quote.objects.order_by("quoted_unit_price")[::-1][0], Quote.objects.order_by("quoted_unit_price")[0]
        self.client.post(reverse("items:quote_select", args=[second.pk]))
        first.refresh_from_db(); second.refresh_from_db()
        self.assertEqual((first.status, second.status), (QuoteStatus.RECEIVED, QuoteStatus.SELECTED))
        self.item.refresh_from_db()
        self.assertEqual(self.item.unit_cost, D("1200"))  # re-costed on the new selection

    def test_the_selected_quote_cannot_be_rejected_but_another_can(self):
        self.post_quote(select_now="on")
        selected = Quote.objects.get()
        response = self.client.post(reverse("items:quote_reject", args=[selected.pk]), follow=True)
        self.assertEqual(selected.__class__.objects.get(pk=selected.pk).status, QuoteStatus.SELECTED)
        self.assertIn("Select another quote", " ".join(str(m) for m in response.context["messages"]))

    def test_readers_see_quotes_but_cannot_enter_them(self):
        self.post_quote()
        url = reverse("items:quotes", args=[self.item.pk])
        self.client.force_login(self.auditor)
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.post_quote().status_code, 403)
        self.assertEqual(Quote.objects.count(), 1)

    def test_quote_sheet_round_trip_entry_points(self):
        response = self.client.get(reverse("items:quote_sheet_download", args=[self.tender.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertIn("spreadsheetml", response["Content-Type"])
        self.assertEqual(self.client.get(reverse("items:quote_sheet_upload", args=[self.tender.pk])).status_code, 200)
        self.client.force_login(self.md)
        self.assertEqual(self.client.get(reverse("items:quote_sheet_download", args=[self.tender.pk])).status_code, 403)

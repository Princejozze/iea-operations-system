"""
items — app 6 in CLAUDE.md §1.

Holds: Item, ItemAttachment, Quote, the costing chain (§4), currency conversion,
bulk specification import, RFQ dispatch and chasing.

May import from: core, accounts, documents, suppliers, tenders. Never from apps below it.
Every model holding business data inherits core.models.Auditable (R7).

Contract with tenders (read by Tender's transition methods through reverse
accessors — see tenders/models.py docstring):
    Item.tender               ForeignKey(Tender, related_name="items")
    ItemAttachment.item       ForeignKey(Item, related_name="attachments"),
                              field ``kind`` in BROCHURE | QUOTATION | DATASHEET
    Item fields checked by R2: unit_cost, supplier, currency, incoterm,
    unit_of_measure, category, make_brand, model_part_number, country_of_origin
"""

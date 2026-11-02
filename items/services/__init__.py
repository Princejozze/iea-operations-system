"""
Services for the items app: work that spans several rows or leaves the database.

    quotes       enter_quote — the supplier's original document and the typed
                 figures, stored together (§5 step 13)
    quote_sheet  the xlsx round-trip for bulk quote entry (§7 "Entry at scale")
    rfq          send_rfq — render, register and send an RFQ with sales@ in cc (R8)

Views call these; they never assemble Quote or RFQ rows themselves.
"""

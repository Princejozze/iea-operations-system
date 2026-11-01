"""
audits — app 7 in CLAUDE.md §1.

Holds: QCAudit, QCAuditLine, specification variance flagging.

May import from: core … items. Never from apps below it.
Every model holding business data inherits core.models.Auditable (R7).

Contract with tenders:
    QCAudit.tender            ForeignKey(Tender, related_name="qc_audits")
    QCAudit.auditor           ForeignKey(User) — call auditor.assert_can_audit(tender)
                              in clean() and save() (R1)
"""
